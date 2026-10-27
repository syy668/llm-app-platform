"""G8 任务级可选步骤节点：长期记忆检索（memory_recall）与上下文压缩（context_compress）。

设计要点（保护规则）：
- 两个节点由 ``build_graph`` **始终注册**，但仅在 state 中对应开关为真时才会被条件边路由到
  （``use_memory`` / ``compress`` 默认 false，走原有直连路径，默认链路零改动）；
- 节点内部再做一次开关判断作为**双保险**：即使被误路由进来，开关为假也直接透传（幂等空操作）；
- 记忆检索 / 压缩失败**绝不阻断主流程**（降级为空操作 + 记日志），与既有"观测能力不阻断业务"一致。

节点职责：
- ``memory_recall``：``use_memory=True`` 时，规划前召回长期记忆（LayeredMemoryService.recall），
  把命中转成 ``memory_hits``（list[str]），供 planner / replan 通过 ``memory_hits[:3]`` 并入 prompt；
- ``context_compress``：``compress=True`` 时，规划后把累积 messages 压缩成 ``context_digest``
  并记入 ``token_ledger`` / ``context_meta``，供后续节点复用与 A/B 统计（**附加**的显式压缩步骤，
  不影响既有 ``plan_context`` 的自动压缩行为）。
"""

from __future__ import annotations

from typing import Any, Callable

from app.agents.common import ledger
from app.core.logging import get_logger
from app.schemas.graph_state import TaskState
from app.services.context_service import ContextService

logger = get_logger(__name__)

AGENT_RECALL = "memory_recall"
AGENT_COMPRESS = "context_compress"


def make_memory_recall_node(context: ContextService) -> Callable[[TaskState], Any]:
    """构造 memory_recall 节点：开关为假直接透传；开关为真召回长期记忆。

    记忆服务在节点内懒构建（后台任务无请求 Session，用全局 session_factory），
    召回失败降级为空列表（不阻断规划）。
    """

    async def memory_recall_node(state: TaskState) -> dict[str, Any]:
        if not state.get("use_memory"):
            return {}
        goal = str(state.get("goal") or "").strip()
        if not goal:
            return {}

        try:
            from app.db.session import get_session_factory
            from app.memory.layered_memory import LayeredMemoryService

            memory = LayeredMemoryService(session_factory=get_session_factory(), context=context)
            hits = await memory.recall(goal, user_id=state.get("creator_id"))
            memory_hits = [hit.as_prompt() for hit in hits[:3]]
            logger.info(
                "memory_recalled",
                extra={"task_id": state.get("task_id"), "hits": len(memory_hits)},
            )
            return {"memory_hits": memory_hits}
        except Exception as exc:  # noqa: BLE001 - 记忆检索失败不阻断主流程
            logger.warning(
                "memory_recall_degraded",
                extra={"task_id": state.get("task_id"), "error": str(exc)[:200]},
            )
            return {"memory_hits": []}

    memory_recall_node.__name__ = "memory_recall_node"
    return memory_recall_node


def make_context_compress_node(context: ContextService) -> Callable[[TaskState], Any]:
    """构造 context_compress 节点：开关为假直接透传；开关为真做显式上下文压缩。

    压缩产物写入 ``context_digest``，记账写入 ``token_ledger[context_compress]`` 与
    ``context_meta``，供下游节点与 A/B 统计复用。失败降级为空操作。
    """

    async def context_compress_node(state: TaskState) -> dict[str, Any]:
        if not state.get("compress"):
            return {}
        messages = list(state.get("messages") or [])
        if not messages:
            return {}

        try:
            result = context.compress(messages, budget=context.budget("executor"))
            if not result.compressed:
                # 压缩无收益（历史很短）→ 保留原文，仅透传摘要
                return {
                    "context_digest": result.digest or "",
                    "token_ledger": ledger(
                        AGENT_COMPRESS,
                        0,
                        0,
                        compress_before=result.before_tokens,
                        compress_after=result.after_tokens,
                        compressed=False,
                    ),
                }
            logger.info(
                "context_compressed",
                extra={
                    "task_id": state.get("task_id"),
                    "before": result.before_tokens,
                    "after": result.after_tokens,
                    "layers": result.layers,
                },
            )
            return {
                "context_digest": result.digest or "",
                "token_ledger": ledger(
                    AGENT_COMPRESS,
                    0,
                    0,
                    compress_before=result.before_tokens,
                    compress_after=result.after_tokens,
                    compressed=True,
                ),
                "context_meta": {
                    "node": AGENT_COMPRESS,
                    "compressed": True,
                    "compress_layers": result.layers,
                    "before_tokens": result.before_tokens,
                    "after_tokens": result.after_tokens,
                    "kept_rounds": result.kept_rounds,
                },
            }
        except Exception as exc:  # noqa: BLE001 - 压缩失败不阻断主流程
            logger.warning(
                "context_compress_failed",
                extra={"task_id": state.get("task_id"), "error": str(exc)[:200]},
            )
            return {}

    context_compress_node.__name__ = "context_compress_node"
    return context_compress_node


__all__ = [
    "AGENT_RECALL",
    "AGENT_COMPRESS",
    "make_memory_recall_node",
    "make_context_compress_node",
]
