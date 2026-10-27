"""Agent 输入 / 输出契约（PRD 8.3 字段级定义 + W2 扩展）。

本文件是 Planner / Executor / Reviewer / Summarizer 与 LangGraph 节点之间的**唯一**
数据契约：所有 LLM 输出都必须能通过这里的 Pydantic 校验才允许进入状态机
（FR-010 验收 3），校验失败走上层修复重试。

与 W1 的关系：``TaskState`` 及基础结构仍在 ``graph_state.py``（W1 已定义，W2 只扩展
reducer 与少量字段），本文件负责补齐 PRD 8.3 中 W1 未落地的 Input / ToolDef 等。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.enums import SubtaskRiskLevel, SubtaskToolHint
from app.schemas.graph_state import (
    ArtifactRef,
    PlanOutput,
    ReviewOutput,
    ReviewScores,
    SubtaskPlan,
    SubtaskResult,
    TimeRange,
)

ToolHint = Literal["sql_query", "chart_render", "kb_search", "notify", "none"]
ReplanCause = Literal["tool_failed", "review_rejected", "human_rejected", "empty_result"]


class StrictModel(BaseModel):
    """统一严格模式：禁止未声明字段，防止 LLM 塞入自由发挥的键。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


class ToolDef(StrictModel):
    """来自 MCP ``tools/list`` 的动态工具定义（零硬编码，PRD 5.3 步骤 2）。"""

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    input_schema: dict[str, Any] = Field(default_factory=dict, alias="inputSchema")
    server: str = Field(default="", max_length=64)


# ---------------------------------------------------------------------------
# Planner（PRD 8.3 PlannerInput）
# ---------------------------------------------------------------------------
class ReplanContext(StrictModel):
    """重规划上下文：只携带**受影响**分支的信息，配合已完成结果保留（FR-003）。"""

    cause: ReplanCause = "tool_failed"
    failed_seqs: list[int] = Field(default_factory=list, max_length=15)
    review_comment: str = Field(default="", max_length=500)
    human_comment: str = Field(default="", max_length=500)
    completed_summary: dict[int, str] = Field(default_factory=dict, description="seq -> 已完成结果摘要")

    @property
    def constraint_text(self) -> str:
        parts = [p for p in (self.review_comment, self.human_comment) if p]
        return " / ".join(parts)


class PlannerInput(StrictModel):
    goal: str = Field(min_length=1, max_length=500)
    time_range: TimeRange
    memory_hits: list[str] = Field(default_factory=list, max_length=3, description="长期记忆注入（≤3 条，各 ≤200 Token）")
    replan_context: ReplanContext | None = None
    tools: list["ToolDef"] = Field(default_factory=list, description="MCP 动态发现的能力清单")

    @property
    def is_replan(self) -> bool:
        return self.replan_context is not None


class PlanValidationReport(StrictModel):
    """DAG 校验结果（FR-002 验收 1：无环 + 拓扑排序通过）。"""

    ok: bool
    order: list[int] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def error_text(self) -> str:
        return "; ".join(self.errors)


# ---------------------------------------------------------------------------
# Executor（PRD 8.3 ExecutorInput / SubtaskResult 扩展）
# ---------------------------------------------------------------------------
class ExecutorInput(StrictModel):
    subtask: SubtaskPlan
    context_digest: str = Field(default="", max_length=8000)
    sibling_summaries: list[str] = Field(default_factory=list, max_length=15)
    tools: list[ToolDef] = Field(default_factory=list)
    # 任务 A：真实表结构摘要（只读库 inspect，标识符文本，无行数据）。
    # 空串 = 拿不到结构（降级），Prompt 不注入该段，行为与升级前一致。
    sql_schema: str = Field(default="", max_length=4000)


class ToolCallRequest(StrictModel):
    """Executor 决定的一次工具调用（LLM function calling 或 hint 直映射）。"""

    tool: str = Field(min_length=1, max_length=32)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=200, description="模型给出的一句话理由（可追溯）")
    source: Literal["hint", "llm"] = "hint"


class ToolCallOutcome(StrictModel):
    """工具调用结果（已瘦身，可进上下文）。"""

    tool: str
    ok: bool
    payload: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    attempts: int = 1
    latency_ms: int = 0
    degraded: bool = False
    data_digest: str = Field(default="", max_length=4000)


class SubtaskExecution(StrictModel):
    """Executor 单子任务的完整执行记录（落 trace + 供 reviewer 取证据）。"""

    subtask_seq: int
    name: str
    tool: str = "none"
    status: Literal["DONE", "FAILED", "SKIPPED", "PENDING"] = "PENDING"
    result: SubtaskResult | None = None
    outcome: ToolCallOutcome | None = None
    loop_count: int = 0
    degraded: bool = False


# ---------------------------------------------------------------------------
# Reviewer（PRD 8.3 ReviewerInput）
# ---------------------------------------------------------------------------
class EvidenceRef(StrictModel):
    """评审证据：只能来自工具返回，禁止外部知识（PRD 8.2 Reviewer 要点 ②）。"""

    kind: Literal["sql", "kb", "chart", "notify", "digest"] = "digest"
    ref: str = Field(default="", max_length=128, description="chart_id / doc_id / subtask seq")
    excerpt: str = Field(default="", max_length=600)
    subtask_seq: int | None = None
    score: float | None = None


class ReviewInput(StrictModel):
    report_digest: str = Field(min_length=1, max_length=12000)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=40)
    plan: PlanOutput
    task_goal: str = Field(default="", max_length=500)


class ReviewSuggestion(StrictModel):
    seq: int = Field(ge=1, le=15)
    advice: str = Field(min_length=1, max_length=300)


class ReviewDecision(StrictModel):
    """Reviewer 节点的最终裁决：结构化评分 + 降级/跳过标记（FR-013 验收 3）。"""

    review: ReviewOutput
    degraded: bool = False
    skipped: bool = False
    skip_reason: str | None = None
    gate_pass: bool = True


# ---------------------------------------------------------------------------
# Summarizer
# ---------------------------------------------------------------------------
class SummaryInput(StrictModel):
    goal: str
    time_range: TimeRange
    results: list[SubtaskResult] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    plan: PlanOutput | None = None
    degraded_notes: list[str] = Field(default_factory=list)


class SummaryOutput(StrictModel):
    markdown: str = Field(min_length=1, max_length=20000)
    cited_charts: list[str] = Field(default_factory=list)
    cited_docs: list[str] = Field(default_factory=list)
    degraded: bool = False


# ---------------------------------------------------------------------------
# 任务创建（FR-001；routers 入参）
# ---------------------------------------------------------------------------
class TaskCreateRequest(StrictModel):
    goal: str = Field(min_length=1, max_length=500, description="自然语言目标")
    time_range: TimeRange
    sync: bool = Field(default=False, description="同步等待至终态（项目二回调模式，W4 使用）")
    # ---- G8：任务级高级开关（默认全关，不破坏默认执行链路） ----
    compress: bool = Field(default=False, description="上下文压缩：True 时在规划后启用压缩分支")
    use_memory: bool = Field(default=False, description="长期记忆检索：True 时规划前召回记忆并入 prompt")
    eval_tag: str | None = Field(default=None, max_length=64, description="评测标签：有值时绑定到 eval_run，供后续评测筛选")


__all__ = [
    "ToolHint",
    "ReplanCause",
    "ReplanContext",
    "PlannerInput",
    "PlanValidationReport",
    "ToolDef",
    "ExecutorInput",
    "ToolCallRequest",
    "ToolCallOutcome",
    "SubtaskExecution",
    "EvidenceRef",
    "ReviewInput",
    "ReviewSuggestion",
    "ReviewDecision",
    "SummaryInput",
    "SummaryOutput",
    "TaskCreateRequest",
    # 透传 W1 契约，便于外部只 import agent_io
    "TimeRange",
    "SubtaskPlan",
    "PlanOutput",
    "SubtaskResult",
    "ArtifactRef",
    "ReviewScores",
    "ReviewOutput",
    "SubtaskRiskLevel",
    "SubtaskToolHint",
]
