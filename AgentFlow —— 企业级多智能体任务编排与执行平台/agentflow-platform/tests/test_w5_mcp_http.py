"""W5-Part1 任务 1 单元测试：MCP HTTP 传输（与 stdio 并存）。

**新增用例**，不改动 ``tests/test_mcp_client.py``（stdio 基线）任何一行。

覆盖点（对应任务书「新增 pytest 单元测试：HTTP 客户端发现工具、调用 sql-query、异常错误码返回」）：
1. HTTP 客户端初始化 / 动态发现工具 / 调用 sql-query；
2. 异常错误码：JSON-RPC ``-32601``、``INVALID_PARAMS``、工具业务错误 ``isError``；
3. 鉴权、通知 202、批量请求、SSE 长会话；
4. 连接池 + 熔断；
5. 同一个 ``MCPServerApp`` 实例同时被 stdio（InMemoryTransport）与 HTTP 服务。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.mcp.transport_http import (
    API_KEY_HEADER,
    MCP_HTTP_CIRCUIT_OPEN,
    HttpSessionRegistry,
    HttpTransport,
    build_mcp_http_router,
    create_http_pool,
    create_mcp_http_app,
    discover_tools_http,
    extract_api_key,
    is_authorized,
    load_server_app,
    sse_event_stream,
)
from app.tools.kb_adapter import CircuitBreaker
from app.tools.mcp_client import MCPClient, MCPTransportError
from app.tools.mcp_client import InMemoryTransport
from app.tools.mcp_servers import sql_query

API_KEY = "w5-test-key"


# ---------------------------------------------------------------------------
# 夹具：一份 Server 实例 + 两套传输
# ---------------------------------------------------------------------------
@pytest.fixture
def sql_app(fake_sql_executor) -> object:
    """同一个 ``MCPServerApp`` 实例（stdio 与 HTTP 共用）。"""
    return sql_query.build_app(fake_sql_executor)


@pytest.fixture
def http_app(sql_app):
    return create_mcp_http_app(sql_app, api_key=API_KEY)


@pytest.fixture
async def http_client(http_app):
    async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://testserver") as client:
        yield client


def make_transport(base_url: str = "http://testserver", *, api_key: str | None = API_KEY, **kwargs) -> HttpTransport:
    """复用调用方的 httpx 客户端（ASGITransport 直连，不占端口）。"""
    return HttpTransport(base_url, server_name="sql-query", api_key=api_key, **kwargs)


async def call_rpc(client: AsyncClient, payload: dict, *, headers: dict | None = None, path: str = "/mcp/v1"):
    merged = {API_KEY_HEADER: API_KEY, **(headers or {})}
    return await client.post(path, json=payload, headers=merged)


# ---------------------------------------------------------------------------
# 1) 协议三方法
# ---------------------------------------------------------------------------
async def test_http_initialize_negotiates_protocol(http_client: AsyncClient) -> None:
    resp = await call_rpc(
        http_client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "pytest"}},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1
    assert body["result"]["protocolVersion"] == "2024-11-05"
    assert body["result"]["serverInfo"]["name"] == "sql-query"


async def test_http_tools_list_returns_input_schema(http_client: AsyncClient) -> None:
    resp = await call_rpc(http_client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = resp.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["sql-query"]
    schema = tools[0]["inputSchema"]
    assert "sql" in schema["properties"]
    assert schema["required"] == ["sql"]


async def test_http_client_discovers_tools_and_calls_sql_query(http_app) -> None:
    """任务书要求：HTTP 客户端「动态发现工具 + 调用 sql-query」。"""
    async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://testserver") as raw:
        transport = make_transport(client=raw)
        client = MCPClient("sql-query", transport, timeout=5)
        await client.start()

        info = await client.initialize()
        assert info["serverInfo"]["name"] == "sql-query"

        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["sql-query"]
        assert tools[0].server == "sql-query"

        result = await client.call_tool("sql-query", {"sql": "SELECT gmv FROM orders LIMIT 3"})
        assert result["row_count"] == 3
        assert result["columns"] == ["category_id", "gmv", "order_cnt"]
        await client.close()


async def test_http_notification_returns_202(http_client: AsyncClient) -> None:
    resp = await call_rpc(http_client, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    assert resp.status_code == 202
    assert not resp.content


# ---------------------------------------------------------------------------
# 2) 异常错误码
# ---------------------------------------------------------------------------
async def test_http_method_not_found_returns_32601(http_client: AsyncClient) -> None:
    resp = await call_rpc(http_client, {"jsonrpc": "2.0", "id": 7, "method": "resources/list", "params": {}})
    body = resp.json()
    assert body["error"]["code"] == -32601
    assert "resources/list" in body["error"]["message"]


async def test_http_unknown_tool_returns_is_error(http_client: AsyncClient) -> None:
    resp = await call_rpc(
        http_client,
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "no-such-tool", "arguments": {}}},
    )
    result = resp.json()["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["code"] == "TOOL_NOT_FOUND"


async def test_http_arguments_must_be_object(http_client: AsyncClient) -> None:
    resp = await call_rpc(
        http_client,
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "sql-query", "arguments": "oops"}},
    )
    assert resp.json()["error"]["code"] == -32602


async def test_http_sql_dml_rejected_with_business_code(http_client: AsyncClient) -> None:
    """工具业务错误必须走 ``isError`` + 业务错误码（与 stdio 语义完全一致）。"""
    resp = await call_rpc(
        http_client,
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "sql-query", "arguments": {"sql": "DELETE FROM orders"}},
        },
    )
    body = resp.json()
    assert body["result"]["isError"] is True
    assert json.loads(body["result"]["content"][0]["text"])["code"] == "SQL_SYNTAX"


async def test_http_parse_error_returns_32700(http_client: AsyncClient) -> None:
    resp = await http_client.post(
        "/mcp/v1",
        content=b"{not-json",
        headers={API_KEY_HEADER: API_KEY, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700


async def test_http_batch_request(http_client: AsyncClient) -> None:
    resp = await call_rpc(
        http_client,
        [
            {"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 12, "method": "ping", "params": {}},
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and len(body) == 2
    assert {item["id"] for item in body} == {11, 12}


# ---------------------------------------------------------------------------
# 3) 鉴权
# ---------------------------------------------------------------------------
async def test_http_requires_api_key(http_client: AsyncClient) -> None:
    resp = await http_client.post("/mcp/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == -32001


async def test_http_accepts_bearer_header(http_client: AsyncClient) -> None:
    resp = await http_client.post(
        "/mcp/v1",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    assert resp.status_code == 200


async def test_http_rejects_wrong_key(http_client: AsyncClient) -> None:
    resp = await http_client.post(
        "/mcp/v1",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={API_KEY_HEADER: "wrong"},
    )
    assert resp.status_code == 401


def test_is_authorized_and_key_extraction() -> None:
    assert is_authorized("", "") is True  # 未配置 key → 免鉴权
    assert is_authorized("abc", "abc") is True
    assert is_authorized("abc", "abd") is False

    class _Req:  # 最小替身，只验证优先级
        def __init__(self, headers, query):
            self.headers = headers
            self.query_params = query

    req = _Req({API_KEY_HEADER: "k1", "Authorization": "Bearer k2"}, {"api_key": "k3"})
    assert extract_api_key(req) == "k1"
    req2 = _Req({"Authorization": "Bearer k2"}, {"api_key": "k3"})
    assert extract_api_key(req2) == "k2"
    req3 = _Req({}, {"api_key": "k3"})
    assert extract_api_key(req3) == "k3"


# ---------------------------------------------------------------------------
# 4) SSE 长会话
# ---------------------------------------------------------------------------
async def test_sse_handshake_returns_endpoint_and_recycles_session(sql_app) -> None:
    """GET /sse：首帧下发 endpoint；流结束后会话自动回收。"""
    registry = HttpSessionRegistry()
    router = build_mcp_http_router({"sql-query": sql_app}, api_key=API_KEY, sessions=registry)
    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/mcp/v1/sse", params={"api_key": API_KEY, "max_seconds": 1})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        session_id = resp.headers["mcp-session-id"]
        assert "event: endpoint" in resp.text
        assert f"session_id={session_id}" in resp.text
        assert "event: close" in resp.text

    assert registry.get(session_id) is None  # 已回收


async def test_sse_session_roundtrip(sql_app) -> None:
    """长会话：endpoint 帧 → POST /messages 投递 → 响应经 SSE ``message`` 帧回流。

    说明：``ASGITransport`` 会把响应体整体缓冲后才返回，无法在客户端侧做「边读边发」，
    因此这里直接消费路由用的同一个生成器 ``sse_event_stream``（生产代码路径一致）。
    """
    registry = HttpSessionRegistry()
    router = build_mcp_http_router({"sql-query": sql_app}, api_key=API_KEY, sessions=registry)
    app = FastAPI()
    app.include_router(router)

    session = registry.create("sql-query")
    stream = sse_event_stream(
        session,
        endpoint=f"/mcp/v1/messages?session_id={session.session_id}&server=sql-query",
        ttl_seconds=10,
        registry=registry,
        heartbeat_seconds=0.2,
    )
    first = await asyncio.wait_for(stream.__anext__(), timeout=5)
    assert "event: endpoint" in first

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        accepted = await client.post(
            "/mcp/v1/messages",
            params={"session_id": session.session_id},
            json={"jsonrpc": "2.0", "id": 21, "method": "tools/list", "params": {}},
            headers={API_KEY_HEADER: API_KEY},
        )
        assert accepted.status_code == 202

    message: dict = {}
    for _ in range(20):
        frame = await asyncio.wait_for(stream.__anext__(), timeout=5)
        if frame.startswith("event: message"):
            message = json.loads(frame.split("data: ", 1)[1].strip())
            break
    await stream.aclose()

    assert message["id"] == 21
    assert message["result"]["tools"][0]["name"] == "sql-query"


async def test_sse_unknown_session_returns_404(http_client: AsyncClient) -> None:
    resp = await http_client.post(
        "/mcp/v1/messages",
        params={"session_id": "deadbeefdeadbeef"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={API_KEY_HEADER: API_KEY},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MCP_HTTP_SESSION_MISSING"


def test_session_registry_ttl_and_capacity() -> None:
    registry = HttpSessionRegistry(max_sessions=2, ttl_seconds=60)
    first = registry.create("sql-query")
    second = registry.create("sql-query")
    assert len(registry) == 2
    third = registry.create("sql-query")  # 超容量 → 淘汰最老
    assert len(registry) == 2
    assert registry.get(first.session_id) is None
    assert registry.get(third.session_id) is third

    # 过期即失效
    third.created_at -= 120
    assert registry.get(third.session_id) is None
    assert second.session_id  # 保留引用避免未使用告警


# ---------------------------------------------------------------------------
# 5) 连接池 / 熔断 / 兼容模式
# ---------------------------------------------------------------------------
async def test_http_pool_discovers_and_routes(http_app) -> None:
    async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://testserver") as raw:
        pool = create_http_pool(["sql-query"], api_key=API_KEY, forward_server=False)
        client = pool.clients["sql-query"]
        client.transport._client = raw  # 复用 ASGI 直连，不占端口
        tools = await pool.discover()
        assert [tool.name for tool in tools] == ["sql-query"]
        assert pool.server_for("sql-query") == "sql-query"
        result = await pool.call("sql-query", {"sql": "SELECT 1 FROM orders LIMIT 1"})
        assert result["row_count"] == 3  # FakeSqlExecutor 固定返回 3 行
        await pool.aclose()


async def test_http_transport_circuit_breaker_opens() -> None:
    transport = HttpTransport(
        "http://127.0.0.1:9",  # 必然拒绝连接
        server_name="sql-query",
        retry_times=0,
        breaker=CircuitBreaker(name="w5-test", failure_threshold=2, recovery_seconds=30),
    )
    await transport.start()
    for _ in range(2):
        with pytest.raises(MCPTransportError):
            await transport.request("tools/list", {}, timeout=1)
    assert transport.breaker.state == "open"

    with pytest.raises(MCPTransportError) as exc:
        await transport.request("tools/list", {}, timeout=1)
    assert exc.value.code == MCP_HTTP_CIRCUIT_OPEN
    await transport.close()


async def test_transport_not_started_raises() -> None:
    transport = HttpTransport("http://127.0.0.1:9", server_name="sql-query")
    with pytest.raises(MCPTransportError) as exc:
        await transport.request("tools/list", {}, timeout=1)
    assert exc.value.code == "MCP_HTTP_NOT_READY"


async def test_same_instance_serves_both_transports(sql_app, fake_sql_executor) -> None:
    """兼容模式验收：同一 ``MCPServerApp`` 既走 stdio 契约也走 HTTP，工具表完全一致。"""
    stdio_client = MCPClient("sql-query", InMemoryTransport(sql_app, server_name="sql-query"))
    tools_stdio = await stdio_client.list_tools()
    stdio_result = await stdio_client.call_tool("sql-query", {"sql": "SELECT 1 FROM orders LIMIT 1"})

    http_app = create_mcp_http_app(sql_app, api_key=API_KEY)
    async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://testserver") as raw:
        http_client = MCPClient("sql-query", make_transport(client=raw))
        tools_http = await http_client.list_tools()
        http_result = await http_client.call_tool("sql-query", {"sql": "SELECT 1 FROM orders LIMIT 1"})
        await http_client.close()

    assert [t.name for t in tools_stdio] == [t.name for t in tools_http]
    assert stdio_result.keys() == http_result.keys()
    assert stdio_result["row_count"] == http_result["row_count"] == 3
    assert fake_sql_executor.calls  # 两次调用都真的落到执行器


async def test_load_server_app_returns_real_business_tools() -> None:
    """``load_server_app`` 直接复用 4 个 Server 的 ``build_app``（不包壳、不改工具）。"""
    for name, tool in (
        ("sql-query", "sql-query"),
        ("kb-search", "kb-search"),
        ("notify", "notify"),
    ):
        app = load_server_app(name)
        assert app.name == name
        assert [t.name for t in app.tools] == [tool]


async def test_unknown_server_query_is_rejected(http_client: AsyncClient) -> None:
    """多 Server 路由：未知 ``?server=`` 明确回 -32600，而不是静默落到默认 Server。"""
    resp = await call_rpc(
        http_client, {"jsonrpc": "2.0", "id": 33, "method": "tools/list"}, path="/mcp/v1?server=bogus"
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32600


def test_router_rejects_empty_server_apps() -> None:
    with pytest.raises(ValueError):
        build_mcp_http_router({}, api_key=API_KEY)


async def test_discover_tools_http_helper(http_app) -> None:
    """便捷函数：连远端 → 发现工具 → 立即关闭（不泄漏连接）。"""
    original = HttpTransport.__init__

    def patched(self, *args, **kwargs):  # noqa: ANN001 - 测试内联补丁
        kwargs["client"] = AsyncClient(transport=ASGITransport(app=http_app), base_url="http://testserver")
        original(self, *args, **kwargs)

    HttpTransport.__init__ = patched  # type: ignore[method-assign]
    try:
        tools = await discover_tools_http("http://testserver", server_name="sql-query", api_key=API_KEY)
    finally:
        HttpTransport.__init__ = original  # type: ignore[method-assign]
    assert [tool.name for tool in tools] == ["sql-query"]
