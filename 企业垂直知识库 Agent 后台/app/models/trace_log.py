"""Trace 链路日志表 ORM 模型。"""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base


class TraceLog(Base):
    __tablename__ = "trace_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, comment="链路ID(UUID)")
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="会话ID")
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="消息ID")
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="用户ID")
    kb_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="命中的知识库")
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="运行会话ID(可选)")
    agent_route: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="最终路由决策")
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="deepseek-chat", comment="模型")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="输入 token")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="输出 token")
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="总 token")
    # 【v1.16】输出被 max_tokens 硬切断（finish_reason=length）的标记。
    # 截断不抛异常、HTTP 仍 200，只看 status 与 error_msg 完全发现不了，
    # 必须有独立布尔位才能在大盘上算「截断率」并据此调 LLM_MAX_TOKENS。
    is_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0"),
        comment="回答是否被 max_tokens 截断",
    )
    llm_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="LLM 调用次数")
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="工具调用次数")
    num_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="检索召回块数")
    retrieval_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="检索耗时(ms)")
    total_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="端到端总耗时(ms)")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", comment="running/success/failed")
    error_msg: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="失败原因")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="开始时间")
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="结束时间")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
