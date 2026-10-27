"""加固任务 4：任务终态 Redis 清理（+ 任务 2 收尾：熔断状态端到端可见）。

覆盖：
1. ``cleanup_task_artifacts`` 原语：异步/同步 delete_thread 分派、无 API 跳过、
   fail-open、取消信号清理；
2. ``TaskService._finalize`` 集成：COMPLETED/FAILED 落库成功后触发清理，
   AWAITING_APPROVAL **不**触发（快照必须保留供 resume）；
3. 任务 2 收尾端到端：executor 连续失败真实触发熔断 → ``ToolRegistry.list_tools``
   可见 ``circuit_open=true``（锁死「写入键 == 注册表查询键」口径，防未来漂移）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.executor import Executor
from app.core.circuit_breaker import ToolCircuitBreaker
from app.core.config import settings
from app.core.w5_config import w5
from app.schemas.graph_state import SubtaskPlan
from app.schemas.orm_models import Task
from app.services.cancel_service import CancelSignal
from app.services.task_cleanup import cleanup_task_artifacts
from app.services.task_service import TaskService
from app.services.tool_registry import ToolRegistry
from app.tools.mcp_client import ToolInvocationError

TIME_RANGE = {"start": "2026-10-12", "end": "2026-10-18"}


# ---------------------------------------------------------------------------
# 1. 清理原语
# ---------------------------------------------------------------------------
class FakeAsyncSaver:
    """记录 adelete_thread 调用的异步 saver 桩。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(str(thread_id))


class FakeSyncSaver:
    """只有同步 delete_thread 的 saver 桩（内存降级后端形态）。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_thread(self, thread_id: str) -> None:
        self.deleted.append(str(thread_id))


class BoomSaver:
    async def adelete_thread(self, thread_id: str) -> None:
        raise RuntimeError("redis 炸了")


@pytest.mark.asyncio
async def test_cleanup_deletes_checkpointer_thread_async() -> None:
    saver = FakeAsyncSaver()
    result = await cleanup_task_artifacts(7, checkpointer=saver)
    assert result == {"checkpointer": True, "cancel_signal": False}
    assert saver.deleted == ["7"]  # thread_id 口径 = str(task_id)


@pytest.mark.asyncio
async def test_cleanup_falls_back_to_sync_delete_thread() -> None:
    saver = FakeSyncSaver()
    result = await cleanup_task_artifacts(9, checkpointer=saver)
    assert result["checkpointer"] is True
    assert saver.deleted == ["9"]


@pytest.mark.asyncio
async def test_cleanup_skips_checkpointer_without_delete_api() -> None:
    """旧版 saver 无删除 API：静默跳过（交给 72h TTL 兜底），不报错。"""
    saver = SimpleNamespace()  # 既无 adelete_thread 也无 delete_thread
    result = await cleanup_task_artifacts(1, checkpointer=saver)
    assert result["checkpointer"] is False


@pytest.mark.asyncio
async def test_cleanup_fail_open_when_saver_raises() -> None:
    result = await cleanup_task_artifacts(1, checkpointer=BoomSaver())
    assert result["checkpointer"] is False  # 失败被吞掉，绝不抛给调用方


@pytest.mark.asyncio
async def test_cleanup_clears_cancel_signal(fake_redis: Any) -> None:
    signal = CancelSignal(redis_client=fake_redis)
    await signal.set(5, reason="manual")
    assert await signal.is_set(5) is True

    result = await cleanup_task_artifacts(5, cancel_signal=signal)
    assert result["cancel_signal"] is True
    assert await signal.is_set(5) is False


# ---------------------------------------------------------------------------
# 2. _finalize 集成：真终态清理、挂起态保留
# ---------------------------------------------------------------------------
class _RecorderSaver:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(str(thread_id))


def _fake_runtime(saver: _RecorderSaver) -> SimpleNamespace:
    return SimpleNamespace(graph=SimpleNamespace(checkpointer=saver))


async def _seed_task(session_factory: Any, *, status: str) -> int:
    async with session_factory() as session:
        task = Task(
            task_no=f"AF20261022-{status[:4]}",
            creator_id=1,
            goal="分析 GMV 下滑原因",
            time_range=TIME_RANGE,
            trace_id=f"trace-{status}-{id(object()):x}",
        )
        session.add(task)
        await session.commit()
        return int(task.id)


@pytest.fixture
def _no_memory(monkeypatch: pytest.MonkeyPatch):
    """关掉分层记忆，隔离 _finalize_memory 对外部的依赖。"""
    monkeypatch.setattr(w5, "ENABLE_LAYERED_MEMORY", False)


@pytest.mark.asyncio
async def test_finalize_completed_cleans_checkpointer(
    session_factory, _no_memory
) -> None:
    from app.schemas.orm_models import TaskStatus

    saver = _RecorderSaver()
    service = TaskService(session_factory=session_factory, runtime=_fake_runtime(saver))
    task_id = await _seed_task(session_factory, status="done")

    state = {"task_id": task_id, "token_ledger": {"planner": {"input": 10, "output": 5}}}
    await service._finalize(task_id, state, TaskStatus.COMPLETED)

    assert saver.deleted == [str(task_id)]  # MySQL 已持久化 → Redis 快照回收
    async with session_factory() as session:
        task = await session.get(Task, task_id)
        assert task.status == TaskStatus.COMPLETED
        assert task.token_usage == 15  # 清理不影响落库结果


@pytest.mark.asyncio
async def test_finalize_awaiting_approval_keeps_checkpointer(
    session_factory, _no_memory
) -> None:
    from app.schemas.orm_models import TaskStatus

    saver = _RecorderSaver()
    service = TaskService(session_factory=session_factory, runtime=_fake_runtime(saver))
    task_id = await _seed_task(session_factory, status="wait")

    await service._finalize(task_id, {"task_id": task_id}, TaskStatus.AWAITING_APPROVAL)

    assert saver.deleted == []  # 挂起态快照必须保留供 resume


@pytest.mark.asyncio
async def test_snapshot_falls_back_to_mysql_after_cleanup(
    session_factory, _no_memory
) -> None:
    """快照清理后的终态任务：GET /snapshot 的 status 回退 MySQL 权威值。"""
    from app.schemas.orm_models import TaskStatus
    from app.services.snapshot_service import SnapshotService

    saver = _RecorderSaver()
    service = TaskService(session_factory=session_factory, runtime=_fake_runtime(saver))
    task_id = await _seed_task(session_factory, status="done2")
    state = {"task_id": task_id, "token_ledger": {"planner": {"input": 1, "output": 1}}}
    await service._finalize(task_id, state, TaskStatus.COMPLETED)
    assert saver.deleted == [str(task_id)]

    class _EmptyGraph:
        """模拟清理后的 checkpointer：aget_state 返回空快照。"""

        async def aget_state(self, config: dict) -> Any:
            return SimpleNamespace(values=None, next=(), tasks=(), metadata={}, created_at=None)

    snap = SnapshotService(_EmptyGraph(), session_factory=session_factory)
    data = await snap.snapshot(task_id)
    assert data["status"] == TaskStatus.COMPLETED.value
    assert data["task_id"] == task_id


# ---------------------------------------------------------------------------
# 3. 任务 2 收尾：executor 触发熔断 → 注册表端到端可见
# ---------------------------------------------------------------------------
class FailingPool:
    """始终抛可重试业务错误的连接池桩。"""

    def __init__(self, code: str = "SQL_TIMEOUT") -> None:
        self.code = code
        self.tools: list[Any] = []
        self.calls = 0

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        self.calls += 1
        raise ToolInvocationError(self.code, "模拟工具失败")


def _subtask(seq: int) -> SubtaskPlan:
    return SubtaskPlan(
        seq=seq,
        name=f"子任务{seq}"[:64],
        description="按目标时间窗取数与归因",
        tool_hint="sql_query",
        depends_on=[],
        risk_level="normal",
        acceptance="返回数据或结论",
    )


@pytest.mark.asyncio
async def test_executor_triggered_circuit_visible_in_registry(
    mock_llm, context_service, fake_redis
) -> None:
    """端到端锁链：executor 写熔断键（sql-query）→ 注册表查询同名键 → circuit_open=true。

    这条测试锁死「executor 写入键 == 注册表查询键」的口径；任何一侧改名
    （如改回下划线）都会在这里红掉，防止"熔断了但页面看不见"的正向缺口。
    """
    breaker = ToolCircuitBreaker(fake_redis, failure_threshold=2, cooldown_seconds=60)
    pool = FailingPool("SQL_TIMEOUT")
    executor = Executor(pool, mock_llm, context_service, backoff_enabled=False, circuit=breaker)

    run = await executor.run_subtask(_subtask(1), siblings=[], digest="", time_range=TIME_RANGE)
    assert run.execution.status == "FAILED"

    # 键已由 executor 真实写入（不是测试手工 SET）
    assert await fake_redis.get(settings.key("tool:circuit", "sql-query")) is not None

    registry = ToolRegistry(redis=fake_redis)
    items = {item["name"]: item for item in await registry.list_tools()}
    assert items["sql-query"]["circuit_open"] is True
    assert items["kb-search"]["circuit_open"] is False  # 隔离：只熔断出事的工具
    assert items["sql-query"]["enabled"] is True  # 人工开关与熔断独立
