"""日志查询（缺口：系统设置页「请求日志」区，NFR 可观测性配套）。

数据源：``task_executions`` 结构化执行记录（唯一持久化的运行轨迹），
不引入外部日志平台（Loki/ELK）——日志本身由 ``app/core/logging.py`` 输出到 stdout JSON。

- ``POST /logs/search``：按 trace_id / task_id / event / level / 时间窗检索，分页返回；
- level 口径：``tool_error`` 非空或 ``event=error`` → ERROR，否则 INFO。

权限：ADMIN / APPROVER 可查（运维配套）；VIEWER 只读但仅限已完成任务视角，
本接口对 VIEWER 开放但数据即 trace 记录（与 GET /tasks/{id}/trace 同源、同为只读回放）。
"""

from __future__ import annotations

from datetime import timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_role
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.schemas.common import ApiResponse
from app.schemas.enums import UserRole
from app.schemas.orm_models import TaskExecution
from app.schemas.w6 import LogItem, LogSearchData, LogSearchRequest
from app.utils.query_parsing import parse_datetime

router = APIRouter(prefix="/logs", tags=["logs"])

_ERROR_EVENTS = {"error", "tool.error"}


@router.post("/search", summary="日志检索（基于 task_executions 结构化记录）", response_model=ApiResponse[LogSearchData])
async def search_logs(
    payload: LogSearchRequest,
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[LogSearchData]:
    conditions: list[Any] = []
    if payload.trace_id:
        conditions.append(TaskExecution.trace_id == payload.trace_id.strip())
    if payload.task_id is not None:
        conditions.append(TaskExecution.task_id == int(payload.task_id))
    if payload.event:
        conditions.append(TaskExecution.event == payload.event.strip())
    if payload.level and payload.level.strip().upper() == "ERROR":
        from sqlalchemy import or_

        conditions.append(or_(TaskExecution.tool_error.is_not(None), TaskExecution.event.in_(_ERROR_EVENTS)))

    start_dt = parse_datetime(payload.start, field="start") if payload.start else None
    end_dt = parse_datetime(payload.end, field="end") if payload.end else None
    if start_dt is not None:
        conditions.append(TaskExecution.started_at >= start_dt)
    if end_dt is not None:
        conditions.append(TaskExecution.started_at <= end_dt)

    where = conditions[0] if len(conditions) == 1 else (and_(*conditions) if conditions else None)

    count_stmt = select(func.count(TaskExecution.id))
    stmt = select(TaskExecution)
    if where is not None:
        count_stmt = count_stmt.where(where)
        stmt = stmt.where(where)

    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        (
            await session.execute(
                stmt.order_by(TaskExecution.started_at.desc(), TaskExecution.id.desc())
                .offset((payload.page - 1) * payload.size)
                .limit(payload.size)
            )
        )
        .scalars()
        .all()
    )

    items: list[LogItem] = []
    for row in rows:
        is_error = row.tool_error is not None or str(row.event) in _ERROR_EVENTS
        items.append(
            LogItem(
                id=int(row.id),
                trace_id=row.trace_id,
                span_id=row.span_id,
                task_id=int(row.task_id),
                node=row.node.value if hasattr(row.node, "value") else str(row.node),
                event=str(row.event),
                tool_name=row.tool_name,
                tool_error=row.tool_error,
                level="ERROR" if is_error else "INFO",
                input_tokens=int(row.input_tokens or 0),
                output_tokens=int(row.output_tokens or 0),
                latency_ms=int(row.latency_ms or 0),
                payload=row.payload,
                # started_at 由 trace_service 以 utcnow() 写入（UTC naive），必须补偏移再序列化，
                # 否则前端会把 `03:15:11` 当本地时间直接显示，比真实时间慢 8 小时（UTC+8）。
                started_at=(
                    row.started_at.replace(tzinfo=timezone.utc).isoformat() if row.started_at else None
                ),
            )
        )

    return ApiResponse.ok(
        LogSearchData(items=items, page=payload.page, size=payload.size, total=total)
    )


__all__ = ["router"]
