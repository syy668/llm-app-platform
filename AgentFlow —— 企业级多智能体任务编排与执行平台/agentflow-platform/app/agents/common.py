"""节点公共工具：State 读取容错、错误结构、Token 记账、风险判定。

为什么需要"容错读取"：State 经 checkpointer 落 Redis 再取回时，Pydantic 模型可能被
反序列化为 dict（取决于序列化器），节点必须两种形态都能处理，否则断点恢复必炸。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.schemas.enums import SubtaskRiskLevel
from app.schemas.graph_state import (
    ArtifactRef,
    SubtaskPlan,
    SubtaskResult,
    TaskState,
    TimeRange,
)


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def as_time_range(value: TimeRange | dict[str, Any] | None) -> TimeRange:
    if isinstance(value, TimeRange):
        return value
    if isinstance(value, dict) and value.get("start") and value.get("end"):
        return TimeRange(start=value["start"], end=value["end"])
    today = date.today()
    return TimeRange(start=today, end=today)


def as_subtask(value: SubtaskPlan | dict[str, Any]) -> SubtaskPlan:
    return value if isinstance(value, SubtaskPlan) else SubtaskPlan.model_validate(value)


def plan_of(state: TaskState) -> list[SubtaskPlan]:
    return [as_subtask(item) for item in state.get("plan") or []]


def as_result(value: SubtaskResult | dict[str, Any]) -> SubtaskResult:
    return value if isinstance(value, SubtaskResult) else SubtaskResult.model_validate(value)


def results_of(state: TaskState) -> dict[str, SubtaskResult]:
    raw = state.get("subtask_results") or {}
    return {str(seq): as_result(value) for seq, value in raw.items()}


def done_seqs(results: dict[str, SubtaskResult]) -> set[int]:
    return {int(seq) for seq, result in results.items() if result.status == "DONE"}


def failed_seqs(results: dict[str, SubtaskResult]) -> set[int]:
    return {int(seq) for seq, result in results.items() if result.status == "FAILED"}


def artifacts_of(state: TaskState) -> list[ArtifactRef]:
    return [
        item if isinstance(item, ArtifactRef) else ArtifactRef.model_validate(item)
        for item in state.get("artifacts") or []
    ]


# ---------------------------------------------------------------------------
# 错误 / 记账
# ---------------------------------------------------------------------------
def node_error(code: str, message: str, node: str, span_id: str | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "node": node, "span_id": span_id}


def ledger(node: str, input_tokens: int, output_tokens: int, **extra: Any) -> dict[str, dict[str, Any]]:
    entry: dict[str, Any] = {"input": int(input_tokens), "output": int(output_tokens), "calls": 1}
    entry.update({k: v for k, v in extra.items() if v is not None})
    return {node: entry}


# ---------------------------------------------------------------------------
# 风险与路由判定
# ---------------------------------------------------------------------------
def is_high_risk(subtask: SubtaskPlan) -> bool:
    return (
        str(subtask.risk_level) == SubtaskRiskLevel.HIGH.value
        or subtask.risk_level == SubtaskRiskLevel.HIGH
        or subtask.tool_name == "notify"
    )


def needs_approval(plan: list[SubtaskPlan]) -> bool:
    """PRD 5.2 边：plan 含高危子任务 -> human_gate。"""
    return any(is_high_risk(item) for item in plan)


def notify_subtask(plan: list[SubtaskPlan]) -> SubtaskPlan | None:
    for item in plan:
        if item.tool_name == "notify":
            return item
    return None


def high_risk_subtasks(plan: list[SubtaskPlan]) -> list[SubtaskPlan]:
    return [item for item in plan if is_high_risk(item)]


def analysis_subtasks(plan: list[SubtaskPlan]) -> list[SubtaskPlan]:
    """非 notify 子任务（reviewer 完整度分母，notify 属"动作"而非"分析"）。"""
    return [item for item in plan if item.tool_name != "notify"]


def total_tokens(state: TaskState) -> int:
    ledger_data = state.get("token_ledger") or {}
    return sum(int(v.get("input", 0)) + int(v.get("output", 0)) for v in ledger_data.values())


__all__ = [
    "as_time_range",
    "as_subtask",
    "plan_of",
    "as_result",
    "results_of",
    "done_seqs",
    "failed_seqs",
    "artifacts_of",
    "node_error",
    "ledger",
    "is_high_risk",
    "needs_approval",
    "notify_subtask",
    "high_risk_subtasks",
    "analysis_subtasks",
    "total_tokens",
]
