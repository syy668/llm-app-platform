"""业务异常与全局异常处理器（统一响应包 {code, message, data, trace_id}）。"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_trace_id

logger = logging.getLogger(__name__)


class ErrorCode(str, Enum):
    """错误码：与 HTTP 状态码解耦，前端按 code 分支处理。"""

    OK = "OK"

    # ---- 鉴权 / 权限（W1 已实现） ----
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    AUTH_USER_DISABLED = "AUTH_USER_DISABLED"
    AUTH_TOKEN_MISSING = "AUTH_TOKEN_MISSING"
    AUTH_TOKEN_INVALID = "AUTH_TOKEN_INVALID"
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
    AUTH_TOKEN_TYPE_INVALID = "AUTH_TOKEN_TYPE_INVALID"
    AUTH_TOKEN_REVOKED = "AUTH_TOKEN_REVOKED"
    AUTH_REFRESH_REVOKED = "AUTH_REFRESH_REVOKED"
    FORBIDDEN = "FORBIDDEN"

    # ---- 通用 ----
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

    # ---- W2+ 预留（业务层启用，W1 不产生） ----
    TASK_INVALID = "TASK_INVALID"          # FR-001
    PLAN_INVALID = "PLAN_INVALID"          # FR-002
    REPLAN_EXHAUSTED = "REPLAN_EXHAUSTED"  # FR-003
    APPROVAL_MISSING = "APPROVAL_MISSING"  # FR-008
    # 状态机冲突（v1.3：归档只允许终态任务）—— 客户端应按"先终止再归档"提示
    TASK_STATE_CONFLICT = "TASK_STATE_CONFLICT"

    # ---- W6（W5-Part2 欠账收口：任务列表 / 大盘聚合 / 评测接口组） ----
    EVAL_UPSTREAM_UNAVAILABLE = "EVAL_UPSTREAM_UNAVAILABLE"  # 项目二 eval API 不可达 / 触发失败
    EVAL_RUN_NOT_FOUND = "EVAL_RUN_NOT_FOUND"                # run_no / run_id 不存在
    EVAL_RUN_DUPLICATE = "EVAL_RUN_DUPLICATE"                # 同 dataset_id+variant 已有进行中评测


# 错误码 -> 默认 HTTP 状态码
_STATUS_MAP: dict[ErrorCode, int] = {
    ErrorCode.AUTH_INVALID_CREDENTIALS: 401,
    ErrorCode.AUTH_USER_DISABLED: 403,
    ErrorCode.AUTH_TOKEN_MISSING: 401,
    ErrorCode.AUTH_TOKEN_INVALID: 401,
    ErrorCode.AUTH_TOKEN_EXPIRED: 401,
    ErrorCode.AUTH_TOKEN_TYPE_INVALID: 401,
    ErrorCode.AUTH_TOKEN_REVOKED: 401,
    ErrorCode.AUTH_REFRESH_REVOKED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.TASK_INVALID: 400,
    ErrorCode.PLAN_INVALID: 422,
    ErrorCode.REPLAN_EXHAUSTED: 409,
    ErrorCode.APPROVAL_MISSING: 403,
    ErrorCode.TASK_STATE_CONFLICT: 409,
    ErrorCode.EVAL_UPSTREAM_UNAVAILABLE: 502,
    ErrorCode.EVAL_RUN_NOT_FOUND: 404,
    ErrorCode.EVAL_RUN_DUPLICATE: 409,
}


class AppException(Exception):
    """业务异常基类：服务层抛出，由全局处理器转为标准响应。"""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str | None = None,
        *,
        status_code: int | None = None,
        detail: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code.value if isinstance(code, ErrorCode) else str(code)
        self.message = message or self.code
        if status_code is not None:
            self.status_code = status_code
        elif isinstance(code, ErrorCode):
            self.status_code = _STATUS_MAP.get(code, 400)
        else:
            self.status_code = 400
        self.detail = detail or {}
        self.headers = headers or {}
        super().__init__(self.message)


def _envelope(code: str, message: str, status_code: int, data: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "data": data,
            "trace_id": get_trace_id() or None,
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """注册 4 类处理器：业务异常、参数校验、HTTP 异常、兜底 500。"""

    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "business_exception",
            extra={
                "path": str(request.url.path),
                "method": request.method,
                "code": exc.code,
                "status": exc.status_code,
                "detail": exc.detail or None,
            },
        )
        return _envelope(exc.code, exc.message, exc.status_code, {"detail": exc.detail} if exc.detail else None)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": ".".join(str(i) for i in err.get("loc", [])), "msg": err.get("msg", ""), "type": err.get("type", "")}
            for err in exc.errors()
        ]
        logger.warning("validation_error", extra={"path": str(request.url.path), "errors": errors})
        return _envelope(ErrorCode.VALIDATION_ERROR.value, "请求参数校验失败", 422, {"errors": errors})

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND.value if exc.status_code == 404 else f"HTTP_{exc.status_code}"
        if exc.status_code == 401:
            code = ErrorCode.AUTH_TOKEN_MISSING.value
        elif exc.status_code == 403:
            code = ErrorCode.FORBIDDEN.value
        return _envelope(code, str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", extra={"path": str(request.url.path), "method": request.method})
        return _envelope(ErrorCode.INTERNAL_ERROR.value, "服务内部错误", 500)
