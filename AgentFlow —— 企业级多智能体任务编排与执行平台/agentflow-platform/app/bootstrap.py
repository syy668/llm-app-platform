"""生产入口：在 W1 ``create_app()`` 之上叠加 W5-Part2 的工程化能力。

## 为什么不在 ``app/main.py`` 里加

``app/main.py`` 属 W1 冻结基线（红线第 1 条）。本文件是**新增的装配层**：

```
create_app()  ← 一行未改
   ├── /health   /ready        （新增探针：容器编排用，语义与 /healthz /readyz 一致）
   ├── /metrics                （Prometheus 文本）
   ├── /observability/status   （埋点 / 会话后端自检）
   ├── OtelTracingMiddleware   （仅 ENABLE_OTEL=true 时挂载）
   └── /mcp/v1                 （仅 MCP_HTTP_ENABLED=true 时挂载，会话表可选 Redis）
```

启动方式（Dockerfile / docker-compose.prod.yml 都用它）：

```bash
uvicorn app.bootstrap:app --host 0.0.0.0 --port 8000
```

## 三处「关闭即零影响」

- ``ENABLE_OTEL=false`` → 不加中间件、不打 patch，``/metrics`` 仍可用（只是空表）；
- ``MCP_HTTP_ENABLED=false`` → 不挂 ``/mcp/v1``，仅 stdio 传输；
- ``SESSION_REGISTRY_BACKEND=memory`` → 不碰 Redis，单进程会话表。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from app import __version__
from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis_client import ping_redis
from app.core.w5_config import w5
from app.core.w6_config import w6
from app.db.session import check_db

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 1) 探针（容器 healthcheck / readinessGate 用）
# ---------------------------------------------------------------------------
def _register_probes(app: FastAPI) -> None:
    @app.get("/health", tags=["health"], summary="存活探针（容器 healthcheck）")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": settings.APP_NAME,
            "version": __version__,
            "checks": {"process": True},
        }

    @app.get("/ready", tags=["health"], summary="就绪探针（MySQL + Redis + 会话后端）")
    async def ready() -> JSONResponse:
        db_ok = await check_db()
        redis_ok = await ping_redis()
        registry = getattr(app.state, "session_registry", None)
        registry_info: dict[str, Any] = {"backend": w6.SESSION_REGISTRY_BACKEND}
        registry_ok = True
        if registry is not None:
            ping = getattr(registry, "ping", None)
            if callable(ping):
                try:
                    registry_ok = bool(ping())
                except Exception:  # noqa: BLE001
                    registry_ok = False
            registry_info["degraded"] = bool(getattr(registry, "degraded", False))
        ready_flag = bool(db_ok and redis_ok and registry_ok)
        return JSONResponse(
            status_code=200 if ready_flag else 503,
            content={
                "status": "ready" if ready_flag else "not_ready",
                "checks": {"mysql": db_ok, "redis": redis_ok, "session_registry": registry_ok},
                "session_registry": registry_info,
                "w6": w6.describe(),
            },
        )


# ---------------------------------------------------------------------------
# 2) 可观测性端点
# ---------------------------------------------------------------------------
def _register_observability(app: FastAPI) -> None:
    from app.observability.otel_tracing import render_metrics, status as otel_status

    @app.get("/metrics", tags=["observability"], summary="Prometheus 文本指标", response_class=PlainTextResponse)
    async def metrics_endpoint() -> PlainTextResponse:
        return PlainTextResponse(render_metrics(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/observability/status", tags=["observability"], summary="埋点与会话后端自检摘要")
    async def observability_status() -> dict[str, Any]:
        registry = getattr(app.state, "session_registry", None)
        sessions: Any = []
        if registry is not None:
            try:
                sessions = registry.snapshot()
            except Exception:  # noqa: BLE001
                sessions = []
        return {
            "version": __version__,
            "otel": otel_status(),
            "session_registry": {
                "backend": type(registry).__name__ if registry is not None else "none",
                "configured": w6.SESSION_REGISTRY_BACKEND,
                "degraded": bool(getattr(registry, "degraded", False)),
                "sessions": sessions,
            },
            "mcp_http": {"enabled": w5.MCP_HTTP_ENABLED, "path": w5.MCP_HTTP_PATH},
        }


# ---------------------------------------------------------------------------
# 3) MCP HTTP 挂载（带可替换会话注册表）
# ---------------------------------------------------------------------------
def _mount_mcp_http(app: FastAPI) -> bool:
    if not w5.MCP_HTTP_ENABLED:
        logger.info("mcp_http_disabled", extra={"hint": "MCP_HTTP_ENABLED=false，仅 stdio 传输"})
        return False

    from app.mcp.session_backend import build_session_registry
    from app.mcp.transport_http import build_mcp_http_router, load_all_server_apps

    registry = build_session_registry()
    apps = load_all_server_apps()
    router = build_mcp_http_router(apps, sessions=registry)
    app.include_router(router)
    app.state.session_registry = registry
    logger.info(
        "mcp_http_mounted",
        extra={
            "path": w5.MCP_HTTP_PATH,
            "servers": sorted(apps),
            "session_backend": type(registry).__name__,
            "auth": w5.mcp_http_auth_enabled,
        },
    )
    return True


# ---------------------------------------------------------------------------
# 4) lifespan 包装（在 W1 lifespan 之外追加 W5-Part2 的启停动作）
# ---------------------------------------------------------------------------
def _wrap_lifespan(app: FastAPI) -> None:
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[Any]:
        from app.observability.otel_tracing import shutdown_tracing

        async with original(application) as state:
            try:
                yield state
            finally:
                registry = getattr(application.state, "session_registry", None)
                aclose = getattr(registry, "aclose", None)
                if callable(aclose):
                    try:
                        result = aclose()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:  # noqa: BLE001
                        pass
                shutdown_tracing()
                logger.info("w5p2_shutdown_complete")

    app.router.lifespan_context = lifespan


# ---------------------------------------------------------------------------
# 5) 工厂
# ---------------------------------------------------------------------------
def create_production_app(*, enable_otel: bool | None = None, mount_mcp: bool | None = None) -> FastAPI:
    """生产应用：W1 主应用 + 探针 + 指标 + 可选 OTel + 可选 MCP HTTP。"""
    from app.main import create_app

    app = create_app()
    app.state.w6 = w6.describe()

    _register_probes(app)
    _register_observability(app)

    # 注意顺序：add_middleware 必须在应用启动前调用（这里还没启动）
    otel_on = w6.ENABLE_OTEL if enable_otel is None else bool(enable_otel)
    if otel_on:
        from app.core.tracing_middleware import install_tracing

        install_tracing(app, force=True)
    else:
        logger.info("otel_disabled", extra={"hint": "ENABLE_OTEL=false，埋点代码零开销"})

    should_mount = w5.MCP_HTTP_ENABLED if mount_mcp is None else bool(mount_mcp)
    if should_mount:
        _mount_mcp_http(app)

    _wrap_lifespan(app)
    logger.info(
        "production_app_ready",
        extra={"otel": otel_on, "mcp_http": should_mount, "session_backend": w6.SESSION_REGISTRY_BACKEND},
    )
    return app


app = create_production_app()


__all__ = ["app", "create_production_app"]
