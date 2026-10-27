"""RAGAS 评测相关的请求/响应模型。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ItemInput(BaseModel):
    question: str = Field(..., min_length=1)
    reference_answer: str = Field(..., min_length=1)
    reference_contexts: list[str] | None = None
    # 【B2-4 新增】分层难例标签（可选）：如 "easy" / "hard" / 业务自定义分层名，
    # 用于 stratum_metrics 分层聚合；不传为 NULL（未分层）。
    stratum: str | None = Field(None, max_length=64)


class DatasetCreate(BaseModel):
    kb_id: int = Field(..., ge=1)
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(None, max_length=512)


class ItemsImport(BaseModel):
    items: list[ItemInput] = Field(..., min_length=1, max_length=500)


class ItemUpdate(BaseModel):
    """【v1.13 新增】样本局部更新（PATCH 语义）：只改请求体里显式出现的字段。

    设计取舍：
    - `reference_answer` 若出现必须非空 —— 允许改回空串等于把样本重新变成
      「context_recall 无法计算」的脏数据，与 `ItemInput` 的 min_length=1 保持一致；
      显式传 null 由 service 层拒绝并给出可读原因（而非 pydantic 的 422 泛化报错）。
    - 三个字段全部 Optional：只补参考答案时不强制回传 stratum / reference_contexts，
      避免前端漏传字段把已有值清空（这正是 PUT 全覆盖语义的坑）。
    """

    reference_answer: str | None = Field(None, min_length=1, max_length=4000)
    reference_contexts: list[str] | None = None
    stratum: str | None = Field(None, max_length=64)


class RagConfig(BaseModel):
    """【新增】请求级 RAG 实验参数（全部可选，不传沿用知识库配置）。

    说明：top_k / search_mode 由问答检索链路请求级生效（vector_only 纯向量 /
    bm25_vector_hybrid 混合，默认混合）；chunk_size 属于入库切片参数，
    查询期无法生效，仅持久化用于实验记录与 A/B 报告展示。
    """

    chunk_size: int | None = Field(None, ge=100, le=4000, description="切片大小（实验记录）")
    search_mode: str | None = Field(
        None,
        pattern="^(vector_only|bm25_vector_hybrid)$",
        description="检索模式（请求级生效：vector_only 纯向量 / bm25_vector_hybrid 混合）",
    )
    top_k: int | None = Field(None, ge=1, le=50, description="检索返回片段数量（请求级生效）")


class RunCreate(BaseModel):
    dataset_id: int = Field(..., ge=1)
    # 【新增】可选 RAG 实验参数；不传则完全沿用知识库配置，兼容旧调用方
    rag_config: RagConfig | None = None


class RunCompareRequest(BaseModel):
    """【B1-5 新增】A/B 对比请求体：两次评测运行必须属于同一数据集。"""

    run_id_a: int = Field(..., ge=1, description="A 组评测运行 ID")
    run_id_b: int = Field(..., ge=1, description="B 组评测运行 ID")


class DatasetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kb_id: int
    name: str
    description: str | None
    item_count: int
    # 【v1.13 新增】参考答案缺失（空串/NULL）的样本数；仅列表接口聚合填充，
    # 其余场景为 None（不参与任何计算，前端据此提示「待补 N 条」）。
    missing_reference: int | None = None
    created_by: int
    created_at: datetime


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    dataset_id: int
    question: str
    reference_answer: str
    reference_contexts: list | None
    # 【B2-4 新增】分层难例标签；历史数据为 NULL
    stratum: str | None = None
    created_at: datetime


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    dataset_id: int
    kb_id: int
    model: str
    status: str
    total_items: int
    success_items: int
    # 【v1.13.1 新增】进度拆细：processed 含「成功+失败+跳过」，前端进度条用它算百分比；
    # failed 用于「失败 N 条」提示。历史数据默认 0（= 未统计），不可用 success 反推。
    processed_items: int = 0
    failed_items: int = 0
    metrics: dict | None
    # 【新增】创建任务时传入的 RAG 实验参数；历史记录/未传时为 None
    rag_config: dict | None = None
    created_by: int
    started_at: datetime | None
    finished_at: datetime | None
    error_msg: str | None
    created_at: datetime


class ScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    item_id: int
    question: str
    answer: str
    retrieved_contexts: list | None
    faithfulness: float | None
    answer_relevancy: float | None
    context_precision: float | None
    context_recall: float | None
    latency_ms: int
    created_at: datetime


def dataset_to_dict(d) -> dict:
    return DatasetOut.model_validate(d).model_dump(mode="json")


def item_to_dict(i) -> dict:
    return ItemOut.model_validate(i).model_dump(mode="json")


def run_to_dict(r) -> dict:
    return RunOut.model_validate(r).model_dump(mode="json")


def score_to_dict(s) -> dict:
    return ScoreOut.model_validate(s).model_dump(mode="json")
