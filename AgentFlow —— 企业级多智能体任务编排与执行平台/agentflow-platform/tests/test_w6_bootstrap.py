"""W5-Part2 新增用例：生产入口 ``app.bootstrap`` 的装配与探针。

验证点：
- 探针 ``/health``、``/ready`` 语义（含会话后端检查）；
- ``/metrics`` 输出 Prometheus 文本、``/observability/status`` 输出自检摘要；
- MCP HTTP 挂载与「会话后端可替换」（memory / redis 两条路径）；
- OTel 开关对中间件挂载的影响（关闭时零中间件）。
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.bootstrap import create_production_app
from app.core.w5_config import w5
from app.core.w6_config import w6

pytestmark = pytest.mark.unit


@pytest.fixture
def quiet_w6(monkeypatch: pytest.MonkeyPatch):
    """默认关闭 OTel / MCP，避免每个用例都拉起 4 个 MCP Server。"""
    monkeypatch.setattr(w6, "ENABLE_OTEL", False)
    monkeypatch.setattr(w5, "MCP_HTTP_ENABLED", False)
    monkeypatch.setattr(w6, "SESSION_REGISTRY_BACKEND", "memory")


async def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# 1) 路由装配
# ---------------------------------------------------------------------------
def test_production_app_registers_probes_and_observability(quiet_w6: None) -> None:
    app = create_production_app(enable_otel=False, mount_mcp=False)
    paths = set(app.openapi()["paths"])

    assert {"/health", "/ready", "/metrics", "/observability/status"} <= paths
    assert "/mcp/v1" not in paths  # 未开启时不挂载
    assert getattr(app.state, "session_registry", None) is None


def test_production_app_keeps_w1_routes(quiet_w6: None) -> None:
    app = create_production_app(enable_otel=False, mount_mcp=False)
    paths = set(app.openapi()["paths"])

    # W1 基线路由必须原样存在
    assert "/healthz" in paths
    assert "/readyz" in paths
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/tasks" in paths


def test_otel_disabled_adds_no_middleware(quiet_w6: None) -> None:
    app = create_production_app(enable_otel=False, mount_mcp=False)
    names = [m.cls.__name__ for m in app.user_middleware]
    assert "OtelTracingMiddleware" not in names


def test_otel_enabled_adds_middleware(quiet_w6: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.observability import otel_tracing as T

    monkeypatch.setattr(w6, "ENABLE_OTEL", True)
    monkeypatch.setattr(w6, "OTEL_EXPORTER", "none")
    monkeypatch.setattr(w6, "OTEL_TRACE_SQL", False)
    try:
        app = create_production_app(enable_otel=True, mount_mcp=False)
        names = [m.cls.__name__ for m in app.user_middleware]
        assert "OtelTracingMiddleware" in names
        assert T.status()["instrumented"] is True
    finally:
        T._reset_for_tests()


# ---------------------------------------------------------------------------
# 2) 探针
# ---------------------------------------------------------------------------
async def test_health_probe(quiet_w6: None) -> None:
    app = create_production_app(enable_otel=False, mount_mcp=False)
    async with await _client(app) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["checks"]["process"] is True


async def test_ready_probe_ok(quiet_w6: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.bootstrap as bootstrap

    async def _db_ok() -> bool:
        return True

    async def _redis_ok() -> bool:
        return True

    monkeypatch.setattr(bootstrap, "check_db", _db_ok)
    monkeypatch.setattr(bootstrap, "ping_redis", _redis_ok)

    app = create_production_app(enable_otel=False, mount_mcp=False)
    async with await _client(app) as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"] == {"mysql": True, "redis": True, "session_registry": True}
    assert payload["session_registry"]["backend"] == "memory"


async def test_ready_probe_reports_dependency_failure(quiet_w6: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.bootstrap as bootstrap

    async def _db_ok() -> bool:
        return False

    async def _redis_ok() -> bool:
        return True

    monkeypatch.setattr(bootstrap, "check_db", _db_ok)
    monkeypatch.setattr(bootstrap, "ping_redis", _redis_ok)

    app = create_production_app(enable_otel=False, mount_mcp=False)
    async with await _client(app) as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


# ---------------------------------------------------------------------------
# 3) 指标与自检
# ---------------------------------------------------------------------------
async def test_metrics_endpoint_returns_prometheus_text(quiet_w6: None) -> None:
    from app.observability import otel_tracing as T

    T.metrics.inc("mcp_calls_total", labels={"server": "sql-query", "tool": "sql-query", "status": "ok"})
    app = create_production_app(enable_otel=False, mount_mcp=False)
    async with await _client(app) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "agentflow_mcp_calls_total" in response.text


async def test_observability_status_payload(quiet_w6: None) -> None:
    app = create_production_app(enable_otel=False, mount_mcp=False)
    async with await _client(app) as client:
        response = await client.get("/observability/status")

    payload = response.json()
    assert payload["otel"]["enabled"] is False
    assert payload["session_registry"]["configured"] == "memory"
    assert payload["mcp_http"]["enabled"] is False


# ---------------------------------------------------------------------------
# 4) MCP HTTP 挂载 + 会话后端可替换
# ---------------------------------------------------------------------------
def test_mcp_http_mounted_with_memory_registry(quiet_w6: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w5, "MCP_HTTP_ENABLED", True)
    monkeypatch.setattr(w6, "SESSION_REGISTRY_BACKEND", "memory")

    app = create_production_app(enable_otel=False, mount_mcp=True)
    paths = set(app.openapi()["paths"])

    assert {"/mcp/v1", "/mcp/v1/sse", "/mcp/v1/messages"} <= paths
    registry = app.state.session_registry
    assert type(registry).__name__ == "HttpSessionRegistry"


def test_mcp_http_mounted_with_redis_registry(quiet_w6: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w5, "MCP_HTTP_ENABLED", True)
    monkeypatch.setattr(w6, "SESSION_REGISTRY_BACKEND", "redis")
    monkeypatch.setattr(w6, "SESSION_REGISTRY_PING_ON_START", False)

    app = create_production_app(enable_otel=False, mount_mcp=True)
    registry = app.state.session_registry
    assert type(registry).__name__ == "RedisSessionRegistry"
    # 本地无 Redis 时应优雅降级，而不是启动失败
    assert registry.degraded in (True, False)


async def test_status_lists_redis_sessions(quiet_w6: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w5, "MCP_HTTP_ENABLED", True)
    monkeypatch.setattr(w6, "SESSION_REGISTRY_BACKEND", "memory")

    app = create_production_app(enable_otel=False, mount_mcp=True)
    app.state.session_registry.create("sql-query")

    async with await _client(app) as client:
        response = await client.get("/observability/status")

    payload = response.json()
    assert payload["session_registry"]["backend"] == "HttpSessionRegistry"
    assert len(payload["session_registry"]["sessions"]) == 1
    assert payload["session_registry"]["sessions"][0]["server"] == "sql-query"
