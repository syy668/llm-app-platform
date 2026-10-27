"""事件总线（FR-009 / W3 交付）：SSE 事件经 Redis Stream 分发。

W1 占位文件在本阶段正式落地，设计要点：

1. **一任务一 Stream**：key = ``agentflow:events:<task_id>``，``XADD MAXLEN ~ N`` 控制长度，
   ``EXPIRE`` 24h 与 PRD 7.2 的回放窗口一致；
2. **游标即 SSE id**：订阅方用 ``XREAD`` 带 ``last_id`` 读取，游标直接对应 SSE 的
   ``Last-Event-ID``——天然去重（同一客户端不会重复收到同一事件），且支持断线重放；
3. **多客户端扇出**：每个客户端各自持游标独立读取，互不影响；事件只写一次（不重复推送）；
4. **降级**：Redis 不可用时自动切到进程内内存队列（单实例可用、多实例失效），只告警一次，
   绝不让 SSE 把服务打崩（PRD 4.2 可靠性要求）。

不做的事：不创建消费组（``XGROUP``）——SSE 是"广播 + 各自回放"语义，消费组会互相抢消息。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.w3_config import w3
from app.schemas.w3 import SseEvent
from app.utils.time import utcnow

logger = get_logger(__name__)

#: 从头开始读的游标
START_ID = "0-0"


@dataclass(slots=True)
class StreamEvent:
    """从 Stream 读到的一条事件。"""

    id: str
    event: str
    data: dict[str, Any]
    trace_id: str | None = None
    ts: str | None = None

    def to_sse(self, *, retry_ms: int | None = None) -> str:
        return SseEvent(
            id=self.id,
            event=self.event,
            data=self.data,
            trace_id=self.trace_id,
            retry_ms=retry_ms,
        ).to_frame()


def _split_id(value: str | None) -> tuple[int, int]:
    """Stream ID 形如 ``1700000000000-0``；解析失败按最小处理，保证比较不抛错。"""
    if not value:
        return (0, 0)
    head, _, tail = str(value).partition("-")
    try:
        return (int(head), int(tail or 0))
    except (TypeError, ValueError):
        return (0, 0)


def id_greater(left: str | None, right: str | None) -> bool:
    return _split_id(left) > _split_id(right)


def _decode(value: Any) -> Any:
    return value.decode() if isinstance(value, bytes) else value


def _to_event(entry_id: Any, fields: dict[str, Any]) -> StreamEvent:
    decoded = {str(_decode(k)): _decode(v) for k, v in (fields or {}).items()}
    raw = decoded.get("data") or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = {"raw": raw}
    return StreamEvent(
        id=str(_decode(entry_id)),
        event=str(decoded.get("event") or "state_update"),
        data=data if isinstance(data, dict) else {"value": data},
        trace_id=decoded.get("trace_id") or None,
        ts=decoded.get("ts") or None,
    )


class EventBus:
    """任务事件发布 / 订阅（Redis Stream 优先，内存降级）。"""

    def __init__(
        self,
        redis_client: Any = None,
        *,
        backend: str | None = None,
        maxlen: int | None = None,
        ttl_seconds: int | None = None,
        key_prefix: str | None = None,
    ) -> None:
        self._redis = redis_client
        self._backend = (backend or w3.STREAM_BACKEND).lower()
        self._maxlen = maxlen or w3.STREAM_MAXLEN
        self._ttl = ttl_seconds or w3.STREAM_TTL_SECONDS
        self._prefix = key_prefix or w3.STREAM_KEY_PREFIX
        # memory 后端或 Redis 故障降级后为 True
        self._degraded = self._backend == "memory"
        self._local: dict[str, list[StreamEvent]] = {}
        self._counter = 0
        self._lock = asyncio.Lock()

    # ---------------- 基础设施 ----------------
    def key(self, task_id: int | str) -> str:
        return settings.key(self._prefix, str(task_id))

    @property
    def backend(self) -> str:
        return "memory" if (self._degraded or self._redis is None) else "redis"

    @property
    def redis_client(self) -> Any:
        """暴露底层客户端，供取消信号 / 任务锁等服务复用同一连接（不新建连接池）。

        注意：必须走 ``_client()`` 惰性初始化。历史实现直接返回 ``self._redis``，
        而 ``_redis`` 只在首次 publish 时由 ``_client()`` 填充——于是「先审批、后发布」
        的路径（resume 后台任务里的 ``task_lock(bus.redis_client, ...)``）拿到 None，
        表现为 ``AttributeError: 'NoneType' object has no attribute 'set'``：
        审批接口照样返回 200「已通过」，后台恢复却静默崩溃，任务永久挂在待审批。
        """
        return self._client()

    def _client(self) -> Any:
        """惰性取共享 Redis 客户端；不主动连，交给首个命令暴露问题。"""
        if self._degraded or self._backend == "memory":
            return None
        if self._redis is None:
            try:
                from app.core.redis_client import get_shared_redis

                self._redis = get_shared_redis()
            except Exception as exc:  # noqa: BLE001 - 客户端构建失败直接降级
                self._degrade("client_init", exc)
                return None
        return self._redis

    def _degrade(self, action: str, exc: BaseException) -> None:
        if not self._degraded:
            logger.warning(
                "event_bus_degraded_to_memory",
                extra={"action": action, "error": str(exc)[:200], "impact": "多实例 SSE 扇出失效"},
            )
        self._degraded = True

    # ---------------- 发布 ----------------
    async def publish(
        self,
        task_id: int | str,
        event: str,
        data: dict[str, Any],
        *,
        trace_id: str | None = None,
    ) -> str:
        """追加一条事件，返回事件 ID（SSE 的 ``id:`` 字段）。"""
        ts = utcnow().isoformat()
        redis = self._client()
        if redis is not None:
            payload = {
                "event": str(event),
                "data": json.dumps(data, ensure_ascii=False, default=str),
                "trace_id": trace_id or "",
                "ts": ts,
            }
            try:
                entry_id = await redis.xadd(
                    self.key(task_id), payload, maxlen=self._maxlen, approximate=True
                )
                await redis.expire(self.key(task_id), self._ttl)
                return str(_decode(entry_id))
            except Exception as exc:  # noqa: BLE001 - 写流失败降级内存，不阻断业务
                self._degrade("publish", exc)
        return await self._local_publish(task_id, event, data, trace_id=trace_id, ts=ts)

    async def _local_publish(
        self,
        task_id: int | str,
        event: str,
        data: dict[str, Any],
        *,
        trace_id: str | None,
        ts: str,
    ) -> str:
        async with self._lock:
            self._counter += 1
            entry_id = f"{int(time.time() * 1000)}-{self._counter}"
            bucket = self._local.setdefault(str(task_id), [])
            bucket.append(StreamEvent(id=entry_id, event=str(event), data=data, trace_id=trace_id, ts=ts))
            if len(bucket) > self._maxlen:
                del bucket[: len(bucket) - self._maxlen]
            return entry_id

    # ---------------- 订阅 ----------------
    async def read(
        self,
        task_id: int | str,
        *,
        last_id: str | None = None,
        count: int | None = None,
        block_ms: int | None = None,
    ) -> list[StreamEvent]:
        """读取 ``last_id`` 之后的事件。

        ``block_ms`` > 0 时阻塞等待新事件（最长该毫秒数），0/None 为非阻塞。
        阻塞语义在 fakeredis 等实现上可能不被支持，失败时退化为非阻塞，不影响正确性。
        """
        cursor = last_id or START_ID
        limit = count or w3.SSE_READ_COUNT
        redis = self._client()
        if redis is not None:
            try:
                streams = await redis.xread(
                    {self.key(task_id): cursor}, count=limit, block=(block_ms or None)
                )
                if not streams:
                    return []
                events: list[StreamEvent] = []
                for _stream_key, entries in streams:
                    for entry_id, fields in entries or []:
                        events.append(_to_event(entry_id, fields))
                return events
            except Exception as exc:  # noqa: BLE001
                self._degrade("read", exc)
        return await self._local_read(task_id, cursor, limit)

    async def _local_read(self, task_id: int | str, cursor: str, limit: int) -> list[StreamEvent]:
        async with self._lock:
            bucket = list(self._local.get(str(task_id), ()))
        return [item for item in bucket if id_greater(item.id, cursor)][:limit]

    async def last_id(self, task_id: int | str) -> str:
        redis = self._client()
        if redis is not None:
            try:
                rows = await redis.xrevrange(self.key(task_id), count=1)
                if rows:
                    return str(_decode(rows[0][0]))
                return START_ID
            except Exception as exc:  # noqa: BLE001
                self._degrade("last_id", exc)
        async with self._lock:
            bucket = self._local.get(str(task_id), ())
        return bucket[-1].id if bucket else START_ID

    async def size(self, task_id: int | str) -> int:
        redis = self._client()
        if redis is not None:
            try:
                return int(await redis.xlen(self.key(task_id)))
            except Exception as exc:  # noqa: BLE001
                self._degrade("size", exc)
        async with self._lock:
            return len(self._local.get(str(task_id), ()))

    async def clear(self, task_id: int | str) -> None:
        """清空某任务的事件流（测试 / 重跑用）。"""
        redis = self._client()
        if redis is not None:
            try:
                await redis.delete(self.key(task_id))
                return
            except Exception as exc:  # noqa: BLE001
                self._degrade("clear", exc)
        async with self._lock:
            self._local.pop(str(task_id), None)

    async def aclose(self) -> None:
        """不关闭共享 Redis 客户端（生命周期归 app 管理），仅释放本地缓存。"""
        async with self._lock:
            self._local.clear()


__all__ = ["EventBus", "StreamEvent", "START_ID", "id_greater"]
