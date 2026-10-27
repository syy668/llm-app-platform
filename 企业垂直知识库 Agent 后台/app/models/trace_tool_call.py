"""Trace 工具调用明细表 ORM 模型。"""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base


class TraceToolCall(Base):
    __tablename__ = "trace_tool_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="关联 trace_logs.trace_id")
    step_name: Mapped[str] = mapped_column(String(16), nullable=False, comment="图节点：router/rag_tool/db_query_tool/report_tool")
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="工具名")
    tool_input: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="工具入参")
    tool_output: Mapped[str | None] = mapped_column(Text, nullable=True, comment="工具输出")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="输入 token")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="输出 token")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="耗时(ms)")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success", comment="success/failed")
    error_msg: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="失败原因")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="开始时间")
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="结束时间")
