"""Reviewer 节点（PRD FR-013）：5 维评分质量闸门。

评分维度：结论有据性 / 引用忠实度 / 数字自洽性 / 完整度 / 格式，各 1~5 分，总分 <3.5 触发 Replan。

两条路径（由 ``MOCK_EXTERNAL_EVAL`` 分流）：
- ``true``：**本地规则评分**（不发起任何外部调用，可离线复现）；
- ``false``：先调项目二 eval 接口算 faithfulness，再由 LLM 产出结构化 ``ReviewOutput``；
  接口失败降级本地规则（``degraded=true``），连续 2 次输出不合规置 ``REVIEW_SKIPPED`` 放行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agents.common import analysis_subtasks, ledger, node_error, plan_of, results_of
from app.agents.llm import LLMClient
from app.agents.prompts import build_reviewer_messages
from app.agents.structured import StructuredOutputError, complete_structured
from app.core.config import settings
from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.agent_io import EvidenceRef, ReviewInput
from app.schemas.graph_state import PlanOutput, ReviewOutput, ReviewScores, SubtaskPlan, SubtaskResult, TaskState
from app.services.context_service import ContextService
from app.services.eval_gateway import EvalGateway

logger = get_logger(__name__)

AGENT_NAME = "reviewer"


@dataclass(slots=True)
class ReviewRun:
    output: ReviewOutput
    degraded: bool = False
    skipped: bool = False
    skip_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    source: str = "local"
    context_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return self.output.scores.total()

    @property
    def passed(self) -> bool:
        return self.skipped or self.total >= w2.REVIEW_PASS_THRESHOLD


def _clamp(score: float) -> int:
    return max(1, min(5, int(round(score))))


def _count_numbers(text: str) -> int:
    import re

    return len(re.findall(r"-?\d+(?:\.\d+)?", text or ""))


#: 评分来源对可信度的折损系数（外部评测 > LLM 直评 > 本地规则 > 跳过放行）
_SOURCE_WEIGHT: dict[str, float] = {
    "external": 1.0,
    "local": 0.82,
    "local_fallback": 0.72,
    "skipped": 0.4,
}


def evidence_ratio(plan: list[SubtaskPlan], results: dict[str, SubtaskResult]) -> float:
    """有实质证据的分析型子任务占比（0~1）；无分析子任务时按 1 处理。"""
    analyzed = analysis_subtasks(plan)
    if not analyzed:
        return 1.0
    with_evidence = sum(
        1
        for item in analyzed
        if (results.get(str(item.seq)) and len(results[str(item.seq)].data_digest) > 20)
    )
    return round(with_evidence / len(analyzed), 4)


def compute_confidence(
    scores: ReviewScores,
    *,
    evidence_coverage: float,
    source: str,
    degraded: bool = False,
    skipped: bool = False,
) -> tuple[float, str]:
    """计算结果可信度（0~1）与构成说明。

    与 ``total`` 的区别：总分是"写得好不好"，可信度是"结论敢不敢用"。因此除分数外
    还看**证据覆盖率**（多少个子任务真的拿到了数据）、**评分来源**（外部评测比本地规则可信）、
    **是否降级/跳过**。纯函数，便于单测与回归。
    """
    score_part = max(0.0, min(1.0, scores.total() / 5.0))
    coverage_part = max(0.0, min(1.0, float(evidence_coverage)))
    weight = _SOURCE_WEIGHT.get(source, 0.8)
    raw = (0.55 * score_part + 0.45 * coverage_part) * weight
    if degraded:
        raw *= 0.9
    if skipped:
        raw = min(raw, 0.4)
    confidence = round(max(0.05, min(0.99, raw)), 2)
    reason = (
        f"评分 {scores.total():.2f}/5（权重0.55）× 证据覆盖 {coverage_part:.0%}（权重0.45）"
        f" × 来源系数 {weight}"
        + ("；本地降级 ×0.9" if degraded else "")
        + ("；连续不合规放行上限 0.4" if skipped else "")
    )
    return confidence, reason


def decide_verdict(total: float, confidence: float, *, passed: bool, skipped: bool = False) -> str:
    """三态判定：PASS 可交付 / REVISE 需重规划 / REJECT 硬性拒绝。

    - 总分低于 ``REVIEW_REJECT_THRESHOLD`` → REJECT（连重规划都不值得，直接降级交付并标注）；
    - 未达通过线，或通过但可信度低于 ``REVIEW_CONFIDENCE_FLOOR`` → REVISE（走重规划）；
    - 其余 → PASS。
    """
    if skipped:
        return "PASS"  # 放行策略：跳过评审不阻塞任务，但可信度已被上限压到 0.4
    if total < w2.REVIEW_REJECT_THRESHOLD:
        return "REJECT"
    if not passed:
        return "REVISE"
    if confidence < w2.REVIEW_CONFIDENCE_FLOOR:
        return "REVISE"
    return "PASS"


def local_review(
    plan: list[SubtaskPlan],
    results: dict[str, SubtaskResult],
    *,
    report_digest: str = "",
    source: str = "local",
) -> ReviewOutput:
    """本地规则评分（可复现、可单测；不引入外部知识）。"""
    analyzed = analysis_subtasks(plan)
    total = max(len(analyzed), 1)
    done = sum(1 for item in analyzed if (results.get(str(item.seq)) and results[str(item.seq)].status == "DONE"))
    with_evidence = sum(
        1
        for item in analyzed
        if (results.get(str(item.seq)) and len(results[str(item.seq)].data_digest) > 20)
    )

    # 完整度：按完成的非 notify 子任务比例
    completeness = _clamp(1 + 4 * done / total)

    # 结论有据性：有实质 data_digest 的子任务比例
    evidence = _clamp(1 + 4 * with_evidence / total)

    # 引用忠实度：计划里要求 kb_search 时，看是否真的拿到未降级片段
    kb_tasks = [item for item in plan if item.tool_name == "kb-search"]
    if not kb_tasks:
        faithfulness = 4
    else:
        kb_ok = any(
            (results.get(str(item.seq)) and results[str(item.seq)].status == "DONE"
             and not results[str(item.seq)].degraded
             and "doc_id" in results[str(item.seq)].data_digest)
            for item in kb_tasks
        )
        faithfulness = 5 if kb_ok else 2

    # 数字自洽性：报告与证据中都出现数字才给高分
    digest_numbers = _count_numbers(report_digest)
    evidence_text = "\n".join(r.data_digest for r in results.values())
    consistency = 4 if digest_numbers and _count_numbers(evidence_text) else (3 if digest_numbers else 2)

    # 格式：有结构化痕迹（列/行数/JSON/【】标题）给 4 分
    format_score = 4 if any(token in evidence_text for token in ("列:", "{", "【")) else 3

    scores = ReviewScores(
        evidence=evidence,
        faithfulness=faithfulness,
        consistency=consistency,
        completeness=completeness,
        format=format_score,
    )
    total_score = scores.total()
    suggestions: list[dict[str, Any]] = []
    if completeness < 4:
        missing = [item.seq for item in analyzed if results.get(str(item.seq)) is None or results[str(item.seq)].status != "DONE"]
        suggestions.append({"seq": missing[0] if missing else 1, "advice": "补全未完成子任务后重新汇总"})
    if faithfulness < 3:
        suggestions.append({"seq": kb_tasks[0].seq if kb_tasks else 1, "advice": "历史周报引用缺失，需补充检索证据"})

    confidence, reason = compute_confidence(
        scores,
        evidence_coverage=evidence_ratio(plan, results),
        source=source,
        degraded=True,
    )
    passed = total_score >= w2.REVIEW_PASS_THRESHOLD
    return ReviewOutput(
        scores=scores,
        total=total_score,
        pass_=passed,
        suggestions=suggestions,
        degraded=True,
        skipped=False,
        confidence=confidence,
        confidence_reason=reason,
        verdict=decide_verdict(total_score, confidence, passed=passed),
    )


def collect_evidence(results: dict[str, SubtaskResult]) -> list[EvidenceRef]:
    evidence: list[EvidenceRef] = []
    for seq, result in sorted(results.items(), key=lambda kv: int(kv[0])):
        kind = "digest"
        tool = (result.tool or "").replace("-", "_")
        if tool in {"sql_query", "kb_search", "chart_render", "notify"}:
            kind = {"sql_query": "sql", "kb_search": "kb", "chart_render": "chart", "notify": "notify"}[tool]
        ref = ""
        if result.artifacts:
            ref = str(result.artifacts[0].ref or result.artifacts[0].url or "")
        evidence.append(
            EvidenceRef(
                kind=kind,  # type: ignore[arg-type]
                ref=ref or f"seq-{seq}",
                excerpt=result.data_digest[:400],
                subtask_seq=int(seq),
            )
        )
    return evidence


async def review_deliverable(
    llm: LLMClient,
    context: ContextService,
    payload: ReviewInput,
    *,
    plan: list[SubtaskPlan],
    results: dict[str, SubtaskResult],
    gateway: EvalGateway | None = None,
    force_local: bool | None = None,
) -> ReviewRun:
    use_local = settings.MOCK_EXTERNAL_EVAL if force_local is None else force_local
    build = context.plan_context(
        "reviewer",
        {
            "system": "reviewer-system",
            "report": payload.report_digest,
            "evidence": json.dumps([e.model_dump() for e in payload.evidence], ensure_ascii=False),
        },
        reserved_output=500,
    )
    meta = {
        "node": "reviewer",
        "budget": build.budget,
        "estimated": build.estimate.total,
        "compressed": build.compressed,
    }

    if use_local:
        output = local_review(plan, results, report_digest=payload.report_digest)
        return ReviewRun(output=output, degraded=True, source="local", context_meta=meta)

    external_score: float | None = None
    degraded = False
    if gateway is not None:
        score = await gateway.faithfulness(payload.report_digest, payload.evidence)
        external_score = score.faithfulness
        degraded = score.degraded

    if external_score is None:
        # 外部评测不可用 → 本地规则降级（FR-013 验收 2）
        output = local_review(plan, results, report_digest=payload.report_digest, source="local_fallback")
        return ReviewRun(output=output, degraded=True, source="local_fallback", context_meta=meta)

    messages = build_reviewer_messages(payload)
    try:
        result = await complete_structured(
            llm, messages, ReviewOutput, temperature=0.0, max_tokens=1200, use_fallback_once=True
        )
    except StructuredOutputError as exc:
        raise ReviewError("REVIEW_INVALID", str(exc), raw=exc.raw) from exc

    output: ReviewOutput = result.value
    output.scores.faithfulness = _clamp(external_score)
    output.total = output.scores.total()
    output.pass_ = output.total >= w2.REVIEW_PASS_THRESHOLD
    output.degraded = degraded
    # 可信度由服务端统一计算，不采信 LLM 自报值（避免"自评满分"）
    output.confidence, output.confidence_reason = compute_confidence(
        output.scores,
        evidence_coverage=evidence_ratio(plan, results),
        source="external",
        degraded=degraded,
    )
    output.verdict = decide_verdict(output.total, output.confidence, passed=output.pass_)

    return ReviewRun(
        output=output,
        degraded=degraded,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        source="external",
        context_meta=meta,
    )


class ReviewError(RuntimeError):
    def __init__(self, code: str, message: str, *, raw: str = "") -> None:
        self.code = code
        self.raw = raw
        super().__init__(message)


def make_reviewer_node(
    llm: LLMClient,
    context: ContextService,
    *,
    gateway: EvalGateway | None = None,
    force_local: bool | None = None,
) -> Callable[[TaskState], Any]:
    async def reviewer_node(state: TaskState) -> dict[str, Any]:
        plan = plan_of(state)
        results = results_of(state)
        report_digest = str(state.get("context_digest") or "") or "\n".join(
            f"#{seq}: {r.data_digest}" for seq, r in results.items()
        )
        payload = ReviewInput(
            report_digest=report_digest[:12000] or "（无产物）",
            evidence=collect_evidence(results),
            plan=PlanOutput(subtasks=plan),
            task_goal=str(state.get("goal") or ""),
        )

        failures = int(state.get("review_failures") or 0)
        try:
            run = await review_deliverable(
                llm,
                context,
                payload,
                plan=plan,
                results=results,
                gateway=gateway,
                force_local=force_local,
            )
        except ReviewError as exc:
            failures += 1
            if failures >= w2.REVIEW_SKIP_AFTER_FAILURES:
                # 宁放行不阻塞，事后进 BadCase 队列（FR-013 验收 3）
                logger.error("review_skipped", extra={"failures": failures, "error": str(exc)[:300]})
                skipped_scores = ReviewScores(evidence=3, faithfulness=3, consistency=3, completeness=3, format=3)
                skip_confidence, skip_reason = compute_confidence(
                    skipped_scores,
                    evidence_coverage=evidence_ratio(plan, results),
                    source="skipped",
                    degraded=True,
                    skipped=True,
                )
                skipped = ReviewOutput(
                    scores=skipped_scores,
                    total=3.5,
                    pass_=True,
                    suggestions=[],
                    degraded=True,
                    skipped=True,
                    skip_reason="REVIEW_SKIPPED：Reviewer 连续输出不合规，按降级策略放行",
                    confidence=skip_confidence,
                    confidence_reason=skip_reason,
                    verdict="PASS",
                )
                return {
                    "review": skipped.model_dump(mode="json"),
                    "eval_score": {
                        "total": 3.5,
                        "dims": skipped.scores.model_dump(),
                        "skipped": True,
                        "confidence": skip_confidence,
                        "verdict": "PASS",
                    },
                    "review_failures": failures,
                    "messages": [{"role": "assistant", "content": f"评审连续失败，已标记 REVIEW_SKIPPED 放行（可信度 {skip_confidence:.0%}）"}],
                }
            fallback_review = local_review(plan, results, report_digest=report_digest)
            return {
                "review_failures": failures,
                "messages": [
                    {"role": "assistant", "content": f"评审输出不合规（{failures} 次），本次走本地降级（可信度 {fallback_review.confidence:.0%}）"}
                ],
                "review": fallback_review.model_dump(mode="json"),
                "eval_score": {
                    "total": fallback_review.scores.total(),
                    "dims": fallback_review.scores.model_dump(),
                    "confidence": fallback_review.confidence,
                    "verdict": fallback_review.verdict,
                    "source": "local_fallback",
                },
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("reviewer_crashed")
            return {
                "error": node_error("REVIEW_ERROR", str(exc)[:300], AGENT_NAME),
                "status": "FAILED",
            }

        logger.info(
            "review_done",
            extra={
                "total": run.total,
                "passed": run.passed,
                "source": run.source,
                "degraded": run.degraded,
                "confidence": run.output.confidence,
                "verdict": run.output.verdict,
            },
        )
        return {
            "review": run.output.model_dump(mode="json"),
            "eval_score": {
                "total": run.total,
                "dims": run.output.scores.model_dump(),
                "pass": run.passed,
                "source": run.source,
                "degraded": run.degraded,
                "confidence": run.output.confidence,
                "verdict": run.output.verdict,
            },
            "review_failures": 0,
            "token_ledger": ledger(AGENT_NAME, run.input_tokens, run.output_tokens),
            "context_meta": run.context_meta,
            "messages": [
                {
                    "role": "assistant",
                    "content": (
                        f"评审完成：总分 {run.total}（{'通过' if run.passed else '未通过'}），"
                        f"可信度 {run.output.confidence:.0%}，判定 {run.output.verdict}"
                    ),
                }
            ],
            "status": "SUMMARIZING",
        }

    reviewer_node.__name__ = "reviewer_node"
    return reviewer_node


__all__ = [
    "AGENT_NAME",
    "ReviewRun",
    "ReviewError",
    "local_review",
    "collect_evidence",
    "review_deliverable",
    "make_reviewer_node",
    "compute_confidence",
    "decide_verdict",
    "evidence_ratio",
]
