"""统一响应信封。

前端 axios 拦截器（evalagent_vue/src/api/http.ts）硬约束：
  - 成功：{code: 0, message, data}，非 0 直接 reject 并弹 ElMessage；
  - 项目三（eval_run_service.py）读 body["data"] or body，两者都兼容。
因此所有接口（含错误）都必须走这个信封，不允许裸返回 ORM 对象。
"""

from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {"code": 0, "message": message, "data": data}


def envelope(code: int, message: str, data: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=code if 100 <= code <= 599 else 500,
        content=jsonable_encoder({"code": code, "message": message, "data": data}),
    )


def paged(items: list[Any], total: int, page: int, size: int) -> dict[str, Any]:
    """分页体：与前端 Page<T>（items/total/page/size）对齐。"""
    return ok({"items": items, "total": total, "page": page, "size": size})
