"""W6：任务分页列表（GET /tasks，缺口 G1，PRD §7.1 / FR-016 验收标准 1）。

角色规则：A/R 可查全部（可按创建人过滤），V 仅本人创建。
脱敏由服务层强制（task_query_service.list_tasks）：
- viewer 传他人 creator_id 被静默覆盖为本人（不报错，避免泄露他人任务存在性）；
- viewer 角色 creator_name 恒为 None。

路由文件独立于存量 ``tasks.py``（W2 创建 + 详情），只读、零改存量。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_role
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.schemas.common import ApiResponse, PageData, PageMeta
from app.schemas.enums import TaskStatus, UserRole
from app.schemas.w6 import TaskListItem
from app.services import task_query_service
from app.utils.query_parsing import parse_datetime, parse_multi_enum, parse_order

router = APIRouter(prefix="/tasks", tags=["tasks"])

_TASK_ORDER = ("created_desc", "created_asc")


@router.get("", summary="任务分页列表（W6 缺口 G1）", response_model=ApiResponse[PageData[TaskListItem]])
async def list_tasks(
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数，1~100"),
    status: str | None = Query(default=None, description="状态过滤，可多值英文逗号分隔（TaskStatus）"),
    creator_id: int | None = Query(default=None, ge=1, description="创建人过滤；viewer 传入被静默覆盖为本人"),
    keyword: str | None = Query(default=None, max_length=64, description="目标关键词，LIKE %kw%"),
    start: str | None = Query(default=None, description="创建时间下界（闭区间），ISO8601"),
    end: str | None = Query(default=None, description="创建时间上界（闭区间），ISO8601"),
    order: str | None = Query(default=None, description="排序：created_desc（默认）/ created_asc"),
    archived: bool | None = Query(
        default=None,
        description="归档过滤：不传=默认口径（排除已归档）；true=只看已归档；false=只看未归档",
    ),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[PageData[TaskListItem]]:
    statuses = parse_multi_enum(status, TaskStatus, field="status")
    start_dt = parse_datetime(start, field="start")
    end_dt = parse_datetime(end, field="end")
    order_by = parse_order(order, _TASK_ORDER, default="created_desc")

    is_viewer = user.role == UserRole.VIEWER
    # creator_id 不存在 → 422（不做 404，避免枚举用户）；viewer 由服务层静默覆盖，不校验
    if not is_viewer and creator_id is not None and not await task_query_service.count_creator(session, creator_id):
        from app.core.exceptions import AppException, ErrorCode

        raise AppException(ErrorCode.VALIDATION_ERROR, f"creator_id={creator_id} 不存在")

    result = await task_query_service.list_tasks(
        session,
        page=page,
        size=size,
        statuses=statuses,
        creator_id=creator_id,
        keyword=keyword,
        start=start_dt,
        end=end_dt,
        order=order_by,
        is_viewer=is_viewer,
        viewer_user_id=int(user.id) if is_viewer else None,
        archived=archived,
    )
    data = PageData[TaskListItem](
        items=result["items"],
        meta=PageMeta(page=page, size=size, total=result["total"]),
    )
    return ApiResponse.ok(data)


__all__ = ["router"]
