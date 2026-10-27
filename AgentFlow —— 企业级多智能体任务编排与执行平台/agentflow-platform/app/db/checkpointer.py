"""LangGraph checkpointer 入口（W2 已启用）。

W1 仅做导入探测；W2 起：每次节点转移后由 LangGraph 自动写快照，
key 形如 ``checkpoint:{thread_id}``（thread_id = task_id），TTL 72h（PRD 5.2）。

真正的构建逻辑在 ``app/services/checkpoint_service.py``（含 Redis → 内存降级），
本文件只保留"能力探测 + 状态上报"两个轻量职责，避免 db 层反向依赖 services 层。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.w2_config import w2

logger = get_logger(__name__)

try:  # pragma: no cover - 依赖缺失时降级，不阻断启动
    from langgraph.checkpoint.redis import RedisSaver  # type: ignore[import-not-found]

    REDIS_SAVER_AVAILABLE = True
except Exception:  # noqa: BLE001
    RedisSaver = None  # type: ignore[assignment]
    REDIS_SAVER_AVAILABLE = False


def checkpointer_status() -> dict[str, Any]:
    """健康检查用：暴露真实启用状态，不做任何连接。"""
    return {
        "redis_saver_available": REDIS_SAVER_AVAILABLE,
        "checkpoint_ttl_seconds": settings.LANGGRAPH_CHECKPOINT_TTL_SECONDS,
        "checkpoint_ttl_hours": round(settings.LANGGRAPH_CHECKPOINT_TTL_SECONDS / 3600, 1),
        "backend": w2.CHECKPOINTER_BACKEND,
        "enabled": True,  # W2 起启用（运行时不可用则自动降级内存，见 checkpoint_service）
    }


async def build_checkpointer() -> Any:
    """返回可用的 checkpointer 实例（RedisSaver 优先，失败降级内存）。"""
    from app.services.checkpoint_service import get_checkpointer

    return await get_checkpointer()


__all__ = ["REDIS_SAVER_AVAILABLE", "checkpointer_status", "build_checkpointer"]
