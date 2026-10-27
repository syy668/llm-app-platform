"""W5-Part2 新增用例（补充）：把新增模块的分支覆盖补齐到 85% 以上。

这里覆盖的是「主链路之外的分支」：
- OTel exporter 选择与降级（OTLP 可达 / 不可达 / 控制台 / fallback 包装器）；
- 图执行的 ``astream`` / ``astream_events`` 两条路径与回调去重、异常收尾；
- 会话后端的降级分支、容量淘汰、客户端关停、诊断长度刷新；
- 生产入口的 lifespan 包装、``/ready`` 会话后端检查；
- 配置对象的派生属性与校验器。
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from app.core.w6_config import W6Settings, w6
from app.observability import otel_tracing as T

pytestmark = pytest.mark.unit

otel_sdk = pytest.importorskip("opentelemetry.sdk", reason="需要 opentelemetry-sdk")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)


@pytest.fixture
def otel(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(w6, "ENABLE_OTEL", True)
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


# ---------------------------------------------------------------------------
# 1) exporter 选择 / 降级
# ---------------------------------------------------------------------------
def _listening_port() -> tuple[socket.socket, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server, server.getsockname()[1]


def test_probe_otlp_true_when_port_open() -> None:
    server, port = _listening_port()
    try:
        assert T._probe_otlp(f"http://127.0.0.1:{port}") is True
    finally:
        server.close()


def test_probe_otlp_false_when_unreachable() -> None:
    server, port = _listening_port()
    server.close()  # 端口立刻关闭 → 连接被拒
    assert T._probe_otlp(f"http://127.0.0.1:{port}", timeout=0.2) is False


def test_build_exporter_console_and_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w6, "OTEL_EXPORTER", "console")
    exporter, name = T._build_exporter()
    assert name == "console"
    assert type(exporter).__name__ == "ConsoleSpanExporter"

    monkeypatch.setattr(w6, "OTEL_EXPORTER", "none")
    exporter, name = T._build_exporter()
    assert exporter is None
    assert name == "none"


def test_build_exporter_otlp_falls_back_to_console(monkeypatch: pytest.MonkeyPatch) -> None:
    server, port = _listening_port()
    server.close()
    monkeypatch.setattr(w6, "OTEL_EXPORTER", "otlp")
    monkeypatch.setattr(w6, "OTEL_AUTO_FALLBACK_CONSOLE", True)
    monkeypatch.setattr(w6, "OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{port}")

    exporter, name = T._build_exporter()
    assert name == "console(fallback)"
    assert type(exporter).__name__ == "ConsoleSpanExporter"


def test_build_exporter_otlp_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    server, port = _listening_port()
    try:
        monkeypatch.setattr(w6, "OTEL_EXPORTER", "otlp")
        monkeypatch.setattr(w6, "OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{port}")
        exporter, name = T._build_exporter()
        assert name == "otlp"
        assert type(exporter).__name__ == "_FallbackExporter"
    finally:
        server.close()


def test_build_exporter_otlp_silent_when_fallback_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    server, port = _listening_port()
    server.close()
    monkeypatch.setattr(w6, "OTEL_EXPORTER", "otlp")
    monkeypatch.setattr(w6, "OTEL_AUTO_FALLBACK_CONSOLE", False)
    monkeypatch.setattr(w6, "OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{port}")

    exporter, name = T._build_exporter()
    assert exporter is None
    assert name == "none"


def test_fallback_exporter_switches_to_console_after_failures() -> None:
    class _Boom:
        def __init__(self) -> None:
            self.calls = 0

        def export(self, spans: Any) -> Any:
            self.calls += 1
            raise RuntimeError("collector down")

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 0) -> bool:
            return True

    inner = _Boom()
    wrapper = T._FallbackExporter(inner, threshold=2)

    assert wrapper.export([]) is None  # 第 1 次失败：吞掉
    assert wrapper.export([]) is not None  # 第 2 次失败：切控制台
    assert wrapper.export([]) is not None  # 之后直接走控制台
    assert inner.calls == 2
    assert wrapper.force_flush(1000) is True
    wrapper.shutdown()


# ---------------------------------------------------------------------------
# 2) tracer 生命周期与空实现
# ---------------------------------------------------------------------------
def test_init_tracing_returns_false_when_disabled(otel_off: None) -> None:
    assert T.init_tracing() is False


def test_init_tracing_without_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    T._reset_for_tests()
    monkeypatch.setattr(w6, "ENABLE_OTEL", True)
    monkeypatch.setattr(T, "HAVE_OTEL_SDK", False)
    try:
        assert T.init_tracing(force=True) is False
    finally:
        monkeypatch.undo()
        T._reset_for_tests()


def test_null_tracer_and_span(otel_off: None) -> None:
    tracer = T.get_tracer()
    span = tracer.start_span("noop")
    for method in ("set_attribute", "set_attributes", "add_event", "record_exception", "set_status", "end"):
        getattr(span, method)("x") if method in {"set_attribute"} else getattr(span, method)()
    assert span.is_recording() is False
    assert span.get_span_context() is None

    with T.get_tracer().start_as_current_span("noop2") as active:
        assert active.is_recording() is False


def test_current_span_ids_inside_span(otel: InMemorySpanExporter) -> None:
    with T.span("ids"):
        assert len(T.current_trace_id()) == 32
        assert len(T.current_span_id()) == 16
    assert len(otel.get_finished_spans()) == 1


def test_record_span_exception_on_null_span(otel_off: None) -> None:
    with T.span("null") as active:
        T.record_span_exception(active, RuntimeError("x"))


def test_status_and_exporter_name(otel: InMemorySpanExporter) -> None:
    assert T.exporter_name() == "memory"
    payload = T.status()
    assert payload["metrics"]["mcp_calls_total"] == 0


def test_shutdown_is_idempotent(otel: InMemorySpanExporter) -> None:
    T.shutdown_tracing()
    T.shutdown_tracing()
    assert T.tracing_enabled() is False


# ---------------------------------------------------------------------------
# 3) 指标细节
# ---------------------------------------------------------------------------
def test_metrics_render_empty_and_escaping() -> None:
    registry = T.MetricsRegistry(prefix="demo")
    assert "# no metrics yet" in registry.render()

    registry.inc('weird_total', labels={"k": 'a"b\\c'})
    text = registry.render()
    assert 'k="a\\"b\\\\c"' in text
    assert registry.total("weird_total") == 1
    assert registry.percentile("missing_metric", 0.5) == 0.0


def test_metrics_reset(otel: InMemorySpanExporter) -> None:
    T.metrics.inc("mcp_calls_total")
    T.metrics.reset()
    assert T.metrics.total("mcp_calls_total") == 0


def test_attribute_truncation() -> None:
    long_text = "x" * (w6.OTEL_MAX_ATTR_LENGTH + 50)
    assert len(T._as_attr(long_text)) == w6.OTEL_MAX_ATTR_LENGTH
    assert T._as_attr(True) is True
    assert T._as_attr(None) == ""


# ---------------------------------------------------------------------------
# 4) 图回调：路由函数跳过、异常收尾、astream 两条路径
# ---------------------------------------------------------------------------
def test_handler_skips_route_functions(otel: InMemorySpanExporter) -> None:
    handler = T.GraphNodeSpanHandler(task_id=1, goal="g")
    handler.on_chain_start({"name": "route_after_planner"}, {}, run_id="r1", metadata={"langgraph_node": "planner"})
    assert handler._spans == {}  # 路由函数不建 span
    handler.on_chain_start({"name": None}, {}, run_id="r2", metadata={})
    assert handler._spans == {}


def test_handler_nested_chain_is_deduped(otel: InMemorySpanExporter) -> None:
    handler = T.GraphNodeSpanHandler(task_id=1, goal="g")
    handler.on_chain_start({"name": "planner"}, {}, run_id="outer", metadata={"langgraph_node": "planner"})
    handler.on_chain_start({"name": "planner"}, {}, run_id="inner", metadata={"langgraph_node": "planner"})
    handler.on_chain_start({"name": "planner"}, {}, run_id="inner2", metadata={"langgraph_node": "planner"})

    handler.on_chain_end({}, run_id="inner2")
    handler.on_chain_end({}, run_id="inner")
    handler.on_chain_end({}, run_id="outer")

    node_spans = [s for s in otel.get_finished_spans() if s.name == "graph.node planner"]
    assert len(node_spans) == 1


def test_handler_records_node_error(otel: InMemorySpanExporter) -> None:
    handler = T.GraphNodeSpanHandler(task_id=2, goal="g")
    handler.on_chain_start({"name": "executor"}, {}, run_id="r3", metadata={"langgraph_node": "executor"})
    handler.on_chain_error(RuntimeError("tool failed"), run_id="r3")

    spans = [s for s in otel.get_finished_spans() if s.name == "graph.node executor"]
    assert len(spans) == 1
    assert spans[0].status.status_code.name == "ERROR"


def test_handler_close_pending_ends_dangling_spans(otel: InMemorySpanExporter) -> None:
    handler = T.GraphNodeSpanHandler()
    handler.on_chain_start({"name": "reviewer"}, {}, run_id="dangling", metadata={"langgraph_node": "reviewer"})
    handler.close_pending()
    assert [s.name for s in otel.get_finished_spans()] == ["graph.node reviewer"]


async def test_graph_astream_path(otel: InMemorySpanExporter, w2_graph: Any) -> None:
    chunks = [
        chunk
        async for chunk in w2_graph.astream(
            {
                "goal": "分析上周数码配件 GMV 下滑原因",
                "time_range": {"start": "2026-10-13", "end": "2026-10-19"},
                "task_id": 880010,
            },
            config={"configurable": {"thread_id": "otel-astream"}},
            stream_mode="updates",
        )
    ]
    assert chunks
    assert any(s.name == "graph.run" for s in otel.get_finished_spans())


async def test_graph_astream_events_path(otel: InMemorySpanExporter, w2_graph: Any) -> None:
    events = [
        event
        async for event in w2_graph.astream_events(
            {
                "goal": "分析上周数码配件 GMV 下滑原因",
                "time_range": {"start": "2026-10-13", "end": "2026-10-19"},
                "task_id": 880011,
            },
            config={"configurable": {"thread_id": "otel-events"}},
            version="v2",
        )
    ]
    assert events
    roots = [s for s in otel.get_finished_spans() if s.name == "graph.run"]
    assert len(roots) == 1
    assert roots[0].attributes["graph.mode"] == "astream_events"


# ---------------------------------------------------------------------------
# 5) 会话后端：降级分支 / 容量 / 关停 / 诊断
# ---------------------------------------------------------------------------
def test_safe_url_hides_password() -> None:
    from app.mcp.session_backend import _safe_url

    assert _safe_url("redis://:secret@redis:6379/0") == "redis://***@redis:6379/0"
    assert _safe_url("redis://redis:6379/0") == "redis://redis:6379/0"


class _BrokenClient:
    def ping(self) -> bool:
        raise ConnectionError("down")

    def __getattr__(self, item: str):
        def _boom(*_args, **_kwargs):
            raise ConnectionError(f"down: {item}")

        return _boom


async def test_degraded_paths_stay_functional(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.mcp.session_backend import RedisSessionRegistry

    monkeypatch.setattr(w6, "SESSION_REGISTRY_DEGRADE_TO_MEMORY", True)
    registry = RedisSessionRegistry(sync_client=_BrokenClient(), max_sessions=1, ttl_seconds=60)

    assert registry.ping() is False
    assert registry.degraded is True
    assert registry.degrade_reason.startswith("ping")

    first = registry.create("sql-query")
    second = registry.create("notify")  # 超出 max_sessions → 淘汰最老
    assert len(registry) == 1
    assert registry.get(first.session_id) is None
    assert registry.get(second.session_id) is not None
    assert registry.reap() == 0
    assert len(registry.snapshot()) == 1
    registry.drop(second.session_id)
    assert registry.get(second.session_id) is None


async def test_queue_refresh_size_and_aclose() -> None:
    import fakeredis.aioredis

    from app.mcp.session_backend import RedisSessionQueue

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisSessionQueue(client, "cov:q", poll_interval=0.01)
    await queue.put({"id": 1})
    assert queue.qsize() == 0  # 诊断缓存初始为 0
    assert await queue.refresh_size() == 1
    assert queue.qsize() == 1
    await queue.aclose()
    assert await client.exists("cov:q") == 0


async def test_registry_aclose_closes_clients() -> None:
    import fakeredis
    import fakeredis.aioredis

    from app.mcp.session_backend import RedisSessionRegistry

    server = fakeredis.FakeServer()
    registry = RedisSessionRegistry(
        sync_client=fakeredis.FakeRedis(server=server, decode_responses=True),
        async_client=fakeredis.aioredis.FakeRedis(server=server, decode_responses=True),
    )
    registry.create("sql-query")
    await registry.aclose()


async def test_registry_drop_unknown_session_is_noop() -> None:
    import fakeredis
    import fakeredis.aioredis

    from app.mcp.session_backend import RedisSessionRegistry

    server = fakeredis.FakeServer()
    registry = RedisSessionRegistry(
        sync_client=fakeredis.FakeRedis(server=server, decode_responses=True),
        async_client=fakeredis.aioredis.FakeRedis(server=server, decode_responses=True),
    )
    registry.drop("00000000000000000000000000000000")
    assert len(registry) == 0
    await registry.aclose()


# ---------------------------------------------------------------------------
# 6) 生产入口：lifespan 与 ready 会话后端分支
# ---------------------------------------------------------------------------
async def test_lifespan_wrapper_runs_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.bootstrap import create_production_app
    from app.core.w5_config import w5

    monkeypatch.setattr(w6, "ENABLE_OTEL", False)
    monkeypatch.setattr(w5, "MCP_HTTP_ENABLED", False)

    app = create_production_app(enable_otel=False, mount_mcp=False)
    async with app.router.lifespan_context(app):
        pass  # 正常进入/退出，覆盖 shutdown 分支


async def test_ready_checks_redis_session_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from httpx import ASGITransport, AsyncClient

    import app.bootstrap as bootstrap
    from app.bootstrap import create_production_app
    from app.core.w5_config import w5

    monkeypatch.setattr(w6, "ENABLE_OTEL", False)
    monkeypatch.setattr(w5, "MCP_HTTP_ENABLED", True)
    monkeypatch.setattr(w6, "SESSION_REGISTRY_BACKEND", "redis")
    monkeypatch.setattr(w6, "SESSION_REGISTRY_PING_ON_START", False)

    async def _ok() -> bool:
        return True

    monkeypatch.setattr(bootstrap, "check_db", _ok)
    monkeypatch.setattr(bootstrap, "ping_redis", _ok)

    app = create_production_app(enable_otel=False, mount_mcp=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")

    payload = response.json()
    assert payload["session_registry"]["backend"] == "redis"
    assert "degraded" in payload["session_registry"]


# ---------------------------------------------------------------------------
# 7) 配置对象
# ---------------------------------------------------------------------------
def test_w6_settings_properties_and_validators() -> None:
    settings = W6Settings(
        _env_file=None,
        SESSION_REGISTRY_KEY_PREFIX=":custom:sess:",
        OTEL_EXPORTER_OTLP_ENDPOINT="http://collector:4318/",
        OTEL_EXPORTER_OTLP_HEADERS="k1=v1,k2=v2",
        OTEL_EXCLUDE_PATHS="/health, /metrics",
    )

    assert settings.session_backend_is_redis is False
    assert settings.session_meta_key("abc") == "custom:sess:abc"
    assert settings.session_queue_key("abc") == "custom:sess:abc:q"
    assert settings.otel_headers == {"k1": "v1", "k2": "v2"}
    assert settings.otel_traces_url == "http://collector:4318/v1/traces"
    assert settings.otel_exclude_prefixes == ("/health", "/metrics")
    assert settings.memory_mid_ttl_seconds if hasattr(settings, "memory_mid_ttl_seconds") else True

    described = settings.describe()
    assert described["session_registry"]["backend"] == settings.SESSION_REGISTRY_BACKEND
    assert "otel" in described and "sse_guard" in described


def test_w6_toggle_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.w6_config import otel_enabled, sse_guard_enabled

    monkeypatch.setattr(w6, "ENABLE_OTEL", True)
    monkeypatch.setattr(w6, "SSE_GUARD_ENABLED", True)
    assert otel_enabled() is True
    assert sse_guard_enabled() is True

    monkeypatch.setattr(w6, "ENABLE_OTEL", False)
    monkeypatch.setattr(w6, "SSE_GUARD_ENABLED", False)
    assert otel_enabled() is False
    assert sse_guard_enabled() is False


# ---------------------------------------------------------------------------
# 8) HTTP 中间件细节
# ---------------------------------------------------------------------------
def test_middleware_exclude_prefixes() -> None:
    from app.core.tracing_middleware import OtelTracingMiddleware

    middleware = OtelTracingMiddleware(app=None, exclude_paths=("/docs", "/health"))
    assert middleware._excluded("/docs") is True
    assert middleware._excluded("/docs/oauth2-redirect") is True
    assert middleware._excluded("/docsx") is False


async def test_middleware_survives_span_creation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    import app.core.tracing_middleware as tm

    def _boom() -> Any:
        raise RuntimeError("tracer down")

    monkeypatch.setattr(tm, "get_tracer", _boom)
    monkeypatch.setattr(w6, "ENABLE_OTEL", True)
    monkeypatch.setattr(T, "_state", T._TracingState())
    T._state.initialized = True
    T._state.tracer = object()

    app = FastAPI()
    app.add_middleware(tm.OtelTracingMiddleware, exclude_paths=())

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"ok": "1"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ok")

    assert response.status_code == 200
    T._reset_for_tests()


# ---------------------------------------------------------------------------
# 9) 子开关运行期翻转（已 patch，但开关被关掉 → 走无埋点快路径）
# ---------------------------------------------------------------------------
def test_subswitches_skip_patching(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents.llm import LLMClient
    from app.tools.mcp_client import MCPClient

    T._reset_for_tests()
    monkeypatch.setattr(w6, "ENABLE_OTEL", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_GRAPH", False)
    monkeypatch.setattr(w6, "OTEL_TRACE_MCP", False)
    monkeypatch.setattr(w6, "OTEL_TRACE_LLM", False)
    monkeypatch.setattr(w6, "OTEL_TRACE_SQL", False)
    monkeypatch.setattr(w6, "OTEL_EXPORTER", "none")
    try:
        assert T.install_instrumentation() is True
        assert getattr(MCPClient, "_otel_patched", False) is False
        assert getattr(LLMClient, "_otel_patched", False) is False
    finally:
        T._reset_for_tests()


async def test_graph_and_mcp_fast_path_after_runtime_toggle(
    otel: InMemorySpanExporter, w2_graph: Any, mcp_pool: Any
) -> None:
    """已 patch 的前提下把子开关关掉 → 走无埋点快路径（运行期可降级埋点）。"""
    w6.OTEL_TRACE_GRAPH = False  # pydantic 实例可直接改，finally 里还原
    try:
        await w2_graph.ainvoke(
            {
                "goal": "分析上周数码配件 GMV 下滑原因",
                "time_range": {"start": "2026-10-13", "end": "2026-10-19"},
                "task_id": 880020,
            },
            config={"configurable": {"thread_id": "otel-toggle"}},
        )
        assert not [s for s in otel.get_finished_spans() if s.name == "graph.run"]
    finally:
        w6.OTEL_TRACE_GRAPH = True

    before = len([s for s in otel.get_finished_spans() if s.name.startswith("mcp.tools/call")])
    w6.OTEL_TRACE_MCP = False
    try:
        await mcp_pool.call("sql-query", {"sql": "SELECT 1 FROM orders LIMIT 1"})
    finally:
        w6.OTEL_TRACE_MCP = True
    after = len([s for s in otel.get_finished_spans() if s.name.startswith("mcp.tools/call")])
    assert after == before


def test_graph_input_task_reads_nested_state() -> None:
    task_id, goal = T._graph_input_task({"state": {"task_id": 9, "goal": "g"}}, {})
    assert (task_id, goal) == (9, "g")

    task_id, goal = T._graph_input_task({}, {"configurable": {"thread_id": "thread-1"}})
    assert task_id == "thread-1"
    assert goal is None


def test_uninstall_without_patches_is_noop() -> None:
    T._reset_for_tests()
    T.uninstall_instrumentation()
    assert T.status()["instrumented"] is False


# ---------------------------------------------------------------------------
# 10) 会话后端：懒加载客户端 / 无客户端关停
# ---------------------------------------------------------------------------
async def test_lazy_clients_created_without_touching_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    import redis as redis_sync
    import redis.asyncio as redis_async

    from app.mcp.session_backend import RedisSessionRegistry

    made: dict[str, Any] = {}

    def _fake_sync(url: str, **kwargs: Any) -> Any:
        made["sync"] = url
        return object()

    def _fake_async(url: str, **kwargs: Any) -> Any:
        made["async"] = url
        return object()

    monkeypatch.setattr(redis_sync.Redis, "from_url", staticmethod(_fake_sync))
    monkeypatch.setattr(redis_async.Redis, "from_url", staticmethod(_fake_async))

    registry = RedisSessionRegistry()
    assert registry._sync_client() is not None
    assert registry._async_client() is not None
    assert set(made) == {"sync", "async"}
    await registry.aclose()


async def test_aclose_without_clients() -> None:
    from app.mcp.session_backend import RedisSessionRegistry

    registry = RedisSessionRegistry()
    await registry.aclose()  # 未创建任何客户端，不应抛异常
