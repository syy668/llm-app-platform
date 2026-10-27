"""StateGraph 装配（PRD 5.2 节点清单 + 边表）。

拓扑（与原 PRD 完全一致）：

    START → planner ─┬→ human_gate ─┬→ executor → reviewer ─┬→ summarizer ─┬→ notifier → END
                     │              └→ replan ──┐            └→ replan ─────┘   └→ END
                     └→ executor                 └→ executor

条件路由：
- ``needs_approval(plan)``：含高危子任务 → human_gate，否则直入 executor；
- human_gate：APPROVED → executor；REJECTED → replan（携带驳回意见）；
- reviewer：总分 ≥3.5 或 REVIEW_SKIPPED → summarizer；否则 replan_count<2 → replan；
- summarizer：plan 含 notify 子任务 → notifier，否则 END；
- 任意节点置 status=FAILED → END（由 TaskService 落终态）。

checkpointer：默认 RedisSaver（TTL 72h），Redis 不可用时自动降级 MemorySaver（见 checkpoint_service）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.common import needs_approval, notify_subtask, plan_of
from app.agents.context_steps import make_context_compress_node, make_memory_recall_node
from app.agents.executor import Executor, make_executor_node
from app.agents.human_gate import ApprovalCreator, ApprovalUpdater, make_human_gate_node
from app.agents.llm import LLMClient
from app.agents.notifier import make_notifier_node
from app.agents.planner import make_planner_node
from app.agents.replan import make_replan_node
from app.agents.review_gate import make_review_gate_node, route_after_review_gate
from app.agents.reviewer import make_reviewer_node
from app.agents.summarizer import make_summarizer_node
from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.agent_io import ToolDef
from app.schemas.graph_state import TaskState
from app.services.context_service import ContextService
from app.services.eval_gateway import EvalGateway
from app.tools.mcp_client import MCPClientPool

logger = get_logger(__name__)

NODE_NAMES = (
    "planner",
    "human_gate",
    "executor",
    "reviewer",
    "review_gate",
    "replan",
    "summarizer",
    "notifier",
    "memory_recall",
    "context_compress",
)


# ---------------------------------------------------------------------------
# 条件路由（纯函数，便于单测）
# ---------------------------------------------------------------------------
def route_from_start(state: TaskState) -> str:
    """START 之后：use_memory=True 先经 memory_recall 召回记忆，否则直入 planner。"""
    if state.get("status") == "FAILED" or state.get("error"):
        return END
    return "memory_recall" if state.get("use_memory") else "planner"


def route_after_planner(state: TaskState) -> str:
    if state.get("status") == "FAILED" or state.get("error"):
        return END
    # G8：compress=True 先经 context_compress 压缩上下文，再进入后续分流
    if state.get("compress"):
        return "context_compress"
    return "human_gate" if needs_approval(plan_of(state)) else "executor"


def route_after_compress(state: TaskState) -> str:
    """context_compress 之后：与 route_after_planner 的 human_gate 分流一致（不再判断 compress）。"""
    if state.get("status") == "FAILED" or state.get("error"):
        return END
    return "human_gate" if needs_approval(plan_of(state)) else "executor"


def route_after_gate(state: TaskState) -> str:
    if state.get("status") == "FAILED" or state.get("error"):
        return END
    approval = state.get("approvals") or {}
    if approval.get("status") == "REJECTED":
        return "replan"
    return "executor"


def route_after_executor(state: TaskState) -> str:
    return END if state.get("status") == "FAILED" or state.get("error") else "reviewer"


def route_after_review(state: TaskState) -> str:
    """评审后分流：PASS → 汇总；REVISE / REJECT → 拒绝重规划分支。

    ``verdict`` 是 Reviewer 服务端判定的三态结论（见 ``agents/reviewer.py::decide_verdict``）：
    - ``PASS``：可交付，直接进 summarizer；
    - ``REVISE``：未达通过线，或分数达标但可信度低于下限 → 带着 suggestions 回 replan；
    - ``REJECT``：总分低于拒绝线 → 有预算也回 replan 做最后一次补救，预算用尽则降级交付。
    无 ``verdict`` 的历史状态（老 checkpoint）仍按总分阈值判断，保持向后兼容。
    """
    if state.get("status") == "FAILED" or state.get("error"):
        return END
    review = state.get("review") or {}
    total = float(review.get("total") or 0.0)
    passed = bool(review.get("pass") or review.get("pass_")) or bool(review.get("skipped"))
    verdict = str(review.get("verdict") or "").upper()
    if verdict == "PASS":
        return "summarizer"
    if not verdict and (passed or total >= w2.REVIEW_PASS_THRESHOLD):
        return "summarizer"
    if int(state.get("replan_count") or 0) < w2.REPLAN_MAX_COUNT:
        return "replan"
    # replan 用尽 → 降级交付（仍在报告中标注）
    return "summarizer"


def route_after_summary(state: TaskState) -> str:
    if state.get("status") == "FAILED" or state.get("error"):
        return END
    return "notifier" if notify_subtask(plan_of(state)) else END


# ---------------------------------------------------------------------------
# 挂起探测
# ---------------------------------------------------------------------------
async def pending_interrupts(graph: Any, config: dict[str, Any] | None) -> list[Any]:
    """读取处于挂起状态的 interrupt。

    为什么需要这个函数：``ainvoke`` 的返回值里**不含** ``__interrupt__``
    （该 key 只出现在 ``astream(stream_mode="updates")`` 的分片里），
    因此"是否停在 human_gate"必须查状态快照，否则任务会被误判成普通终态。
    """
    if config is None or not hasattr(graph, "aget_state"):
        return []
    try:
        snapshot = await graph.aget_state(config)
    except Exception:  # noqa: BLE001 - 探测失败不改变主流程结论
        logger.warning("pending_interrupt_probe_failed")
        return []
    items: list[Any] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        items.extend(getattr(task, "interrupts", ()) or ())
    return items


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
def build_graph(
    *,
    llm: LLMClient,
    pool: MCPClientPool,
    context: ContextService,
    tools: list[ToolDef] | None = None,
    gateway: EvalGateway | None = None,
    checkpointer: Any = None,
    approval_creator: ApprovalCreator | None = None,
    approval_updater: ApprovalUpdater | None = None,
    force_local_review: bool | None = None,
    review_intervention: bool | None = None,
    circuit: Any = None,
) -> Any:
    tool_defs = tools if tools is not None else pool.tools
    executor = Executor(pool, llm, context, tools=tool_defs, circuit=circuit)
    # v1.1：REJECT 人工批注闸门。默认读配置；关闭时 reviewer 的边直连 replan，
    # 与 v1.1 之前完全一致（便于一键回退）。
    intervention_on = (
        bool(w2.REVIEW_HUMAN_INTERVENTION_ENABLED) if review_intervention is None else bool(review_intervention)
    )

    builder = StateGraph(TaskState)
    builder.add_node("planner", make_planner_node(llm, context, tools=tool_defs))
    builder.add_node(
        "human_gate", make_human_gate_node(creator=approval_creator, updater=approval_updater)
    )
    builder.add_node("executor", make_executor_node(executor))
    builder.add_node(
        "reviewer",
        make_reviewer_node(llm, context, gateway=gateway, force_local=force_local_review),
    )
    builder.add_node("replan", make_replan_node(llm, context, tools=tool_defs))
    builder.add_node("review_gate", make_review_gate_node(enabled=intervention_on))
    builder.add_node("summarizer", make_summarizer_node(llm, context))
    builder.add_node("notifier", make_notifier_node(pool, context))
    # G8：可选步骤节点（始终注册，条件边决定是否经过；默认 false 直连，不破坏默认链路）
    builder.add_node("memory_recall", make_memory_recall_node(context))
    builder.add_node("context_compress", make_context_compress_node(context))

    builder.add_conditional_edges(
        START, route_from_start, {"memory_recall": "memory_recall", "planner": "planner", END: END}
    )
    builder.add_edge("memory_recall", "planner")
    builder.add_conditional_edges(
        "planner",
        route_after_planner,
        {"human_gate": "human_gate", "executor": "executor", "context_compress": "context_compress", END: END},
    )
    builder.add_conditional_edges(
        "context_compress", route_after_compress, {"human_gate": "human_gate", "executor": "executor", END: END}
    )
    builder.add_conditional_edges(
        "human_gate", route_after_gate, {"executor": "executor", "replan": "replan", END: END}
    )
    builder.add_conditional_edges("executor", route_after_executor, {"reviewer": "reviewer", END: END})
    # reviewer 的判定函数（route_after_review）零改动；只把它的 "replan" 出边串到 review_gate：
    # 这样 REJECT 时能挂起等人工批注，而 PASS 仍直接进 summarizer、预算用尽仍降级交付。
    builder.add_conditional_edges(
        "reviewer",
        route_after_review,
        {
            "summarizer": "summarizer",
            "replan": "review_gate" if intervention_on else "replan",
            END: END,
        },
    )
    if intervention_on:
        builder.add_conditional_edges(
            "review_gate",
            route_after_review_gate,
            {"replan": "replan", "summarizer": "summarizer"},
        )
    builder.add_edge("replan", "executor")
    builder.add_conditional_edges(
        "summarizer", route_after_summary, {"notifier": "notifier", END: END}
    )
    builder.add_edge("notifier", END)

    graph = builder.compile(checkpointer=checkpointer)
    logger.info(
        "graph_compiled",
        extra={"nodes": list(NODE_NAMES), "checkpointer": type(checkpointer).__name__ if checkpointer else None},
    )
    return graph


@dataclass
class AgentRuntime:
    """一次进程生命周期内复用的运行时（graph + MCP 连接池 + LLM）。"""

    graph: Any
    pool: MCPClientPool
    llm: LLMClient
    context: ContextService
    tools: list[ToolDef] = field(default_factory=list)
    checkpointer: Any = None

    async def aclose(self) -> None:
        await self.pool.aclose()
        closer = getattr(self.checkpointer, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:  # noqa: BLE001
                logger.warning("checkpointer_close_failed")


async def create_runtime(
    *,
    pool: MCPClientPool | None = None,
    llm: LLMClient | None = None,
    context: ContextService | None = None,
    gateway: EvalGateway | None = None,
    checkpointer: Any = None,
    approval_creator: ApprovalCreator | None = None,
    approval_updater: ApprovalUpdater | None = None,
    force_local_review: bool | None = None,
    review_intervention: bool | None = None,
    discover_tools: bool = True,
) -> AgentRuntime:
    """生产/测试通用入口：未传入的依赖按默认策略构建。"""
    from app.services.checkpoint_service import get_checkpointer

    if pool is None:
        # 任务 B：改为进程级共享池单例——此前这里与 w3_runtime 各自 create_default，
        # 8 个 MCP 子进程并存、熔断/重启状态分裂。共享池内部已挂 mcp_event_hook
        # （非流式入口 TaskService.run 依赖它把 tool.call/tool.result 写进 task_executions），
        # 工具轨迹不因单例化而丢失；显式传 pool 的测试路径行为完全不变。
        from app.tools.mcp_client import get_shared_pool

        runtime_pool = await get_shared_pool()
    else:
        runtime_pool = pool
    if discover_tools and not runtime_pool.tools:
        await runtime_pool.discover()
    runtime_llm = llm or LLMClient()
    runtime_context = context or ContextService()
    saver = checkpointer if checkpointer is not None else await get_checkpointer()
    # 工具注册表（v1.1）：被 ADMIN 禁用的工具不再进入 Planner 的可用工具集。
    # 过滤函数内部对任何异常都退回全量，注册表故障绝不阻塞任务装配；
    # 连接池本身不摘除工具进程，已挂起任务的断点续跑不受影响。
    from app.services.tool_registry import filter_enabled_tools

    enabled_tools = await filter_enabled_tools(runtime_pool.tools)
    graph = build_graph(
        llm=runtime_llm,
        pool=runtime_pool,
        context=runtime_context,
        tools=enabled_tools,
        gateway=gateway,
        checkpointer=saver,
        approval_creator=approval_creator,
        approval_updater=approval_updater,
        force_local_review=force_local_review,
        review_intervention=review_intervention,
    )
    return AgentRuntime(
        graph=graph,
        pool=runtime_pool,
        llm=runtime_llm,
        context=runtime_context,
        tools=runtime_pool.tools,
        checkpointer=saver,
    )


__all__ = [
    "NODE_NAMES",
    "route_from_start",
    "route_after_planner",
    "route_after_compress",
    "route_after_gate",
    "route_after_executor",
    "route_after_review",
    "route_after_summary",
    "pending_interrupts",
    "build_graph",
    "AgentRuntime",
    "create_runtime",
]
