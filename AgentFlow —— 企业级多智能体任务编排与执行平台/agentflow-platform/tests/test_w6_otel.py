"""W5-Part2 新增用例：OpenTelemetry 全链路埋点。

覆盖：
- 开关关闭时**零副作用**（不 patch、不建 span）；
- FastAPI 中间件：正常路由建 span、探针路径排除、5xx 标记 ERROR；
- LangGraph：节点 span（每个节点一个）+ graph.run 根 span + task.id/goal 属性；
- MCP：stdio/HTTP 共用 ``call_tool``，span 属性含工具名 / 服务名 / 错误码，指标计数；
- LLM：token 消耗计数与模型属性；
- 指标：Prometheus 文本格式、P95 计算。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.w6_config import w6
from app.observability import otel_tracing as T

pytestmark = pytest.mark.unit

otel_sdk = pytest.importorskip("opentelemetry.sdk", reason="需要 opentelemetry-sdk（requirements-observability.txt）")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode  # noqa: E402


@pytest.fixture
def otel(monkeypatch: pytest.MonkeyPatch):
    """打开埋点 + 注入内存 exporter，结束后完整还原。"""
    monkeypatch.setattr(w6, "ENABLE_OTEL", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_HTTP", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_GRAPH", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_MCP", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_LLM", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_SQL", True)

    exporter = InMemorySpanExporter()
    assert T.init_tracing(span_exporter=exporter) is True
    assert T.install_instrumentation() is True
    T.metrics.reset()
    try:
        yield exporter
    finally:
        T._reset_for_tests()


@pytest.fixture
def otel_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(w6, "ENABLE_OTEL", False)
    T._reset_for_tests()
    yield
    T._reset_for_tests()


def _names(exporter: InMemorySpanExporter) -> list[str]:
    return [span.name for span in exporter.get_finished_spans()]


# ---------------------------------------------------------------------------
# 1) 开关语义
# ---------------------------------------------------------------------------
def test_install_is_noop_when_disabled(otel_off: None) -> None:
    from app.tools.mcp_client import MCPClient

    assert T.install_instrumentation() is False
    assert T.tracing_enabled() is False
    assert getattr(MCPClient, "_otel_patched", False) is False


def test_span_helper_is_noop_when_disabled(otel_off: None) -> None:
    with T.span("should_not_record", attributes={"x": 1}) as active:
        assert active.is_recording() is False
    assert T.current_trace_id() == ""


def test_enable_produces_real_spans(otel: InMemorySpanExporter) -> None:
    with T.span("manual.span", attributes={"demo": "yes"}) as active:
        assert active.is_recording() is True
    spans = otel.get_finished_spans()
    assert [s.name for s in spans] == ["manual.span"]
    assert spans[0].attributes["demo"] == "yes"


# ---------------------------------------------------------------------------
# 2) HTTP 中间件
# ---------------------------------------------------------------------------
async def test_http_middleware_spans_and_skips_probes(otel: InMemorySpanExporter) -> None:
    from app.core.tracing_middleware import OtelTracingMiddleware

    app = FastAPI()
    app.add_middleware(OtelTracingMiddleware, exclude_paths=("/health",))

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "1"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("boom")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/ping")).status_code == 200
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/boom")).status_code == 500

    spans = {s.name: s for s in otel.get_finished_spans()}
    assert "GET /ping" in spans
    assert "GET /health" not in spans  # 探针被排除
    assert spans["GET /ping"].attributes["http.response.status_code"] == 200
    error_span = next(s for s in otel.get_finished_spans() if s.name.startswith("GET /boom"))
    assert error_span.status.status_code == StatusCode.ERROR
    assert T.metrics.total("http_requests_total") >= 3


async def test_http_middleware_sets_trace_headers(otel: InMemorySpanExporter) -> None:
    from app.core.tracing_middleware import SPAN_ID_HEADER, TRACE_ID_HEADER, OtelTracingMiddleware

    app = FastAPI()
    app.add_middleware(OtelTracingMiddleware, exclude_paths=())

    @app.get("/trace")
    async def trace() -> dict[str, str]:
        return {"ok": "1"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/trace")

    assert response.headers[TRACE_ID_HEADER]
    assert response.headers[SPAN_ID_HEADER]


# ---------------------------------------------------------------------------
# 3) LangGraph 节点 span
# ---------------------------------------------------------------------------
async def test_graph_nodes_each_produce_one_span(otel: InMemorySpanExporter, w2_graph: Any) -> None:
    await w2_graph.ainvoke(
        {
            "goal": "分析上周数码配件 GMV 下滑原因",
            "time_range": {"start": "2026-10-13", "end": "2026-10-19"},
            "task_id": 880001,
        },
        config={"configurable": {"thread_id": "otel-graph-880001"}},
    )

    spans = otel.get_finished_spans()
    node_spans = [s for s in spans if s.name.startswith("graph.node ")]
    node_names = sorted({s.attributes["langgraph.node"] for s in node_spans})

    # 7 节点图在本地规则评审下至少经过 planner / executor / reviewer / summarizer
    assert {"planner", "executor", "reviewer", "summarizer"} <= set(node_names)
    # 每个节点只应有一个 span（回调内外两层已去重）
    for name in node_names:
        assert len([s for s in node_spans if s.attributes["langgraph.node"] == name]) == 1, name

    roots = [s for s in spans if s.name == "graph.run"]
    assert len(roots) == 1  # ainvoke → astream 内部调用不会重复开根 span
    assert roots[0].attributes["task.id"] == 880001
    assert "数码配件" in roots[0].attributes["graph.goal"]

    # 节点 span 应挂在 graph.run 之下
    root_span_id = roots[0].context.span_id
    assert all(s.parent is not None and s.parent.span_id == root_span_id for s in node_spans)
    assert T.metrics.histogram_values("graph_node_duration_seconds")


async def test_graph_span_absent_when_disabled(otel_off: None, w2_graph: Any) -> None:
    await w2_graph.ainvoke(
        {
            "goal": "分析上周数码配件 GMV 下滑原因",
            "time_range": {"start": "2026-10-13", "end": "2026-10-19"},
            "task_id": 880002,
        },
        config={"configurable": {"thread_id": "otel-graph-880002"}},
    )
    assert T.tracing_enabled() is False


# ---------------------------------------------------------------------------
# 4) MCP 调用 span
# ---------------------------------------------------------------------------
async def test_mcp_call_span_and_metrics(otel: InMemorySpanExporter, fake_sql_executor: Any) -> None:
    from app.tools.mcp_client import InMemoryTransport, MCPClient
    from app.tools.mcp_servers import sql_query

    client = MCPClient("sql-query", InMemoryTransport(sql_query.build_app(fake_sql_executor)))
    await client.start()
    try:
        result = await client.call_tool("sql-query", {"sql": "SELECT 1 FROM orders LIMIT 1"})
    finally:
        await client.close()

    assert result is not None
    spans = [s for s in otel.get_finished_spans() if s.name.startswith("mcp.tools/call")]
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["mcp.server"] == "sql-query"
    assert attrs["mcp.tool.name"] == "sql-query"
    assert attrs["rpc.system"] == "mcp"
    assert attrs["rpc.method"] == "tools/call"
    assert attrs["mcp.transport"] == "InMemoryTransport"
    assert T.metrics.total("mcp_calls_total") == 1
    assert T.metrics.percentile("mcp_call_duration_seconds", 0.95) > 0


async def test_mcp_error_span_records_code(otel: InMemorySpanExporter, fake_sql_executor: Any) -> None:
    from app.tools.mcp_client import InMemoryTransport, MCPClient, ToolInvocationError
    from app.tools.mcp_servers import sql_query

    client = MCPClient("sql-query", InMemoryTransport(sql_query.build_app(fake_sql_executor)))
    await client.start()
    try:
        with pytest.raises(ToolInvocationError):
            await client.call_tool("sql-query", {"sql": "DELETE FROM orders"})
    finally:
        await client.close()

    spans = [s for s in otel.get_finished_spans() if s.name.startswith("mcp.tools/call")]
    assert len(spans) == 1
    assert spans[0].attributes["mcp.error.code"]
    assert spans[0].status.status_code == StatusCode.ERROR
    assert T.metrics.total("mcp_calls_total") == 1


# ---------------------------------------------------------------------------
# 5) LLM 调用 span 与 token 指标
# ---------------------------------------------------------------------------
async def test_llm_span_records_tokens(otel: InMemorySpanExporter, mock_llm: Any) -> None:
    response = await mock_llm.complete([{"role": "user", "content": "请给出上周 GMV 结论"}])

    spans = [s for s in otel.get_finished_spans() if s.name == "gen_ai.chat"]
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == response.model
    assert attrs["gen_ai.usage.total_tokens"] >= 0
    assert T.metrics.total("llm_calls_total") == 1
    assert T.metrics.total("llm_tokens_total") == attrs["gen_ai.usage.total_tokens"]


# ---------------------------------------------------------------------------
# 6) 指标导出
# ---------------------------------------------------------------------------
def test_metrics_render_prometheus_text(otel: None) -> None:  # noqa: ARG001 - 仅用其副作用
    T.metrics.inc("mcp_calls_total", labels={"server": "sql-query", "tool": "sql-query", "status": "ok"})
    T.metrics.observe("mcp_call_duration_seconds", 0.2, {"server": "sql-query", "tool": "sql-query"})
    T.metrics.observe("mcp_call_duration_seconds", 0.6, {"server": "sql-query", "tool": "sql-query"})

    text = T.render_metrics()
    assert "# TYPE agentflow_mcp_calls_total counter" in text
    assert 'agentflow_mcp_calls_total{server="sql-query",status="ok",tool="sql-query"} 1' in text
    assert "agentflow_mcp_call_duration_seconds_count" in text
    assert 'quantile="0.95"' in text
    assert T.metrics.percentile("mcp_call_duration_seconds", 0.95, {"server": "sql-query", "tool": "sql-query"}) == 0.6


def test_status_reports_configuration(otel: None) -> None:  # noqa: ARG001
    payload = T.status()
    assert payload["enabled"] is True
    assert payload["instrumented"] is True
    assert payload["sdk_available"] is True
    assert payload["trace_targets"]["graph"] is True


# ---------------------------------------------------------------------------
# 7) 卸载还原
# ---------------------------------------------------------------------------
def test_uninstall_restores_patches(otel: None) -> None:  # noqa: ARG001
    from app.agents.llm import LLMClient
    from app.tools.mcp_client import MCPClient

    T.uninstall_instrumentation()
    assert getattr(MCPClient, "_otel_patched", False) is False
    assert getattr(LLMClient, "_otel_patched", False) is False
