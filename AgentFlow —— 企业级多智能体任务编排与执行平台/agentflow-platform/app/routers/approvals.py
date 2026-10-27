"""审批收件箱（FR-011）：列出卡在 AWAITING_APPROVAL 的任务及阻断原因。

与 ``POST /tasks/{id}/resume`` 的分工：
- 收件箱只读，负责"让人看见要批什么"（阻断原因 + 高危子任务预览 + 过期时间）；
- 决策动作统一走 resume（一次调用同时完成"落审批单 + 恢复图执行"），避免两处写审批状态。

G4（v1.0 缺口收口）：``GET /approvals`` 增加 ``status`` 过滤，支持「已处理」列表
（APPROVED / REJECTED / EXPIRED），数据源 ``approvals`` 表按终态过滤；
G9（v1.0 缺口收口）：``POST /approvals/batch`` 批量审批，内部复用单条 resume 逻辑。

v1.3 口径修订（PRD L245「审批挂起超 24h 自动失效」的落地）：
- 待处理收件箱**只含未过期的 PENDING 单**，过期单自动落到 ``?status=EXPIRED`` 的
  「已过期」页签（只读）。侧边栏红点数 ``total``，因此过期的僵尸单不会再让红点常亮；
- 每个条目带 ``resumable``：``false`` 表示 checkpoint 已丢（如 Redis 被清/降级重启），
  批了也会 409 ``TASK_NOT_PAUSED``，前端据此禁用行内「审批」并提示走重发/归档。

权限：ADMIN 只看本人任务，APPROVER 看全量（与 PRD 2.1 角色矩阵一致）；
VIEWER 不参与审批，访问直接 403（由 require_role 拦截）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_role
from app.core.redis_client import get_redis
from app.core.w3_config import w3
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.schemas.common import ApiResponse
from app.schemas.enums import ApprovalStatus, UserRole
from app.routers.tasks_ext import get_stream_service
from app.services.interrupt_service import get_approval_by_id, list_paused_tasks, list_processed_approvals
from app.services.stream_service import StreamingTaskService

router = APIRouter(prefix="/approvals", tags=["approvals"])

_PROCESSED_STATUSES = (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED)


@router.get("", summary="审批列表（待处理收件箱 / 已处理历史）", response_model=ApiResponse[dict])
async def list_approvals(
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    size: int = Query(default=20, ge=1, le=w3.APPROVAL_INBOX_MAX_PAGE_SIZE, description="每页条数"),
    status: str | None = Query(
        default=None,
        description="审批状态过滤。缺省=PENDING 收件箱；传 APPROVED/REJECTED/EXPIRED（逗号分隔）→ 已处理列表",
    ),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER)),
    session: AsyncSession = Depends(get_db),
    redis_client: Any = Depends(get_redis),
) -> ApiResponse[dict]:
    creator_id = int(user.id) if user.role == UserRole.ADMIN else None

    # 缺省 → 待处理收件箱（原逻辑不变）
    if status is None or not status.strip():
        data = await list_paused_tasks(
            session, creator_id=creator_id, page=page, size=size, redis_client=redis_client
        )
        return ApiResponse.ok(data)

    # 已处理列表：解析逗号分隔的终态过滤
    from app.utils.query_parsing import parse_multi_enum

    requested = parse_multi_enum(status, ApprovalStatus, field="status")
    statuses = tuple(s for s in (requested or []) if s in _PROCESSED_STATUSES)
    if not statuses:
        # 传了 status 但全是非法终态（如 PENDING）→ 回退待处理收件箱
        data = await list_paused_tasks(
            session, creator_id=creator_id, page=page, size=size, redis_client=redis_client
        )
        return ApiResponse.ok(data)
    data = await list_processed_approvals(
        session, creator_id=creator_id, statuses=statuses, page=page, size=size
    )
    return ApiResponse.ok(data)


@router.get("/{approval_id}", summary="审批单详情", response_model=ApiResponse[dict])
async def get_approval(
    approval_id: int = Path(ge=1),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER)),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[dict]:
    data = await get_approval_by_id(approval_id, session=session)
    return ApiResponse.ok(data)


class BatchDecisionItem(BaseModel):
    """批量审批单条：task_id + 决策（复用 ResumeRequest 的 decision 语义）。"""

    model_config = {"extra": "forbid"}

    task_id: int = Field(ge=1, description="目标任务 ID")
    decision: str = Field(description="APPROVED / REJECTED（大小写不敏感）")
    comment: str = Field(default="", max_length=500, description="审批意见；驳回时作为重规划的硬约束")


class BatchDecisionRequest(BaseModel):
    """POST /approvals/batch 请求体。"""

    model_config = {"extra": "forbid"}

    decisions: list[BatchDecisionItem] = Field(min_length=1, max_length=100, description="批量决策列表，最多 100 条")
    sync: bool | None = Field(default=None, description="True=阻塞到终态/下次挂起，None=走配置默认值")


@router.post("/batch", summary="批量审批（复用单条 resume 逻辑）", response_model=ApiResponse[dict])
async def batch_approve(
    payload: BatchDecisionRequest,
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER)),
    service: StreamingTaskService = Depends(get_stream_service),
) -> ApiResponse[dict]:
    """逐条复用 ``StreamingTaskService.resume``，单条失败不中断其余，最终汇总成功/失败结果。

    注意：审批写入本身是幂等的（resume 内部 ensure_resumable 会校验挂起态），
    这里不做跨任务强事务——每条独立提交，失败原因如实回传，由前端汇总展示。
    """
    from app.core.exceptions import AppException

    results: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0

    for item in payload.decisions:
        normalized = "APPROVED" if item.decision.upper().startswith("APPROVE") else "REJECTED"
        try:
            result = await service.resume(
                item.task_id,
                decision=normalized,
                comment=item.comment,
                decided_by=user.id,
                approval_id=None,
                sync=payload.sync,
            )
            results.append(
                {
                    "task_id": item.task_id,
                    "ok": True,
                    "status": result.get("status"),
                    "approval_id": result.get("approval_id"),
                }
            )
            succeeded += 1
        except AppException as exc:
            failed += 1
            results.append(
                {"task_id": item.task_id, "ok": False, "code": exc.code, "message": exc.message}
            )
        except Exception as exc:  # noqa: BLE001 - 批量场景单条异常需隔离，不能炸掉整个批次
            failed += 1
            results.append({"task_id": item.task_id, "ok": False, "code": "INTERNAL_ERROR", "message": str(exc)})

    return ApiResponse.ok(
        {"succeeded": succeeded, "failed": failed, "results": results},
        message=f"批量审批完成：成功 {succeeded}，失败 {failed}",
    )


__all__ = ["router"]
