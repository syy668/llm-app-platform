"""对话会话表 ORM 模型。"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # user_id 不建外键：users.id 为 BIGINT UNSIGNED，ORM 用带符号 BIGINT 会导致外键类型不兼容；
    # 会话归属由鉴权依赖校验，用户采用软禁用不硬删，无级联需求。
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="发起人 users.id")
    kb_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True, comment="绑定的主知识库(可空)")
    title: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="会话标题(首问摘要)")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
