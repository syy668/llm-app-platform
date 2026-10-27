"""v1.0 缺口收口接口测试：G4 已处理审批 / G9 批量审批 / 趋势 / 日志 / G7 TraceStep 字段。

对照前端 agentflow-web 预留点位：
- G4：GET /approvals?status=APPROVED|REJECTED|EXPIRED（已处理 Tab）
- G9：POST /approvals/batch（批量通过，复用单条 resume，部分失败隔离）
- 趋势：GET /dashboards/trend（工作台「任务完成趋势」卡）
- 日志：POST /logs/search（系统设置「请求日志」区，基于 task_executions）
- G7：GET /tasks/{id}/trace 出参补 span_id / parent_span_id / subtask_id

只做只读查询 / 参数校验 / 权限拦截验证，不驱动真实 LangGraph 执行链路
（批量审批的"成功"路径由既有 resume 用例覆盖，这里只验"失败隔离"与"权限"）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.enums import (
    ApprovalStatus,
    ApprovalType,
    ExecutionNode,
    TaskStatus,
    UserRole,
)
from app.schemas.orm_models import Approval, Task, TaskExecution, User
from app.utils.ids import new_span_id, new_trace_id
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


async def _seed_task(
    session: AsyncSession,
    creator_id: int,
    *,
    goal: str,
    status: TaskStatus,
    created_at,
    trace_id: str | None = None,
) -> Task:
    task = Task(
        task_no=f"AFGAP{len(goal):04d}-{creator_id}-{created_at.strftime('%H%M%S%f')[:6]}",
        creator_id=creator_id,
        goal=goal,
        time_range={"start": "2026-10-01", "end": "2026-10-21"},
        status=status,
        replan_count=0,
        token_usage=100,
        trace_id=trace_id or new_trace_id(),
        created_at=created_at,
    )
    session.add(task)
    await session.flush()
    return task


async def _seed_approval(
    session: AsyncSession,
    task_id: int,
    *,
    status: ApprovalStatus,
    decided_by: int | None = None,
    decide_comment: str | None = None,
    decided_at=None,
) -> Approval:
    approval = Approval(
        task_id=task_id,
        type=ApprovalType.HIGH_RISK_TOOL,
        reason="测试高危工具",
        status=status,
        requested_by=0,
        decided_by=decided_by,
        decide_comment=decide_comment,
        decided_at=decided_at,
    )
    session.add(approval)
    await session.flush()
    return approval


async def _seed_execution(
    session: AsyncSession,
    task_id: int,
    trace_id: str,
    *,
    event: str = "tool.call",
    tool_name: str = "sql_query",
    tool_error: str | None = None,
    started_at=None,
    parent_span_id: str | None = None,
    subtask_id: int | None = None,
) -> TaskExecution:
    row = TaskExecution(
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=parent_span_id,
        task_id=task_id,
        subtask_id=subtask_id,
        node=ExecutionNode.EXECUTOR,
        event=event,
        tool_name=tool_name,
        tool_error=tool_error,
        input_tokens=10,
        output_tokens=20,
        latency_ms=15,
        payload={"sql": "SELECT 1"},
        started_at=started_at or utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


@pytest_asyncio.fixture
async def gap_seed(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int],
):
    """种子：admin 任务（1 待审批 + 1 已完成）+ 终态审批单 + 执行记录。"""
    base = utcnow().replace(microsecond=0)
    admin_id = seeded_users["admin_user"]
    data: dict[str, object] = {}
    async with session_factory() as session:
        admin = await session.get(User, admin_id)
        pending_task = await _seed_task(
            session, admin.id, goal="生成运营群周报通知", status=TaskStatus.AWAITING_APPROVAL,
            created_at=base - timedelta(hours=2),
        )
        done_task = await _seed_task(
            session, admin.id, goal="分析华南退货率", status=TaskStatus.COMPLETED,
            created_at=base - timedelta(hours=3),
        )
        # 终态审批单：1 条 APPROVED（挂在 done_task），1 条 REJECTED（挂在 pending_task）
        approved = await _seed_approval(
            session, done_task.id, status=ApprovalStatus.APPROVED,
            decided_by=admin.id, decide_comment="同意", decided_at=base - timedelta(hours=1),
        )
        rejected = await _seed_approval(
            session, pending_task.id, status=ApprovalStatus.REJECTED,
            decided_by=admin.id, decide_comment="重规划", decided_at=base - timedelta(minutes=30),
        )
        # 执行记录：done_task 下有 1 条 tool.call + 1 条 error
        exec_ok = await _seed_execution(
            session, done_task.id, done_task.trace_id, event="tool.call",
            tool_name="sql_query", started_at=base - timedelta(hours=2, minutes=30),
        )
        exec_err = await _seed_execution(
            session, done_task.id, done_task.trace_id, event="error",
            tool_name="chart_render", tool_error="CHART_RENDER_FAILED",
            started_at=base - timedelta(hours=2), parent_span_id=exec_ok.span_id,
        )
        await session.commit()
        data["pending_task_id"] = int(pending_task.id)
        data["done_task_id"] = int(done_task.id)
        data["approved_id"] = int(approved.id)
        data["rejected_id"] = int(rejected.id)
        data["trace_id"] = done_task.trace_id
        data["exec_ok_id"] = int(exec_ok.id)
        data["exec_err_id"] = int(exec_err.id)
        data["admin_id"] = admin.id
    return data


# ---------------------------------------------------------------------------
# G7：TraceStep 出参补 span_id / parent_span_id / subtask_id
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_trace_step_includes_span_fields(
    session_factory: async_sessionmaker[AsyncSession], gap_seed: dict[str, object],
):
    """单元级：TraceService.list_steps 出参必须含 span_id / parent_span_id / subtask_id。"""
    from app.services.trace_service import TraceService

    trace = TraceService(session_factory)
    steps = await trace.list_steps(task_id=int(gap_seed["done_task_id"]))
    assert len(steps) >= 2, "应至少返回 2 条执行记录"
    by_id = {it["id"]: it for it in steps}
    err_row = by_id[gap_seed["exec_err_id"]]
    ok_row = by_id[gap_seed["exec_ok_id"]]
    # G7 核心：三个 span 字段必须下发
    assert "span_id" in err_row and err_row["span_id"]
    assert err_row["parent_span_id"] == ok_row["span_id"]
    assert "subtask_id" in err_row


# ---------------------------------------------------------------------------
# G4：GET /approvals?status=... 已处理列表
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_approvals_processed_list(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], gap_seed: dict[str, object],
):
    token = await _login(client, "admin_user")
    response = await client.get("/api/v1/approvals?status=APPROVED,REJECTED", headers=_auth_header(token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 2
    statuses = {it["status"] for it in data["items"]}
    assert statuses == {"APPROVED", "REJECTED"}
    # 已处理条目应带 task_no / goal / decided_by / decide_comment
    for it in data["items"]:
        assert "task_no" in it and "goal" in it
        assert "decided_by" in it and "decide_comment" in it


@pytest.mark.asyncio
async def test_approvals_default_still_inbox(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], gap_seed: dict[str, object],
):
    """缺省 status 仍走待处理收件箱（原逻辑不变）。"""
    token = await _login(client, "admin_user")
    response = await client.get("/api/v1/approvals", headers=_auth_header(token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # 收件箱条目是任务视角：含 status（任务状态 AWAITING_APPROVAL），不含 decided_by/decide_comment
    assert "items" in data
    if data["items"]:
        item = data["items"][0]
        assert item["status"] == "AWAITING_APPROVAL"
        assert "decided_by" not in item
        assert "decide_comment" not in item


@pytest.mark.asyncio
async def test_approvals_processed_viewer_403(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], gap_seed: dict[str, object],
):
    token = await _login(client, "viewer_user")
    response = await client.get("/api/v1/approvals?status=APPROVED", headers=_auth_header(token))
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# G9：POST /approvals/batch 批量审批（失败隔离 + 权限 + 参数校验）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_batch_approve_partial_failure_isolated(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], gap_seed: dict[str, object],
):
    """不存在任务 + 已完成任务的审批：应失败但不炸掉批次，返回逐条结果。"""
    token = await _login(client, "admin_user")
    done_id = gap_seed["done_task_id"]  # 已 COMPLETED，resume 会报错
    missing_id = 999999
    response = await client.post(
        "/api/v1/approvals/batch",
        json={
            "decisions": [
                {"task_id": missing_id, "decision": "APPROVED", "comment": ""},
                {"task_id": done_id, "decision": "APPROVED", "comment": ""},
            ]
        },
        headers=_auth_header(token),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["failed"] == 2
    assert data["succeeded"] == 0
    assert len(data["results"]) == 2
    for r in data["results"]:
        assert r["ok"] is False
        assert "code" in r and "message" in r


@pytest.mark.asyncio
async def test_batch_approve_empty_422(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], gap_seed: dict[str, object],
):
    token = await _login(client, "admin_user")
    response = await client.post(
        "/api/v1/approvals/batch", json={"decisions": []}, headers=_auth_header(token)
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_batch_approve_viewer_403(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], gap_seed: dict[str, object],
):
    token = await _login(client, "viewer_user")
    response = await client.post(
        "/api/v1/approvals/batch",
        json={"decisions": [{"task_id": 1, "decision": "APPROVED", "comment": ""}]},
        headers=_auth_header(token),
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 趋势：GET /dashboards/trend
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dashboard_trend(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], gap_seed: dict[str, object],
):
    token = await _login(client, "admin_user")
    # end 必须带上时分秒：传纯日期会被解析成当天 00:00，把"今天 01~02 点"的种子任务排除掉
    # （原写法在 UTC 04:00 之后必然 0 条，属测试自身的窗口 bug，与接口无关）
    now = utcnow().replace(microsecond=0)
    response = await client.get(
        "/api/v1/dashboards/trend",
        params={"start": (now - timedelta(days=7)).date().isoformat(), "end": now.isoformat()},
        headers=_auth_header(token),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "points" in data and "window" in data
    assert len(data["points"]) >= 1
    for pt in data["points"]:
        assert {"date", "task_total", "completed", "completion_rate"} <= set(pt)
    # 今日应有任务（种子在 base = utcnow 附近）
    assert sum(pt["task_total"] for pt in data["points"]) >= 1


@pytest.mark.asyncio
async def test_dashboard_trend_invalid_window(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], gap_seed: dict[str, object],
):
    token = await _login(client, "admin_user")
    response = await client.get(
        "/api/v1/dashboards/trend",
        params={"start": "2026-10-21T00:00:00", "end": "2026-10-20T00:00:00"},
        headers=_auth_header(token),
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 日志：POST /logs/search
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_logs_search_by_trace_id(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], gap_seed: dict[str, object],
):
    token = await _login(client, "admin_user")
    trace_id = gap_seed["trace_id"]
    response = await client.post(
        "/api/v1/logs/search", json={"trace_id": trace_id}, headers=_auth_header(token)
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 2
    assert len(data["items"]) == 2
    levels = {it["level"] for it in data["items"]}
    assert levels == {"INFO", "ERROR"}


@pytest.mark.asyncio
async def test_logs_search_error_level_filter(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], gap_seed: dict[str, object],
):
    token = await _login(client, "admin_user")
    trace_id = gap_seed["trace_id"]
    response = await client.post(
        "/api/v1/logs/search", json={"trace_id": trace_id, "level": "ERROR"}, headers=_auth_header(token)
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["tool_error"] == "CHART_RENDER_FAILED"
    assert data["items"][0]["level"] == "ERROR"


@pytest.mark.asyncio
async def test_logs_search_viewer_200(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], gap_seed: dict[str, object],
):
    """日志检索对 viewer 开放（只读运维配套，同 trace 回放）。"""
    token = await _login(client, "viewer_user")
    response = await client.post("/api/v1/logs/search", json={}, headers=_auth_header(token))
    assert response.status_code == 200
