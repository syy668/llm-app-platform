"""Agent 智能体问答接口。"""
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..deps import CurrentUser, get_current_user, get_own_conversation
from ..rate_limit import rate_limit_ai
from ...core.database import get_db
from ...core.response import ok
from ...models import Conversation
from ...services import agent_service, conversation_service, eval_service

router = APIRouter(prefix="/conversations", tags=["Agent 问答"])


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    kb_id: int | None = Field(None, ge=1, description="可选，指定知识库；缺省使用会话绑定的知识库")
    top_k: int | None = Field(
        None, ge=1, le=50,
        description="可选，本次问答 RAG 检索条数；缺省使用知识库配置（kb.top_k）",
    )
    route_enabled: dict[Literal["rag", "db_query", "report", "parallel_compare"], bool] | None = Field(
        None,
        description="可选，本次问答路由开关；缺省全部启用，置 false 的路由回退为直接回答",
    )


@router.post("/{conversation_id}/chat")
def chat(
    conversation_id: int,
    payload: ChatRequest,
    conv: Conversation = Depends(get_own_conversation),
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    _rate: None = Depends(rate_limit_ai),
):
    """对指定会话发起 Agent 问答（三路路由：RAG / 业务库 / 直接回答）。"""
    options = payload.model_dump(include={"top_k", "route_enabled"}, exclude_none=True)
    return ok(agent_service.chat(db, user, conv, payload.question, payload.kb_id, options=options))


feedback_router = APIRouter(tags=["Agent 问答"])


class FeedbackRequest(BaseModel):
    """消息反馈请求：rating 取 1（赞）或 -1（踩），comment 可选。"""

    message_id: int = Field(..., ge=1)
    rating: Literal[1, -1]
    comment: str | None = Field(None, max_length=500)


@feedback_router.post("/chat/feedback")
def chat_feedback(
    payload: FeedbackRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """对本人会话内的 AI 回答做点赞/点踩反馈（可重复提交更新）。"""
    return ok(conversation_service.set_feedback(db, user, payload.message_id, payload.rating, payload.comment))


class BadcaseRequest(BaseModel):
    """点踩样本入评测集请求：message_id 必填，原因可选。"""

    message_id: int = Field(..., ge=1)
    reason: str | None = Field(None, max_length=500, description="点踩原因（可选，写入反馈备注）")


@feedback_router.post("/chat/badcase")
def chat_badcase(
    payload: BadcaseRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """【v1.9】把被点踩的回答一键沉淀到「用户反馈Badcase」评测集（数据飞轮）。

    权限口径：登录用户即可调用，但**只能上报自己的消息**；集合名固定、按知识库聚合，
    不接受任意 dataset_id —— 避免普通用户借上报接口往别人的评测集塞样本。
    """
    return ok(eval_service.add_badcase_from_message(db, user, payload.message_id, payload.reason))
