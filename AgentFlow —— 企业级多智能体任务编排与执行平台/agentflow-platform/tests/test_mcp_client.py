"""MCP Client 与协议底座单测（FR-005 ~ FR-008 / 硬性约束 3）。

覆盖：
1. ``initialize`` + ``tools/list`` 动态发现（工具名不与 server 名硬编码绑定）；
2. ``tools/call`` 成功路径：业务 payload 走 ``structuredContent``；
3. 业务错误 → ``ToolInvocationError`` 且携带 PRD 错误码与 retryable 标记；
4. 协议错误（方法未实现 / JSON-RPC error）→ ``MCPTransportError``；
5. 单 server 不可用不影响其它 server（降级不阻断）；
6. notify 的审批前置闸门：无 APPROVED 记录一律拒绝且不落库。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.schemas.enums import ApprovalStatus, ApprovalType
from app.schemas.mcp import METHOD_NOT_FOUND
from app.schemas.orm_models import Approval
from app.schemas.orm_models_ext import MockMessage
from app.tools.mcp_client import (
    MCPClient,
    MCPClientPool,
    MCPTransport,
    MCPTransportError,
    ToolInvocationError,
)
from app.tools.mcp_stdio import MCPServerApp
from app.tools.mcp_servers import sql_query

pytestmark = pytest.mark.unit


class StubTransport(MCPTransport):
    """按脚本回包的传输层桩（可抛出异常模拟子进程崩溃）。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.calls.append(method)
        item = self._responses.pop(0) if self._responses else {"result": {}}
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------
async def test_discover_registers_all_tools(mcp_pool: MCPClientPool) -> None:
    names = {tool.name for tool in mcp_pool.tools}

    assert names == {"sql-query", "chart-render", "kb-search", "notify"}
    assert mcp_pool.unavailable == {}
    for name in names:
        assert mcp_pool.server_for(name) == name


async def test_tool_def_carries_json_schema(mcp_pool: MCPClientPool) -> None:
    tool = next(item for item in mcp_pool.tools if item.name == "sql-query")

    assert tool.server == "sql-query"
    assert "sql" in (tool.input_schema.get("properties") or {})


async def test_discover_degrades_single_broken_server(mcp_pool: MCPClientPool) -> None:
    broken = MCPClient("broken", StubTransport([RuntimeError("子进程启动失败")]))
    pool = MCPClientPool({"broken": broken, **mcp_pool.clients})

    # 复用已发现的索引，只验证坏 server 被记录而不抛异常
    tools = await pool.discover()

    assert "broken" in pool.unavailable
    assert "子进程启动失败" in pool.unavailable["broken"]
    assert {t.name for t in tools} >= {"sql-query"}
    await pool.aclose()


# ---------------------------------------------------------------------------
# 调用成功
# ---------------------------------------------------------------------------
async def test_call_kb_search_returns_chunks(mcp_pool: MCPClientPool) -> None:
    payload = await mcp_pool.call("kb-search", {"query": "库存周转天数 断货风险", "top_k": 3})

    assert payload["chunks"], "Mock 知识库应命中周报片段"
    assert payload["degraded"] is False
    assert payload["chunks"][0]["doc_id"].startswith("kb-")


async def test_call_sql_query_returns_rows(mcp_pool: MCPClientPool) -> None:
    payload = await mcp_pool.call(
        "sql-query", {"sql": "SELECT category_id, gmv FROM orders", "max_rows": 100}
    )

    assert payload["row_count"] == 3
    assert payload["applied_limit"] == 100


async def test_events_are_emitted(mcp_pool: MCPClientPool) -> None:
    events: list[tuple[str, dict]] = []
    mcp_pool.clients["kb-search"].on_event = lambda name, payload: events.append((name, payload))

    await mcp_pool.call("kb-search", {"query": "库存"})

    names = [name for name, _ in events]
    assert names == ["tool.call", "tool.result"]
    assert events[0][1]["tool"] == "kb-search"
    assert events[1][1]["ok"] is True
    assert events[1][1]["latency_ms"] >= 0


# ---------------------------------------------------------------------------
# 业务错误
# ---------------------------------------------------------------------------
async def test_write_sql_is_rejected_with_business_code(mcp_pool: MCPClientPool) -> None:
    with pytest.raises(ToolInvocationError) as excinfo:
        await mcp_pool.call("sql-query", {"sql": "DELETE FROM orders"})

    assert excinfo.value.code == "SQL_SYNTAX"
    assert excinfo.value.retryable is False  # 确定性错误：不重试


async def test_retryable_error_flag(mcp_pool: MCPClientPool, fake_sql_executor) -> None:
    import asyncio

    fake_sql_executor.raise_exc = asyncio.TimeoutError()

    with pytest.raises(ToolInvocationError) as excinfo:
        await mcp_pool.call("sql-query", {"sql": "SELECT id FROM orders"})

    assert excinfo.value.code == "SQL_TIMEOUT"
    assert excinfo.value.retryable is True  # PRD 4.2：可退避重试


async def test_unknown_tool(mcp_pool: MCPClientPool) -> None:
    with pytest.raises(ToolInvocationError) as excinfo:
        await mcp_pool.call("ghost-tool", {})

    assert excinfo.value.code == "TOOL_NOT_FOUND"


async def test_invalid_arguments_rejected_by_schema(mcp_pool: MCPClientPool) -> None:
    with pytest.raises(ToolInvocationError) as excinfo:
        await mcp_pool.call("sql-query", {})  # 缺 sql

    assert excinfo.value.code == "INVALID_PARAMS"
    assert excinfo.value.retryable is False


# ---------------------------------------------------------------------------
# 协议错误
# ---------------------------------------------------------------------------
async def test_unimplemented_method_returns_method_not_found(fake_sql_executor) -> None:
    app: MCPServerApp = sql_query.build_app(fake_sql_executor)
    response = await app.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}}
    )

    assert response is not None
    assert response["error"]["code"] == METHOD_NOT_FOUND


async def test_jsonrpc_error_raises_transport_error() -> None:
    transport = StubTransport(
        [
            {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "stub"}}},  # initialize
            {"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "内部异常"}},
        ]
    )
    client = MCPClient("stub", transport, timeout=5)

    await client.initialize()
    with pytest.raises(MCPTransportError):
        await client.call_tool("anything", {})


async def test_transport_timeout_surfaces_as_error() -> None:
    import asyncio

    transport = StubTransport([asyncio.TimeoutError()])
    client = MCPClient("stub", transport, timeout=1)

    with pytest.raises(asyncio.TimeoutError):
        await client.initialize()


# ---------------------------------------------------------------------------
# notify 审批闸门
# ---------------------------------------------------------------------------
async def test_notify_blocked_without_approval(
    mcp_pool: MCPClientPool, session_factory
) -> None:
    with pytest.raises(ToolInvocationError) as excinfo:
        await mcp_pool.call(
            "notify",
            {"title": "周报", "content": "内容", "receivers": ["ops"], "task_id": 777},
        )

    assert excinfo.value.code == "APPROVAL_MISSING"
    async with session_factory() as session:
        rows = (await session.execute(select(MockMessage))).scalars().all()
    assert rows == []  # 未落库＝未出网


async def test_notify_sent_after_approval(
    mcp_pool: MCPClientPool, session_factory
) -> None:
    async with session_factory() as session:
        session.add(
            Approval(
                task_id=1,
                type=ApprovalType.HIGH_RISK_TOOL,
                reason="推送运营群",
                status=ApprovalStatus.APPROVED,
                requested_by=0,
            )
        )
        await session.commit()

    payload = await mcp_pool.call(
        "notify",
        {"title": "周报", "content": "GMV 环比 -8%", "receivers": ["ops"], "task_id": 1},
    )

    assert payload["mock"] is True
    assert payload["status"] == "SENT"
    assert payload["message_id"]

    async with session_factory() as session:
        message = (await session.execute(select(MockMessage))).scalar_one()
    assert message.task_id == 1
    assert message.channel == "feishu"
