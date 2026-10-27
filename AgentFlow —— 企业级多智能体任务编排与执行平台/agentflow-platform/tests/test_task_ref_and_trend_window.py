"""2026-10-27 评测页三处修复的回归测试。

1. GET /tasks/{ref} 双格式：数字主键 / 任务编号（AF 前缀）均可——
   前端各列表只展示 task_no，数字主键从不渲染，只收数字等于让人无从查起；
2. GET /eval/metrics 默认窗口从「今日」改为「近 7 天（含今日）」——
   当日没有 DONE 运行时今日窗口必然空态，被误读为「指标趋势没数据」（bug）；
3. default_trend_window 单元口径。
"""

from __future__ import annotations

from datetime import timedelta
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.deps import get_redis
from app.db.session import get_db
from app.main import create_app
from app.routers.tasks import get_task_service
from app.schemas.enums import EvalRunStatus, EvalSource, EvalVariant, TaskStatus
from app.schemas.orm_models import EvalRun, Task
from app.services.eval_run_service import default_trend_window
from app.services.task_service import TaskService
from app.utils.ids import new_trace_id
from app.utils.time import utcnow

TEST_PASSWORD = "Passw0rd!"


@pytest_asyncio.fixture
async def ref_client(
    session_factory: async_sessionmaker[AsyncSession],
    fake_redis,
    seeded_users: dict[str, int],
) -> AsyncIterator[AsyncClient]:
    """与 conftest.client 等价，额外把 get_task_service 指向测试库的 TaskService。

    GET /tasks/{ref} 的 TaskService 是裸构造（生产连真实 MySQL），测试必须覆写，
    否则会拿着用例编号去查真实库（test_v13 同款教训）。
    """
    app = create_app()

    async def _get_db_override():
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def _get_redis_override():
        yield fake_redis

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_redis] = _get_redis_override
    app.dependency_overrides[get_task_service] = lambda: TaskService(session_factory=session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http_client:
        yield http_client


async def _login(client: AsyncClient, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["access_token"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_task(
    session: AsyncSession,
    creator_id: int,
    *,
    task_no: str,
) -> Task:
    now = utcnow().replace(microsecond=0)
    task = Task(
        task_no=task_no,
        creator_id=creator_id,
        goal="编号查询用例",
        time_range={"start": "2026-10-01", "end": "2026-10-20"},
        status=TaskStatus.COMPLETED,
        replan_count=0,
        token_usage=1000,
        trace_id=new_trace_id(),
        created_at=now - timedelta(minutes=5),
        finished_at=now,
    )
    session.add(task)
    await session.flush()
    return task


@pytest.mark.asyncio
async def test_get_task_by_task_no(
    ref_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int],
):
    """AF 前缀任务编号可直查（旧行为：路径参数 int → 422，界面唯一可见的编号反而查不到）。"""
    async with session_factory() as session:
        task = await _seed_task(session, seeded_users["admin_user"], task_no="AFREF-0001")
        await session.commit()

    token = await _login(ref_client, "admin_user")
    response = await ref_client.get("/api/v1/tasks/AFREF-0001", headers=_h(token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["task_no"] == "AFREF-0001"
    assert data["task_id"] == task.id


@pytest.mark.asyncio
async def test_get_task_by_numeric_id_still_works(
    ref_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int],
):
    """数字主键直查保持兼容（既有调用方 / 详情页深链不受影响）。"""
    async with session_factory() as session:
        task = await _seed_task(session, seeded_users["admin_user"], task_no="AFREF-0002")
        await session.commit()

    token = await _login(ref_client, "admin_user")
    response = await ref_client.get(f"/api/v1/tasks/{task.id}", headers=_h(token))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["task_id"] == task.id


@pytest.mark.asyncio
async def test_get_task_by_unknown_ref_404(
    ref_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int],
):
    async with session_factory() as session:
        await _seed_task(session, seeded_users["admin_user"], task_no="AFREF-0003")
        await session.commit()

    token = await _login(ref_client, "admin_user")
    response = await ref_client.get("/api/v1/tasks/AFNOPE-9999", headers=_h(token))
    assert response.status_code == 404, response.text


def test_default_trend_window_spans_seven_days():
    now = utcnow()
    start, end = default_trend_window(now)
    assert (end - start).days == 7
    assert start <= now <= end


@pytest.mark.asyncio
async def test_eval_metrics_default_window_includes_yesterday(
    ref_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
):
    """昨日 DONE 运行必须落入默认趋势窗口（旧「今日」窗口漏掉 → 趋势卡空态误判）。"""
    base = utcnow().replace(microsecond=0)
    async with session_factory() as session:
        session.add(
            EvalRun(
                run_no="AFRUNTEST-9001",
                source=EvalSource.REGRESSION,
                dataset_id="agentflow-30",
                variant=EvalVariant.COMPRESS_OFF,
                ext_run_id=None,
                metrics={"task_completion": 0.9, "token_cost": 1_000_000, "latency_p95": 400},
                status=EvalRunStatus.DONE,
                detail={"samples_count": 30},
                created_at=base - timedelta(days=1),
            )
        )
        await session.commit()

    token = await _login(ref_client, "admin_user")
    response = await ref_client.get("/api/v1/eval/metrics", headers=_h(token))
    assert response.status_code == 200, response.text
    points = response.json()["data"]["points"]
    assert points, "近 7 天默认窗口应包含昨天的 DONE 运行（旧今日窗口会漏掉 → 空态误判）"
    assert sum(point["run_count"] for point in points) >= 1
