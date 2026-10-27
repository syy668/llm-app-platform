"""消息反馈表 ORM 模型（问答点赞/点踩，一人一条可改评）。"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base

FEEDBACK_LIKE = 1
FEEDBACK_DISLIKE = -1


class MessageFeedback(Base):
    __tablename__ = "message_feedbacks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True, comment="消息ID（一条消息一条反馈）",
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="反馈用户 users.id")
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="评分：1=赞 -1=踩")
    comment: Mapped[str | None] = mapped_column(Text, nullable=True, comment="可选文字反馈")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
