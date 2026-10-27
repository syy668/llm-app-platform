"""评测运行相关 ORM 模型。"""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base

# 运行状态
RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_FAILED = "failed"
# 【v1.13 新增】用户主动停止。与 failed 刻意区分：
#   - failed = 系统/上游故障，指标不可信、需要排查；
#   - cancelled = 人到点刹车，已完成部分的指标仍然真实可用（但样本不全，
#     不能与 done 的 run 做 A/B，前端 selectableRun 只放行 done 已保证这一点）。
RUN_CANCELLED = "cancelled"

# 终态集合：进入这些状态后不再接受取消/删除等操作
TERMINAL_STATUSES = (RUN_DONE, RUN_FAILED, RUN_CANCELLED)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="数据集")
    kb_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="知识库")
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="deepseek-chat", comment="评测模型")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RUN_PENDING, comment="pending/running/done/failed/cancelled")
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 【v1.13.1 新增】进度口径拆细，供前端进度条与失败提示使用。
    # processed_items = 已处理样本数（成功 + 失败 + 跳过），用来算进度百分比 ——
    # 只用 success/total 会把「跳过」与「失败」都算成没跑，进度条永远到不了 100%，
    # 用户会误以为任务卡死（这正是此前 27/30 显示 90% 看起来像仍在运行的原因）。
    processed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已处理样本数（成功+失败+跳过）")
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="评测失败样本数（已重试仍失败）")
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="汇总指标均值")
    # 【新增】请求级 RAG 实验参数（chunk_size / search_mode / top_k），可选。
    # 为 null 表示未传入，沿用知识库自身持久化配置；历史数据自动为 NULL，完全向后兼容。
    # 注意：top_k / search_mode 问答检索时请求级生效（vector_only 纯向量 /
    # bm25_vector_hybrid 混合）；chunk_size（入库切片参数）仅持久化供 A/B 报告展示。
    rag_config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None, comment="请求级RAG实验参数(可选)")
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="失败原因")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class EvalRunScore(Base):
    __tablename__ = "eval_run_scores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_contexts: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="实际召回片段")
    faithfulness: Mapped[float | None] = mapped_column(Float, nullable=True, comment="忠实度")
    answer_relevancy: Mapped[float | None] = mapped_column(Float, nullable=True, comment="答案相关性")
    context_precision: Mapped[float | None] = mapped_column(Float, nullable=True, comment="上下文精确率")
    context_recall: Mapped[float | None] = mapped_column(Float, nullable=True, comment="上下文召回率")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
