"""W6 任务分页列表查询服务（GET /tasks，缺口 G1）。

PRD §7.1 + W6接口设计文档 §1：
- 角色规则：A/R 可查全部（可按创建人过滤），V 仅本人创建；
- 脱敏 1：viewer 忽略请求中的 creator_id，强制 creator_id = 本人（静默覆盖，不报错）；
- 脱敏 2：viewer 角色 creator_name 恒为 None（PRD：V 无创建人字段）；
- 字段名复用 task_service.get_detail 顶层命名，去掉 subtasks 重负载。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.schemas.enums import TaskStatus
from app.schemas.orm_models import Task, User
from app.schemas.w6 import TaskListItem

# 终态集合：duration_s 只对终态任务计算
_TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}

_ORDERS = {"created_desc", "created_asc"}


def _error_brief(error: dict[str, Any] | None) -> dict[str, Any] | None:
    """仅取 {code, message}，span_id 不下发（列表不需要）。"""
    if not isinstance(error, dict):
        return None
    return {"code": error.get("code"), "message": error.get("message")}


async def count_creator(
    session: AsyncSession,
    creator_id: int,
) -> bool:
    """creator_id 存在性校验（不存在 → 路由层 422，避免枚举用户语义放在校验层）。"""
    user = await session.get(User, creator_id)
    return user is not None


async def list_tasks(
    session: AsyncSession,
    *,
    page: int = 1,
    size: int = 20,
    statuses: list[TaskStatus] | None = None,
    creator_id: int | None = None,
    keyword: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    order: str = "created_desc",
    is_viewer: bool = False,
    viewer_user_id: int | None = None,
    archived: bool | None = None,
) -> dict[str, Any]:
    """分页查询任务列表。

    viewer 时 creator_id 被强制覆盖为 viewer_user_id，creator_name 输出 None。
    ``archived``：None=默认口径（排除已归档）、True=只看已归档、False=只看未归档。
    返回 {"items": [TaskListItem...], "total": int}。
    """
    if order not in _ORDERS:
        raise ValueError(f"非法排序: {order}")

    # 脱敏第 1 条：viewer 传他人 creator_id 时静默覆盖为本人
    effective_creator = viewer_user_id if is_viewer else creator_id
    if is_viewer and viewer_user_id is None:
        # 理论上不会发生（路由层已保证），防御式返回空集
        return {"items": [], "total": 0}

    conditions: list[ColumnElement[bool]] = []
    if statuses:
        conditions.append(Task.status.in_(statuses))
    if effective_creator is not None:
        conditions.append(Task.creator_id == effective_creator)
    if keyword:
        # LIKE %kw%：SQLite / MySQL 兼容写法（内部数据量千级，不建全文索引）
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(Task.goal.like(f"%{escaped}%", escape="\\"))
    if start is not None:
        conditions.append(Task.created_at >= start)
    if end is not None:
        conditions.append(Task.created_at <= end)
    # 归档过滤：默认（None/False）只看未归档，True 时只看已归档。
    # 注意：detail / export / 看板聚合**不**受此过滤影响 —— 归档只是列表整理，
    # 直接按 ID 打开仍可查看，审计链不受影响。
    if archived:
        conditions.append(Task.archived_at.is_not(None))
    else:
        conditions.append(Task.archived_at.is_(None))

    total = (
        await session.execute(select(func.count(Task.id)).where(*conditions))
    ).scalar_one()

    stmt = (
        select(Task, User.username)
        .join(User, Task.creator_id == User.id)
        .where(*conditions)
        .order_by(Task.created_at.desc() if order == "created_desc" else Task.created_at.asc())
        .offset((page - 1) * size)
        .limit(size)
    )
    rows = (await session.execute(stmt)).all()

    items: list[TaskListItem] = []
    for task, username in rows:
        finished = task.finished_at
        duration: int | None = None
        if finished is not None and task.created_at is not None:
            duration = int((finished - task.created_at).total_seconds())
        items.append(
            TaskListItem(
                task_id=task.id,
                task_no=task.task_no,
                goal=task.goal,
                time_range=dict(task.time_range or {}),
                status=task.status.value if hasattr(task.status, "value") else str(task.status),
                replan_count=int(task.replan_count or 0),
                token_usage=int(task.token_usage or 0),
                trace_id=task.trace_id,
                creator_id=int(task.creator_id),
                # 脱敏第 2 条：viewer 恒为 None
                creator_name=None if is_viewer else username,
                error=_error_brief(task.error),
                created_at=task.created_at.isoformat() if task.created_at else None,
                finished_at=finished.isoformat() if finished else None,
                duration_s=duration,
                archived_at=task.archived_at.isoformat() if task.archived_at else None,
            )
        )

    return {"items": items, "total": int(total or 0)}


__all__ = ["list_tasks", "count_creator", "_TERMINAL_STATUSES"]
