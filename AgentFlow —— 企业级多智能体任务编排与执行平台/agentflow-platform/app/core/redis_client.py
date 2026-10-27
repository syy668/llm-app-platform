"""Redis 异步客户端（连接池复用，进程内单例）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client: Redis | None = None


def _build_client() -> Redis:
    return Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        socket_connect_timeout=3,
        socket_timeout=5,
        health_check_interval=30,
    )


def get_shared_redis() -> Redis:
    """惰性单例；避免 import 期建立连接。"""
    global _client
    if _client is None:
        _client = _build_client()
    return _client


async def get_redis() -> AsyncIterator[Redis]:
    """FastAPI 依赖：测试可整体 override。"""
    yield get_shared_redis()


async def ping_redis() -> bool:
    try:
        return bool(await get_shared_redis().ping())
    except Exception:  # noqa: BLE001
        logger.warning("redis_ping_failed")
        return False


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
