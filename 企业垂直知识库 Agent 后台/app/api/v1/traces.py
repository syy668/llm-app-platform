"""Trace 日志查询接口（仅管理员）。"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..deps import CurrentUser, require_admin
from ...core.database import get_db
from ...core.response import ok
from ...services import trace_service

router = APIRouter(prefix="/traces", tags=["Trace 日志"])


@router.get("")
def list_traces(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user_id: int | None = None,
    route: str | None = None,
    status: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    _: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """分页 Trace 列表。"""
    return ok(trace_service.list_traces(db, page, size, user_id, route, status, start, end))


@router.get("/stats/summary")
def summary(
    start: datetime | None = None,
    end: datetime | None = None,
    _: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Trace 汇总（调用量 / 平均耗时 / token / 路由分布）。"""
    return ok(trace_service.summary(db, start, end))


@router.get("/{trace_id}")
def get_trace(
    trace_id: str,
    _: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Trace 详情（含工具调用明细）。"""
    return ok(trace_service.get_trace(db, trace_id))


@router.get("/{trace_id}/tool-calls")
def list_tool_calls(
    trace_id: str,
    _: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """某条 Trace 的工具调用明细列表。"""
    return ok(trace_service.list_tool_calls(db, trace_id))
