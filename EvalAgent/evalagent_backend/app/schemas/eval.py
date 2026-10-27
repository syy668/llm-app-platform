"""Pydantic 出入参模型。

命名对齐前端 src/api/types.ts，避免第四步改前端类型定义。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ============================ 评测集 ============================
class AgentRole(BaseModel):
    """多智能体评测中的单个 agent 角色声明。"""

    role: str = Field(..., description="角色名，如 planner / retriever / summarizer")
    agent_id: str | None = Field(None, description="上游 agent 标识")
    weight: float = Field(1.0, ge=0, le=1, description="该角色在加权总分中的权重")


class DatasetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    code: str | None = Field(None, max_length=64)
    description: str | None = Field(None, max_length=512)
    target_type: Literal["kb", "agentflow", "multi_agent"] = "kb"
    target_ref: str | None = Field(None, max_length=128)
    kb_id: int | None = None
    agent_scope: list[AgentRole] | None = None
    scenario: str | None = Field(None, max_length=64)
    tags: list[str] | None = None
    version: str = "v1"
    created_by: int | None = None


class DatasetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str | None
    name: str
    description: str | None
    target_type: str
    target_ref: str | None
    kb_id: int | None
    agent_scope: list | None
    scenario: str | None
    tags: list | None
    version: str
    item_count: int
    is_active: bool
    created_by: int | None
    created_at: datetime
    updated_at: datetime


class ItemCreate(BaseModel):
    question: str = Field(..., min_length=1)
    reference_answer: str | None = None
    reference_contexts: list[str] | None = None
    stratum: str | None = Field(None, max_length=64)
    difficulty: int | None = Field(None, ge=1, le=5)
    item_type: Literal["rag_qa", "tool_call", "multi_turn", "agent_task"] = "rag_qa"
    expected_tools: list[str] | None = None
    expected_agents: list[str] | None = None
    max_latency_ms: int | None = Field(None, ge=1)
    meta: dict[str, Any] | None = None


class ItemsImport(BaseModel):
    items: list[ItemCreate] = Field(..., min_length=1, max_length=500, description="单次最多 500 条")


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    dataset_id: int
    seq: int
    question: str
    reference_answer: str | None
    reference_contexts: list | None
    stratum: str | None
    difficulty: int | None
    item_type: str
    expected_tools: list | None
    expected_agents: list | None
    max_latency_ms: int | None
    created_at: datetime


# ============================ 评测任务 ============================
class RunCreate(BaseModel):
    """触发评测。

    同时兼容两种调用方：
      - 前端 evalagent_vue：{dataset_id, rag_config, variant, created_by}
      - 项目三 AgentFlow：{dataset_id, target:"agentflow", variant, callback_run_no}
        ← 其 dataset_id 是字符串语义（默认 "agentflow-30"），按业务编码 code 解析
    """

    # 主键（前端）或业务编码（项目三 / 外部系统）；解析见 api/v1/runs.py::_resolve_dataset
    dataset_id: int | str
    target: Literal["kb", "agentflow", "multi_agent"] | None = None
    target_ref: str | None = None
    variant: str | None = Field(None, max_length=16)
    rag_config: dict[str, Any] | None = None
    evaluator_config: dict[str, Any] | None = None
    agent_config: dict[str, Any] | None = None
    callback_run_no: str | None = Field(None, max_length=64, description="项目三的 run_no，落 ext_run_ref")
    trace_id: str | None = Field(None, max_length=64)
    created_by: int | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_no: str
    dataset_id: int
    dataset_version: str | None
    target: str
    target_ref: str | None
    variant: str | None
    status: str
    total_items: int
    processed_items: int
    success_items: int
    failed_items: int
    skipped_items: int
    metrics: dict | None
    metric_detail: dict | None
    rag_config: dict | None
    evaluator_config: dict | None
    total_tokens: int
    cost_usd: float | None
    trace_id: str | None
    ext_run_ref: str | None
    error_msg: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class ScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    item_id: int
    item_seq: int | None
    stratum: str | None
    question: str
    answer: str | None
    reference_answer: str | None
    retrieved_contexts: list | None
    faithfulness: float | None
    answer_relevancy: float | None
    context_precision: float | None
    context_recall: float | None
    metric_scores: dict | None
    agent_role: str | None
    latency_ms: int
    tokens: int
    suspect: bool
    status: str
    error_msg: str | None
    scored_at: datetime | None
    created_at: datetime


class CompareRequest(BaseModel):
    """A/B 对比请求。两个 run 必须属于同一评测集（否则问题无法逐条对齐）。"""

    run_id_a: int
    run_id_b: int
