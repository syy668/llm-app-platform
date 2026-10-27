"""Swagger 共享错误响应描述（工程化收尾新增，纯文档层，零业务影响）。

统一生成 400/401/403/422/500 的 OpenAPI responses 描述，
供新增路由通过 `responses=std_errors(400, 401, 403)` 方式挂载，
保证接口文档中的错误码口径全局一致。

用法：
    from app.core.openapi import std_errors

    @router.post("/refresh-token", responses=std_errors(400, 401, 500))
    def refresh_token(...): ...
"""
from __future__ import annotations

DEFAULT_MESSAGES: dict[int, str] = {
    400: "业务校验失败（如 A/B 对比的两个评测运行不属于同一数据集）",
    401: "未登录 / 访问令牌或刷新令牌无效、过期、已拉黑",
    403: "已登录但权限不足（非管理员访问管理员接口 / 越权访问他人资源）",
    404: "资源不存在",
    422: "请求参数校验失败",
    500: "服务器内部错误（生产环境不返回堆栈，详情见服务端日志 trace_id）",
}


def _error_example(code: int) -> dict:
    """单个错误码的 OpenAPI Response 描述（含统一响应体示例）。"""
    return {
        "description": DEFAULT_MESSAGES.get(code, "错误"),
        "content": {
            "application/json": {
                "example": {"code": code, "message": DEFAULT_MESSAGES.get(code, "错误"), "data": None},
                "schema": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "integer", "description": "业务错误码，非 0 即失败"},
                        "message": {"type": "string", "description": "人类可读的错误提示"},
                        "data": {"type": "object", "nullable": True, "description": "成功时为业务数据；失败时恒为 null"},
                    },
                    "required": ["code", "message", "data"],
                },
            }
        },
        "headers": {
            "X-Trace-ID": {
                "description": "本次请求的全局唯一追踪ID，与服务端日志 trace_id 对应",
                "schema": {"type": "string"},
            }
        },
    }


def std_errors(*codes: int) -> dict[int, dict]:
    """批量生成指定错误码的 OpenAPI responses 字典。"""
    return {code: _error_example(code) for code in codes}


# 高频组合，进一步简化路由装饰器书写
AUTH_ERRORS = std_errors(401, 500)
ADMIN_ERRORS = std_errors(401, 403, 500)
