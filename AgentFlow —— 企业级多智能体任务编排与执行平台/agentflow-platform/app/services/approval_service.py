"""审批服务（PRD FR-011）：human_gate 建单 / 落决策，W3 REST 复用同一套能力。

W2 只交付**被 human_gate 与 notify 前置校验直接用到**的部分：
- ``create_pending_approval``：interrupt 前建 PENDING 单据（24h 过期）
- ``apply_decision``：恢复后落 APPROVED / REJECTED
- ``get_approval`` / ``list_pending``：供 W3 审批收件箱与 notify 复核

REST 接口（GET /approvals、POST /approvals/{id}/decide）属 W3，本阶段不注册路由。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import AppException, ErrorCode
from app.core.logging import get_logger
from app.core.w3_errors import APPROVAL_EXPIRED, conflict
from app.schemas.enums import ApprovalStatus, ApprovalType
from app.schemas.orm_models import Approval
from app.utils.time import utcnow

logger = get_logger(__name__)

DEFAULT_APPROVAL_TTL_HOURS = 24  # PRD 6.5 / 旅程二异常流


def _factory(session_factory: async_sessionmaker[AsyncSession] | None) -> async_sessionmaker[AsyncSession]:
    if session_factory is not None:
        return session_factory
    from app.db.session import get_session_factory

    return get_session_factory()


async def create_pending_approval(
    *,
    task_id: int | None,
    subtask_id: int | None = None,
    type: str = ApprovalType.HIGH_RISK_TOOL.value,  # noqa: A002 - 与 PRD 字段名保持一致
    reason: str = "",
    payload: dict[str, Any] | None = None,
    ttl_hours: int = DEFAULT_APPROVAL_TTL_HOURS,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any]:
    """创建 PENDING 审批单，返回 interrupt 所需的最小信息。"""
    if task_id is None:
        raise AppException(ErrorCode.VALIDATION_ERROR, "创建审批单缺少 task_id")
    try:
        task_id_int = int(task_id)
    except (TypeError, ValueError) as exc:
        raise AppException(ErrorCode.VALIDATION_ERROR, f"非法 task_id: {task_id}") from exc

    expires_at = utcnow() + timedelta(hours=ttl_hours)
    approval = Approval(
        task_id=task_id_int,
        subtask_id=subtask_id,
        type=type,
        reason=(reason or "高危动作需审批")[:255],
        payload=payload or {},
        status=ApprovalStatus.PENDING,
        requested_by=0,  # PRD 6.5：系统发起固定 0
        expires_at=expires_at,
    )
    async with _factory(session_factory)() as session:
        session.add(approval)
        await session.commit()
        await session.refresh(approval)
        logger.info(
            "approval_pending_created",
            extra={"approval_id": approval.id, "task_id": task_id_int, "expires_at": expires_at.isoformat()},
        )
        return {
            "approval_id": approval.id,
            "status": approval.status.value,
            "expires_at": expires_at.isoformat(),
            "type": approval.type.value,
            "reason": approval.reason,
        }


async def apply_decision(
    approval_id: int,
    *,
    decision: str,
    comment: str = "",
    decided_by: int | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any]:
    """落审批决策（幂等：已终态直接返回当前状态）。

    **L2 二次校验**（PRD L245：审批挂起超 24h 即失效）：定时收口最长有一个扫描周期
    （默认 5 分钟）的滞后，这段时间里过期单在数据库里仍是 PENDING。若不加这道闸，
    就会出现"显示已过期、却还能点通过"的窗口。命中时顺手把单子落为 EXPIRED 再抛 409，
    这样即使 sweep 挂了，状态也会被第一个撞上来的审批动作纠正。
    """
    status = ApprovalStatus.APPROVED if str(decision).upper() in {"APPROVED", "APPROVE"} else ApprovalStatus.REJECTED
    async with _factory(session_factory)() as session:
        approval = await session.get(Approval, int(approval_id))
        if approval is None:
            raise AppException(ErrorCode.NOT_FOUND, f"审批单 {approval_id} 不存在")
        if approval.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED}:
            return _to_dict(approval)
        if approval.status == ApprovalStatus.PENDING and approval.expires_at is not None:
            if approval.expires_at < utcnow():
                approval.status = ApprovalStatus.EXPIRED
                approval.decided_at = utcnow()
                await session.commit()
                logger.warning(
                    "approval_expired_on_decide",
                    extra={"approval_id": int(approval_id), "expires_at": approval.expires_at.isoformat()},
                )
                raise conflict(
                    APPROVAL_EXPIRED,
                    "审批单已超过 24h 有效期，不能再批准；请让任务创建人重新发起",
                    approval_id=int(approval_id),
                    expires_at=approval.expires_at.isoformat(),
                )
        approval.status = status
        approval.decide_comment = (comment or "")[:500] or None
        approval.decided_by = decided_by
        approval.decided_at = utcnow()
        await session.commit()
        await session.refresh(approval)
        logger.info(
            "approval_decided",
            extra={"approval_id": approval.id, "status": approval.status.value, "decided_by": decided_by},
        )
        return _to_dict(approval)


async def get_approval(
    approval_id: int,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any] | None:
    async with _factory(session_factory)() as session:
        approval = await session.get(Approval, int(approval_id))
        return _to_dict(approval) if approval is not None else None


async def find_approved(
    task_id: int,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any] | None:
    async with _factory(session_factory)() as session:
        stmt = (
            select(Approval)
            .where(Approval.task_id == int(task_id), Approval.status == ApprovalStatus.APPROVED)
            .order_by(Approval.id.desc())
            .limit(1)
        )
        approval = (await session.execute(stmt)).scalar_one_or_none()
        return _to_dict(approval) if approval is not None else None


async def list_pending(
    *,
    page: int = 1,
    size: int = 20,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[dict[str, Any]]:
    """W3 审批收件箱数据源（本阶段仅供内部/测试调用）。"""
    async with _factory(session_factory)() as session:
        stmt = (
            select(Approval)
            .where(Approval.status == ApprovalStatus.PENDING)
            .order_by(Approval.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_dict(item) for item in rows]


def _to_dict(approval: Approval) -> dict[str, Any]:
    return {
        "approval_id": approval.id,
        "task_id": approval.task_id,
        "subtask_id": approval.subtask_id,
        "type": approval.type.value,
        "reason": approval.reason,
        "payload": approval.payload or {},
        "status": approval.status.value,
        "requested_by": approval.requested_by,
        "decided_by": approval.decided_by,
        "decide_comment": approval.decide_comment,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
    }


class ApprovalService:
    """按请求持有的 Session 版本（W3 REST 用），W2 内部走模块级函数。"""

    def __init__(self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self.session = session
        self._factory = session_factory

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        return await create_pending_approval(**kwargs, session_factory=self._factory)

    async def decide(self, approval_id: int, *, decision: str, comment: str = "", decided_by: int | None = None) -> dict[str, Any]:
        return await apply_decision(
            approval_id, decision=decision, comment=comment, decided_by=decided_by, session_factory=self._factory
        )

    async def pending(self, page: int = 1, size: int = 20) -> list[dict[str, Any]]:
        return await list_pending(page=page, size=size, session_factory=self._factory)


__all__ = [
    "DEFAULT_APPROVAL_TTL_HOURS",
    "ApprovalService",
    "create_pending_approval",
    "apply_decision",
    "get_approval",
    "find_approved",
    "list_pending",
]
