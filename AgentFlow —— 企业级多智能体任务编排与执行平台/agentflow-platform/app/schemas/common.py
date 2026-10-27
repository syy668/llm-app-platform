"""通用响应包与分页模型（统一 {code, message, data, trace_id}，PRD 7.1）。"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from app.core.context import get_trace_id
from app.core.exceptions import ErrorCode

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: str = Field(default=ErrorCode.OK.value, description="业务码，成功恒为 OK")
    message: str = Field(default="ok", description="提示信息")
    data: T | None = Field(default=None, description="业务数据")
    trace_id: str | None = Field(default=None, description="本次请求 trace_id，便于日志关联")

    @classmethod
    def ok(cls, data: T | None = None, message: str = "ok") -> "ApiResponse[T]":
        return cls(code=ErrorCode.OK.value, message=message, data=data, trace_id=get_trace_id() or None)


class PageMeta(BaseModel):
    page: int = Field(ge=1, default=1)
    size: int = Field(ge=1, le=100, default=20)
    total: int = Field(ge=0, default=0)


class PageData(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list, description="当前页数据")
    meta: PageMeta = Field(default_factory=PageMeta)
