"""对话会话接口：新建会话、会话列表、会话消息、重命名、删除会话。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..deps import CurrentUser, get_current_user, get_own_conversation
from ...core.database import get_db
from ...core.response import ok
from ...models import Conversation
from ...schemas.conversation import ConversationCreate, ConversationRename
from ...services import conversation_service

router = APIRouter(prefix="/conversations", tags=["对话会话"])


@router.post("")
def create_conversation(
    payload: ConversationCreate = ConversationCreate(),
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """新建会话（归属当前登录用户）。"""
    return ok(conversation_service.create_conversation(db, user, payload.kb_id))


@router.get("")
def list_conversations(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """当前用户的会话列表。"""
    return ok(conversation_service.list_conversations(db, user, page, size))


@router.get("/{conversation_id}/messages")
def list_messages(
    conversation_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=1000),
    conv: Conversation = Depends(get_own_conversation),
    db: Session = Depends(get_db),
):
    """会话消息列表（仅本人）。"""
    return ok(conversation_service.list_messages(db, conv, page, size))


@router.put("/{conversation_id}")
def rename_conversation(
    conversation_id: int,
    payload: ConversationRename,
    conv: Conversation = Depends(get_own_conversation),
    db: Session = Depends(get_db),
):
    """重命名会话（仅本人）。"""
    return ok(conversation_service.rename_conversation(db, conv, payload.title))


@router.delete("/{conversation_id}")
def delete_conversation(
    conversation_id: int,
    conv: Conversation = Depends(get_own_conversation),
    db: Session = Depends(get_db),
):
    """删除会话（级联删除消息）。"""
    return ok(conversation_service.delete_conversation(db, conv))
