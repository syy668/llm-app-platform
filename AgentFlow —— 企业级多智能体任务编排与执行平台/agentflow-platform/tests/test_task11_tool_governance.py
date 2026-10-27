"""任务 11：工具治理中间件——版本路由适配器 + 幂等结果缓存。"""

from __future__ import annotations

import json

import pytest
from fakeredis.aioredis import FakeRedis

from app.core.context import task_id_var
from app.core.w2_config import w2
from app.schemas.mcp import McpErrorCode
from app.services.tool_registry import TOOL_CATALOG, ToolRegistry
from app.tools.mcp_client import MCPClient, MCPClientPool, MCPTransport, ToolInvocationError
from app.tools.tool_governance import IdempotencyStore, ToolVersionRouter


# ---------------------------------------------------------------------------
# 测试基建：可编程的 MCP 传输层（记录每一次 request）
# ---------------------------------------------------------------------------
class FakeTransport(MCPTransport):
    def __init__(self, tools: list[dict], handler) -> None:
        self.tools = tools
        self.handler = handler
        self.calls: list[dict] = []
        self._next_id = 0

    async def start(self) -> None:
        return None

    async def request(self, method: str, params: dict, timeout: float) -> dict:
        self.calls.append({"method": method, "params": params})
        self._next_id += 1
        envelope = {"jsonrpc": "2.0", "id": self._next_id}
        if method == "initialize":
            envelope["result"] = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake", "version": "1"}}
            return envelope
        if method == "tools/list":
            envelope["result"] = {"tools": self.tools}
            return envelope
        if method == "tools/call":
            result = self.handler(params["name"], params["arguments"])
            if hasattr(result, "__await__"):
                result = await result
            envelope["result"] = {"structuredContent": result}
            return envelope
        raise AssertionError(f"unexpected method {method}")

    async def close(self) -> None:
        return None

    def tool_calls(self) -> list[dict]:
        return [c["params"] for c in self.calls if c["method"] == "tools/call"]


KB_TOOLS_V1_ONLY = [
    {"name": "kb-search", "description": "知识库检索 v1", "inputSchema": {"type": "object"}},
    {"name": "notify", "description": "经营通知", "inputSchema": {"type": "object"}},
]
KB_TOOLS_WITH_V2 = KB_TOOLS_V1_ONLY + [
    {"name": "kb-search-v2", "description": "知识库检索 v2", "inputSchema": {"type": "object"}},
]


def _handler_v1(name: str, args: dict) -> dict:
    return {"ok": True, "echo_name": name, "echo_args": args}


async def _make_pool(tools, handler, *, routes=None, redis=None):
    transport = FakeTransport(tools, handler)
    pool = MCPClientPool(
        {"fake-srv": MCPClient("fake-srv", transport, timeout=5)},
        version_router=ToolVersionRouter(routes or {}),
        idempotency=IdempotencyStore(redis, ttl_seconds=600) if redis is not None else None,
    )
    await pool.discover()
    return pool, transport


# ---------------------------------------------------------------------------
# 改进 1：版本路由
# ---------------------------------------------------------------------------
def test_router_passthrough_when_no_routes():
    resolved = ToolVersionRouter({}).resolve("kb-search", {"query": "库存", "top_k": 2})
    assert (resolved.wire_tool, resolved.version, resolved.changed) == ("kb-search", "v1", False)
    assert resolved.wire_arguments == {"query": "库存", "top_k": 2}


def test_router_maps_kb_search_v2_payload():
    router = ToolVersionRouter({"kb-search": "v2"})
    resolved = router.resolve("kb-search", {"query": "库存风险", "top_k": 3, "tenant": "agentflow"})
    assert resolved.changed and resolved.version == "v2"
    assert resolved.wire_tool == "kb-search-v2"
    # v1 叫 query，v2 叫 search_query；其余参数透传
    assert resolved.wire_arguments == {"search_query": "库存风险", "top_k": 3, "tenant": "agentflow"}
    # 原始参数保持不变（幂等键 / 回退都用它）
    assert resolved.arguments == {"query": "库存风险", "top_k": 3, "tenant": "agentflow"}


def test_router_default_reads_w2_config():
    assert ToolVersionRouter().routes == dict(w2.TOOL_VERSION_ROUTES)


async def test_pool_routes_v2_end_to_end():
    pool, transport = await _make_pool(KB_TOOLS_WITH_V2, _handler_v1, routes={"kb-search": "v2"})
    result = await pool.call("kb-search", {"query": "库存", "top_k": 2})
    calls = transport.tool_calls()
    assert len(calls) == 1
    assert calls[0]["name"] == "kb-search-v2"
    assert calls[0]["arguments"] == {"search_query": "库存", "top_k": 2}
    assert result["echo_args"] == {"search_query": "库存", "top_k": 2}


async def test_pool_falls_back_to_v1_when_v2_not_deployed():
    """目标版本工具未上线（tools/list 没发现）→ 自动回退 v1 原契约，调用不失败。"""
    pool, transport = await _make_pool(KB_TOOLS_V1_ONLY, _handler_v1, routes={"kb-search": "v2"})
    result = await pool.call("kb-search", {"query": "库存", "top_k": 2})
    calls = transport.tool_calls()
    assert calls[0]["name"] == "kb-search"
    assert calls[0]["arguments"] == {"query": "库存", "top_k": 2}  # 回退用 v1 原参数
    assert result["ok"] is True


def test_registry_meta_exposes_version():
    assert TOOL_CATALOG["kb-search"].version == "v1"


async def test_registry_list_tools_includes_version():
    registry = ToolRegistry(redis=None)
    items = await registry.list_tools()
    assert all(item["version"] == "v1" for item in items)


# ---------------------------------------------------------------------------
# 改进 2：幂等保护
# ---------------------------------------------------------------------------
class SideEffectServer:
    """模拟带副作用的工具（如推送群消息）：记录真实执行次数。"""

    def __init__(self, *, fail_first: int = 0) -> None:
        self.executed: list[dict] = []
        self.fail_first = fail_first

    async def __call__(self, name: str, args: dict) -> dict:
        if self.fail_first > 0:
            self.fail_first -= 1
            raise ToolInvocationError(McpErrorCode.MCP_TIMEOUT.value, "工具执行超时")
        self.executed.append({"name": name, "args": args})
        return {"status": "SENT", "message_id": f"m{len(self.executed)}"}


async def test_idempotent_retry_executes_side_effect_only_once():
    """同一任务内同一参数调用 3 次（模拟超时重试）→ 真实副作用只发生 1 次。"""
    redis = FakeRedis(decode_responses=True)
    server = SideEffectServer()
    pool, _ = await _make_pool(KB_TOOLS_V1_ONLY, server)
    pool.idempotency = IdempotencyStore(redis, ttl_seconds=600)

    token = task_id_var.set(777)
    try:
        results = [await pool.call("notify", {"title": "周报", "content": "GMV 下滑"}) for _ in range(3)]
    finally:
        task_id_var.reset(token)

    assert len(server.executed) == 1  # 真实只执行一次
    assert len({json.dumps(r, sort_keys=True) for r in results}) == 1  # 三次返回一致
    assert results[0]["message_id"] == "m1"


async def test_idempotency_key_ttl_and_isolation():
    """TTL 生效；不同参数 / 不同任务互不命中。"""
    redis = FakeRedis(decode_responses=True)
    server = SideEffectServer()
    pool, _ = await _make_pool(KB_TOOLS_V1_ONLY, server)
    store = IdempotencyStore(redis, ttl_seconds=600)
    pool.idempotency = store

    token = task_id_var.set(1)
    try:
        await pool.call("notify", {"title": "a"})
        key = store.build_key(1, "notify", {"title": "a"})
        assert await redis.exists(key)
        assert 0 < await redis.ttl(key) <= 600
        await pool.call("notify", {"title": "b"})  # 不同参数 → 真实执行
    finally:
        task_id_var.reset(token)

    token = task_id_var.set(2)
    try:
        await pool.call("notify", {"title": "a"})  # 不同任务 → 真实执行（跨任务隔离）
    finally:
        task_id_var.reset(token)

    assert len(server.executed) == 3


async def test_idempotency_skipped_without_task_id():
    """离线/评测路径未绑定 task_id → 不做幂等（行为与升级前一致）。"""
    redis = FakeRedis(decode_responses=True)
    server = SideEffectServer()
    pool, _ = await _make_pool(KB_TOOLS_V1_ONLY, server)
    pool.idempotency = IdempotencyStore(redis)

    for _ in range(2):
        await pool.call("notify", {"title": "a"})
    assert len(server.executed) == 2


async def test_idempotency_failure_not_cached():
    """失败/超时不落缓存：第一次失败后，重试仍会真实发起。"""
    redis = FakeRedis(decode_responses=True)
    server = SideEffectServer(fail_first=1)
    pool, transport = await _make_pool(KB_TOOLS_V1_ONLY, server)
    pool.idempotency = IdempotencyStore(redis)

    token = task_id_var.set(9)
    try:
        with pytest.raises(ToolInvocationError):
            await pool.call("notify", {"title": "a"})
        result = await pool.call("notify", {"title": "a"})
    finally:
        task_id_var.reset(token)

    assert len(transport.tool_calls()) == 2  # 失败的那次也真实发出过
    assert result["status"] == "SENT"


async def test_idempotency_fail_open_on_redis_error():
    """Redis 故障 fail-open：读写异常被吞掉，工具调用照常进行。"""

    class BrokenRedis:
        async def get(self, key):
            raise ConnectionError("redis down")

        async def set(self, key, value, ex=None):
            raise ConnectionError("redis down")

    server = SideEffectServer()
    pool, _ = await _make_pool(KB_TOOLS_V1_ONLY, server)
    pool.idempotency = IdempotencyStore(BrokenRedis())

    token = task_id_var.set(5)
    try:
        for _ in range(2):
            result = await pool.call("notify", {"title": "a"})
    finally:
        task_id_var.reset(token)
    assert len(server.executed) == 2
    assert result["status"] == "SENT"


async def test_pool_without_idempotency_unchanged():
    """未接线幂等（默认测试路径 from_apps）→ 行为与升级前逐字节一致。"""
    server = SideEffectServer()
    pool, _ = await _make_pool(KB_TOOLS_V1_ONLY, server)
    assert pool.idempotency is None
    for _ in range(2):
        await pool.call("notify", {"title": "a"})
    assert len(server.executed) == 2
