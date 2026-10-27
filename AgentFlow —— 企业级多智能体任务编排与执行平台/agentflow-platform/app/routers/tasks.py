"""任务接口（PRD FR-001）：创建任务 / 查询详情。

W2 只交付两个业务后端接口（**不含任何前端代码**）：
- ``POST /api/v1/tasks``：创建任务并触发 LangGraph 执行（A 角色）
- ``GET  /api/v1/tasks/{id}``：任务详情（A 本人 / R 全部 / V 仅已完成）

SSE（GET /tasks/{id}/events）、审批收件箱、cancel / resume 属 W3，本文件不实现。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path

from app.core.deps import require_role
from app.core.exceptions import AppException, ErrorCode
from app.core.logging import get_logger
from app.schemas.agent_io import TaskCreateRequest
from app.schemas.auth import CurrentUser
from app.schemas.common import ApiResponse
from app.schemas.enums import TaskStatus, UserRole
from app.services.task_service import TaskService

logger = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


async def get_task_service() -> TaskService:
    """依赖注入点：测试通过 app.dependency_overrides 替换为内存实现。"""
    return TaskService()


@router.post("", summary="创建任务并触发编排执行", response_model=ApiResponse[dict])
async def create_task(
    payload: TaskCreateRequest,
    user: CurrentUser = Depends(require_role(UserRole.ADMIN)),
    service: TaskService = Depends(get_task_service),
) -> ApiResponse[dict]:
    """校验目标与时间范围 → 落库 → 异步驱动 graph（sync=true 时阻塞至终态）。"""
    result = await service.create_task(
        goal=payload.goal,
        time_range=payload.time_range,
        creator_id=user.id,
        sync=payload.sync,
        compress=payload.compress,
        use_memory=payload.use_memory,
        eval_tag=payload.eval_tag,
    )
    return ApiResponse.ok(result)


@router.get("/{task_ref}", summary="任务详情（含子任务与运行期产物）", response_model=ApiResponse[dict])
async def get_task(
    # 数字 = 主键；任务编号（AF 前缀）= 按 task_no 解析（前端列表只展示 task_no）
    task_ref: str = Path(min_length=1, max_length=64),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    service: TaskService = Depends(get_task_service),
) -> ApiResponse[dict]:
    detail: dict[str, Any] = await service.get_detail_by_ref(task_ref)

    if user.role == UserRole.VIEWER and detail["status"] != TaskStatus.COMPLETED.value:
        raise AppException(ErrorCode.FORBIDDEN, "访客仅可查看已完成任务")
    if user.role == UserRole.ADMIN and int(detail["creator_id"]) != int(user.id):
        raise AppException(ErrorCode.FORBIDDEN, "只能查看本人创建的任务")

    if user.role == UserRole.VIEWER:
        detail.pop("creator_id", None)
        detail["subtasks"] = [
            {k: v for k, v in item.items() if k in {"seq", "name", "tool_hint", "status", "risk_level"}}
            for item in detail.get("subtasks", [])
        ]

    return ApiResponse.ok(detail)


__all__ = ["router", "get_task_service"]
