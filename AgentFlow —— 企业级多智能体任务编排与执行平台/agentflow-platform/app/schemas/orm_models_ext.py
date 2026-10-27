"""W2 扩展表：mock_messages（notify MCP Server 的 Mock 落库目标）。

为什么单独成文件：
- W1 的 ``orm_models.py`` 严格对应 PRD 第六章 7 张表，本阶段不改动；
- notify 恒为 Mock（PRD 非目标 #3），消息体只落这张表 + 结构化日志，绝不出网；
- 与 7 张表共用同一个 ``Base.metadata``，``alembic revision --autogenerate`` 会一并生成。

表结构：message_id（UUID，唯一）+ 渠道 / 标题 / 正文 / 接收人 + 关联任务与审批。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, pk_column
from app.schemas.mcp import NotifyStatus


class MockMessage(Base, TimestampMixin):
    __tablename__ = "mock_messages"
    __table_args__ = (Index("ix_mock_messages_task_id_created_at", "task_id", "created_at"),)

    id: Mapped[int] = pk_column()
    message_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, comment="Mock 消息 UUID")
    task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="归属任务（无审批时不落库）")
    approval_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="依据的审批记录")
    channel: Mapped[str] = mapped_column(String(16), nullable=False, comment="feishu / email（Mock）")
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="消息正文（Mock，不出网）")
    receivers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=NotifyStatus.SENT.value)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="Mock 发送时间")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MockMessage id={self.id} message_id={self.message_id} status={self.status}>"


__all__ = ["MockMessage"]
