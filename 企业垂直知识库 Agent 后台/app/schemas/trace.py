"""Trace 日志相关的响应模型。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TraceLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trace_id: str
    conversation_id: int | None
    message_id: int | None
    user_id: int | None
    kb_id: int | None
    agent_route: str | None
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    # 【v1.16】输出是否被 max_tokens 截断（默认 False，兼容未升级的历史行/旧客户端）
    is_truncated: bool = False
    llm_call_count: int
    tool_call_count: int
    num_chunks: int
    retrieval_ms: int
    total_latency_ms: int
    status: str
    error_msg: str | None
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime


class TraceToolCallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trace_id: str
    step_name: str
    tool_name: str
    tool_input: dict | None
    tool_output: str | None
    latency_ms: int
    status: str
    error_msg: str | None
    started_at: datetime | None
    ended_at: datetime | None


def trace_to_dict(t) -> dict:
    return TraceLogOut.model_validate(t).model_dump(mode="json")


def tool_call_to_dict(t) -> dict:
    return TraceToolCallOut.model_validate(t).model_dump(mode="json")
