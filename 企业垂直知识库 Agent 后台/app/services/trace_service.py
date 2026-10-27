"""Trace 查询服务（仅管理员）。"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core.exceptions import NotFoundError
from ..models.trace_log import TraceLog
from ..models.trace_tool_call import TraceToolCall
from ..schemas.trace import tool_call_to_dict, trace_to_dict


def _apply_filters(q, user_id=None, route=None, status=None, start=None, end=None):
    if user_id:
        q = q.filter(TraceLog.user_id == user_id)
    if route:
        q = q.filter(TraceLog.agent_route == route)
    if status:
        q = q.filter(TraceLog.status == status)
    if start:
        q = q.filter(TraceLog.started_at >= start)
    if end:
        q = q.filter(TraceLog.started_at <= end)
    return q


def list_traces(
    db: Session, page: int, size: int,
    user_id=None, route=None, status=None, start=None, end=None,
) -> dict:
    """分页 Trace 列表。"""
    q = _apply_filters(db.query(TraceLog), user_id, route, status, start, end)
    total = q.count()
    items = q.order_by(TraceLog.id.desc()).offset((page - 1) * size).limit(size).all()
    return {"total": total, "items": [trace_to_dict(t) for t in items]}


def get_trace(db: Session, trace_id: str) -> dict:
    """Trace 详情（含工具调用明细）。"""
    t = db.query(TraceLog).filter(TraceLog.trace_id == trace_id).first()
    if not t:
        raise NotFoundError("Trace 不存在")
    calls = (
        db.query(TraceToolCall)
        .filter(TraceToolCall.trace_id == trace_id)
        .order_by(TraceToolCall.id.asc())
        .all()
    )
    return {**trace_to_dict(t), "tool_calls": [tool_call_to_dict(c) for c in calls]}


def list_tool_calls(db: Session, trace_id: str) -> dict:
    """某条 Trace 的工具调用明细列表。"""
    calls = (
        db.query(TraceToolCall)
        .filter(TraceToolCall.trace_id == trace_id)
        .order_by(TraceToolCall.id.asc())
        .all()
    )
    return {"total": len(calls), "items": [tool_call_to_dict(c) for c in calls]}


def summary(db: Session, start=None, end=None) -> dict:
    """汇总：调用量、平均耗时、token 消耗、路由分布。"""
    base = _apply_filters(db.query(TraceLog), start=start, end=end)
    total = base.count()

    avg_latency = _apply_filters(
        db.query(func.coalesce(func.avg(TraceLog.total_latency_ms), 0)), start=start, end=end,
    ).scalar() or 0
    token_sum = _apply_filters(
        db.query(func.coalesce(func.sum(TraceLog.total_tokens), 0)), start=start, end=end,
    ).scalar() or 0
    llm_call_sum = _apply_filters(
        db.query(func.coalesce(func.sum(TraceLog.llm_call_count), 0)), start=start, end=end,
    ).scalar() or 0

    route_rows = _apply_filters(
        db.query(TraceLog.agent_route, func.count(TraceLog.id)).group_by(TraceLog.agent_route),
        start=start, end=end,
    ).all()
    route_dist = {r or "unknown": c for r, c in route_rows}

    return {
        "total": total,
        "avg_latency_ms": round(float(avg_latency), 2),
        "token_sum": int(token_sum),
        "llm_call_sum": int(llm_call_sum),
        "route_distribution": route_dist,
    }
