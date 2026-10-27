"""W6 数据模型（W5-Part2 欠账收口）：任务分页列表 / 大盘聚合 / 评测接口组。

对齐：docs/W6接口设计文档.md §1~§3 与 agentflow-web 前端 types。
信封统一走 ApiResponse / PageData（app/schemas/common.py）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# §1 GET /tasks —— 任务分页列表
# ---------------------------------------------------------------------------
class TaskListItem(BaseModel):
    """复用 task_service.get_detail 顶层字段名（前端 types/task.ts 已按此建模），
    去掉重负载（不含 subtasks 数组），新增列表特有字段。"""

    task_id: int
    task_no: str
    goal: str
    time_range: dict[str, Any] = Field(default_factory=dict)
    status: str
    replan_count: int
    token_usage: int
    trace_id: str
    creator_id: int
    """viewer 角色时恒为 None（脱敏：V 无创建人字段）。"""
    creator_name: str | None = None
    """仅 {code, message}；span_id 不下发。"""
    error: dict[str, Any] | None = None
    created_at: str | None = None
    finished_at: str | None = None
    """服务端算好：finished_at - created_at 秒；未终态为 None。"""
    duration_s: int | None = None
    """归档时间；NULL=未归档。默认列表（archived 不传）不含已归档任务。"""
    archived_at: str | None = None


# ---------------------------------------------------------------------------
# §2 GET /dashboards/summary 与 /dashboards/tools
# ---------------------------------------------------------------------------
class WindowRange(BaseModel):
    start: str
    end: str


class DashboardSummary(BaseModel):
    """与前端 P02 五张指标卡一一对应。"""

    window: WindowRange
    task_total: int
    """COMPLETED / 终态总数；窗口内无终态任务时 0.0。"""
    completion_rate: float
    """当前积压（不限定窗口），与 GET /approvals 的 total 同源。"""
    pending_approvals: int
    """窗口内终态任务均值；无样本 None。"""
    avg_duration_s: float | None = None
    """viewer 角色恒为 None（脱敏）。"""
    token_usage_total: int | None = None
    avg_token_per_task: float | None = None
    """窗口内 GROUP BY status，六键恒齐（缺的补 0）。"""
    status_breakdown: dict[str, int] = Field(default_factory=dict)


class ToolStatItem(BaseModel):
    tool_name: str
    call_count: int
    """成功次数 / 总数（成功 = tool_error 为空）；无记录时接口整体 items 为空。"""
    success_rate: float
    """执行耗时均值；全量无耗时记录（latency_ms 恒 0）时 None。"""
    avg_latency_ms: float | None = None


class ToolStatsData(BaseModel):
    window: WindowRange
    items: list[ToolStatItem] = Field(default_factory=list)


class FailureReasonItem(BaseModel):
    """GET /dashboards/failure-reasons 行 —— 数据源 ``tasks.error.code``（原型 P02「失败原因 Top 5」）。"""

    reason_code: str
    count: int
    """该错误码下首条 ``error.message``，供前端悬浮提示；无则 None。"""
    sample_message: str | None = None


class FailureReasonData(BaseModel):
    window: WindowRange
    """窗口内 FAILED 任务总数（含未标注），用于卡片脚注对账。"""
    failed_total: int
    """按 count 降序取前 5；error.code 缺失的归入 ``UNKNOWN``。"""
    items: list[FailureReasonItem] = Field(default_factory=list)


class TrendPoint(BaseModel):
    """GET /dashboards/trend 单个时间桶（缺口：前端工作台「任务完成趋势」卡）。"""

    date: str = Field(description="日期，YYYY-MM-DD")
    task_total: int = Field(description="当日新建任务数")
    completed: int = Field(description="当日完成（COMPLETED）任务数")
    """completed / 终态总数；当日无终态任务时 0.0。"""
    completion_rate: float


class TrendData(BaseModel):
    window: WindowRange
    points: list[TrendPoint] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §4 POST /logs/search —— 日志查询（缺口：系统设置页请求日志区）
# ---------------------------------------------------------------------------
class LogSearchRequest(BaseModel):
    """基于 task_executions 结构化执行记录检索（不引入外部日志平台）。"""

    model_config = {"extra": "forbid"}

    trace_id: str | None = Field(default=None, max_length=64, description="按任务 trace_id 精确过滤")
    task_id: int | None = Field(default=None, ge=1, description="按任务 ID 过滤")
    event: str | None = Field(default=None, max_length=32, description="事件类型：enter/exit/tool.call/tool.result/replan/error")
    level: str | None = Field(default=None, description="级别：ERROR 仅错误记录；缺省=全部")
    start: str | None = Field(default=None, description="时间窗起点 ISO8601")
    end: str | None = Field(default=None, description="时间窗终点 ISO8601")
    page: int = Field(default=1, ge=1)
    size: int = Field(default=50, ge=1, le=200)


class LogItem(BaseModel):
    id: int
    trace_id: str
    span_id: str
    task_id: int
    node: str
    event: str
    tool_name: str | None = None
    tool_error: str | None = None
    level: str = Field(description="INFO / ERROR（tool_error 或 event=error 时 ERROR）")
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    payload: dict[str, Any] | None = None
    started_at: str | None = None


class LogSearchData(BaseModel):
    items: list[LogItem] = Field(default_factory=list)
    page: int
    size: int
    total: int


# ---------------------------------------------------------------------------
# §3 评测接口组 /eval/runs
# ---------------------------------------------------------------------------
class EvalRunItem(BaseModel):
    """直接映射 eval_runs 表列，不造新字段。"""

    run_id: int
    run_no: str
    source: str
    variant: str | None = None
    dataset_id: str | None = None
    # G8：评测标签（绑定 eval_run，支持筛选）
    eval_tag: str | None = None
    ext_run_id: str | None = None
    status: str
    """沿用表注释既有键：task_completion/plan_f1/tool_acc/token_cost/latency_p50/latency_p95；DONE 前可为 None。"""
    metrics: dict[str, Any] | None = None
    """detail JSON 的样本计数（逐样本明细不下发）。"""
    sample_count: int | None = None
    """FAILED 时一句话原因（detail.error / detail.error_message）。"""
    error_message: str | None = None
    """True = MOCK_EXTERNAL_EVAL 下由 `_mock_metrics()` 随机生成的指标，不可用于业务结论。
    前端据此打「Mock」红标并把指标置灰（v1.3 去伪装）。"""
    is_mock: bool = False
    """v1.4 真实评测来源：project1_ragas = 项目一 RAGAS 底座；None = 历史/mock 记录。"""
    eval_backend: str | None = None
    """真实评测的判分模型名（项目一 eval_llm_model，temperature 固定 0）。"""
    judge_model: str | None = None
    """指标口径说明：kb_retrieval_chain = 评的是「知识库检索+生成」链路，
    **不是** AgentFlow 端到端多智能体编排质量。前端须明示，防止误读。"""
    metrics_scope: str | None = None
    """真实评测墙钟耗时（秒），取自项目一 started_at→finished_at。"""
    duration_s: float | None = None
    created_at: str | None = None
    finished_at: str | None = None


class EvalRunSummaryGroup(BaseModel):
    variant: str | None = None
    run_count: int
    avg_task_completion: float | None = None
    avg_plan_f1: float | None = None
    avg_tool_acc: float | None = None
    #: v1.4 真实 RAGAS 四指标（项目一判定底座的原始指标名，不做语义映射）。
    #: 历史 mock 数据没有这四个键 → 恒为 None，前端显示「—」而不是补 0。
    avg_faithfulness: float | None = None
    avg_answer_relevancy: float | None = None
    avg_context_precision: float | None = None
    avg_context_recall: float | None = None
    """viewer 角色恒为 None（脱敏）。"""
    avg_token_cost: float | None = None
    """以 compress_off 组 avg_token_cost 为基线：1 - 该组/基线；基线组自身 None；无基线组时两组均 None。"""
    token_saving_rate: float | None = None
    avg_latency_p95_s: float | None = None


class EvalRunSummaryData(BaseModel):
    window: str | None = None
    groups: list[EvalRunSummaryGroup] = Field(default_factory=list)


class EvalMetricsPoint(BaseModel):
    date: str
    run_count: int
    avg_task_completion: float | None = None
    avg_plan_f1: float | None = None
    avg_tool_acc: float | None = None
    #: v1.4 真实 RAGAS 四指标（同一 run 日内取算术均值；空值不参与，不补 0）
    avg_faithfulness: float | None = None
    avg_answer_relevancy: float | None = None
    avg_context_precision: float | None = None
    avg_context_recall: float | None = None
    """viewer 角色恒为 None（脱敏）。"""
    avg_token_cost: float | None = None
    avg_latency_p95_s: float | None = None


class EvalMetricsData(BaseModel):
    points: list[EvalMetricsPoint] = Field(default_factory=list)


class EvalRunCreateRequest(BaseModel):
    """触发回归评测（FR-016 验收标准 2）。extra="forbid" 与 W3 契约风格一致。"""

    model_config = {"extra": "forbid"}

    dataset_id: str = Field(default="agentflow-30", max_length=64)
    variant: str | None = None
    note: str | None = Field(default=None, max_length=200)
    # G8：评测标签（由任务创建时 eval_tag 透传，绑定到 eval_run，支持评测筛选）
    eval_tag: str | None = Field(default=None, max_length=64, description="评测标签，用于筛选")


class EvalRunCreateResult(BaseModel):
    run_id: int
    run_no: str
    status: str


__all__ = [
    "TaskListItem",
    "WindowRange",
    "DashboardSummary",
    "ToolStatItem",
    "ToolStatsData",
    "TrendPoint",
    "TrendData",
    "LogSearchRequest",
    "LogItem",
    "LogSearchData",
    "EvalRunItem",
    "EvalRunSummaryGroup",
    "EvalRunSummaryData",
    "EvalMetricsPoint",
    "EvalMetricsData",
    "EvalRunCreateRequest",
    "EvalRunCreateResult",
]
