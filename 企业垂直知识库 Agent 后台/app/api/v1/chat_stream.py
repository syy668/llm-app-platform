"""SSE 流式问答接口（保留原非流式接口不变，仅新增本接口）。"""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ..deps import CurrentUser, get_current_user, get_own_conversation
from ..rate_limit import rate_limit_ai
from ...models import Conversation
from ...services import stream_service
from .chat import ChatRequest

router = APIRouter(prefix="/conversations", tags=["Agent 问答"])


@router.post("/{conversation_id}/chat/stream")
async def chat_stream(
    conversation_id: int,
    payload: ChatRequest,
    _conv: Conversation = Depends(get_own_conversation),  # 复用会话归属校验（越权返回 403）
    user: CurrentUser = Depends(get_current_user),
    _rate: None = Depends(rate_limit_ai),
):
    """SSE 流式问答（text/event-stream）。事件：route / delta / report / done / error。"""

    async def event_stream():
        # 仅在请求携带对话参数时才传 options，保持对旧调用签名兼容
        options = payload.model_dump(include={"top_k", "route_enabled"}, exclude_none=True)
        kwargs = {"options": options} if options else {}
        async for event in stream_service.stream_chat(
            user, conversation_id, payload.question, payload.kb_id, **kwargs
        ):
            yield event

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
