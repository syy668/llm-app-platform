"""replan 节点（PRD FR-003）：汇总失败上下文 / 驳回意见 → 受影响分支重排。

设计要点：
- **不全部重跑**：已完成（DONE）子任务结果原样保留；失败/跳过的子任务用 SKIPPED 占位，
  让 executor 重跑并覆盖（State 的 subtask_results 是合并语义，无法"删除"键）；
- **replan_count 上限 2**：达上限返回 ``REPLAN_EXHAUSTED``，任务终态 FAILED（FR-003 验收 2）；
- 复用 planner 的重规划模式（同一 LLM 调用路径、不同提示模板），避免逻辑分叉。
"""

from __future__ import annotations

from typing import Any, Callable

from app.agents.common import as_time_range, failed_seqs, ledger, results_of
from app.agents.llm import LLMClient
from app.agents.planner import PlannerError, run_planner
from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.agent_io import PlannerInput, ReplanContext, ToolDef
from app.schemas.graph_state import SubtaskResult, TaskState
from app.services.context_service import ContextService

logger = get_logger(__name__)

AGENT_NAME = "replan"


def _build_context(state: TaskState) -> ReplanContext:
    """优先级：人工驳回意见 > 评审意见 > 工具失败。"""
    results = results_of(state)
    review = state.get("review") or {}
    approval = state.get("approvals") or {}
    raw = state.get("replan_context") or {}

    cause = "tool_failed"
    review_comment = ""
    human_comment = ""

    if raw.get("cause") == "human_rejected" or approval.get("status") == "REJECTED":
        cause = "human_rejected"
        human_comment = str(raw.get("human_comment") or approval.get("comment") or "")
    elif review and not review.get("pass", True):
        cause = "review_rejected"
        suggestions = review.get("suggestions") or []
        review_comment = "; ".join(
            f"#{item.get('seq')}: {item.get('advice')}" for item in suggestions[:3] if isinstance(item, dict)
        ) or f"评审未通过：总分 {review.get('total')}"

    completed = {
        int(result.subtask_seq): result.data_digest[:300]
        for result in results.values()
        if result.status == "DONE"
    }
    return ReplanContext(
        cause=cause,  # type: ignore[arg-type]
        failed_seqs=sorted(failed_seqs(results)),
        review_comment=review_comment[:500],
        human_comment=human_comment[:500],
        completed_summary=completed,
    )


def make_replan_node(
    llm: LLMClient,
    context: ContextService,
    *,
    tools: list[ToolDef] | None = None,
) -> Callable[[TaskState], Any]:
    async def replan_node(state: TaskState) -> dict[str, Any]:
        count = int(state.get("replan_count") or 0)
        if count >= w2.REPLAN_MAX_COUNT:
            logger.error("replan_exhausted", extra={"replan_count": count})
            return {
                "error": {
                    "code": "REPLAN_EXHAUSTED",
                    "message": f"重规划次数已达上限 {w2.REPLAN_MAX_COUNT}，任务终止",
                    "node": AGENT_NAME,
                },
                "status": "FAILED",
                "messages": [{"role": "assistant", "content": "重规划次数用尽，任务失败"}],
            }

        replan_context = _build_context(state)
        payload = PlannerInput(
            goal=str(state.get("goal") or ""),
            time_range=as_time_range(state.get("time_range")),
            memory_hits=list(state.get("memory_hits") or [])[:3],
            replan_context=replan_context,
            tools=tools or [],
        )

        try:
            run = await run_planner(llm, context, payload, history=list(state.get("messages") or []))
        except PlannerError as exc:
            logger.error("replan_failed", extra={"code": exc.code})
            return {
                "error": {"code": exc.code, "message": str(exc)[:300], "node": AGENT_NAME},
                "status": "FAILED",
                "messages": [{"role": "assistant", "content": f"重规划失败：{exc.code}"}],
            }

        # 保留 DONE，其余用 SKIPPED 占位以便 executor 重跑
        existing = results_of(state)
        preserved: dict[str, SubtaskResult] = {}
        new_seqs = {item.seq for item in run.plan.subtasks}
        for seq_str, result in existing.items():
            if result.status == "DONE":
                preserved[seq_str] = result
            elif int(seq_str) in new_seqs:
                preserved[seq_str] = SubtaskResult(
                    subtask_seq=result.subtask_seq,
                    status="SKIPPED",
                    data_digest="重规划待重跑",
                    tool=result.tool,
                )

        new_count = min(count + 1, w2.REPLAN_MAX_COUNT)
        logger.info(
            "replan_done",
            extra={
                "cause": replan_context.cause,
                "replan_count": new_count,
                "subtasks": len(run.plan.subtasks),
                "preserved": len([r for r in preserved.values() if r.status == "DONE"]),
            },
        )

        return {
            "plan": run.plan.subtasks,
            "plan_assumptions": run.plan.assumptions,
            "replan_count": new_count,
            "replan_context": None,
            "subtask_results": preserved,
            "token_ledger": ledger(
                AGENT_NAME,
                run.input_tokens,
                run.output_tokens,
                compress_before=run.context.before_tokens,
                compress_after=run.context.after_tokens,
                compressed=run.context.compressed,
            ),
            "context_meta": {"node": "replan", "budget": run.context.budget, "estimated": run.context.estimate.total},
            "messages": [
                {
                    "role": "assistant",
                    "content": f"重规划完成（第 {new_count} 次，原因 {replan_context.cause}）：{len(run.plan.subtasks)} 个子任务",
                }
            ],
            "status": "RUNNING",
        }

    replan_node.__name__ = "replan_node"
    return replan_node


__all__ = ["AGENT_NAME", "make_replan_node", "_build_context"]
