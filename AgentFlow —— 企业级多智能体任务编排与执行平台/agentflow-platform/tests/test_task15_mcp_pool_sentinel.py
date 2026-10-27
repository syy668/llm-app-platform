"""任务 B：MCP 子进程生命周期加固。

1. 健康哨兵：只读工具遭遇 stdio 子进程瞬断（EOF/管道断裂 → MCP_PROTOCOL_ERROR）时，
   自动重启子进程一次并重试原请求；notify（有外发副作用）不自动重试；
   重启后仍失败则原样抛出；MCP_TIMEOUT 是超时语义，不触发重启。
2. Pool 单例：get_shared_pool() 全进程只建一次；aclose 共享池后单例摘除、可重建；
   create_default 仅被单例路径调用一次。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.tools.mcp_client import (
    MCPClient,
    MCPClientPool,
    MCPTransport,
    MCPTransportError,
    McpErrorCode,
    get_shared_pool,
)


class ScriptedTransport(MCPTransport):
    """脚本化 stdio 传输：可控地在前 N 次 tools/call 抛「子进程已退出」。"""

    def __init__(self, *, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.starts = 0
        self.closes = 0
        self.tool_calls = 0
        self.initializes = 0

    async def start(self) -> None:
        self.starts += 1

    async def close(self) -> None:
        self.closes += 1

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if method == "initialize":
            self.initializes += 1
            return {
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "scripted", "version": "0.0.0"},
                }
            }
        if method == "tools/list":
            return {"result": {"tools": [{"name": "sql-query", "description": "t", "inputSchema": {}}]}}
        # tools/call
        self.tool_calls += 1
        if self.tool_calls <= self.fail_times:
            raise MCPTransportError("MCP Server 已退出（stdout EOF）")
        payload = {"columns": ["channel"], "rows": [["direct"]]}
        return {
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "structuredContent": payload,
            }
        }


async def _client(transport: MCPTransport) -> MCPClient:
    client = MCPClient("sql-query", transport, timeout=2.0)
    await client.start()
    return client


# ---------------------------------------------------------------------------
# 1. 健康哨兵
# ---------------------------------------------------------------------------
async def test_sentinel_restarts_and_retries_readonly_tool() -> None:
    """只读工具瞬断 → 自动重启一次 → 原请求重试成功。"""
    transport = ScriptedTransport(fail_times=1)
    client = await _client(transport)

    payload = await client.call_tool("sql-query", {"sql": "SELECT 1"})

    assert payload == {"columns": ["channel"], "rows": [["direct"]]}
    assert transport.tool_calls == 2, "首次失败 + 重试一次"
    assert transport.closes == 1 and transport.starts == 2, "重启 = close + start"
    assert transport.initializes == 2, "重启后重新握手 initialize"


async def test_sentinel_never_retries_notify() -> None:
    """notify 有真实外发副作用——瞬断也直接抛给 Executor，由其重试矩阵决定。"""
    transport = ScriptedTransport(fail_times=1)
    client = await _client(transport)

    with pytest.raises(MCPTransportError):
        await client.call_tool("notify", {"channel": "feishu"})

    assert transport.tool_calls == 1
    assert transport.starts == 1, "不允许自动重启"


async def test_sentinel_gives_up_when_restart_fails() -> None:
    """重启后子进程仍然秒退 → 只重启一次，第二次异常原样抛出。"""
    transport = ScriptedTransport(fail_times=99)
    client = await _client(transport)

    with pytest.raises(MCPTransportError):
        await client.call_tool("sql-query", {"sql": "SELECT 1"})

    assert transport.starts == 2, "只允许一次自动重启"
    assert transport.tool_calls == 2


async def test_sentinel_ignores_timeout_errors() -> None:
    """MCP_TIMEOUT 是超时语义而非进程死亡，不触发重启重试。"""
    transport = ScriptedTransport()
    client = await _client(transport)

    calls = {"n": 0}
    original_request = transport.request

    async def boom(method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if method == "tools/call":
            calls["n"] += 1
            raise MCPTransportError("MCP 调用超时（tools/call, 8s）", McpErrorCode.MCP_TIMEOUT.value)
        return await original_request(method, params, timeout)

    transport.request = boom  # type: ignore[method-assign]
    with pytest.raises(MCPTransportError):
        await client.call_tool("sql-query", {"sql": "SELECT 1"})

    assert calls["n"] == 1, "超时只原样抛出，不重启重试"
    assert transport.starts == 1


async def test_sentinel_success_path_single_call() -> None:
    """正常路径零开销：不重启、只调用一次。"""
    transport = ScriptedTransport(fail_times=0)
    client = await _client(transport)

    await client.call_tool("sql-query", {"sql": "SELECT 1"})

    assert transport.tool_calls == 1
    assert transport.starts == 1 and transport.closes == 0


# ---------------------------------------------------------------------------
# 2. 共享池单例
# ---------------------------------------------------------------------------
class _FakePool(MCPClientPool):
    """不走真子进程的桩池：记录 create/discover/aclose 次数。"""

    instances: list["_FakePool"] = []

    def __init__(self) -> None:
        super().__init__(clients={})
        self.discovered = 0
        self.closed = 0
        _FakePool.instances.append(self)

    async def discover(self) -> list[Any]:
        self.discovered += 1
        return []

    async def aclose(self) -> None:
        self.closed += 1
        await super().aclose()


@pytest.fixture()
def _reset_shared_pool():
    import app.tools.mcp_client as m

    m._shared_pool = None
    m._shared_pool_lock = None
    yield
    m._shared_pool = None
    m._shared_pool_lock = None


async def test_shared_pool_is_process_wide_singleton(_reset_shared_pool, monkeypatch) -> None:
    """两次 get_shared_pool 返回同一实例，create_default 只发生一次。"""
    import app.tools.mcp_client as m

    _FakePool.instances.clear()
    created = {"n": 0}

    async def fake_create_default(*, on_event: Any = None) -> MCPClientPool:
        created["n"] += 1
        return _FakePool()

    monkeypatch.setattr(m.MCPClientPool, "create_default", staticmethod(fake_create_default))

    pool_a = await get_shared_pool()
    pool_b = await get_shared_pool()
    assert pool_a is pool_b
    assert created["n"] == 1
    assert pool_a.discovered == 1
    assert len(_FakePool.instances) == 1


async def test_shared_pool_rebuilds_after_aclose(_reset_shared_pool, monkeypatch) -> None:
    """共享池被 aclose（reset_runtime 语义）→ 单例摘除 → 下次重建新池。"""
    import app.tools.mcp_client as m

    async def fake_create_default(*, on_event: Any = None) -> MCPClientPool:
        return _FakePool()

    monkeypatch.setattr(m.MCPClientPool, "create_default", staticmethod(fake_create_default))

    pool_a = await get_shared_pool()
    await pool_a.aclose()
    assert m._shared_pool is None, "aclose 后单例引用必须摘除"

    pool_b = await get_shared_pool()
    assert pool_b is not pool_a


async def test_shared_pool_concurrent_calls_create_once(_reset_shared_pool, monkeypatch) -> None:
    """并发首次调用只创建一次（锁生效）。"""
    import asyncio

    import app.tools.mcp_client as m

    created = {"n": 0}

    async def fake_create_default(*, on_event: Any = None) -> MCPClientPool:
        created["n"] += 1
        await asyncio.sleep(0.01)
        return _FakePool()

    monkeypatch.setattr(m.MCPClientPool, "create_default", staticmethod(fake_create_default))

    pools = await asyncio.gather(*(get_shared_pool() for _ in range(5)))
    assert len({id(p) for p in pools}) == 1
    assert created["n"] == 1
