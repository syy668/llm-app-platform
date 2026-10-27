"""全局异常处理器（企业级统一错误出口）。

拦截四类异常并统一转换为 {code, message, data} 结构：
  - RequestValidationError -> 422 参数校验失败
  - BizError 及其子类      -> 按 exc.status_code 返回业务错误
  - StarletteHTTPException -> 框架级 HTTP 异常兜底（如 404 未匹配路由）
  - Exception（未捕获）    -> 500，堆栈只写日志，前端不暴露内部细节

【工程化收尾 新增】
  1. 环境分级：APP_ENV=dev 时 500 响应在 data 中携带完整堆栈便于排障；
     prod（默认）只返回简洁业务提示，绝不暴露代码堆栈；
  2. 所有异常自动打印 error 级日志，包含 trace_id / 请求路径 / 用户ID；
  3. 错误响应头统一回写 X-Trace-ID，与请求日志（core/middleware.py）串成完整链路。

业务代码仍从 app.core.exceptions 导入 BizError 等异常类，
本模块只负责"注册处理器"这一件事。
"""
import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .exceptions import BizError
from .middleware import get_trace_id

logger = logging.getLogger(__name__)


def _error_body(code: int, message: str, data=None) -> dict:
    """统一错误响应体：code 非 0；data 在生产环境恒为 null。"""
    return {"code": code, "message": message, "data": data}


def _log_error(request: Request, exc: Exception, status_code: int) -> None:
    """所有异常统一打印 error 级日志：trace_id / 方法 / 路径 / 用户ID。"""
    user_id = getattr(request.state, "user_id", None)
    logger.error(
        "请求异常 | %s %s | status=%d | user_id=%s | %s: %s",
        request.method,
        request.url.path,
        status_code,
        user_id if user_id is not None else "匿名",
        type(exc).__name__,
        exc,
        exc_info=(status_code >= 500),
    )


def _error_response(request: Request, status_code: int, body: dict) -> JSONResponse:
    """构造错误响应并回写 X-Trace-ID 响应头。"""
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers={"X-Trace-ID": get_trace_id()},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器，保证所有错误都以统一结构返回。"""

    @app.exception_handler(BizError)
    async def biz_error_handler(request: Request, exc: BizError):
        # 业务异常：错误码与 HTTP 状态码对齐，消息可直接展示给用户
        _log_error(request, exc, exc.status_code)
        return _error_response(
            request, exc.status_code, _error_body(exc.code, exc.message)
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        # 只提取第一条校验错误，避免把内部结构泄露给前端
        message = "请求参数不合法"
        if exc.errors():
            first = exc.errors()[0]
            loc = ".".join(str(x) for x in first.get("loc", []))
            message = f"参数校验失败: {loc} - {first.get('msg', '')}"
        _log_error(request, exc, 422)
        return _error_response(request, 422, _error_body(422, message))

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException):
        # 兜底处理框架级 HTTP 异常（如未匹配路由 404）
        _log_error(request, exc, exc.status_code)
        return _error_response(
            request, exc.status_code, _error_body(exc.status_code, str(exc.detail))
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        # 未预期异常：完整堆栈只记录到 logger
        _log_error(request, exc, 500)
        from ..config import settings

        if settings.debug:
            # 开发环境：data 中携带完整堆栈，前端/Postman 可直接定位问题
            data = {
                "trace_id": get_trace_id(),
                "exception": type(exc).__name__,
                "stack": traceback.format_exc(),
            }
            return _error_response(request, 500, _error_body(500, "服务器内部错误", data))
        # 生产环境（默认）：只返回简洁业务提示，不暴露任何内部细节
        return _error_response(request, 500, _error_body(500, "服务器内部错误"))
