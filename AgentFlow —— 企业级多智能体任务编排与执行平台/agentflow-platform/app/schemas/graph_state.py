"""LangGraph 全局 State 与基础 Agent IO（PRD 5.2 / 8.3）。

版本说明：
- W1 只定义裸结构（无 reducer、无节点）；
- W2 补齐 **reducer**（messages / subtask_results / artifacts / token_ledger 的合并策略）
  并新增 replan / review / summary 等运行期字段，供 ``agents/graph.py`` 装配使用。

Reducer 约定（决定并发节点与循环回边如何合并状态）：
- ``messages``：追加（LLM 消息流只增不减）
- ``subtask_results``：按 seq 覆盖合并（replan 后新结果覆盖旧结果，未涉及的保留）
- ``artifacts``：追加（产物天然幂等，按 id 去重由下游处理）
- ``token_ledger``：按节点深合并（同一节点多次进入时 input/output 累加）
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

from app.schemas.enums import SubtaskRiskLevel, SubtaskToolHint

# ---------------------------------------------------------------------------
# Reducer
# ---------------------------------------------------------------------------
def add_messages(left: list[dict[str, Any]] | None, right: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """messages 追加合并（LangGraph 要求 reducer 为纯函数、可交换）。"""
    return [*(left or []), *(right or [])]


def merge_dict(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """浅合并：右侧覆盖左侧同 key。用于 subtask_results。"""
    merged: dict[str, Any] = dict(left or {})
    merged.update(right or {})
    return merged


def merge_token_ledger(
    left: dict[str, "TokenLedgerEntry"] | None,
    right: dict[str, "TokenLedgerEntry"] | None,
) -> dict[str, "TokenLedgerEntry"]:
    """token_ledger 深合并：同节点累加 input/output，压缩字段取最新值。"""
    merged: dict[str, TokenLedgerEntry] = {k: dict(v) for k, v in (left or {}).items()}
    for node, entry in (right or {}).items():
        current = merged.setdefault(node, {})
        for key in ("input", "output"):
            if key in entry:
                current[key] = int(current.get(key, 0)) + int(entry[key])  # type: ignore[literal-required]
        for key in ("compress_before", "compress_after", "compressed", "calls"):
            if key in entry:
                current[key] = entry[key]  # type: ignore[literal-required]
    return merged


def add_artifacts(left: list["ArtifactRef"] | None, right: list["ArtifactRef"] | None) -> list["ArtifactRef"]:
    """产物追加，按 artifact_id 去重（None 视为不参与去重）。"""
    merged: list[ArtifactRef] = list(left or [])
    seen = {a.artifact_id for a in merged if a.artifact_id is not None}
    for item in right or []:
        if item.artifact_id is not None and item.artifact_id in seen:
            continue
        merged.append(item)
    return merged


# ---------------------------------------------------------------------------
# Agent IO（PRD 8.3）
# ---------------------------------------------------------------------------
ToolHintLiteral = Literal["sql_query", "chart_render", "kb_search", "notify", "none"]


class TimeRange(BaseModel):
    start: date
    end: date


class SubtaskPlan(BaseModel):
    """Planner 输出的单个子任务。"""

    seq: int = Field(ge=1, le=15)
    name: str = Field(max_length=64)
    description: str = Field(max_length=300)
    tool_hint: SubtaskToolHint | ToolHintLiteral = SubtaskToolHint.NONE
    depends_on: list[int] = Field(default_factory=list)
    risk_level: SubtaskRiskLevel = SubtaskRiskLevel.NORMAL
    acceptance: str = Field(default="", max_length=200)

    @property
    def tool_name(self) -> str:
        """tool_hint -> MCP 工具名（下划线转连字符，对齐 PRD 工具命名）。"""
        value = self.tool_hint.value if isinstance(self.tool_hint, SubtaskToolHint) else str(self.tool_hint)
        return value.replace("_", "-")


class PlanOutput(BaseModel):
    subtasks: list[SubtaskPlan] = Field(default_factory=list, min_length=0, max_length=15)
    assumptions: list[str] = Field(default_factory=list, max_length=5)
    replan_count: int = Field(default=0, ge=0, le=2)


class ArtifactRef(BaseModel):
    artifact_id: int | None = None
    type: Literal["chart", "report", "table"]
    url: str | None = None
    title: str | None = None
    ref: str | None = Field(default=None, description="chart_id / 文件路径等外部引用")


class SubtaskResult(BaseModel):
    subtask_seq: int
    status: Literal["DONE", "FAILED", "SKIPPED"]
    data_digest: str = Field(default="", max_length=4000)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    tool: str | None = None
    attempts: int = 0
    degraded: bool = False


class ReviewScores(BaseModel):
    evidence: int = Field(ge=1, le=5, description="结论有据性")
    faithfulness: int = Field(ge=1, le=5, description="引用忠实度")
    consistency: int = Field(ge=1, le=5, description="数字自洽性")
    completeness: int = Field(ge=1, le=5, description="完整度")
    format: int = Field(ge=1, le=5, description="格式")

    def total(self) -> float:
        """5 维等权平均，保留 2 位小数（PRD FR-013 阈值 3.5）。"""
        values = [self.evidence, self.faithfulness, self.consistency, self.completeness, self.format]
        return round(sum(values) / len(values), 2)


class ReviewOutput(BaseModel):
    scores: ReviewScores
    total: float = Field(ge=1.0, le=5.0)
    pass_: bool = Field(default=True, alias="pass")
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    # W2 扩展：降级与跳过标记（FR-013 验收 2 / 3）
    degraded: bool = False
    skipped: bool = False
    skip_reason: str | None = None
    # v1.1：结果可信度 0~1（由证据覆盖率、评分来源、降级状态综合得出，非 LLM 自报）
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="结果可信度 0~1")
    confidence_reason: str = Field(default="", description="可信度构成说明")
    #: PASS=可交付；REVISE=需重规划改进；REJECT=硬性拒绝（不可直接交付）
    verdict: str = Field(default="PASS", description="PASS / REVISE / REJECT")

    model_config = {"populate_by_name": True}

    def normalized_total(self) -> float:
        """总分与分项不一致时以分项为准（防止 LLM 自报总分虚高）。"""
        return self.scores.total()


# ---------------------------------------------------------------------------
# TaskState（PRD 5.2 字段级定义 + W2 运行期字段）
# ---------------------------------------------------------------------------
class ApprovalState(TypedDict, total=False):
    approval_id: int | None
    status: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED"]
    type: str
    reason: str
    comment: str
    decided_by: int | None


class TaskError(TypedDict, total=False):
    code: str
    message: str
    span_id: str | None
    node: str


class TokenLedgerEntry(TypedDict, total=False):
    input: int
    output: int
    compress_before: int
    compress_after: int
    compressed: bool
    calls: int


class ContextMeta(TypedDict, total=False):
    """上下文工程记账（进 trace，供 FR-010 验收与压缩 A/B 统计）。"""

    node: str
    budget: int
    estimated: int
    compressed: bool
    compress_layers: int
    before_tokens: int
    after_tokens: int
    kept_rounds: int


class TaskState(TypedDict, total=False):
    """LangGraph 全局状态（PRD 5.2）。

    ``total=False``：节点只返回自己改动的增量字段，未返回的字段由 reducer 保留。
    """

    # ---- 标识 ----
    task_id: int | str
    trace_id: str
    creator_id: int

    # ---- 输入（不参与压缩，恒保留） ----
    goal: str
    time_range: TimeRange | dict[str, Any]
    memory_hits: list[str]

    # ---- G8：任务级高级开关（默认 false，只读透传，节点据此条件分支） ----
    compress: bool
    use_memory: bool
    eval_tag: str | None

    # ---- 规划 ----
    plan: list[SubtaskPlan]
    plan_assumptions: list[str]
    replan_count: int
    replan_context: dict[str, Any] | None

    # ---- 执行 ----
    subtask_results: Annotated[dict[str, SubtaskResult], merge_dict]
    approvals: ApprovalState
    notify_result: dict[str, Any] | None

    # ---- 评审 / 汇总 ----
    review: dict[str, Any] | None
    review_failures: int
    # ---- v1.1：REJECT 后的人工批注（status: ANNOTATED / APPROVED） ----
    review_intervention: dict[str, Any] | None
    summary: str
    degraded: bool

    # ---- 上下文工程 ----
    messages: Annotated[list[dict[str, Any]], add_messages]
    token_ledger: Annotated[dict[str, TokenLedgerEntry], merge_token_ledger]
    context_digest: str
    context_meta: ContextMeta

    # ---- 产物与终态 ----
    artifacts: Annotated[list[ArtifactRef], add_artifacts]
    eval_score: dict[str, Any]
    error: TaskError | None
    status: str


__all__ = [
    "TimeRange",
    "SubtaskPlan",
    "PlanOutput",
    "ArtifactRef",
    "SubtaskResult",
    "ReviewScores",
    "ReviewOutput",
    "ApprovalState",
    "TaskError",
    "TokenLedgerEntry",
    "ContextMeta",
    "TaskState",
    "ToolHintLiteral",
    "add_messages",
    "merge_dict",
    "merge_token_ledger",
    "add_artifacts",
]
