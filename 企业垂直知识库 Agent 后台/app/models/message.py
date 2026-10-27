"""消息表 ORM 模型（多轮对话历史）。"""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base

# 消息角色
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_SYSTEM = "system"
ROLE_TOOL = "tool"

# 消息状态
MSG_RUNNING = "running"
MSG_DONE = "done"
MSG_FAILED = "failed"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False, index=True, comment="所属会话",
    )
    # user_id 不建外键，理由同 conversations.user_id
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="发言用户 users.id")
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=ROLE_USER, comment="消息角色")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="消息正文")
    agent_route: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="Agent 最终路由")
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True, comment="关联 Trace")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="输入 token")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="输出 token")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="本消息总耗时(ms)")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=MSG_RUNNING, comment="消息状态")
    error_msg: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="失败原因")
    # 【v1.10】结构化来源快照：回答里 [来源i] / 「《文档》第N块」的溯源目标。
    # 只在该条 assistant 消息生成时写入一次，之后不再变化（历史消息也能溯源）。
    # 用 JSON 而非关联表：来源是「回答当时的检索结果快照」，不随文档重新分块而变。
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="引用来源快照")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
