"""W5-Part2 新增用例：SSE 分布式会话（Redis 注册表 / 队列 / 降级 / 回收）。

对应任务 1「容器化改造 —— 多副本 SSE 会话共享」。
全部用例只用 ``fakeredis``，不依赖真实 Redis，CI 可离线跑。
"""

from __future__ import annotations

import asyncio
import time

import fakeredis
import fakeredis.aioredis
import pytest

from app.core.w6_config import w6
from app.mcp.session_backend import (
    RedisSessionQueue,
    RedisSessionRegistry,
    build_session_registry,
)
from app.mcp.transport_http import HttpSessionRegistry, sse_event_stream


def _pair(server: fakeredis.FakeServer | None = None) -> tuple[RedisSessionRegistry, fakeredis.FakeServer]:
    """构造一份注册表（同步 + 异步客户端共享同一个 FakeServer，等价于「同一个 Redis」）。"""
    srv = server or fakeredis.FakeServer()
    registry = RedisSessionRegistry(
        sync_client=fakeredis.FakeRedis(server=srv, decode_responses=True),
        async_client=fakeredis.aioredis.FakeRedis(server=srv, decode_responses=True),
        poll_interval=0.01,
        ttl_seconds=60,
    )
    return registry, srv


# ---------------------------------------------------------------------------
# 1) 后端选择
# ---------------------------------------------------------------------------
def test_build_registry_memory_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w6, "SESSION_REGISTRY_BACKEND", "memory")
    registry = build_session_registry()
    assert isinstance(registry, HttpSessionRegistry)
    assert not isinstance(registry, RedisSessionRegistry)


def test_build_registry_redis_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w6, "SESSION_REGISTRY_BACKEND", "redis")
    monkeypatch.setattr(w6, "SESSION_REGISTRY_PING_ON_START", False)
    registry = build_session_registry(
        sync_client=fakeredis.FakeRedis(decode_responses=True),
        async_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
    )
    assert isinstance(registry, RedisSessionRegistry)
    assert registry.degraded is False


# ---------------------------------------------------------------------------
# 2) 跨副本可见性（多 Pod 场景的核心断言）
# ---------------------------------------------------------------------------
async def test_session_visible_across_two_registries() -> None:
    """A 副本 create 的会话，B 副本必须能 get 到（否则 SSE 会 404 断流）。"""
    pod_a, server = _pair()
    pod_b, _ = _pair(server)

    session = pod_a.create("sql-query")
    seen = pod_b.get(session.session_id)

    assert seen is not None
    assert seen.session_id == session.session_id
    assert seen.server_name == "sql-query"
    assert len(pod_a) == 1

    await pod_a.aclose()
    await pod_b.aclose()


async def test_drop_visible_across_two_registries() -> None:
    """A 副本 drop 后，B 副本不能再读到（防止已关闭会话被本地缓存「复活」）。"""
    pod_a, server = _pair()
    pod_b, _ = _pair(server)

    session = pod_a.create("notify")
    assert pod_b.get(session.session_id) is not None
    assert pod_b.get(session.session_id) is not None  # 二次读取走 Redis，不应命中脏缓存

    pod_a.drop(session.session_id)
    assert pod_b.get(session.session_id) is None
    assert len(pod_a) == 0

    await pod_a.aclose()
    await pod_b.aclose()


async def test_get_unknown_session_returns_none() -> None:
    registry, _ = _pair()
    assert registry.get("ffffffffffffffffffffffffffffffff") is None
    await registry.aclose()


# ---------------------------------------------------------------------------
# 3) 队列语义：FIFO / 关闭哨兵 / 长度上限
# ---------------------------------------------------------------------------
async def test_redis_queue_fifo_and_close_sentinel() -> None:
    srv = fakeredis.FakeServer()
    async_client = fakeredis.aioredis.FakeRedis(server=srv, decode_responses=True)
    queue = RedisSessionQueue(async_client, "test:q", poll_interval=0.01)

    await queue.put({"id": 1})
    await queue.put({"id": 2})
    assert await queue.get() == {"id": 1}
    assert await queue.get() == {"id": 2}

    await queue.put(None)  # 关闭哨兵
    assert await queue.get() is None


async def test_redis_queue_trims_to_max_items() -> None:
    srv = fakeredis.FakeServer()
    async_client = fakeredis.aioredis.FakeRedis(server=srv, decode_responses=True)
    queue = RedisSessionQueue(async_client, "test:trim", max_items=8, poll_interval=0.01)

    for i in range(20):
        await queue.put({"id": i})

    remaining = []
    while True:
        raw = await async_client.rpop("test:trim")
        if raw is None:
            break
        remaining.append(raw)
    assert len(remaining) == 8  # 只保留最新 8 条


# ---------------------------------------------------------------------------
# 4) 与既有 SSE 生成器兼容（关键：sse_event_stream 一行未改）
# ---------------------------------------------------------------------------
async def test_sse_event_stream_consumes_redis_queue() -> None:
    registry, _ = _pair()
    session = registry.create("kb-search")
    endpoint = f"/mcp/v1/messages?session_id={session.session_id}&server=kb-search"

    await session.queue.put({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}})

    stream = sse_event_stream(session, endpoint=endpoint, ttl_seconds=5, registry=registry, heartbeat_seconds=0.2)

    first = await asyncio.wait_for(stream.__anext__(), timeout=3)
    assert first.startswith("event: endpoint")
    assert session.session_id in first

    second = await asyncio.wait_for(stream.__anext__(), timeout=3)
    assert second.startswith("event: message")
    assert '"id": 7' in second

    await stream.aclose()
    await registry.aclose()


# ---------------------------------------------------------------------------
# 5) 过期回收
# ---------------------------------------------------------------------------
async def test_reap_removes_expired_sessions() -> None:
    registry, _ = _pair()
    session = registry.create("chart-render")

    # 把 index 里的创建时间改到「两倍 TTL 之前」，模拟自然过期
    registry._sync_client().zadd(  # noqa: SLF001 - 测试直接操纵 index 时间戳
        w6.SESSION_REGISTRY_INDEX_KEY,
        {session.session_id: time.time() - registry.ttl_seconds * 2},
    )

    removed = registry.reap()
    assert removed == 1
    assert registry.get(session.session_id) is None
    assert len(registry) == 0
    await registry.aclose()


async def test_get_expired_session_returns_none() -> None:
    registry, _ = _pair()
    session = registry.create("sql-query")
    registry._sync_client().hset(  # noqa: SLF001
        registry.meta_key(session.session_id),
        "created_at",
        time.time() - registry.ttl_seconds * 2,
    )
    assert registry.get(session.session_id) is None
    await registry.aclose()


# ---------------------------------------------------------------------------
# 6) Redis 故障降级（不阻断服务）
# ---------------------------------------------------------------------------
class _BrokenClient:
    """所有命令都抛连接错误，用来验证降级路径。"""

    def ping(self) -> bool:
        raise ConnectionError("redis down")

    def __getattr__(self, item: str):
        def _boom(*_args, **_kwargs):
            raise ConnectionError(f"redis down: {item}")

        return _boom


async def test_degrade_to_memory_when_redis_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w6, "SESSION_REGISTRY_DEGRADE_TO_MEMORY", True)
    registry = RedisSessionRegistry(sync_client=_BrokenClient())

    assert registry.ping() is False
    assert registry.degraded is True

    session = registry.create("sql-query")  # 降级后仍能建会话（进程内）
    assert session.session_id
    assert registry.get(session.session_id) is not None
    assert len(registry) == 1


async def test_no_degrade_raises_when_disallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w6, "SESSION_REGISTRY_DEGRADE_TO_MEMORY", False)
    monkeypatch.setattr(w6, "SESSION_REGISTRY_BACKEND", "redis")
    monkeypatch.setattr(w6, "SESSION_REGISTRY_PING_ON_START", True)

    with pytest.raises(RuntimeError):
        build_session_registry(sync_client=_BrokenClient())


# ---------------------------------------------------------------------------
# 7) 诊断接口
# ---------------------------------------------------------------------------
async def test_snapshot_exposes_pending_count() -> None:
    registry, _ = _pair()
    session = registry.create("notify")
    await session.queue.put({"id": 1})
    await session.queue.put({"id": 2})

    snapshot = registry.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0]["session_id"] == session.session_id
    assert snapshot[0]["server"] == "notify"
    assert snapshot[0]["pending"] == 2
    assert snapshot[0]["backend"] == "redis"

    await registry.aclose()


async def test_queue_key_naming_follows_prefix() -> None:
    registry, _ = _pair()
    session = registry.create("sql-query")
    assert registry.meta_key(session.session_id) == f"{w6.SESSION_REGISTRY_KEY_PREFIX}:{session.session_id}"
    assert registry.queue_key(session.session_id) == f"{w6.SESSION_REGISTRY_KEY_PREFIX}:{session.session_id}:q"
    await registry.aclose()
