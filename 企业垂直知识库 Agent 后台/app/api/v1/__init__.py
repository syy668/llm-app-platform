"""v1 路由聚合。"""
from fastapi import APIRouter

from .auth import router as auth_router
from .chat import router as chat_router
from .chat_stream import router as chat_stream_router
from .conversations import router as conversations_router
from .documents import router as documents_router
from .eval import router as eval_router
from .eval_export import router as eval_export_router
from .kbs import router as kbs_router
from .reports import router as reports_router
from .system import router as system_router
from .traces import router as traces_router
from .users import router as users_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(kbs_router)
api_router.include_router(documents_router)
api_router.include_router(conversations_router)
api_router.include_router(chat_router)
api_router.include_router(chat_stream_router)
from .chat import feedback_router  # noqa: E402  （/chat/feedback 独立前缀路由）
api_router.include_router(feedback_router)
api_router.include_router(traces_router)
api_router.include_router(reports_router)
api_router.include_router(eval_router)
api_router.include_router(eval_export_router)
api_router.include_router(system_router)
