"""v1.3 任务归档（第一阶段）接口测试。

覆盖点：
1. 终态任务可归档 / 可取消归档，落库 ``archived_at`` 正确；
2. 幂等：重复归档返回 ``changed=false`` 且不刷新时间戳；
3. 非终态（PLANNING / RUNNING / AWAITING_APPROVAL / SUMMARIZING）→ 409 TASK_STATE_CONFLICT；
4. 列表口径：默认排除已归档、``archived=true`` 只看已归档、``archived=false`` 只看未归档；
5. 权限：ADMIN / APPROVER 均可归档任意终态任务（全局治理），VIEWER 无归档权限；
6. 归档只是"列表整理"：详情接口不受影响（审计链完整保留）；
7. 任务不存在 → 404。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.enums import TaskStatus
from app.schemas.orm_models import Task
from app.utils.ids import new_trace_id
from app.utils.time import utcnow

TEST_PASSWORD = "Passw0rd!"
BASE = "/api/v1/tasks"


async def _login(client: AsyncClient, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["access_token"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed(
    session: AsyncSession,
    creator_id: int,
    *,
    task_no: str,
    status: TaskStatus,
    ago_minutes: int = 0,
) -> Task:
    created = utcnow().replace(microsecond=0) - timedelta(minutes=ago_minutes)
    finished = created + timedelta(seconds=90) if status in {
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
    } else None
    task = Task(
        task_no=task_no,
        creator_id=creator_id,
        goal=f"归档用例 {task_no}",
        time_range={"start": "2026-10-01", "end": "2026-10-20"},
        status=status,
        replan_count=0,
        token_usage=1000,
        trace_id=new_trace_id(),
        created_at=created,
        finished_at=finished,
    )
    session.add(task)
    await session.flush()
    return task


@pytest_asyncio.fixture
async def archive_tasks(session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int]):
    """admin 名下：1 完成 + 1 运行中 + 1 待审批 + 1 失败；approver 名下：1 完成。"""
    async with session_factory() as session:
        admin_id = seeded_users["admin_user"]
        approver_id = seeded_users["approver_user"]
        done = await _seed(session, admin_id, task_no="AFARCH-0001", status=TaskStatus.COMPLETED, ago_minutes=50)
        running = await _seed(session, admin_id, task_no="AFARCH-0002", status=TaskStatus.RUNNING, ago_minutes=40)
        awaiting = await _seed(session, admin_id, task_no="AFARCH-0003", status=TaskStatus.AWAITING_APPROVAL, ago_minutes=30)
        failed = await _seed(session, admin_id, task_no="AFARCH-0004", status=TaskStatus.FAILED, ago_minutes=20)
        others = await _seed(session, approver_id, task_no="AFARCH-0005", status=TaskStatus.COMPLETED, ago_minutes=10)
        await session.commit()
        return {
            "done": done.id, "running": running.id, "awaiting": awaiting.id,
            "failed": failed.id, "approver_own": others.id,
        }


async def _archived_at(session_factory: async_sessionmaker[AsyncSession], task_id: int):
    async with session_factory() as session:
        row = await session.execute(select(Task.archived_at).where(Task.id == task_id))
        return row.scalar_one()


# ---------------------------------------------------------------------------
# 1) 基本归档 / 取消归档
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_archive_completed_task(client: AsyncClient, seeded_users, archive_tasks, session_factory):
    token = await _login(client, "admin_user")
    task_id = archive_tasks["done"]

    response = await client.post(f"{BASE}/{task_id}/archive", json={"archived": True}, headers=_h(token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["archived"] is True and data["changed"] is True
    assert data["archived_at"] and data["task_no"] == "AFARCH-0001"
    assert await _archived_at(session_factory, task_id) is not None


@pytest.mark.asyncio
async def test_archive_is_idempotent(client: AsyncClient, seeded_users, archive_tasks, session_factory):
    """重复归档不报错、不刷新时间戳（时间戳变化说明写了脏数据）。"""
    token = await _login(client, "admin_user")
    task_id = archive_tasks["done"]

    first = await client.post(f"{BASE}/{task_id}/archive", json={"archived": True}, headers=_h(token))
    assert first.status_code == 200
    stamp = first.json()["data"]["archived_at"]

    second = await client.post(f"{BASE}/{task_id}/archive", json={"archived": True}, headers=_h(token))
    assert second.status_code == 200
    body = second.json()["data"]
    assert body["changed"] is False
    assert body["archived_at"] == stamp


@pytest.mark.asyncio
async def test_unarchive_restores_visibility(client: AsyncClient, seeded_users, archive_tasks, session_factory):
    token = await _login(client, "admin_user")
    task_id = archive_tasks["failed"]

    assert (await client.post(f"{BASE}/{task_id}/archive", json={"archived": True}, headers=_h(token))).status_code == 200
    assert await _archived_at(session_factory, task_id) is not None

    response = await client.post(f"{BASE}/{task_id}/archive", json={"archived": False}, headers=_h(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["archived"] is False and data["changed"] is True
    assert data["archived_at"] is None
    assert await _archived_at(session_factory, task_id) is None


@pytest.mark.asyncio
async def test_archive_without_body_defaults_to_archive(client: AsyncClient, seeded_users, archive_tasks):
    """省略 body 视为归档（前端最常用的调用形态）。"""
    token = await _login(client, "admin_user")
    response = await client.post(f"{BASE}/{archive_tasks['done']}/archive", headers=_h(token))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["archived"] is True


# ---------------------------------------------------------------------------
# 2) 非终态拦截（409）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["running", "awaiting"])
async def test_archive_non_terminal_rejected(client: AsyncClient, seeded_users, archive_tasks, key):
    token = await _login(client, "admin_user")
    response = await client.post(f"{BASE}/{archive_tasks[key]}/archive", json={"archived": True}, headers=_h(token))
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "TASK_STATE_CONFLICT"
    assert "终态" in body["message"]


# ---------------------------------------------------------------------------
# 3) 列表口径
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_hides_archived_by_default(client: AsyncClient, seeded_users, archive_tasks):
    token = await _login(client, "admin_user")
    archived_id = archive_tasks["done"]
    await client.post(f"{BASE}/{archived_id}/archive", json={"archived": True}, headers=_h(token))

    default = await client.get(BASE, params={"size": 100}, headers=_h(token))
    assert default.status_code == 200
    default_ids = [item["task_id"] for item in default.json()["data"]["items"]]
    assert archived_id not in default_ids
    assert archive_tasks["failed"] in default_ids

    only_archived = await client.get(BASE, params={"size": 100, "archived": "true"}, headers=_h(token))
    archived_ids = [item["task_id"] for item in only_archived.json()["data"]["items"]]
    assert archived_ids == [archived_id]
    assert only_archived.json()["data"]["items"][0]["archived_at"] is not None

    unarchived = await client.get(BASE, params={"size": 100, "archived": "false"}, headers=_h(token))
    assert archived_id not in [item["task_id"] for item in unarchived.json()["data"]["items"]]


@pytest.mark.asyncio
async def test_archived_total_matches_filter(client: AsyncClient, seeded_users, archive_tasks):
    """分页 total 必须与过滤口径一致，否则前端分页会出现"第 2 页空白"。"""
    token = await _login(client, "admin_user")
    for key in ("done", "failed"):
        await client.post(f"{BASE}/{archive_tasks[key]}/archive", json={"archived": True}, headers=_h(token))

    archived = await client.get(BASE, params={"archived": "true"}, headers=_h(token))
    assert archived.json()["data"]["meta"]["total"] == 2

    default = await client.get(BASE, headers=_h(token))
    # admin 的列表不限创建人：5 条（admin 4 + approver 1）里归档掉 2 条 → 默认 3 条
    assert default.json()["data"]["meta"]["total"] == 3


# ---------------------------------------------------------------------------
# 4) 权限
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_can_archive_others(client: AsyncClient, seeded_users, archive_tasks, session_factory):
    """ADMIN 拥有全局治理权：别人的终态任务也能归档（与详情页"仅本人"刻意不对齐）。

    理由：详情页限制 ADMIN 仅本人是读隐私防线；归档不读正文、不改数据、可随时取消，
    若不允许，管理员在「全部任务」里看到别人的任务时按钮就成了摆设。
    """
    token = await _login(client, "admin_user")
    task_id = archive_tasks["approver_own"]
    response = await client.post(f"{BASE}/{task_id}/archive", json={"archived": True}, headers=_h(token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["archived"] is True and data["changed"] is True
    assert await _archived_at(session_factory, task_id) is not None


@pytest.mark.asyncio
async def test_approver_can_archive_any(client: AsyncClient, seeded_users, archive_tasks):
    token = await _login(client, "approver_user")
    response = await client.post(
        f"{BASE}/{archive_tasks['done']}/archive", json={"archived": True}, headers=_h(token)
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["archived"] is True


@pytest.mark.asyncio
async def test_viewer_forbidden(client: AsyncClient, seeded_users, archive_tasks):
    token = await _login(client, "viewer_user")
    response = await client.post(
        f"{BASE}/{archive_tasks['done']}/archive", json={"archived": True}, headers=_h(token)
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 5) 归档不影响记录本身（审计链保留）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_archive_keeps_record_and_status(client: AsyncClient, seeded_users, archive_tasks, session_factory):
    """归档只是打时间戳：任务行、task_no、状态、完成时间全部原样保留。

    这里刻意**不走 HTTP 详情接口**：`GET /tasks/{id}` 的 TaskService 是裸构造的
    （`get_task_service` 返回 `TaskService()`），不经过 get_db 覆盖，会打到真实 MySQL，
    用它断言等于在断言线上库的数据。直接查库才能验证语义本身。
    """
    token = await _login(client, "admin_user")
    task_id = archive_tasks["done"]

    response = await client.post(f"{BASE}/{task_id}/archive", json={"archived": True}, headers=_h(token))
    assert response.status_code == 200

    async with session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None, "归档后任务行必须还在（这是归档与删除的本质区别）"
        assert task.task_no == "AFARCH-0001"
        assert task.status == TaskStatus.COMPLETED
        assert task.finished_at is not None
        assert task.archived_at is not None


@pytest.mark.asyncio
async def test_archive_missing_task_404(client: AsyncClient, seeded_users):
    token = await _login(client, "admin_user")
    response = await client.post(f"{BASE}/999999/archive", json={"archived": True}, headers=_h(token))
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
