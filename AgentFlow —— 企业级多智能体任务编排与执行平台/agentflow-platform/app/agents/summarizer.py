"""summarizer 节点（PRD 5.2）：把子任务产物汇总为 markdown 报告。

- 输入：全部子任务结果 + 图表产物 + 降级说明；
- 输出：``SummaryOutput``（markdown / 引用图表 / 引用文档 / degraded），并写入 ``artifacts``；
- 结构化校验失败按 PRD 4.2 处理：修复 2 次 + 备用模型 1 次仍失败 → 节点 FAILED（不静默出稿）。
"""

from __future__ import annotations

from typing import Any, Callable

from app.agents.common import (
    artifacts_of,
    as_time_range,
    ledger,
    node_error,
    notify_subtask,
    plan_of,
    results_of,
)
from app.agents.llm import LLMClient
from app.agents.prompts import build_summarizer_messages
from app.agents.structured import StructuredOutputError, complete_structured
from app.core.logging import get_logger
from app.schemas.agent_io import SummaryInput, SummaryOutput
from app.schemas.graph_state import ArtifactRef, TaskState
from app.services.context_service import ContextService

logger = get_logger(__name__)

AGENT_NAME = "summarizer"


def _degraded_notes(state: TaskState) -> list[str]:
    notes: list[str] = []
    results = results_of(state)
    if any(r.degraded for r in results.values()):
        notes.append("部分子任务执行降级（重试/缩窗/SQL 改写），结论置信度下降")
    if any("引用缺失" in r.data_digest for r in results.values()):
        notes.append("历史周报引用缺失：知识库不可达，已按降级策略继续")
    if state.get("review") and (state["review"] or {}).get("degraded"):
        notes.append("评审使用本地规则降级评分")
    if int(state.get("replan_count") or 0) > 0:
        notes.append(f"本报告经过 {state.get('replan_count')} 次重规划")
    return notes


def make_summarizer_node(llm: LLMClient, context: ContextService) -> Callable[[TaskState], Any]:
    async def summarizer_node(state: TaskState) -> dict[str, Any]:
        plan = plan_of(state)
        results = results_of(state)
        artifacts = artifacts_of(state)

        payload = SummaryInput(
            goal=str(state.get("goal") or ""),
            time_range=as_time_range(state.get("time_range")),
            results=[r for r in results.values() if r.status == "DONE"],
            artifacts=artifacts,
            plan=None,
            degraded_notes=_degraded_notes(state),
        )

        build = context.plan_context(
            "summarizer",
            {
                "system": "summarizer-system",
                "goal": payload.goal,
                "results": "\n".join(f"#{r.subtask_seq}: {r.data_digest[:800]}" for r in payload.results),
                "artifacts": "\n".join(f"{a.type}:{a.ref or a.url}" for a in artifacts),
            },
            reserved_output=1000,
        )

        messages = build_summarizer_messages(payload)
        try:
            result = await complete_structured(
                llm, messages, SummaryOutput, temperature=0.3, max_tokens=2400, use_fallback_once=True
            )
        except StructuredOutputError as exc:
            logger.error("summarizer_invalid", extra={"error": str(exc)[:300]})
            return {
                "error": {"code": "SUMMARY_INVALID", "message": str(exc)[:300], "node": AGENT_NAME},
                "status": "FAILED",
                "messages": [{"role": "assistant", "content": "报告生成失败：SUMMARY_INVALID"}],
            }

        summary: SummaryOutput = result.value

        # v2.1 防幻觉拦截：模型按 Prompt 强约束主动声明 [NO_DATA] → 任务以
        # NO_DATA_FOUND 终止，不再往下走生成"看似完整"的报告——
        # 静默的编造比带错误码的失败更危险（route_after_summary 对 FAILED 直接 END）。
        if "[NO_DATA]" in (summary.markdown or ""):
            logger.warning(
                "summary_no_data",
                extra={"task_id": state.get("task_id"), "chars": len(summary.markdown or "")},
            )
            return {
                "error": node_error(
                    "NO_DATA_FOUND",
                    "参考资料不足以支撑结论（模型输出 [NO_DATA]），任务按失败终止",
                    AGENT_NAME,
                ),
                "status": "FAILED",
                "summary": summary.markdown,
                "messages": [
                    {"role": "assistant", "content": "参考资料未包含答案（模型输出 [NO_DATA]），任务以 NO_DATA_FOUND 终止"}
                ],
            }

        report_ref = ArtifactRef(
            type="report",
            ref=f"report:{state.get('task_id')}",
            title="经营分析报告",
            url=None,
        )

        logger.info(
            "summary_generated",
            extra={
                "chars": len(summary.markdown),
                "charts": len(summary.cited_charts),
                "docs": len(summary.cited_docs),
                "degraded": summary.degraded or bool(payload.degraded_notes),
            },
        )

        return {
            "summary": summary.markdown,
            "artifacts": [report_ref],
            "messages": [{"role": "assistant", "content": f"报告已生成（{len(summary.markdown)} 字）"}],
            "token_ledger": ledger(
                AGENT_NAME,
                result.input_tokens,
                result.output_tokens,
                compress_before=build.before_tokens,
                compress_after=build.after_tokens,
                compressed=build.compressed,
            ),
            "context_meta": {
                "node": "summarizer",
                "budget": build.budget,
                "estimated": build.estimate.total,
                "compressed": build.compressed,
            },
            "degraded": bool(summary.degraded or payload.degraded_notes),
            # 有 notify 子任务时后续还会进 notifier，终态由 notifier 置 COMPLETED
            "status": "SUMMARIZING" if notify_subtask(plan) else "COMPLETED",
        }

    summarizer_node.__name__ = "summarizer_node"
    return summarizer_node


__all__ = ["AGENT_NAME", "make_summarizer_node"]
