"""W3 运行时装配：在 W2 图之上挂载「事件钩子 + 幂等审批落库 + LLM 轨迹」。

为什么不复用 ``task_service.get_runtime()``：
- 那个单例的 MCP 池没有 ``on_event`` 钩子（W2 建池时不传），拿不到工具调用事件；
- W3 需要把审批建单/落库绑定到"当前 Session 工厂"（便于单测用 SQLite），
  W2 的默认实现走全局 MySQL 工厂；
- 两者图结构完全一致（同一个 ``build_graph``、同一套节点），只是依赖注入不同。

幂等审批建单（W3 必须补的一处）：
LangGraph 恢复时**会重新执行整个 human_gate 节点**，而节点在 ``interrupt()`` 之前就调用了
``create_pending_approval``。若不拦截，每次 resume 都会多插一条 PENDING 审批单。
W2 的 ``human_gate`` 靠 state 里的 ``approvals.approval_id`` 做幂等，但首次执行时该字段
尚未写回 state（节点还没返回），所以这一层幂等只能由注入的 creator 兜住。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.graph import AgentRuntime, build_graph
from app.agents.llm import LLMClient
from app.core.logging import get_logger
from app.schemas.agent_io import ToolDef
from app.services.approval_service import apply_decision, create_pending_approval
from app.services.checkpoint_service import get_checkpointer
from app.services.context_service import ContextService
from app.services.interrupt_service import find_pending_approval
from app.services.stream_context import InstrumentedLLM, mcp_event_hook
from app.tools.mcp_client import MCPClientPool

logger = get_logger(__name__)

_runtime: AgentRuntime | None = None
_runtime_lock = asyncio.Lock()


def _session_factory(
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> async_sessionmaker[AsyncSession]:
    if session_factory is not None:
        return session_factory
    from app.db.session import get_session_factory

    return get_session_factory()


def make_idempotent_approval_creator(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """同一任务已有 PENDING 审批单时复用，避免 resume 重复建单。"""
    factory = _session_factory(session_factory)

    async def creator(payload: dict[str, Any]) -> dict[str, Any]:
        task_id = payload.get("task_id")
        if task_id is not None:
            existing = await find_pending_approval(int(task_id), session_factory=factory)
            if existing is not None:
                logger.info(
                    "approval_reused",
                    extra={"task_id": int(task_id), "approval_id": existing["approval_id"]},
                )
                return {
                    "approval_id": existing["approval_id"],
                    "status": existing["status"],
                    "expires_at": existing["expires_at"],
                    "type": existing.get("type"),
                    "reason": existing.get("reason"),
                }
        return await create_pending_approval(**payload, session_factory=factory)

    return creator


def make_approval_updater(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> Callable[[int, str, str, int | None], Awaitable[None]]:
    """审批决策落库（task_id / 结果 / 操作人 / 时间 / 驳回意见）。"""
    factory = _session_factory(session_factory)

    async def updater(approval_id: int, decision: str, comment: str, decided_by: int | None) -> None:
        await apply_decision(
            int(approval_id),
            decision=decision,
            comment=comment,
            decided_by=decided_by,
            session_factory=factory,
        )

    return updater


async def create_w3_runtime(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    pool: MCPClientPool | None = None,
    llm: Any = None,
    context: ContextService | None = None,
    checkpointer: Any = None,
    force_local_review: bool | None = None,
    discover_tools: bool = True,
    circuit: Any = None,
) -> AgentRuntime:
    """构建 W3 运行时（生产与单测通用入口）。"""
    factory = _session_factory(session_factory)
    # 任务 B：pool 收敛为进程级共享池单例（与 graph.create_runtime 同一套 4 个 MCP 子进程），
    # 消灭双 Pool 并存。审批建单幂等 / Session 工厂绑定等 W3 差异仍在 runtime 层，不受影响。
    from app.tools.mcp_client import get_shared_pool

    runtime_pool = pool or await get_shared_pool()
    if discover_tools and not runtime_pool.tools:
        await runtime_pool.discover()

    # 加固任务 2：熔断器。仅生产默认装配路径（未显式传 pool）挂载——显式传 pool 的
    # 单测路径不挂，避免测试触碰真实 Redis（熔断器本身 fail-open，但省去连接尝试）。
    if circuit is None and pool is None:
        from app.core.circuit_breaker import ToolCircuitBreaker
        from app.core.redis_client import get_shared_redis

        circuit = ToolCircuitBreaker(get_shared_redis())

    runtime_llm = InstrumentedLLM(llm or LLMClient())
    runtime_context = context or ContextService()
    saver = checkpointer if checkpointer is not None else await get_checkpointer()

    graph = build_graph(
        llm=runtime_llm,  # type: ignore[arg-type] - 透明代理，契约与 LLMClient 一致
        pool=runtime_pool,
        context=runtime_context,
        tools=runtime_pool.tools,
        checkpointer=saver,
        approval_creator=make_idempotent_approval_creator(factory),
        approval_updater=make_approval_updater(factory),
        force_local_review=force_local_review,
        circuit=circuit,
    )
    logger.info(
        "w3_runtime_ready",
        extra={
            "tools": len(runtime_pool.tools),
            "checkpointer": type(saver).__name__,
            "unavailable_servers": list(runtime_pool.unavailable),
        },
    )
    return AgentRuntime(
        graph=graph,
        pool=runtime_pool,
        llm=runtime_llm,  # type: ignore[arg-type]
        context=runtime_context,
        tools=runtime_pool.tools,
        checkpointer=saver,
    )


async def get_w3_runtime() -> AgentRuntime:
    """进程内单例（4 个 MCP 子进程只起一次）。"""
    global _runtime
    if _runtime is not None:
        return _runtime
    async with _runtime_lock:
        if _runtime is None:
            _runtime = await create_w3_runtime()
        return _runtime


async def reset_w3_runtime() -> None:
    global _runtime
    if _runtime is None:
        return
    await _runtime.aclose()
    _runtime = None


def tool_defs(runtime: AgentRuntime) -> list[ToolDef]:
    return list(runtime.tools or [])


__all__ = [
    "create_w3_runtime",
    "get_w3_runtime",
    "reset_w3_runtime",
    "make_idempotent_approval_creator",
    "make_approval_updater",
]
