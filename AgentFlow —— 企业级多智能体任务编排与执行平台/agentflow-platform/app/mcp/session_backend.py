"""SSE 会话注册表后端：内存（默认）/ Redis（多副本共享）—— W5-Part2 任务 1。

## 为什么需要这一层

W5-Part1 的 ``HttpSessionRegistry`` 把会话放在**进程内 dict** 里，配合
``asyncio.Queue`` 做「POST /messages 投递 → GET /sse 回流」。单实例完全够用，
但多副本部署下有两个硬伤：

1. 负载均衡把 ``GET /sse`` 打到 Pod-A、把 ``POST /messages`` 打到 Pod-B 时，Pod-B
   查不到会话 → 恒 404，SSE 直接断；
2. Pod 重启 / 滚动发布时所有 SSE 会话蒸发，客户端只能从头重连。

## 解法：只换「会话存储」，不动事件流生成器

``sse_event_stream()`` 只依赖会话对象的两个能力 —— ``session.queue.get()`` 与
``session.session_id``。因此只要提供一个**队列鸭子类型**（``RedisSessionQueue``），
把 ``HttpMcpSession.queue`` 换成它，生成器一行都不用改：

```
GET /sse          → registry.create()  → 会话元数据写 Redis(hash) + index(zset)
POST /messages    → registry.get()     → 从 Redis 读元数据（跨 Pod 可见）
                    await session.queue.put(resp) → LPUSH mcp:sess:<id>:q
sse_event_stream  → await session.queue.get()  → RPOP（阻塞轮询）
```

## 同步 / 异步双客户端（设计取舍，务必知晓）

``build_mcp_http_router`` 里的路由是 **async**，但调用注册表的方法是 **同步**的
（``registry.get(session_id)``、``registry.create(...)``），这是 Part1 既定的接口契约，
本阶段不允许改 ``transport_http.py``。而 ``redis.asyncio`` 只能 await，同步路由里
没法用。因此：

- **元数据**（HGETALL / HSET / DEL，都是 O(1) 小对象）走**同步客户端**，
  ``socket_timeout`` 默认 50ms —— 这是明确的**有界阻塞**，最坏情况 50ms 后降级，
  不会拖垮事件循环；
- **消息队列**（可能阻塞等待）全部走**异步客户端**，绝不在事件循环里同步阻塞。

这个取舍写进了 ``production-deploy-guide.md`` 的「已知限制」一节。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.w6_config import w6
from app.mcp.transport_http import HttpMcpSession, HttpSessionRegistry

logger = get_logger(__name__)

# 队列里出现该哨兵值表示「会话被显式关闭」，消费端应结束事件流
_CLOSE_SENTINEL = "null"


# ---------------------------------------------------------------------------
# 1) Redis 队列（鸭子类型：只暴露 get / put / qsize，供 sse_event_stream 使用）
# ---------------------------------------------------------------------------
class RedisSessionQueue:
    """跨进程消息队列：LPUSH 投递，RPOP 消费（FIFO）。

    刻意**不用 BLPOP**：``sse_event_stream`` 外层已经用 ``asyncio.wait_for`` 做超时，
    BLPOP 被取消时存在「值已弹出但协程被取消」的丢消息窗口；RPOP + 短轮询没有这个问题，
    代价是最大 ``poll_interval``（默认 50ms）的额外延迟，对 SSE 场景完全可接受。
    """

    __slots__ = ("_client", "_key", "_max_items", "_poll_interval", "_cached_size")

    def __init__(
        self,
        client: Any,
        key: str,
        *,
        max_items: int | None = None,
        poll_interval: float | None = None,
    ) -> None:
        self._client = client
        self._key = key
        self._max_items = max_items or w6.SESSION_REGISTRY_QUEUE_MAX
        self._poll_interval = poll_interval or w6.SESSION_REGISTRY_POLL_INTERVAL_SECONDS
        self._cached_size = 0

    @property
    def key(self) -> str:
        return self._key

    async def put(self, item: Any) -> None:
        """投递一条 JSON-RPC 响应；队列超长时裁掉最老的（LPUSH 后 LTRIM 保留最新 N 条）。"""
        payload = _CLOSE_SENTINEL if item is None else json.dumps(item, ensure_ascii=False, default=str)
        await self._client.lpush(self._key, payload)
        await self._client.ltrim(self._key, 0, self._max_items - 1)

    async def get(self) -> Any:
        """取一条消息；返回 ``None`` 表示会话已关闭（哨兵值，非「暂时没有」）。"""
        while True:
            raw = await self._client.rpop(self._key)
            if raw is None:
                await asyncio.sleep(self._poll_interval)
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if raw == _CLOSE_SENTINEL:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:  # pragma: no cover - 队列里只会有本模块写的 JSON
                logger.warning("session_queue_bad_payload", extra={"key": self._key, "raw": str(raw)[:120]})
                continue

    def qsize(self) -> int:
        """仅用于诊断；返回上次观察到的长度（同步接口无法 await LLEN）。"""
        return self._cached_size

    async def refresh_size(self) -> int:
        try:
            self._cached_size = int(await self._client.llen(self._key) or 0)
        except Exception:  # noqa: BLE001 - 诊断接口，失败不影响主流程
            self._cached_size = 0
        return self._cached_size

    async def aclose(self) -> None:
        try:
            await self._client.delete(self._key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("session_queue_close_failed", extra={"key": self._key, "error": str(exc)[:160]})


# ---------------------------------------------------------------------------
# 2) Redis 会话注册表（HttpSessionRegistry 的可替换实现）
# ---------------------------------------------------------------------------
class RedisSessionRegistry(HttpSessionRegistry):
    """把会话表外置到 Redis；接口与 ``HttpSessionRegistry`` 完全一致（可直接注入）。

    - ``create / get / drop / reap / __len__ / snapshot`` 签名与语义保持不变；
    - Redis 故障且 ``SESSION_REGISTRY_DEGRADE_TO_MEMORY=true`` 时自动退回父类（进程内），
      只保证「单实例继续可用」，不会让请求 500。
    """

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        prefix: str | None = None,
        index_key: str | None = None,
        ttl_seconds: int | None = None,
        max_sessions: int | None = None,
        sync_client: Any = None,
        async_client: Any = None,
        poll_interval: float | None = None,
    ) -> None:
        super().__init__(max_sessions=max_sessions, ttl_seconds=ttl_seconds)
        self._url = redis_url or settings.redis_url
        self._prefix = (prefix or w6.SESSION_REGISTRY_KEY_PREFIX).strip(":")
        self._index_key = (index_key or w6.SESSION_REGISTRY_INDEX_KEY).strip(":")
        self._poll_interval = poll_interval or w6.SESSION_REGISTRY_POLL_INTERVAL_SECONDS
        self._sync = sync_client
        self._async = async_client
        self._degraded = False
        self._degrade_reason = ""

    # ---------------- 客户端懒加载 ----------------
    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def degrade_reason(self) -> str:
        return self._degrade_reason

    def _sync_client(self) -> Any:
        if self._sync is None:
            import redis as redis_sync

            self._sync = redis_sync.Redis.from_url(
                self._url,
                decode_responses=True,
                socket_timeout=w6.SESSION_REGISTRY_SYNC_TIMEOUT_SECONDS,
                socket_connect_timeout=w6.SESSION_REGISTRY_SYNC_TIMEOUT_SECONDS,
            )
        return self._sync

    def _async_client(self) -> Any:
        if self._async is None:
            from redis.asyncio import Redis as AsyncRedis

            self._async = AsyncRedis.from_url(
                self._url,
                decode_responses=True,
                socket_timeout=w6.SESSION_REGISTRY_SYNC_TIMEOUT_SECONDS * 20,
                socket_connect_timeout=w6.SESSION_REGISTRY_SYNC_TIMEOUT_SECONDS * 20,
            )
        return self._async

    def _degrade(self, action: str, exc: BaseException) -> None:
        if self._degraded:
            return
        self._degraded = True
        self._degrade_reason = f"{action}: {type(exc).__name__}"
        logger.warning(
            "session_registry_degraded_to_memory",
            extra={"action": action, "error": str(exc)[:200], "fallback": w6.SESSION_REGISTRY_DEGRADE_TO_MEMORY},
        )

    def _use_redis(self) -> bool:
        return not self._degraded

    # ---------------- 元数据 key ----------------
    def meta_key(self, session_id: str) -> str:
        return f"{self._prefix}:{session_id}"

    def queue_key(self, session_id: str) -> str:
        return f"{self._prefix}:{session_id}:q"

    # ---------------- 生命周期 ----------------
    def ping(self) -> bool:
        """探活（同步、有界超时）；``/ready`` 探针与启动自检使用。"""
        try:
            return bool(self._sync_client().ping())
        except Exception as exc:  # noqa: BLE001
            self._degrade("ping", exc)
            return False

    def create(self, server_name: str) -> HttpMcpSession:
        if not self._use_redis():
            return super().create(server_name)

        try:
            self.reap()
            session_id = uuid.uuid4().hex
            now = time.time()
            client = self._sync_client()
            pipe = client.pipeline()
            pipe.hset(
                self.meta_key(session_id),
                mapping={"server": server_name, "created_at": now, "last_seen": now, "pid": _pid()},
            )
            pipe.expire(self.meta_key(session_id), self.ttl_seconds)
            pipe.zadd(self._index_key, {session_id: now})
            pipe.execute()
        except Exception as exc:  # noqa: BLE001
            self._degrade("create", exc)
            if not w6.SESSION_REGISTRY_DEGRADE_TO_MEMORY:
                raise
            return super().create(server_name)

        session = self._build_session(session_id, server_name, created_at=now, last_seen=now)
        self._sessions[session_id] = session  # 本地镜像：同 Pod 后续 get 命中 L1
        logger.info(
            "mcp_http_session_open",
            extra={"session_id": session_id, "server": server_name, "backend": "redis"},
        )
        return session

    def get(self, session_id: str) -> HttpMcpSession | None:
        if not self._use_redis():
            return super().get(session_id)

        # 刻意**不做本地缓存直读**：drop() 可能发生在别的副本上，
        # L1 命中会把已关闭的会话「复活」，多副本下表现为消息投进黑洞。
        # 一次 HGETALL 是 O(1) 小对象，远低于任何业务查询成本，换来的是强一致。
        try:
            meta = self._sync_client().hgetall(self.meta_key(session_id))
        except Exception as exc:  # noqa: BLE001
            self._degrade("get", exc)
            return super().get(session_id) if w6.SESSION_REGISTRY_DEGRADE_TO_MEMORY else None

        if not meta:
            self._sessions.pop(session_id, None)
            return None

        created_at = _as_float(meta.get("created_at"), time.time())
        last_seen = _as_float(meta.get("last_seen"), created_at)
        if time.time() - created_at > self.ttl_seconds:
            self.drop(session_id)
            return None

        session = self._build_session(
            session_id,
            str(meta.get("server") or ""),
            created_at=created_at,
            last_seen=last_seen,
        )
        self._sessions[session_id] = session
        self._touch_remote(session_id)
        return session

    def drop(self, session_id: str) -> None:
        existed = self._sessions.pop(session_id, None) is not None
        if self._use_redis():
            try:
                client = self._sync_client()
                # 先给在线消费者投一个关闭哨兵（队列保留 5s 让其读完），再删元数据
                if existed:
                    client.lpush(self.queue_key(session_id), _CLOSE_SENTINEL)
                    client.expire(self.queue_key(session_id), 5)
                client.delete(self.meta_key(session_id))
                client.zrem(self._index_key, session_id)
            except Exception as exc:  # noqa: BLE001
                self._degrade("drop", exc)
        if existed:
            logger.info("mcp_http_session_close", extra={"session_id": session_id, "backend": "redis"})

    def reap(self) -> int:
        """回收超过 TTL 的会话；多副本下每个实例都会跑，靠 ZREM 幂等。"""
        if not self._use_redis():
            return super().reap()
        try:
            cutoff = time.time() - self.ttl_seconds
            stale = self._sync_client().zrangebyscore(self._index_key, "-inf", cutoff, start=0, num=200)
        except Exception as exc:  # noqa: BLE001
            self._degrade("reap", exc)
            return super().reap() if w6.SESSION_REGISTRY_DEGRADE_TO_MEMORY else 0

        for session_id in stale or []:
            self.drop(str(session_id))
        return len(stale or [])

    def __len__(self) -> int:  # pragma: no cover - 仅诊断
        if not self._use_redis():
            return super().__len__()
        try:
            return int(self._sync_client().zcard(self._index_key) or 0)
        except Exception:  # noqa: BLE001
            return super().__len__()

    def snapshot(self) -> list[dict[str, Any]]:
        if not self._use_redis():
            return super().snapshot()
        try:
            ids = self._sync_client().zrange(self._index_key, 0, 99)
            client = self._sync_client()
            out: list[dict[str, Any]] = []
            now = time.time()
            for session_id in ids or []:
                meta = client.hgetall(self.meta_key(str(session_id))) or {}
                out.append(
                    {
                        "session_id": str(session_id),
                        "server": meta.get("server"),
                        "age_seconds": round(now - _as_float(meta.get("created_at"), now), 3),
                        "pending": int(client.llen(self.queue_key(str(session_id))) or 0),
                        "backend": "redis",
                        "pid": meta.get("pid"),
                    }
                )
            return out
        except Exception as exc:  # noqa: BLE001
            self._degrade("snapshot", exc)
            return super().snapshot()

    # ---------------- 内部 ----------------
    def _build_session(self, session_id: str, server_name: str, *, created_at: float, last_seen: float) -> HttpMcpSession:
        """构造与父类同类型的会话对象，只把 ``queue`` 换成 Redis 队列。"""
        # HttpMcpSession 的时间字段是 monotonic 口径（用于 age 计算），
        # 这里把 epoch 时间换算成等价的 monotonic 值，保证父类 ``age`` 语义不变。
        mono_created = time.monotonic() - max(0.0, time.time() - created_at)
        mono_seen = time.monotonic() - max(0.0, time.time() - last_seen)
        return HttpMcpSession(
            session_id=session_id,
            server_name=server_name,
            queue=RedisSessionQueue(
                self._async_client(),
                self.queue_key(session_id),
                poll_interval=self._poll_interval,
            ),
            created_at=mono_created,
            last_seen=mono_seen,
        )

    def _touch_remote(self, session_id: str) -> None:
        try:
            client = self._sync_client()
            pipe = client.pipeline()
            pipe.hset(self.meta_key(session_id), "last_seen", time.time())
            pipe.expire(self.meta_key(session_id), self.ttl_seconds)
            pipe.execute()
        except Exception as exc:  # noqa: BLE001 - 心跳续期失败不影响本次请求
            logger.warning("session_registry_touch_failed", extra={"session_id": session_id, "error": str(exc)[:120]})

    async def aclose(self) -> None:
        """释放连接（应用关停时调用）。"""
        self._sessions.clear()
        for client in (self._async, self._sync):
            if client is None:
                continue
            closer = getattr(client, "aclose", None) or getattr(client, "close", None)
            if closer is None:
                continue
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# 3) 工厂：按环境变量选择后端
# ---------------------------------------------------------------------------
def build_session_registry(backend: str | None = None, **kwargs: Any) -> HttpSessionRegistry:
    """``SESSION_REGISTRY_BACKEND=memory|redis`` → 对应注册表实例。

    - ``memory``：原样返回 ``HttpSessionRegistry``（W5-Part1 行为，零依赖）；
    - ``redis``：返回 ``RedisSessionRegistry``；启动探活失败且允许降级时，
      实例内部标记 ``degraded=True`` 并退回进程内实现（服务不中断）。
    """
    choice = (backend or w6.SESSION_REGISTRY_BACKEND).strip().lower()
    if choice != "redis":
        return HttpSessionRegistry(**kwargs)

    registry = RedisSessionRegistry(**kwargs)
    if w6.SESSION_REGISTRY_PING_ON_START and not registry.ping():
        if not w6.SESSION_REGISTRY_DEGRADE_TO_MEMORY:
            raise RuntimeError("SESSION_REGISTRY_BACKEND=redis 但 Redis 探活失败，且未允许降级")
        logger.warning("session_registry_unavailable_use_memory", extra={"url": _safe_url(settings.redis_url)})
    else:
        logger.info(
            "session_registry_ready",
            extra={"backend": "redis", "prefix": w6.SESSION_REGISTRY_KEY_PREFIX},
        )
    return registry


def _pid() -> int:
    import os

    return os.getpid()


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_url(url: str) -> str:
    """隐藏 Redis 密码后再进日志。"""
    if "@" not in url:
        return url
    head, _, tail = url.partition("@")
    scheme, sep, _ = head.partition("://")
    return f"{scheme}{sep}***@{tail}" if sep else f"***@{tail}"


__all__ = ["RedisSessionQueue", "RedisSessionRegistry", "build_session_registry"]
