"""取消信号（任务终止的跨进程开关）。

为什么需要独立信号而不是直接改 DB 状态：
- 运行中的任务在**另一个 asyncio 任务**里跑图，改 DB 只能被"下一次查询"看到；
- 图执行到节点边界时需要一个廉价的"是否该停"探测点，Redis 单 key 最合适；
- 取消后保留 TTL，避免 key 泄漏；同一任务重新提交时会覆盖。

Redis 不可用 → 进程内集合降级（单实例可用），与 EventBus 的降级策略保持一致。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.w3_config import w3

logger = get_logger(__name__)


class CancelSignal:
    """任务取消标记的读写。"""

    def __init__(
        self,
        redis_client: Any = None,
        *,
        ttl_seconds: int | None = None,
        key_prefix: str = "cancel",
    ) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds or w3.CANCEL_FLAG_TTL_SECONDS
        self._prefix = key_prefix
        self._local: dict[str, str] = {}
        self._lock = asyncio.Lock()
        # Redis 一旦失败就永久降级为进程内标记：取消探测在节点边界高频调用，
        # 不能每次都去撞一次连接超时（否则单任务会产生上百条告警 + 明显延迟）。
        self._degraded = False

    def key(self, task_id: int | str) -> str:
        return settings.key(self._prefix, str(task_id))

    def _client(self) -> Any:
        if self._degraded:
            return None
        if self._redis is not None:
            return self._redis
        try:
            from app.core.redis_client import get_shared_redis

            self._redis = get_shared_redis()
        except Exception:  # noqa: BLE001
            self._degraded = True
            return None
        return self._redis

    def _degrade(self, action: str, exc: BaseException) -> None:
        if not self._degraded:
            logger.warning(
                "cancel_signal_degraded_to_memory",
                extra={"action": action, "error": str(exc)[:200]},
            )
        self._degraded = True

    async def set(self, task_id: int | str, *, reason: str = "") -> None:
        client = self._client()
        if client is not None:
            try:
                await client.set(self.key(task_id), reason or "1", ex=self._ttl)
                return
            except Exception as exc:  # noqa: BLE001
                self._degrade("set", exc)
        async with self._lock:
            self._local[str(task_id)] = reason or "1"

    async def is_set(self, task_id: int | str) -> bool:
        return (await self.reason(task_id)) is not None

    async def reason(self, task_id: int | str) -> str | None:
        client = self._client()
        if client is not None:
            try:
                value = await client.get(self.key(task_id))
                if value is None:
                    return None
                return value.decode() if isinstance(value, bytes) else str(value)
            except Exception as exc:  # noqa: BLE001
                self._degrade("get", exc)
        async with self._lock:
            return self._local.get(str(task_id))

    async def clear(self, task_id: int | str) -> None:
        client = self._client()
        if client is not None:
            try:
                await client.delete(self.key(task_id))
            except Exception:  # noqa: BLE001
                pass
        async with self._lock:
            self._local.pop(str(task_id), None)


__all__ = ["CancelSignal"]
