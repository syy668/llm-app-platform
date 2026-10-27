"""加固任务 5：导出流式化 + 分批游标 + 平替快照。

覆盖点：
1. ``TraceService.iter_steps`` 分批游标：keyset 分页顺序/limit/after_id/读尽提前退出；
2. 导出端点（任务归档 + 轨迹）流式输出整体是**合法且完整**的 JSON（json.loads 整体验证），
   steps 内容、顺序、truncated/total_steps/metrics 与库内数据一致；
3. 大体积假数据的内存断言：生成器/消费方全程持有内存（tracemalloc 峰值）远小于完整
   文件体积，证明是增量拼接而不是一次性组装；
4. 平替快照：终态任务快照清理后 ``GET /tasks/{id}/snapshot`` 的 values 带
   MySQL 回退数据（_source=mysql_fallback + plan 子任务列表），前端查看器零改动可用。

测试环境沿用 conftest：SQLite 内存库 + fakeredis；span 直接批量插入（不走图执行），
鉴权依赖直接 override（导出是只读端点，登录链路不在本文件验证范围）。
"""

from __future__ import annotations

import json
import tracemalloc
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.deps import get_current_user
from app.main import create_app
from app.routers.task_export import _steps_stream
from app.routers.tasks_ext import get_stream_service
from app.schemas.auth import CurrentUser
from app.schemas.enums import TaskStatus, UserRole
from app.schemas.orm_models import Task, TaskExecution, User
from app.services.stream_service import StreamingTaskService
from app.services.trace_service import TraceService
from tests.conftest import _install_overrides, fake_redis, seeded_users, session_factory  # noqa: F401

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 夹具：最小可用的导出服务 + 客户端
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def seeded_task(session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int]) -> Task:
    """建一个 COMPLETED 任务（admin 创建，详情/导出权限都过得去）。"""
    async with session_factory() as session:
        task = Task(
            creator_id=seeded_users["admin_user"],
            task_no="T-EXPORT-1",
            trace_id="trace-export-1",
            goal="导出压测任务",
            status=TaskStatus.COMPLETED,
            time_range={"start": "2026-10-12", "end": "2026-10-18"},
            token_usage=1234,
            replan_count=0,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task


async def _seed_spans(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: int,
    count: int,
    *,
    payload_size: int = 0,
) -> list[int]:
    """批量插入 span（一次 commit），返回 id 列表（升序）。"""
    rows = [
        TaskExecution(
            trace_id=f"trace-{task_id}",
            span_id=f"span-{task_id}-{i:06d}",
            task_id=task_id,
            node="executor",
            event="tool.call" if i % 4 == 0 else "exit",
            tool_name="sql-query" if i % 4 == 0 else None,
            latency_ms=i,
            payload=({"blob": "x" * payload_size} if payload_size else {"i": i}),
        )
        for i in range(count)
    ]
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()
    async with session_factory() as session:
        result = await session.execute(
            select(TaskExecution.id).where(TaskExecution.task_id == task_id).order_by(TaskExecution.id.asc())
        )
        return [int(r) for r in result.scalars().all()]


class _NoRuntimeService(StreamingTaskService):
    """导出只走 get_detail + trace.count + iter_steps；runtime 显式不可用，
    用于验证"快照缺失/图运行时不可用时导出依然成功"的降级路径。"""

    async def runtime(self):  # noqa: ANN202
        raise RuntimeError("runtime not needed for export")


async def _collect(
    trace: TraceService, task_id: int, *, limit: int, batch: int, after_id: int = 0
) -> list[dict[str, Any]]:
    return [s async for s in trace.iter_steps(task_id=task_id, limit=limit, batch=batch, after_id=after_id)]


@pytest_asyncio.fixture
async def export_client(
    session_factory: async_sessionmaker[AsyncSession],
    fake_redis,  # noqa: ANN001 - conftest 夹具
    seeded_users: dict[str, int],
) -> AsyncIterator[AsyncClient]:
    app = create_app()
    _install_overrides(app, session_factory, fake_redis)

    admin_id = seeded_users["admin_user"]

    async def _fake_user() -> CurrentUser:
        async with session_factory() as session:
            user = await session.get(User, admin_id)
        return CurrentUser(id=user.id, username=user.username, role=user.role, status=int(user.status))

    class _BusStub:
        redis_client = fake_redis

    service = _NoRuntimeService(session_factory=session_factory, bus=_BusStub())
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_stream_service] = lambda: service

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", timeout=60) as http_client:
        yield http_client


# ---------------------------------------------------------------------------
# 1. iter_steps 分批游标
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_iter_steps_keyset_pagination(
    session_factory: async_sessionmaker[AsyncSession], seeded_task: Task
) -> None:
    ids = await _seed_spans(session_factory, int(seeded_task.id), 25)
    trace = TraceService(session_factory)

    # 全量（batch=10 → 3 批）：升序且与库内 id 一致
    collected = await _collect(trace, int(seeded_task.id), limit=500, batch=10)
    assert [s["id"] for s in collected] == ids

    # limit 生效：只要前 12 条
    limited = await _collect(trace, int(seeded_task.id), limit=12, batch=5)
    assert [s["id"] for s in limited] == ids[:12]

    # after_id 生效：跳过前 20 条
    after = await _collect(trace, int(seeded_task.id), limit=500, batch=4, after_id=ids[19])
    assert [s["id"] for s in after] == ids[20:]

    # 空任务：直接空序列
    empty = [s async for s in trace.iter_steps(task_id=999_999, batch=10)]
    assert empty == []


# ---------------------------------------------------------------------------
# 2. 导出端点：流式 JSON 整体合法 + 内容正确
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_task_export_streams_valid_json(
    export_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], seeded_task: Task
) -> None:
    ids = await _seed_spans(session_factory, int(seeded_task.id), 120)

    response = await export_client.get(f"/api/v1/tasks/{seeded_task.id}/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-agentflow-schema"] == "agentflow.task_export.v1"

    data = json.loads(response.text)  # 整体可解析 = 拼接逻辑构造性正确
    assert data["schema"] == "agentflow.task_export.v1"
    assert data["task"]["task_id"] == int(seeded_task.id)
    assert data["metrics"]["token_usage"] == 1234
    assert data["metrics"]["tool_calls"] == 30  # i%4==0 → 120/4
    assert data["trace"]["total_steps"] == 120
    assert data["trace"]["truncated"] is False
    steps = data["trace"]["steps"]
    assert [s["id"] for s in steps] == ids
    assert steps[0]["event"] == "tool.call"


@pytest.mark.asyncio
async def test_trace_export_streams_valid_json(
    export_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], seeded_task: Task
) -> None:
    ids = await _seed_spans(session_factory, int(seeded_task.id), 45, payload_size=64)

    response = await export_client.get(f"/api/v1/tasks/{seeded_task.id}/trace/export")
    assert response.status_code == 200, response.text
    assert response.headers["x-agentflow-schema"] == "agentflow.trace_export.v1"

    data = json.loads(response.text)
    assert data["schema"] == "agentflow.trace_export.v1"
    assert data["goal"] == "导出压测任务"
    assert data["total_steps"] == 45
    assert data["truncated"] is False
    assert [s["id"] for s in data["steps"]] == ids


@pytest.mark.asyncio
async def test_export_truncation_flag(
    export_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_task: Task,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超过导出上限：流内只给上限条数，truncated=true，total_steps=实际上限。"""
    from app.routers import task_export as te

    monkeypatch.setattr(te, "_MAX_EXPORT_STEPS", 10)
    await _seed_spans(session_factory, int(seeded_task.id), 33)

    response = await export_client.get(f"/api/v1/tasks/{seeded_task.id}/trace/export")
    assert response.status_code == 200
    data = json.loads(response.text)
    assert data["total_steps"] == 10
    assert data["truncated"] is True
    assert len(data["steps"]) == 10


# ---------------------------------------------------------------------------
# 3. 内存断言：增量拼接而非一次性组装
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_streaming_consumer_memory_is_bounded(
    session_factory: async_sessionmaker[AsyncSession], seeded_task: Task
) -> None:
    """800 条 × 8KB payload ≈ 6.4MB 文本；按 StreamingResponse 的真实消费方式
    （不保留 chunk，只累计长度）测 tracemalloc 峰值：应约等于单批（100×8KB），
    而不是全量 6.4MB。"""
    await _seed_spans(session_factory, int(seeded_task.id), 800, payload_size=8192)
    trace = TraceService(session_factory)

    tracemalloc.start()
    total_len = 0
    async for chunk in _steps_stream(trace, int(seeded_task.id), "{", "}", limit=800, batch=100):
        total_len += len(chunk)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert total_len > 5_000_000  # 数据量到位
    assert peak < 3_000_000, f"峰值 {peak} 应≈单批（0.8MB）+编码开销，不应接近全量 6.4MB"


# ---------------------------------------------------------------------------
# 4. 平替快照（加固任务 4 收尾）：终态任务 values 用 MySQL 数据填充
# ---------------------------------------------------------------------------
class _StubGraph:
    """snapshot() 只读 graph.aget_state；stub 直接返回指定状态（该 langgraph 版本
    的 MemorySaver 无 aget_state，stub 同时隔离 collect_interrupts 路径）。"""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._values = values or {}

    async def aget_state(self, config):  # noqa: ANN001, ANN202
        from types import SimpleNamespace

        return SimpleNamespace(
            values=dict(self._values), next=(), metadata={}, created_at=None, tasks=()
        )


@pytest.mark.asyncio
async def test_snapshot_mysql_fallback_values(
    session_factory: async_sessionmaker[AsyncSession], seeded_task: Task
) -> None:
    from app.services.snapshot_service import SnapshotService

    snap = SnapshotService(_StubGraph(), session_factory=session_factory)
    data = await snap.snapshot(int(seeded_task.id))

    assert data["status"] == "COMPLETED"  # MySQL 权威状态
    values = data["values"]
    assert values["_source"] == "mysql_fallback"
    assert values["goal"] == "导出压测任务"
    assert values["token_usage"] == 1234
    assert values["plan"] == []  # 无子任务时空列表，字段仍在


@pytest.mark.asyncio
async def test_snapshot_fallback_keeps_running_tasks_untouched(
    session_factory: async_sessionmaker[AsyncSession], seeded_task: Task
) -> None:
    """快照存在（非终态清理场景）时绝不混入 MySQL 平替数据。"""
    from app.services.snapshot_service import SnapshotService

    snap = SnapshotService(
        _StubGraph({"status": "RUNNING", "plan": [{"seq": 1, "name": "x"}], "messages": []}),
        session_factory=session_factory,
    )
    data = await snap.snapshot(int(seeded_task.id))

    assert data["status"] == "RUNNING"
    assert "_source" not in data["values"]  # 未触发平替
    assert data["values"]["plan"] == [{"seq": 1, "name": "x"}]
