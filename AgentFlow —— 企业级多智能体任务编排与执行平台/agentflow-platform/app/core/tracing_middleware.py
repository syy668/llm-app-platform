"""HTTP 层埋点中间件 —— W5-Part2 任务 2.1。

## 为什么是「新增中间件」而不是改 ``main.py``

``create_app()`` 属 W1 冻结基线。本模块只提供一个**可插拔**的中间件类 +
``install_tracing(app)`` 装配函数，由新的生产入口 ``app.bootstrap`` 挂载。
不装 = 完全不存在这层中间件，W1 行为不变。

## span 语义（OTel HTTP semconv）

- span 名：``"{method} {route}"``（未知路由时退化为 path，避免高基数爆炸）
- 属性：``http.request.method`` / ``url.path`` / ``url.scheme`` / ``server.address`` /
  ``http.response.status_code`` / ``http.route`` / ``trace.id``
- 状态：5xx → ``ERROR``；4xx 记属性但不标 ERROR（客户端错误不是服务端故障）
- 排除：``/health`` ``/ready`` ``/healthz`` ``/readyz`` ``/metrics`` ``/docs`` 等探针与文档路径

## 长连接（SSE）

SSE 的 span 生命周期 = 整个连接时长，这是**期望行为**：客户端断开时
Starlette 会取消任务，``finally`` 里正常结束 span，并在属性里记
``http.sse=true``，便于在 Jaeger 里按「长 span」筛选出流式请求。
"""

from __future__ import annotations

import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger
from app.core.w6_config import w6
from app.observability.otel_tracing import (
    SpanKind,
    StatusCode,
    current_span_id,
    current_trace_id,
    get_tracer,
    metrics,
    record_span_exception,
    tracing_enabled,
)

logger = get_logger(__name__)

TRACE_ID_HEADER = "X-Trace-ID"
SPAN_ID_HEADER = "X-Span-ID"
TRACEPARENT_HEADER = "traceparent"


class OtelTracingMiddleware(BaseHTTPMiddleware):
    """为每个 HTTP 请求生成一个 SERVER span，并记录请求指标。"""

    def __init__(self, app: Any, *, exclude_paths: tuple[str, ...] | None = None) -> None:
        super().__init__(app)
        self.exclude_paths = exclude_paths or w6.otel_exclude_prefixes

    def _excluded(self, path: str) -> bool:
        return any(path == item or path.startswith(item.rstrip("/") + "/") for item in self.exclude_paths)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not (w6.OTEL_TRACE_HTTP and tracing_enabled()) or self._excluded(request.url.path):
            return await call_next(request)

        method = request.method
        path = request.url.path
        started = time.perf_counter()
        span_cm = self._start_span(request, method, path)
        if span_cm is None:  # 埋点自身异常：直接放过，绝不影响业务
            return await call_next(request)

        with span_cm as active:
            response: Response | None = None
            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001 - 记录后原样抛出
                record_span_exception(active, exc)
                self._record_metric(method, path, 500)
                raise
            finally:
                elapsed = time.perf_counter() - started
                status_code = response.status_code if response is not None else 499
                route = self._route_of(request, path)
                self._annotate(active, method, path, route, status_code, elapsed)
                if response is not None:
                    self._expose_ids(response)
            return response

    # ---------------- 内部 ----------------
    def _start_span(self, request: Request, method: str, path: str) -> Any:
        try:
            tracer = get_tracer()
            attrs = {
                "http.request.method": method,
                "url.path": path,
                "url.scheme": request.url.scheme,
                "server.address": request.url.hostname or "",
                "server.port": request.url.port or 0,
                "http.user_agent": request.headers.get("user-agent", "")[:200],
                "http.sse": "text/event-stream" in (request.headers.get("accept") or ""),
                "trace.id": current_trace_id(),
            }
            return tracer.start_as_current_span(
                f"{method} {path}",
                kind=SpanKind.SERVER,
                attributes={k: v for k, v in attrs.items() if v not in (None, "")},
                record_exception=True,
                set_status_on_exception=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("otel_http_span_failed", extra={"path": path, "error": str(exc)[:160]})
            return None

    def _route_of(self, request: Request, path: str) -> str:
        route = request.scope.get("route")
        template = getattr(route, "path", None)
        return str(template or path)

    def _annotate(self, active: Any, method: str, path: str, route: str, status_code: int, elapsed: float) -> None:
        try:
            active.set_attribute("http.route", route)
            active.set_attribute("http.response.status_code", status_code)
            active.set_attribute("http.duration_ms", round(elapsed * 1000, 2))
            if status_code >= 500:
                active.set_status(StatusCode.ERROR, f"HTTP {status_code}")
        except Exception:  # noqa: BLE001
            pass
        self._record_metric(method, route, status_code)

    def _record_metric(self, method: str, route: str, status_code: int) -> None:
        metrics.inc("http_requests_total", labels={"method": method, "route": route, "status": status_code})

    def _expose_ids(self, response: Response) -> None:
        """把 trace/span id 回写响应头，方便前端与网关排障。"""
        trace_id = current_trace_id()
        if trace_id:
            response.headers.setdefault(TRACE_ID_HEADER, trace_id)
            span_id = current_span_id()
            if span_id:
                response.headers.setdefault(SPAN_ID_HEADER, span_id)


def install_tracing(app: Any, *, force: bool | None = None) -> bool:
    """装配 HTTP 埋点（中间件 + 业务埋点 patch）。返回是否真的装配。"""
    enabled = w6.ENABLE_OTEL if force is None else bool(force)
    if not enabled:
        logger.info("otel_http_middleware_skipped", extra={"enable_otel": w6.ENABLE_OTEL})
        return False

    from app.observability.otel_tracing import install_instrumentation

    if not install_instrumentation(force=True):
        return False

    app.add_middleware(OtelTracingMiddleware)
    logger.info(
        "otel_http_middleware_installed",
        extra={"exclude_paths": list(w6.otel_exclude_prefixes)},
    )
    return True


__all__ = ["OtelTracingMiddleware", "install_tracing", "TRACE_ID_HEADER", "SPAN_ID_HEADER"]
