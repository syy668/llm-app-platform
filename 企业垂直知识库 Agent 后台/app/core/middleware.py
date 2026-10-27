"""HTTP 请求日志中间件 + 全局 TraceID 链路。

对每个进入的请求：
1. 生成全局唯一 TraceID（优先复用上游网关传入的 X-Request-ID，否则 uuid4），
   挂载到 request.state.trace_id，并写入 ContextVar —— 任意层级的
   logging 日志自动携带 trace_id（由 logging.TraceIdFilter 注入）；
2. 响应头回写 X-Trace-ID，前端/网关可直接取用排障；
3. 记录一行 stdout 日志（INFO 级）：trace_id | 方法 | 路径 | 客户端 IP | 状态码 | 耗时。

仅记录元信息，不读取请求体（避免大文件上传时把 body 缓存进内存），
不落库，直接经根日志器输出到标准输出。

注意：ContextVar 与 SSE 流式接口兼容 —— same-task 内所有日志均能取到 trace_id；
后台评测线程属于另一上下文，trace 日志沿用自身 trace_id 落库（见 core/tracing.py）。
"""
import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)

# 全局请求 TraceID 上下文：中间件写入，日志过滤器读取；无请求上下文时为 "-"
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")


def get_trace_id() -> str:
    """取当前请求 TraceID（无请求上下文返回 "-"，保证日志格式稳定）。"""
    return trace_id_var.get()


def _client_ip(request: Request) -> str:
    """取客户端 IP：优先 X-Forwarded-For 第一跳（适配反代），否则直连地址。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """生成全局 TraceID 并记录每个请求的路径、方法、客户端 IP、状态码与耗时。"""

    async def dispatch(self, request: Request, call_next):
        # 1) TraceID：优先复用上游 X-Request-ID（网关/压测链路追踪），否则新生成
        trace_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.trace_id = trace_id
        token = trace_id_var.set(trace_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 异常仍交给全局异常处理器；这里补记一条失败日志（含耗时）
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "%s | %s %s | client=%s | status=500 | %.0fms",
                trace_id,
                request.method,
                request.url.path,
                _client_ip(request),
                elapsed_ms,
            )
            trace_id_var.reset(token)
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "%s | %s %s | client=%s | status=%d | %.0fms",
            trace_id,
            request.method,
            request.url.path,
            _client_ip(request),
            response.status_code,
            elapsed_ms,
        )
        # 2) 响应头回写 TraceID，便于前端与网关关联排障
        response.headers["X-Trace-ID"] = trace_id
        trace_id_var.reset(token)
        return response
