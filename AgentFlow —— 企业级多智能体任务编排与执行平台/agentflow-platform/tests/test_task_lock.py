"""任务 1：task 粒度 Redis 分布式锁（resume 并发互斥）测试。

覆盖三层：
1. 锁原语（app/core/redis_lock.py）：持有互斥 / 释放后可重取 / 跨任务互不影响 /
   抢锁失败抛 409 TASK_LOCKED「操作太频繁，请稍后再试」；
2. 服务层并发：StreamingTaskService.resume 同 task 并发驱动 → 恰好一个成功、
   一个 TASK_LOCKED，图驱动只执行一次（防状态覆盖的核心保证）；
3. 批量审批 HTTP：POST /approvals/batch 部分 succeed、部分抢锁失败，
   单条失败不阻塞其余（失败隔离 + 明确错误码回传）。

Redis 均为 fakeredis（与 conftest 一致），锁实现只用普通命令（SET NX EX /
WATCH+MULTI），不依赖 Lua。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import AppException
from app.core.redis_lock import TASK_LOCK_PREFIX, task_lock
from app.core.w3_errors import TASK_LOCKED
from app.db.session import get_db
from app.core.redis_client import get_redis
from app.main import create_app
from app.routers.tasks_ext import get_stream_service
from app.schemas.enums import TaskStatus
from app.schemas.orm_models import Task
from app.services.event_bus import EventBus
from app.services.stream_service import StreamingTaskService
from app.utils.ids import new_trace_id
from app.utils.time import utcnow

TEST_PASSWORD = "Passw0rd!"


async def _login(client: AsyncClient, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["access_token"]


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_awaiting_task(
    session_factory: async_sessionmaker[AsyncSession], creator_id: int
) -> Task:
    """种子一个 AWAITING_APPROVAL 任务（resume 的合法前置状态）。"""
    now = utcnow()
    async with session_factory() as session:
        task = Task(
            task_no=f"AFLOCK{creator_id}-{now.strftime('%H%M%S%f')[:6]}",
            creator_id=creator_id,
            goal="并发 resume 互斥测试",
            time_range={"start": "2026-10-01", "end": "2026-10-21"},
            status=TaskStatus.AWAITING_APPROVAL,
            replan_count=0,
            token_usage=10,
            trace_id=new_trace_id(),
            created_at=now,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task


# ---------------------------------------------------------------------------
# 1. 锁原语
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_lock_conflict_when_held(fake_redis):
    """锁被持有时再抢：抛 409 TASK_LOCKED，消息为「操作太频繁，请稍后再试」。"""
    key = f"{TASK_LOCK_PREFIX}42"
    assert await fake_redis.set(key, "holder-token", nx=True, ex=30)

    with pytest.raises(AppException) as exc_info:
        async with task_lock(fake_redis, 42):
            pytest.fail("不应进入锁内")
    exc = exc_info.value
    assert exc.code == TASK_LOCKED
    assert "操作太频繁" in exc.message
    assert exc.status_code == 409
    # 失败后不得误删他人锁
    assert await fake_redis.get(key) == "holder-token"


@pytest.mark.asyncio
async def test_lock_released_after_exit(fake_redis):
    """正常退出锁后应释放（key 被删除），可再次获取。"""
    async with task_lock(fake_redis, 7):
        assert await fake_redis.get(f"{TASK_LOCK_PREFIX}7") is not None
    assert await fake_redis.get(f"{TASK_LOCK_PREFIX}7") is None

    async with task_lock(fake_redis, 7):  # 第二次获取应成功
        pass


@pytest.mark.asyncio
async def test_lock_is_per_task(fake_redis):
    """粒度为 task_id：task 1 被锁定不影响 task 2。"""
    async with task_lock(fake_redis, 1):
        async with task_lock(fake_redis, 2):  # 不同任务不互斥
            pass


@pytest.mark.asyncio
async def test_release_does_not_delete_other_holders_lock(fake_redis):
    """锁过期被他人重新持有后，原持有者的释放不得误删新锁。"""
    key = f"{TASK_LOCK_PREFIX}9"
    old_lock = task_lock(fake_redis, 9, ttl=1, wait=0)
    await old_lock.__aenter__()
    old_token = await fake_redis.get(key)

    # 模拟旧锁 TTL 已过、被新持有者抢走
    await fake_redis.delete(key)
    new_token = "new-holder"
    assert await fake_redis.set(key, new_token, nx=True, ex=30)
    assert new_token != old_token

    await old_lock.__aexit__(None, None, None)
    assert await fake_redis.get(key) == new_token


# ---------------------------------------------------------------------------
# 2. 服务层并发：同一任务并发 resume，恰好一个成功
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_resume_single_winner(
    fake_redis, session_factory, seeded_users, monkeypatch
):
    """两个"管理员"同时 approve 同一任务：只有一个驱动图执行，另一个 TASK_LOCKED。"""
    import app.services.stream_service as ss_mod

    task = await _seed_awaiting_task(session_factory, seeded_users["admin_user"])

    async def fake_ensure_resumable(*args, **kwargs):  # 绕开真实图依赖
        return {"approval_id": 101}

    async def fake_load_snapshot(*args, **kwargs):
        return SimpleNamespace(next=())

    monkeypatch.setattr(ss_mod, "ensure_resumable", fake_ensure_resumable)
    monkeypatch.setattr(ss_mod, "load_snapshot", fake_load_snapshot)

    service = StreamingTaskService(
        bus=EventBus(fake_redis),
        session_factory=session_factory,
        runtime=SimpleNamespace(graph=None),
    )

    drive_calls: list[int] = []

    async def fake_drive(task_id: int, *, resume_payload=None):
        drive_calls.append(task_id)
        await asyncio.sleep(0.3)  # 模拟图驱动耗时，制造竞态窗口
        return {}

    service._drive = fake_drive  # type: ignore[method-assign]

    async def try_resume() -> dict | AppException:
        try:
            # sync=True：走同步驱动路径（锁包住整个图驱动），并发行为在测试内可确定性观测；
            # 异步后台路径的锁覆盖由 _resume_safely 内的 task_lock 保证
            return await service.resume(
                int(task.id),
                decision="APPROVED",
                decided_by=seeded_users["approver_user"],
                sync=True,
            )
        except AppException as exc:
            return exc

    results = await asyncio.gather(try_resume(), try_resume())
    successes = [r for r in results if isinstance(r, dict)]
    conflicts = [r for r in results if isinstance(r, AppException)]

    assert len(successes) == 1, f"应恰好一个成功，实际 {len(successes)}"
    assert len(conflicts) == 1, f"应恰好一个抢锁失败，实际 {len(conflicts)}"
    assert conflicts[0].code == TASK_LOCKED
    assert "操作太频繁" in conflicts[0].message
    assert drive_calls == [int(task.id)], "图驱动必须只执行一次（防状态覆盖）"

    # 锁已释放：后续正常 resume 可重新进入
    drive_calls.clear()

    async def instant_drive(task_id: int, *, resume_payload=None):
        drive_calls.append(task_id)
        return {}

    service._drive = instant_drive  # type: ignore[method-assign]
    result = await service.resume(
        int(task.id),
        decision="APPROVED",
        decided_by=seeded_users["approver_user"],
        sync=True,
    )
    assert result["resumed"] is True


# ---------------------------------------------------------------------------
# 2b. cancel 与 resume 互斥（同一把 task 锁）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_conflicts_while_resume_holds_lock(
    fake_redis, session_factory, seeded_users, monkeypatch
):
    """resume 持锁驱动期间发起 cancel：抛 409 TASK_LOCKED（取消专用文案），
    任务状态保持 AWAITING_APPROVAL 不被污染。"""
    import app.services.stream_service as ss_mod

    task = await _seed_awaiting_task(session_factory, seeded_users["admin_user"])

    async def fake_ensure_resumable(*args, **kwargs):
        return {"approval_id": 101}

    async def fake_load_snapshot(*args, **kwargs):
        return SimpleNamespace(next=(), values={})

    monkeypatch.setattr(ss_mod, "ensure_resumable", fake_ensure_resumable)
    monkeypatch.setattr(ss_mod, "load_snapshot", fake_load_snapshot)

    service = StreamingTaskService(
        bus=EventBus(fake_redis),
        session_factory=session_factory,
        runtime=SimpleNamespace(graph=None),
    )

    async def slow_drive(task_id: int, *, resume_payload=None):
        await asyncio.sleep(0.3)  # 持锁期间
        return {}

    service._drive = slow_drive  # type: ignore[method-assign]

    # 先让 resume 拿到锁（后台跑）
    drive_task = asyncio.create_task(
        service.resume(
            int(task.id),
            decision="APPROVED",
            decided_by=seeded_users["approver_user"],
            sync=True,
        )
    )
    await asyncio.sleep(0.05)  # 确保 resume 已持锁

    with pytest.raises(AppException) as exc_info:
        await service.cancel(int(task.id), reason="测试取消", user_id=seeded_users["approver_user"])
    exc = exc_info.value
    assert exc.code == TASK_LOCKED
    assert "无法取消" in exc.message
    assert exc.status_code == 409

    await drive_task  # 等 resume 正常收尾

    # 状态未被取消污染（fake drive 不改状态，仍是 AWAITING_APPROVAL）
    async with session_factory() as session:
        reloaded = await session.get(Task, int(task.id))
        assert reloaded is not None and reloaded.status == TaskStatus.AWAITING_APPROVAL

    # 锁释放后 cancel 可正常收口
    result = await service.cancel(int(task.id), reason="补刀取消", user_id=seeded_users["approver_user"])
    assert result["canceled"] is True


# ---------------------------------------------------------------------------
# 3. 批量审批：部分成功、部分抢锁失败，失败不阻塞其余
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_batch_partial_lock_failure_isolated(
    fake_redis, session_factory, seeded_users
):
    """POST /approvals/batch：task A 正常 resume，task B 被他人持锁 →
    succeeded=1 / failed=1，失败条目回传 TASK_LOCKED 与友好文案。"""
    from types import SimpleNamespace as _NS

    app = create_app()

    async def _db_override():
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def _redis_override():
        yield fake_redis

    ok_task = await _seed_awaiting_task(session_factory, seeded_users["admin_user"])

    async def stub_resume(task_id: int, **kwargs):
        if int(task_id) == int(ok_task.id):
            return {"task_id": task_id, "status": "RUNNING", "approval_id": 1}
        # 模拟该任务正被另一个管理员持有锁
        raise AppException(
            TASK_LOCKED,
            "操作太频繁，请稍后再试",
            status_code=409,
            detail={"task_id": task_id},
        )

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_redis] = _redis_override
    app.dependency_overrides[get_stream_service] = lambda: _NS(resume=stub_resume)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http:
        token = await _login(http, "approver_user")
        locked_id = int(ok_task.id) + 100000  # 任意非 ok 任务，stub 对其抛锁冲突
        response = await http.post(
            "/api/v1/approvals/batch",
            json={
                "decisions": [
                    {"task_id": locked_id, "decision": "APPROVED", "comment": ""},
                    {"task_id": int(ok_task.id), "decision": "APPROVED", "comment": ""},
                ]
            },
            headers=_auth_header(token),
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["succeeded"] == 1
    assert data["failed"] == 1
    by_task = {r["task_id"]: r for r in data["results"]}
    failed = by_task[locked_id]
    assert failed["ok"] is False
    assert failed["code"] == TASK_LOCKED
    assert "操作太频繁" in failed["message"]
    assert by_task[int(ok_task.id)]["ok"] is True
