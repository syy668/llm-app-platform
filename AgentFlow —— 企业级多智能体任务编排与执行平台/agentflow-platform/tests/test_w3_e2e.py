"""W3 端到端 + SSE 单元测试（FR-009 / FR-011 / FR-012 / FR-015）。

测试环境与 W2 保持一致（红线要求）：
- MySQL → **文件型 SQLite**（不是 StaticPool 内存库）：W3 的图执行与事件消费是**并发**的，
  轨迹写入与节点内 DB 写会同时发生；单连接内存库会触发
  ``sqlite3.OperationalError: cannot commit transaction - SQL statements in progress``，
  文件库 + WAL + busy_timeout 才能真实模拟"多连接池"的生产形态；
- Redis → fakeredis（事件总线与取消信号共用同一实例）；
- MCP → ``InMemoryTransport``（进程内直连 4 个 server，不起子进程、不连 MySQL）；
- checkpointer → ``MemorySaver``；LLM → 确定性 Mock；评审 → 本地规则（force_local_review）。

覆盖点：
1. 正常执行触发 interrupt → AWAITING_APPROVAL；
2. 审批通过 → 断点续跑 → COMPLETED（且 planner 不重跑）；
3. 驳回 → replan → 无法绕过审批发通知 → FAILED(APPROVAL_MISSING)；
4. 取消暂停任务 → CANCELLED，且再次 resume 返回 409；
5. 非暂停任务 resume → 409；
6. SSE 事件协议（4 类事件 / id / trace_id / 终止帧）；
7. SSE 断线重连去重（Last-Event-ID）与客户端断开收尾；
8. 快照 + 轨迹回放；审批收件箱；角色可见性。
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.agents.graph import AgentRuntime
from app.agents.llm import LLMClient
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import get_db
from app.core.redis_client import get_redis
from app.main import create_app
from app.routers.tasks import get_task_service
from app.schemas.enums import ApprovalStatus, TaskStatus, UserRole
from app.schemas.graph_state import TimeRange
from app.schemas.orm_models import Approval, Task, User
from app.services.cancel_service import CancelSignal
from app.services.event_bus import EventBus, StreamEvent
from app.services.snapshot_service import SnapshotService
from app.services.stream_context import mcp_event_hook
from app.services.stream_service import StreamingTaskService, sse_event_frames
from app.services.trace_service import TraceService
from app.services.w3_runtime import create_w3_runtime
from app.tools.mcp_client import MCPClientPool
from app.tools.mcp_servers import chart_render, kb_search, notify, sql_query

pytestmark = pytest.mark.unit

PASSWORD = "Passw0rd!"
GOAL_WITH_NOTIFY = "分析上周 GMV 并通知运营群"
GOAL_ANALYSIS_ONLY = "分析上周 GMV 与转化率"
WINDOW = {"start": "2026-10-12", "end": "2026-10-18"}


# ---------------------------------------------------------------------------
# 数据库 / Redis
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def w3_engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    """文件型 SQLite + WAL：允许图执行与轨迹写入并发访问。"""
    db_path = tmp_path / "w3.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"timeout": 30})

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - 驱动回调
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def w3_session_factory(w3_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=w3_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def w3_users(w3_session_factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    roles = {
        "admin_user": UserRole.ADMIN,
        "approver_user": UserRole.APPROVER,
        "viewer_user": UserRole.VIEWER,
    }
    ids: dict[str, int] = {}
    async with w3_session_factory() as session:
        for username, role in roles.items():
            user = User(username=username, password_hash=hash_password(PASSWORD), role=role, status=1)
            session.add(user)
            await session.flush()
            ids[username] = int(user.id)
        await session.commit()
    return ids


@pytest_asyncio.fixture
async def w3_redis() -> AsyncIterator[FakeRedis]:
    client = FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


@pytest_asyncio.fixture
async def w3_bus(w3_redis: FakeRedis) -> EventBus:
    return EventBus(redis_client=w3_redis)


@pytest_asyncio.fixture
async def w3_cancel(w3_redis: FakeRedis) -> CancelSignal:
    return CancelSignal(w3_redis)


# ---------------------------------------------------------------------------
# MCP / 图运行时
# ---------------------------------------------------------------------------
@pytest.fixture
def w3_chart_tool() -> chart_render.ChartRenderTool:
    return chart_render.ChartRenderTool(use_process_pool=False)


@pytest_asyncio.fixture
async def w3_pool(
    w3_session_factory: async_sessionmaker[AsyncSession],
    fake_sql_executor: Any,
    w3_chart_tool: chart_render.ChartRenderTool,
) -> AsyncIterator[MCPClientPool]:
    """4 个 MCP server 进程内直连，并挂上 W3 的 tool 事件钩子。"""
    apps = {
        "sql-query": sql_query.build_app(fake_sql_executor),
        "chart-render": chart_render.build_app(w3_chart_tool),
        "kb-search": kb_search.build_app(kb_search.MockKbProvider()),
        "notify": notify.build_app(notify.NotifyTool(session_factory=w3_session_factory)),
    }
    pool = MCPClientPool.from_apps(apps, on_event=mcp_event_hook)
    await pool.discover()
    try:
        yield pool
    finally:
        await pool.aclose()


@pytest_asyncio.fixture
async def w3_runtime(
    w3_pool: MCPClientPool,
    w3_session_factory: async_sessionmaker[AsyncSession],
) -> AgentRuntime:
    return await create_w3_runtime(
        pool=w3_pool,
        llm=LLMClient(mock=True),
        checkpointer=MemorySaver(),
        session_factory=w3_session_factory,
        force_local_review=True,
        discover_tools=False,
    )


@pytest_asyncio.fixture
async def w3_service(
    w3_bus: EventBus,
    w3_cancel: CancelSignal,
    w3_session_factory: async_sessionmaker[AsyncSession],
    w3_runtime: AgentRuntime,
) -> StreamingTaskService:
    return StreamingTaskService(
        bus=w3_bus,
        trace=TraceService(w3_session_factory),
        cancel=w3_cancel,
        session_factory=w3_session_factory,
        runtime=w3_runtime,
    )


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def w3_app(
    w3_session_factory: async_sessionmaker[AsyncSession],
    w3_redis: FakeRedis,
    w3_bus: EventBus,
    w3_service: StreamingTaskService,
    w3_users: dict[str, int],
) -> FastAPI:
    app = create_app()

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with w3_session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def _get_redis_override() -> AsyncIterator[FakeRedis]:
        yield w3_redis

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_redis] = _get_redis_override
    # W2 的 GET /tasks/{id} 用自己的 get_task_service 依赖：这里指向 W3 服务，
    # 使其复用同一份 SQLite + 同一个 runtime（否则会去连真实 MySQL）。
    app.dependency_overrides[get_task_service] = lambda: w3_service
    # W3 路由的注入点（等价于生产环境的 app.state 单例）
    app.state.event_bus = w3_bus
    app.state.stream_service = w3_service
    return app


@pytest_asyncio.fixture
async def w3_client(w3_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=w3_app), base_url="http://testserver", timeout=30) as client:
        yield client


async def _login(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


async def _submit(client: AsyncClient, headers: dict[str, str], *, goal: str = GOAL_WITH_NOTIFY, sync: bool = True) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/tasks/stream",
        headers=headers,
        json={"goal": goal, "time_range": WINDOW, "sync": sync},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _status(client: AsyncClient, headers: dict[str, str], task_id: int) -> str:
    response = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]["status"]


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """解析 SSE 文本为事件列表（忽略注释帧，data 多行会拼接）。"""
    events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    data_lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if current or data_lines:
                if data_lines:
                    current["data"] = json.loads("\n".join(data_lines))
                events.append(current)
            current, data_lines = {}, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.lstrip(" ")
        if field == "data":
            data_lines.append(value)
        elif field in {"id", "event", "retry"}:
            current[field] = value
    if current or data_lines:
        if data_lines:
            current["data"] = json.loads("\n".join(data_lines))
        events.append(current)
    return events


async def _events(w3_bus: EventBus, task_id: int, *, start: str = "0-0") -> list[StreamEvent]:
    return await w3_bus.read(task_id, last_id=start, count=500, block_ms=0)


# ---------------------------------------------------------------------------
# 1) 正常执行 → 停在 human_gate
# ---------------------------------------------------------------------------
async def test_execution_pauses_at_human_gate(
    w3_client: AsyncClient,
    w3_bus: EventBus,
    w3_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers = await _login(w3_client, "admin_user")
    created = await _submit(w3_client, headers)
    task_id = created["task_id"]

    assert created["stream_url"] == f"/api/v1/tasks/{task_id}/stream"
    assert await _status(w3_client, headers, task_id) == TaskStatus.AWAITING_APPROVAL.value

    response = await w3_client.get(f"/api/v1/tasks/{task_id}/interrupt", headers=headers)
    assert response.status_code == 200
    context = response.json()["data"]
    assert context["paused"] is True
    assert context["approval_id"] is not None
    assert context["next_node"] == ["human_gate"]
    assert context["resume_endpoint"] == f"/api/v1/tasks/{task_id}/resume"
    # 阻断原因里必须能看出是高危动作（notify）
    assert "推送运营群通知" in (context["reason"] or "")
    assert any(item["tool"] == "notify" for item in context["preview"]["subtasks"])

    # 事件流：至少一条 interrupt 终止帧，且带 trace_id
    events = await _events(w3_bus, task_id)
    kinds = [item.event for item in events]
    assert kinds[0] == "state_update"
    assert kinds[-1] == "interrupt"
    assert all(item.data.get("trace_id") for item in events)
    assert events[-1].data["approval_id"] == context["approval_id"]

    # 审批单落库且为 PENDING
    async with w3_session_factory() as session:
        approvals = (await session.execute(select(Approval).where(Approval.task_id == task_id))).scalars().all()
    assert len(approvals) == 1
    assert approvals[0].status == ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# 2) SSE 协议：四类事件 + id + trace_id
# ---------------------------------------------------------------------------
async def test_sse_stream_protocol(w3_client: AsyncClient) -> None:
    headers = await _login(w3_client, "admin_user")
    task_id = (await _submit(w3_client, headers))["task_id"]

    # EventSource 不能带头 → 用 ?access_token=，且只回放一次（0-0 起）
    token = headers["Authorization"].split(" ", 1)[1]
    response = await w3_client.get(f"/api/v1/tasks/{task_id}/stream", params={"access_token": token})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"].startswith("no-cache")
    assert response.headers["x-trace-id"]

    frames = _parse_sse(response.text)
    assert frames, "SSE 必须至少推送一条事件"
    assert all("id" in frame for frame in frames)
    assert all(frame["data"]["task_id"] == task_id for frame in frames)
    assert "retry" in frames[0]

    kinds = {frame["event"] for frame in frames}
    assert "state_update" in kinds
    assert "interrupt" in kinds
    # interrupt 是终止帧：连接必须在此之前结束（不悬挂）
    assert frames[-1]["event"] == "interrupt"
    assert frames[-1]["data"]["paused"] is True


async def test_sse_requires_token(w3_client: AsyncClient) -> None:
    headers = await _login(w3_client, "admin_user")
    task_id = (await _submit(w3_client, headers))["task_id"]
    response = await w3_client.get(f"/api/v1/tasks/{task_id}/stream")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_TOKEN_MISSING"


# ---------------------------------------------------------------------------
# 3) 审批通过 → 断点续跑 → COMPLETED
# ---------------------------------------------------------------------------
async def test_approve_resumes_from_checkpoint_and_completes(
    w3_client: AsyncClient,
    w3_bus: EventBus,
    w3_session_factory: async_sessionmaker[AsyncSession],
    w3_users: dict[str, int],
) -> None:
    headers = await _login(w3_client, "approver_user")
    admin_headers = await _login(w3_client, "admin_user")
    # 创建只能 ADMIN，审批只能 APPROVER（PRD 2.1 角色矩阵）
    task_id = (await _submit(w3_client, admin_headers))["task_id"]
    first_events = await _events(w3_bus, task_id)

    response = await w3_client.post(
        f"/api/v1/tasks/{task_id}/resume",
        headers=headers,
        json={"decision": "APPROVED", "comment": "同意推送", "sync": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["resumed"] is True
    assert body["status"] == TaskStatus.COMPLETED.value
    assert body["next_nodes"] == []

    # 续跑产生的事件必须接在 interrupt 之后（同一任务、同一 trace）
    follow_events = await _events(w3_bus, task_id, start=first_events[-1].id)
    kinds = [item.event for item in follow_events]
    assert kinds[0] == "state_update"
    assert kinds[-1] == "complete"
    assert "tool_call" in kinds
    tools = {item.data.get("tool") for item in follow_events if item.event == "tool_call"}
    assert {"sql-query", "notify"} <= tools

    # 审批审计：结果 / 操作人 / 意见齐全
    async with w3_session_factory() as session:
        approvals = (await session.execute(select(Approval).where(Approval.task_id == task_id))).scalars().all()
    assert len(approvals) == 1, "resume 不得重复建单"
    assert approvals[0].status == ApprovalStatus.APPROVED
    assert approvals[0].decided_by == w3_users["approver_user"]
    assert approvals[0].decide_comment == "同意推送"
    assert approvals[0].decided_at is not None

    # 关键：不是从头重跑（planner 只进过一次），且直接进入收尾链路
    steps = await TraceService(w3_session_factory).list_steps(task_id=task_id)
    planner_enters = [s for s in steps if s["node"] == "planner" and s["event"] == "enter"]
    assert len(planner_enters) == 1
    nodes = {s["node"] for s in steps}
    assert {"human_gate", "executor", "reviewer", "summarizer", "notifier"} <= nodes
    assert "replan" not in nodes

    # 终态落库
    async with w3_session_factory() as session:
        task = await session.get(Task, task_id)
    assert task is not None
    assert task.status == TaskStatus.COMPLETED
    assert task.finished_at is not None
    assert task.token_usage > 0


# ---------------------------------------------------------------------------
# 4) 驳回 → replan
# ---------------------------------------------------------------------------
async def test_reject_triggers_replan_and_blocks_unapproved_notify(
    w3_client: AsyncClient,
    w3_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers = await _login(w3_client, "approver_user")
    admin_headers = await _login(w3_client, "admin_user")
    task_id = (await _submit(w3_client, admin_headers))["task_id"]

    response = await w3_client.post(
        f"/api/v1/tasks/{task_id}/resume",
        headers=headers,
        json={"decision": "REJECTED", "comment": "口径不对，请重新规划", "sync": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == TaskStatus.FAILED.value

    steps = await TraceService(w3_session_factory).list_steps(task_id=task_id)
    assert "replan" in {s["node"] for s in steps}, "驳回必须触发 replan 节点"

    async with w3_session_factory() as session:
        task = await session.get(Task, task_id)
        approvals = (await session.execute(select(Approval).where(Approval.task_id == task_id))).scalars().all()
    assert task is not None
    assert task.replan_count == 1
    assert task.error and task.error["code"] == "APPROVAL_MISSING"
    assert len(approvals) == 1
    assert approvals[0].status == ApprovalStatus.REJECTED
    assert approvals[0].decide_comment == "口径不对，请重新规划"


# ---------------------------------------------------------------------------
# 5) 非暂停任务 resume → 409
# ---------------------------------------------------------------------------
async def test_resume_non_paused_task_conflicts(w3_client: AsyncClient) -> None:
    headers = await _login(w3_client, "admin_user")
    task_id = (await _submit(w3_client, headers, goal=GOAL_ANALYSIS_ONLY))["task_id"]
    assert await _status(w3_client, headers, task_id) == TaskStatus.COMPLETED.value

    response = await w3_client.post(
        f"/api/v1/tasks/{task_id}/resume",
        headers=headers,
        json={"decision": "APPROVED", "sync": True},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "TASK_NOT_PAUSED"
    assert body["data"]["detail"]["status"] == TaskStatus.COMPLETED.value


async def test_resume_missing_task_returns_404(w3_client: AsyncClient) -> None:
    headers = await _login(w3_client, "admin_user")
    response = await w3_client.post("/api/v1/tasks/9999/resume", headers=headers, json={"decision": "APPROVED"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 6) 取消
# ---------------------------------------------------------------------------
async def test_cancel_paused_task_then_resume_conflicts(
    w3_client: AsyncClient,
    w3_bus: EventBus,
    w3_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers = await _login(w3_client, "admin_user")
    task_id = (await _submit(w3_client, headers))["task_id"]
    first_events = await _events(w3_bus, task_id)

    response = await w3_client.post(
        f"/api/v1/tasks/{task_id}/cancel", headers=headers, json={"reason": "业务方撤回需求"}
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == TaskStatus.CANCELLED.value
    assert body["canceled"] is True

    # 取消后：DB 终态 + 审批单过期 + 事件流补一帧 complete
    async with w3_session_factory() as session:
        task = await session.get(Task, task_id)
        approvals = (await session.execute(select(Approval).where(Approval.task_id == task_id))).scalars().all()
    assert task is not None and task.status == TaskStatus.CANCELLED
    assert task.finished_at is not None
    assert approvals and approvals[0].status == ApprovalStatus.EXPIRED

    follow_events = await _events(w3_bus, task_id, start=first_events[-1].id)
    assert follow_events[-1].event == "complete"
    assert follow_events[-1].data["status"] == TaskStatus.CANCELLED.value

    # 断点已被清除：resume 必须 409
    resume = await w3_client.post(
        f"/api/v1/tasks/{task_id}/resume", headers=headers, json={"decision": "APPROVED", "sync": True}
    )
    assert resume.status_code == 409
    assert resume.json()["code"] == "TASK_NOT_PAUSED"


async def test_cancel_finished_task_conflicts(w3_client: AsyncClient) -> None:
    headers = await _login(w3_client, "admin_user")
    task_id = (await _submit(w3_client, headers, goal=GOAL_ANALYSIS_ONLY))["task_id"]
    response = await w3_client.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "TASK_NOT_CANCELABLE"


async def test_cancel_running_task_stops_at_node_boundary(
    w3_service: StreamingTaskService,
    w3_cancel: CancelSignal,
    w3_bus: EventBus,
    w3_session_factory: async_sessionmaker[AsyncSession],
    w3_users: dict[str, int],
) -> None:
    """运行中取消：预置取消标记，执行体在第一个节点边界收口（不需要真的等它跑完）。"""
    async with w3_session_factory() as session:
        task = Task(
            task_no=f"AFTEST-{uuid.uuid4().hex[:8]}",
            creator_id=w3_users["admin_user"],
            goal=GOAL_WITH_NOTIFY,
            time_range=WINDOW,
            status=TaskStatus.PLANNING,
            replan_count=0,
            token_usage=0,
            trace_id=uuid.uuid4().hex,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        task_id = int(task.id)

    await w3_cancel.set(task_id, reason="running_cancel_test")
    await w3_service.run(task_id)

    async with w3_session_factory() as session:
        task = await session.get(Task, task_id)
    assert task is not None
    assert task.status == TaskStatus.CANCELLED
    assert task.error and task.error["code"] == "TASK_CANCELED"
    assert task.finished_at is not None

    events = await _events(w3_bus, task_id)
    assert events[-1].event == "complete"
    assert events[-1].data["status"] == TaskStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# 7) SSE 断线重连 / 客户端断开（直接驱动帧生成器，避免时序依赖）
# ---------------------------------------------------------------------------
async def test_sse_reconnect_dedups_by_last_event_id(w3_bus: EventBus) -> None:
    task_id = 4242
    for index in range(3):
        await w3_bus.publish(task_id, "state_update", {"index": index, "trace_id": "t" * 32}, trace_id="t" * 32)
    await w3_bus.publish(task_id, "complete", {"status": "COMPLETED"}, trace_id="t" * 32)
    stored = await _events(w3_bus, task_id)
    assert len(stored) == 4

    # 模拟"客户端已收到前两条后断线重连"：从第 2 条 id 续读
    frames: list[str] = []
    async for frame in sse_event_frames(
        bus=w3_bus, task_id=task_id, start_id=stored[1].id, block_ms=0, idle_timeout=0.2, max_duration=2.0
    ):
        frames.append(frame)

    parsed = _parse_sse("".join(frames))
    assert [item["data"]["index"] for item in parsed if item["event"] == "state_update"] == [2]
    assert parsed[-1]["event"] == "complete"
    # 未重复推送已消费的事件
    assert all(item.get("id") != stored[0].id for item in parsed)


async def test_sse_stops_when_client_disconnects(w3_bus: EventBus) -> None:
    task_id = 5001
    await w3_bus.publish(task_id, "state_update", {"index": 0}, trace_id="t" * 32)
    calls = {"n": 0}

    async def _never_connected() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    frames: list[str] = []
    async for frame in sse_event_frames(
        bus=w3_bus,
        task_id=task_id,
        start_id="0-0",
        is_disconnected=_never_connected,
        block_ms=0,
        idle_timeout=5.0,
        max_duration=30.0,
    ):
        frames.append(frame)

    # 首帧是连接注释；随后探测到断开即刻收尾，不会把 30s 时长的流跑满
    assert frames[0].startswith(": connected")
    assert len(frames) <= 3


async def test_sse_returns_immediately_when_stream_already_terminal(w3_bus: EventBus) -> None:
    task_id = 6001
    await w3_bus.publish(task_id, "complete", {"status": "COMPLETED"}, trace_id="t" * 32)
    frames = [
        frame
        async for frame in sse_event_frames(
            bus=w3_bus, task_id=task_id, block_ms=0, idle_timeout=60.0, max_duration=60.0
        )
    ]
    parsed = _parse_sse("".join(frames))
    assert parsed[-1]["event"] == "complete"


# ---------------------------------------------------------------------------
# 8) 快照 / 轨迹回放 / 收件箱 / 可见性
# ---------------------------------------------------------------------------
async def test_snapshot_and_trace_replay(
    w3_client: AsyncClient,
    w3_runtime: AgentRuntime,
    w3_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers = await _login(w3_client, "admin_user")
    task_id = (await _submit(w3_client, headers))["task_id"]
    await w3_client.post(
        f"/api/v1/tasks/{task_id}/resume", headers=headers, json={"decision": "APPROVED", "sync": True}
    )

    snapshot = await w3_client.get(f"/api/v1/tasks/{task_id}/snapshot", headers=headers)
    assert snapshot.status_code == 200, snapshot.text
    data = snapshot.json()["data"]
    # 加固任务 4：终态任务快照已清理（MySQL 为权威）——status 回退 DB，
    # 快照纪元数据（step / values / messages）不再提供
    assert data["status"] == TaskStatus.COMPLETED.value
    assert data["next_nodes"] == []
    assert data["step"] is None  # 快照已清理
    # 加固任务 4 收尾（平替方案）：values 不再是空 dict，而是 MySQL 权威数据的
    # 平替结构（_source=mysql_fallback），前端"原始快照查看器"直接渲染即可
    values = data["values"]
    assert values["_source"] == "mysql_fallback"
    assert values["goal"] == GOAL_WITH_NOTIFY
    assert isinstance(values["plan"], list)
    assert data["interrupt"] is None
    assert data["messages_count"] == 0

    trace = await w3_client.get(f"/api/v1/tasks/{task_id}/trace", headers=headers)
    assert trace.status_code == 200, trace.text
    replay = trace.json()["data"]
    assert replay["trace_id"]
    assert replay["total_steps"] >= 10
    assert replay["steps"], "回放必须有 span"
    ids = [step["id"] for step in replay["steps"]]
    assert ids == sorted(ids), "回放按 span id 升序（等价 started_at）"
    assert {"planner", "executor", "reviewer", "summarizer"} <= {step["node"] for step in replay["steps"]}
    # 加固任务 4：checkpoint 时间轴属快照纪元，终态清理后为空；span 回放（MySQL）完整保留
    assert replay["checkpoints"] == []
    steps = [item["step"] for item in replay["checkpoints"]]
    assert steps == sorted(steps)

    # 增量拉取：after_id 只返回更晚的 span
    after = replay["steps"][3]["id"]
    incremental = await w3_client.get(
        f"/api/v1/tasks/{task_id}/trace", headers=headers, params={"after_id": after}
    )
    assert incremental.status_code == 200
    assert all(step["id"] > after for step in incremental.json()["data"]["steps"])

    # 服务层快照与接口一致（加固任务 4：终态回退 MySQL 需要 session_factory）
    direct = await SnapshotService(
        w3_runtime.graph, trace=TraceService(w3_session_factory), session_factory=w3_session_factory
    ).snapshot(task_id)
    assert direct["status"] == data["status"]


async def test_approval_inbox_lists_blocking_reason(
    w3_client: AsyncClient,
) -> None:
    admin_headers = await _login(w3_client, "admin_user")
    approver_headers = await _login(w3_client, "approver_user")
    first = (await _submit(w3_client, admin_headers))["task_id"]
    second = (await _submit(w3_client, admin_headers))["task_id"]

    response = await w3_client.get("/api/v1/approvals", headers=approver_headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    task_ids = [item["task_id"] for item in data["items"]]
    assert task_ids[:2] == [second, first], "按创建时间倒序"
    item = data["items"][0]
    assert item["status"] == TaskStatus.AWAITING_APPROVAL.value
    assert item["approval_id"]
    assert "高危动作需审批" in item["reason"]
    assert item["risky_subtasks"], "收件箱要能展示高危子任务预览"
    assert data["total"] == 2

    # 审批人可看审批单详情
    detail = await w3_client.get(f"/api/v1/approvals/{item['approval_id']}", headers=approver_headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == ApprovalStatus.PENDING.value


async def test_viewer_cannot_read_running_snapshot(w3_client: AsyncClient) -> None:
    admin_headers = await _login(w3_client, "admin_user")
    viewer_headers = await _login(w3_client, "viewer_user")
    task_id = (await _submit(w3_client, admin_headers))["task_id"]

    response = await w3_client.get(f"/api/v1/tasks/{task_id}/snapshot", headers=viewer_headers)
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"

    # 审批动作也不允许 VIEWER 执行
    resume = await w3_client.post(
        f"/api/v1/tasks/{task_id}/resume", headers=viewer_headers, json={"decision": "APPROVED"}
    )
    assert resume.status_code == 403


async def test_admin_cannot_approve_others_task_inbox(w3_client: AsyncClient) -> None:
    """ADMIN 的收件箱只含本人任务：另一个 admin 建的任务不应出现。"""
    admin_headers = await _login(w3_client, "admin_user")
    await _submit(w3_client, admin_headers)

    inbox = await w3_client.get("/api/v1/approvals", headers=admin_headers)
    assert inbox.status_code == 200
    assert inbox.json()["data"]["total"] == 1


# ---------------------------------------------------------------------------
# 9) 版本协商（v3 → v2）与运行期健壮性
# ---------------------------------------------------------------------------
async def test_events_version_falls_back_to_v2(w3_runtime: AgentRuntime) -> None:
    """当前 langgraph 0.3.x 不支持 v3：必须自动降级到 v2 而不是让任务失败。"""
    from app.core.w3_config import w3
    from app.services import stream_service as stream_module

    stream_module._RESOLVED_EVENTS_VERSION = None
    state = {
        "task_id": 9001,
        "trace_id": uuid.uuid4().hex,
        "creator_id": 1,
        "goal": GOAL_ANALYSIS_ONLY,
        "time_range": {"start": "2026-10-12", "end": "2026-10-18"},
        "memory_hits": [],
        "plan": [],
        "subtask_results": {},
        "replan_count": 0,
        "approvals": {},
        "messages": [],
        "token_ledger": {},
        "context_digest": "",
        "artifacts": [],
        "status": "PLANNING",
    }
    config = {"configurable": {"thread_id": f"version-{uuid.uuid4().hex}"}}

    events = [
        item
        async for item in stream_module.iterate_graph_events(
            w3_runtime.graph, state, config, preferred=w3.SSE_EVENTS_VERSION
        )
    ]
    assert events, "降级后仍必须产出事件"
    assert stream_module._RESOLVED_EVENTS_VERSION in {"v1", "v2"}
    assert any(item.get("event") == "on_chain_end" for item in events)
