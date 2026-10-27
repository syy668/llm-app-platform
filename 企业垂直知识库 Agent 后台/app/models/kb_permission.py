"""知识库访问授权表 ORM 模型（kb_permissions，简化 RBAC 的库级授权）。

admin 默认全库可访问无需授权记录；创建者（owner）天然拥有其库的全部权限。
permission: read=可问答/只读查看 write=可上传与管理文档
"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base

PERM_READ = "read"
PERM_WRITE = "write"


class KBPermission(Base):
    __tablename__ = "kb_permissions"
    __table_args__ = (UniqueConstraint("kb_id", "user_id", name="uk_kb_user"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False, index=True, comment="知识库ID",
    )
    # user_id 不建外键：users.id 为 BIGINT UNSIGNED，类型不兼容（error 3780），
    # 与 conversations.user_id 等既有模型惯例一致；用户仅软删除，无需级联。
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="被授权用户ID")
    permission: Mapped[str] = mapped_column(String(16), nullable=False, default=PERM_READ, comment="read=只读 write=可管理")
    granted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="授权人 users.id")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
