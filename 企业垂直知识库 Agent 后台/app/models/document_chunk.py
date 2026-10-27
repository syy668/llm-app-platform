"""文档分块表 ORM 模型（RAG 文本块，向量本体存 Chroma）。"""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True, comment="所属文档",
    )
    kb_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False, index=True, comment="所属知识库(冗余)",
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, comment="块序号")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="块原文")
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="估算 token 数")
    # 注意：DB 列名为 metadata，但 ORM 属性命名为 chunk_metadata，
    # 以避免与 Declarative 基类保留属性 metadata 冲突。
    chunk_metadata: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True, comment="来源信息")
    chroma_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True, comment="Chroma 向量ID")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
