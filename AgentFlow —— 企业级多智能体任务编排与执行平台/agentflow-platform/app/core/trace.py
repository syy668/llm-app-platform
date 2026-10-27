"""TraceID 中间件：入口生成 / 透传 trace_id，回写响应头，输出访问日志（NFR-M2）。"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.context import trace_id_var
from app.core.logging import get_logger
from app.utils.ids import new_trace_id

logger = get_logger("agentflow.access")

TRACE_HEADER = "X-Trace-ID"


class TraceIDMiddleware(BaseHTTPMiddleware):
    """在请求生命周期内绑定 trace_id（ContextVar），贯穿日志 / 异常 / 响应头。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        trace_id = request.headers.get(TRACE_HEADER) or new_trace_id()
        token = trace_id_var.set(trace_id)
        request.state.trace_id = trace_id
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[TRACE_HEADER] = trace_id
            return response
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "http_request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "latency_ms": elapsed_ms,
                    "client": request.client.host if request.client else None,
                },
            )
            trace_id_var.reset(token)
