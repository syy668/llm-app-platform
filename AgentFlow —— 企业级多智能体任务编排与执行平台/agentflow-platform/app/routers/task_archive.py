"""任务归档（v1.3，第一阶段）。

设计定位
--------
归档**不是删除**：它只是把任务从"全部任务"默认列表里收起来，数据、子任务、
审批单、轨迹、产物全部原样保留，任务详情按 ID 仍可打开，随时可以取消归档。
真正的删除（软删除 + 级联清理 Redis checkpoint / 图表文件）是第二阶段，不在本文件范围。

为什么不做行内物理删除
----------------------
任务的审计价值高于列表整洁度：一个任务背后挂着 trace / 审批单 / 记忆。
行内一键删除等于破坏可追溯链，而且 51 行的全量列表误触成本太高。归档是这份诉求的
正确抽象 —— 用户要的是"别让它占着我的列表"，不是"抹掉证据"。

接口
----
``POST /tasks/{task_id}/archive``
    请求体 ``{"archived": true}`` 归档、``{"archived": false}`` 取消归档；省略 body 视为归档。
    幂等：目标状态已满足时返回 ``changed=false``，不报错、不改时间戳。

约束
----
- 只有终态（COMPLETED / FAILED / CANCELLED）能归档；执行中的任务归档会让人以为
  "任务消失了但还在后台跑"，返回 409 TASK_STATE_CONFLICT。
- **权限（刻意与详情不对齐）**：
  - ``GET /tasks/{id}`` 仍是"ADMIN 仅本人创建"——那是**读隐私**，防止管理员随手翻别人的
    业务问题原文；
  - 归档是**全局治理动作**：任何 ADMIN 可以归档任何终态任务，APPROVER 同样不限。
    理由是管理员在「全部任务」里能看到所有人的任务，若按钮点下去 403，这个能力就是摆设；
    而归档本身不读取正文、不改变数据，最坏后果只是"列表里收起来了"，可随时取消。
  - VIEWER 无此能力（访客只能看已完成任务，没有整理列表的语义）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_role
from app.core.exceptions import AppException, ErrorCode
from app.core.logging import get_logger
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.schemas.common import ApiResponse
from app.schemas.enums import TaskStatus, UserRole
from app.schemas.orm_models import Task
from app.utils.time import utcnow

logger = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])

#: 允许归档的状态集合（终态）。非终态归档会让"任务消失但还在跑"，语义不一致。
ARCHIVABLE_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})


class ArchiveRequest(BaseModel):
    """归档请求体。默认归档，传 false 取消归档。"""

    archived: bool = Field(default=True, description="true=归档；false=取消归档")


def _status_of(task: Task) -> str:
    return task.status.value if hasattr(task.status, "value") else str(task.status)


@router.post(
    "/{task_id}/archive",
    summary="归档 / 取消归档任务（仅终态，可逆）",
    response_model=ApiResponse[dict],
)
async def archive_task(
    task_id: int = Path(ge=1),
    payload: ArchiveRequest | None = None,
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER)),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[dict]:
    task = await session.get(Task, task_id)
    if task is None:
        raise AppException(ErrorCode.NOT_FOUND, f"任务 {task_id} 不存在")

    # 刻意不做 assert_visible：详情接口的"ADMIN 仅本人"是读隐私防线，
    # 而归档是全局治理动作 —— ADMIN / APPROVER 都可以归档任何终态任务，
    # 否则管理员在「全部任务」里看到别人任务时，按钮点下去就是 403，形同摆设。
    # 归档不读正文、不改数据、可随时取消，越权成本仅为"列表里暂时收起来"。

    want_archived = True if payload is None else bool(payload.archived)
    changed = False

    if want_archived:
        status = _status_of(task)
        if status not in {s.value for s in ARCHIVABLE_STATUSES}:
            raise AppException(
                ErrorCode.TASK_STATE_CONFLICT,
                f"任务当前状态为 {status}，只有终态（已完成 / 失败 / 已终止）才能归档；"
                "如需清理请先终止任务",
            )
        if task.archived_at is None:
            task.archived_at = utcnow()
            changed = True
    else:
        if task.archived_at is not None:
            task.archived_at = None
            changed = True

    if changed:
        await session.commit()
        await session.refresh(task)
        logger.info(
            "task_archived" if want_archived else "task_unarchived",
            extra={
                "task_id": task_id,
                "task_no": task.task_no,
                "operator": user.id,
                "status": _status_of(task),
            },
        )
    else:
        logger.info(
            "task_archive_noop",
            extra={"task_id": task_id, "operator": user.id, "archived": want_archived},
        )

    archived_at: datetime | None = task.archived_at
    data: dict[str, Any] = {
        "task_id": task.id,
        "task_no": task.task_no,
        "status": _status_of(task),
        "archived": archived_at is not None,
        "archived_at": archived_at.isoformat() if archived_at else None,
        "changed": changed,
    }
    return ApiResponse.ok(data)


__all__ = ["router", "ARCHIVABLE_STATUSES", "ArchiveRequest"]
