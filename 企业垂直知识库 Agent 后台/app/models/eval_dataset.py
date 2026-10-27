"""评测数据集相关 ORM 模型。"""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base


class EvalDataset(Base):
    __tablename__ = "eval_datasets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="评测目标知识库")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="数据集名称")
    description: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="描述")
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="样本数")
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="创建人")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class EvalDatasetItem(Base):
    __tablename__ = "eval_dataset_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="所属数据集")
    question: Mapped[str] = mapped_column(Text, nullable=False, comment="评测问题")
    reference_answer: Mapped[str] = mapped_column(Text, nullable=False, comment="参考答案")
    reference_contexts: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="参考上下文(标准片段)数组")
    # 【B2-4 新增】分层难例标签（可选）：如 easy / hard / 业务自定义分层名；
    # 历史数据自动为 NULL（未分层），stratum_metrics 分层聚合时归入"未分层"。
    stratum: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="分层难例标签(可选)")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
