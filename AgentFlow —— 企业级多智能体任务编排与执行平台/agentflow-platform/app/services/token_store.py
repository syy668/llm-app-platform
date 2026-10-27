"""Token 存储（Redis）：refresh 轮换登记 + access 黑名单。

Key 设计（统一前缀 settings.REDIS_KEY_PREFIX = agentflow）：
    agentflow:auth:refresh:{jti}        -> user_id       TTL = refresh 剩余有效期
    agentflow:auth:refresh:user:{uid}   -> ZSET(jti, exp) 单用户会话索引（限会话数）
    agentflow:auth:blacklist:{jti}      -> "1"           TTL = access 剩余有效期
"""

from __future__ import annotations

from datetime import datetime

from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger
from app.utils.time import utcnow

logger = get_logger(__name__)


def _ttl_seconds(expires_at: datetime | None) -> int:
    if expires_at is None:
        return 1
    return max(1, int((expires_at - utcnow()).total_seconds()))


class TokenStore:
    """鉴权状态的 Redis 侧读写；不接触数据库。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # ---------------- key ----------------
    @staticmethod
    def refresh_key(jti: str) -> str:
        return settings.key("auth", "refresh", jti)

    @staticmethod
    def user_sessions_key(user_id: int) -> str:
        return settings.key("auth", "refresh", "user", str(user_id))

    @staticmethod
    def blacklist_key(jti: str) -> str:
        return settings.key("auth", "blacklist", jti)

    # ---------------- refresh ----------------
    async def register_refresh(self, *, jti: str, user_id: int, expires_at: datetime) -> None:
        ttl = _ttl_seconds(expires_at)
        pipe = self._redis.pipeline()
        pipe.set(self.refresh_key(jti), str(user_id), ex=ttl)
        pipe.zadd(self.user_sessions_key(user_id), {jti: expires_at.timestamp()})
        # 会话索引多留 1 天，避免 refresh 刚过期时索引先消失导致统计偏差
        pipe.expire(self.user_sessions_key(user_id), ttl + 86400)
        await pipe.execute()
        await self._trim_user_sessions(user_id)

    async def consume_refresh(self, jti: str) -> bool:
        """一次性消费：删除成功表示本次轮换合法，否则视为重放。"""
        key = self.refresh_key(jti)
        pipe = self._redis.pipeline()
        pipe.get(key)
        pipe.delete(key)
        value, deleted = await pipe.execute()
        return bool(value) and bool(deleted)

    async def revoke_refresh(self, jti: str) -> None:
        await self._redis.delete(self.refresh_key(jti))

    async def revoke_user_sessions(self, user_id: int) -> int:
        """强制下线：清空该用户全部 refresh 会话。"""
        key = self.user_sessions_key(user_id)
        jtis = await self._redis.zrange(key, 0, -1)
        if jtis:
            await self._redis.delete(*[self.refresh_key(jti) for jti in jtis])
        await self._redis.delete(key)
        return len(jtis)

    async def _trim_user_sessions(self, user_id: int) -> None:
        limit = settings.AUTH_MAX_SESSIONS_PER_USER
        if limit <= 0:
            return
        key = self.user_sessions_key(user_id)
        # 清理已过期会话，再按过期时间淘汰最旧会话
        await self._redis.zremrangebyscore(key, "-inf", utcnow().timestamp())
        count = await self._redis.zcard(key)
        if count > limit:
            stale = await self._redis.zrange(key, 0, count - limit - 1)
            if stale:
                await self._redis.delete(*[self.refresh_key(jti) for jti in stale])
                await self._redis.zrem(key, *stale)

    # ---------------- blacklist ----------------
    async def blacklist_access(self, *, jti: str, expires_at: datetime | None) -> None:
        await self._redis.set(self.blacklist_key(jti), "1", ex=_ttl_seconds(expires_at))

    async def is_blacklisted(self, jti: str) -> bool:
        return bool(await self._redis.exists(self.blacklist_key(jti)))

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001
            logger.warning("token_store_ping_failed")
            return False
