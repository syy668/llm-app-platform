"""对话会话 / 消息相关的请求与响应模型。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConversationCreate(BaseModel):
    kb_id: int | None = Field(None, ge=1, description="绑定的主知识库ID，可空")


class ConversationRename(BaseModel):
    """会话重命名请求。"""

    title: str = Field(..., min_length=1, max_length=100, description="新标题（1~100 字）")

    @field_validator("title")
    @classmethod
    def _strip_and_check(cls, value: str) -> str:
        """去首尾空白；纯空白视为空标题拒绝。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("标题不能为空")
        return stripped


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    kb_id: int | None
    title: str | None
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    user_id: int
    role: str
    content: str
    agent_route: str | None
    trace_id: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    status: str
    error_msg: str | None
    # 【v1.10】引用来源快照；老消息为 None（前端据此把溯源渲染成不可点击的静态样式）
    sources: list[dict] | None = None
    created_at: datetime


def conv_to_dict(conv) -> dict:
    return ConversationOut.model_validate(conv).model_dump(mode="json")


def msg_to_dict(msg) -> dict:
    return MessageOut.model_validate(msg).model_dump(mode="json")
