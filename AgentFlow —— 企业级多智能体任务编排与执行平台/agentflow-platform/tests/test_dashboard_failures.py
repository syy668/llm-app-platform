"""工作台「失败原因 Top 5」—— GET /dashboards/failure-reasons。

口径：
- 数据源 ``tasks.error.code``（JSON 列），窗口内 ``status=FAILED`` 的任务；
- ``error`` 为空或缺 ``code`` → 归入 ``UNKNOWN``（早期失败任务两层都没落码，如实上报）；
- Top 5 按 count 降序、同数按码名升序（稳定可断言）；
- CANCELLED 不计入（取消是用户动作，不是失败原因）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.enums import TaskStatus
from app.schemas.orm_models import Task
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


async def _seed(
    session: AsyncSession,
    creator_id: int,
    *,
    status: TaskStatus,
    error: dict | None,
    goal: str,
    created_at,
) -> Task:
    from app.utils.ids import new_trace_id

    task = Task(
        task_no=f"AFERR-{new_trace_id()[-10:]}",
        creator_id=creator_id,
        goal=goal,
        time_range={"start": "2026-10-01", "end": "2026-10-21"},
        status=status,
        replan_count=0,
        token_usage=10,
        trace_id=new_trace_id(),
        error=error,
        created_at=created_at,
    )
    session.add(task)
    await session.flush()
    return task


@pytest_asyncio.fixture
async def seeded_failures(
    session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int]
) -> None:
    """3 类码 + 2 条未标注 + 1 条 CANCELLED（不应计入）+ 1 条窗口外失败。"""
    from app.schemas.orm_models import User

    base = utcnow().replace(microsecond=0)
    async with session_factory() as session:
        admin = await session.get(User, seeded_users["admin_user"])
        plan = [
            (TaskStatus.FAILED, {"code": "TOOL_TIMEOUT", "message": "sql-query 超时"}, "超时任务A"),
            (TaskStatus.FAILED, {"code": "TOOL_TIMEOUT", "message": "sql-query 再次超时"}, "超时任务B"),
            (TaskStatus.FAILED, {"code": "PLAN_INVALID", "message": "计划缺依赖"}, "计划非法"),
            (TaskStatus.FAILED, {"code": "APPROVAL_MISSING", "message": "找不到审批单"}, "审批缺失"),
            (TaskStatus.FAILED, None, "未标注失败A"),
            (TaskStatus.FAILED, {"message": "只有 message 没有 code"}, "未标注失败B"),
            (TaskStatus.CANCELLED, {"code": "TASK_CANCELED"}, "用户取消"),
            (TaskStatus.COMPLETED, {"code": "IGNORED"}, "已完成不计"),
        ]
        for status, error, goal in plan:
            await _seed(session, admin.id, status=status, error=error, goal=goal, created_at=base - timedelta(hours=1))
        # 窗口外（3 天前）的失败，不应进今日窗口
        await _seed(
            session,
            admin.id,
            status=TaskStatus.FAILED,
            error={"code": "OLD_ERROR", "message": "很久以前"},
            goal="窗口外失败",
            created_at=base - timedelta(days=3),
        )
        await session.commit()


async def test_failure_reasons_top5(
    client: AsyncClient, seeded_failures: None, seeded_users: dict[str, int]
) -> None:
    token = await _login(client, "admin_user")
    response = await client.get("/api/v1/dashboards/failure-reasons", headers=_auth_header(token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert data["failed_total"] == 6  # 8 条计划里 6 条 FAILED（含未标注），不含 CANCELLED/COMPLETED/窗口外
    by_code = {item["reason_code"]: item for item in data["items"]}
    assert set(by_code) == {"TOOL_TIMEOUT", "PLAN_INVALID", "APPROVAL_MISSING", "UNKNOWN"}
    assert by_code["TOOL_TIMEOUT"]["count"] == 2
    assert by_code["UNKNOWN"]["count"] == 2
    assert by_code["PLAN_INVALID"]["count"] == 1
    # 样例 message 取该码首条
    assert by_code["TOOL_TIMEOUT"]["sample_message"] == "sql-query 超时"
    assert by_code["UNKNOWN"]["sample_message"] == "只有 message 没有 code"
    # count 降序
    counts = [item["count"] for item in data["items"]]
    assert counts == sorted(counts, reverse=True)


async def test_failure_reasons_empty_window(client: AsyncClient, seeded_users: dict[str, int]) -> None:
    """无失败任务的窗口：items 为空数组、failed_total=0，不报错。"""
    token = await _login(client, "admin_user")
    response = await client.get(
        "/api/v1/dashboards/failure-reasons",
        params={"start": "2020-01-01T00:00:00", "end": "2020-01-02T00:00:00"},
        headers=_auth_header(token),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["failed_total"] == 0
    assert data["items"] == []


async def test_failure_reasons_invalid_window(client: AsyncClient, seeded_users: dict[str, int]) -> None:
    token = await _login(client, "admin_user")
    response = await client.get(
        "/api/v1/dashboards/failure-reasons",
        params={"start": "2026-10-02T00:00:00", "end": "2026-10-01T00:00:00"},
        headers=_auth_header(token),
    )
    assert response.status_code == 422 or response.status_code == 400
