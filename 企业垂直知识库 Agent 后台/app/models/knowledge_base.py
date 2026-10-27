"""知识库表 ORM 模型。"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base

KB_STATUS_ACTIVE = "active"
KB_STATUS_ARCHIVED = "archived"


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="知识库名称")
    description: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="描述")
    # 说明：owner_id 不建外键。users.id 为 BIGINT UNSIGNED，ORM 用带符号 BIGINT 会导致
    # 外键类型不兼容；且用户采用软禁用不硬删，无级联需求，故仅作普通列。
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="创建者 users.id")
    embedding_model: Mapped[str] = mapped_column(String(64), nullable=False, default="default", comment="Embedding 模型标识")
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, default=500, comment="分块大小(字符)")
    chunk_overlap: Mapped[int] = mapped_column(Integer, nullable=False, default=50, comment="分块重叠(字符)")
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5, comment="默认召回条数")
    rerank_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="重排序过滤阈值")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=KB_STATUS_ACTIVE, comment="状态")
    doc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="文档数(冗余统计)")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
