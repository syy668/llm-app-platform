"""文档表 ORM 模型。"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base

# 解析状态（本模块同步解析，仅使用 pending/done/failed 三种流转）
PARSE_PENDING = "pending"
PARSE_DONE = "done"
PARSE_FAILED = "failed"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False, index=True, comment="所属知识库",
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="文档标题/文件名")
    file_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="文件类型")
    file_path: Mapped[str] = mapped_column(String(512), nullable=False, comment="存储路径")
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, comment="字节数")
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="SHA256 去重")
    parse_status: Mapped[str] = mapped_column(String(16), nullable=False, default=PARSE_PENDING, comment="解析状态")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="分块数")
    error_msg: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="失败原因")
    uploaded_by: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="上传人 users.id")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
