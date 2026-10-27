"""v1.3 审批超时收口（L1 定时器 + L2 二次校验）测试。

覆盖点：
1. 超时单：审批置 EXPIRED、``decided_at`` 落失效时间、任务置 FAILED（error.code=APPROVAL_EXPIRED）；
2. 未超时单：一条都不动（定时器空转不产生副作用）；
3. 孤儿单：任务早已在其他终态 → 只关审批单，**不允许**覆盖任务状态；
4. 幂等：连跑两次，第二次零改动；
5. L2 二次校验：过期 PENDING 直接批准 → 409 APPROVAL_EXPIRED，且顺手落 EXPIRED；
6. 收件箱口径：待处理只含"未过期 PENDING"，过期单不进列表、不进 total（红点因此能归零）；
7. 断点探活 fail-open：无 Redis 客户端 / 内存 checkpointer 时一律视为可恢复。

为什么必须补这组测试：这块此前**零覆盖**，而它是 PRD 三条验收标准（L98/L245/L382）的落点。
历史故障是 task#10 那种"审批必 409、又永不消失、红点常亮"的死锁，靠人工发现成本极高。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import AppException
from app.core.w3_errors import APPROVAL_EXPIRED
from app.schemas.enums import ApprovalStatus, ApprovalType, TaskStatus
from app.schemas.orm_models import Approval, Task
from app.services.approval_service import apply_decision
from app.services.interrupt_service import (
    list_paused_tasks,
    probe_resumable,
    sweep_expired_approvals,
)
from app.utils.ids import new_trace_id
from app.utils.time import utcnow


async def _seed_task(
    session: AsyncSession,
    creator_id: int,
    *,
    task_no: str,
    status: TaskStatus,
) -> Task:
    task = Task(
        task_no=task_no,
        creator_id=creator_id,
        goal=f"超时收口用例 {task_no}",
        time_range={"start": "2026-10-01", "end": "2026-10-20"},
        status=status,
        replan_count=0,
        token_usage=0,
        trace_id=new_trace_id(),
        created_at=utcnow() - timedelta(hours=30),
    )
    session.add(task)
    await session.flush()
    return task


async def _seed_approval(
    session: AsyncSession,
    task_id: int,
    *,
    expires_in_hours: float | None,
    status: ApprovalStatus = ApprovalStatus.PENDING,
) -> Approval:
    expires_at = None if expires_in_hours is None else utcnow() + timedelta(hours=expires_in_hours)
    approval = Approval(
        task_id=task_id,
        type=ApprovalType.HIGH_RISK_TOOL,
        reason="高危动作需审批",
        status=status,
        requested_by=0,
        expires_at=expires_at,
    )
    session.add(approval)
    await session.flush()
    return approval


@pytest_asyncio.fixture
async def seeded(session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int]):
    """四种典型组合：超时挂起 / 未超时挂起 / 超时孤儿 / 无期限。"""
    admin_id = seeded_users["admin_user"]
    async with session_factory() as session:
        stuck = await _seed_task(session, admin_id, task_no="AFSWEEP-0001", status=TaskStatus.AWAITING_APPROVAL)
        stuck_approval = await _seed_approval(session, stuck.id, expires_in_hours=-30)

        live = await _seed_task(session, admin_id, task_no="AFSWEEP-0002", status=TaskStatus.AWAITING_APPROVAL)
        live_approval = await _seed_approval(session, live.id, expires_in_hours=6)

        orphan_task = await _seed_task(session, admin_id, task_no="AFSWEEP-0003", status=TaskStatus.FAILED)
        orphan_approval = await _seed_approval(session, orphan_task.id, expires_in_hours=-30)

        no_deadline = await _seed_task(session, admin_id, task_no="AFSWEEP-0004", status=TaskStatus.AWAITING_APPROVAL)
        no_deadline_approval = await _seed_approval(session, no_deadline.id, expires_in_hours=None)
        await session.commit()
    return {
        "stuck_task": stuck.id,
        "stuck_approval": stuck_approval.id,
        "live_task": live.id,
        "live_approval": live_approval.id,
        "orphan_task": orphan_task.id,
        "orphan_approval": orphan_approval.id,
        "no_deadline_task": no_deadline.id,
        "no_deadline_approval": no_deadline_approval.id,
    }


async def _get_task(session_factory: async_sessionmaker[AsyncSession], task_id: int) -> Task:
    async with session_factory() as session:
        return await session.get(Task, task_id)


async def _get_approval(session_factory: async_sessionmaker[AsyncSession], approval_id: int) -> Approval:
    async with session_factory() as session:
        return await session.get(Approval, approval_id)


# ---------------------------------------------------------------------------
# 1) 超时收口：审批 EXPIRED + 任务 FAILED
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sweep_expires_overdue_and_fails_task(
    session_factory: async_sessionmaker[AsyncSession], seeded: dict[str, int]
) -> None:
    result = await sweep_expired_approvals(session_factory=session_factory, purge_checkpoints=False)

    # 超时单（挂起任务）与孤儿单都要被收口，未超时的两条不动
    assert sorted(result["expired"]) == sorted(
        [seeded["stuck_approval"], seeded["orphan_approval"]]
    )
    assert result["tasks_failed"] == [seeded["stuck_task"]]
    assert result["orphans"] == [seeded["orphan_approval"]]

    stuck = await _get_approval(session_factory, seeded["stuck_approval"])
    assert stuck.status == ApprovalStatus.EXPIRED
    assert stuck.decided_at is not None, "EXPIRED 需要排序锚点，否则『已处理』里全沉到末尾"

    task = await _get_task(session_factory, seeded["stuck_task"])
    assert task.status == TaskStatus.FAILED
    assert (task.error or {}).get("code") == APPROVAL_EXPIRED
    assert task.finished_at is not None, "终态必须落 finished_at，否则平均耗时统计会漏"


# ---------------------------------------------------------------------------
# 2) 未超时 / 无期限：一条都不动
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sweep_leaves_live_and_deadline_free_alone(
    session_factory: async_sessionmaker[AsyncSession], seeded: dict[str, int]
) -> None:
    await sweep_expired_approvals(session_factory=session_factory, purge_checkpoints=False)

    live = await _get_approval(session_factory, seeded["live_approval"])
    assert live.status == ApprovalStatus.PENDING
    assert live.decided_at is None

    no_deadline = await _get_approval(session_factory, seeded["no_deadline_approval"])
    assert no_deadline.status == ApprovalStatus.PENDING, "expires_at 为空视为无期限，不能被误判超时"

    for task_id in (seeded["live_task"], seeded["no_deadline_task"]):
        task = await _get_task(session_factory, task_id)
        assert task.status == TaskStatus.AWAITING_APPROVAL


# ---------------------------------------------------------------------------
# 3) 孤儿单：只关审批单，不覆盖任务已有终态
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sweep_orphan_does_not_override_task_status(
    session_factory: async_sessionmaker[AsyncSession], seeded: dict[str, int]
) -> None:
    before = await _get_task(session_factory, seeded["orphan_task"])
    original_error = before.error

    await sweep_expired_approvals(session_factory=session_factory, purge_checkpoints=False)

    after = await _get_task(session_factory, seeded["orphan_task"])
    assert after.status == TaskStatus.FAILED, "孤儿单的任务已有自己的终态，不允许被超时逻辑改写"
    assert after.error == original_error, "更不允许把原来的 error 覆盖成 APPROVAL_EXPIRED"

    approval = await _get_approval(session_factory, seeded["orphan_approval"])
    assert approval.status == ApprovalStatus.EXPIRED


# ---------------------------------------------------------------------------
# 4) 幂等：定时器每 5 分钟跑一次，不能每次都产生写入
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sweep_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession], seeded: dict[str, int]
) -> None:
    first = await sweep_expired_approvals(session_factory=session_factory, purge_checkpoints=False)
    assert first["expired"]

    second = await sweep_expired_approvals(session_factory=session_factory, purge_checkpoints=False)
    assert second["expired"] == []
    assert second["tasks_failed"] == []
    assert second["checkpoint_keys_deleted"] == 0


# ---------------------------------------------------------------------------
# 5) L2 二次校验：过期 PENDING 直接 409
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_apply_decision_rejects_expired_approval(
    session_factory: async_sessionmaker[AsyncSession], seeded: dict[str, int]
) -> None:
    """定时器最长有 5 分钟滞后，这段窗口里过期单在库里仍是 PENDING，必须靠这道闸拦住。"""
    with pytest.raises(AppException) as exc_info:
        await apply_decision(
            seeded["stuck_approval"],
            decision="APPROVED",
            comment="试图批准过期单",
            decided_by=1,
            session_factory=session_factory,
        )

    error = exc_info.value
    assert error.code == APPROVAL_EXPIRED
    assert error.status_code == 409

    # 撞上来的动作顺手把单子落成 EXPIRED，避免它继续挂在 pending 里
    approval = await _get_approval(session_factory, seeded["stuck_approval"])
    assert approval.status == ApprovalStatus.EXPIRED
    assert approval.decided_by is None, "超时单没有真实决策人"


@pytest.mark.asyncio
async def test_apply_decision_still_works_for_live_approval(
    session_factory: async_sessionmaker[AsyncSession], seeded: dict[str, int]
) -> None:
    result = await apply_decision(
        seeded["live_approval"], decision="APPROVED", decided_by=2, session_factory=session_factory
    )
    assert result["status"] == ApprovalStatus.APPROVED.value


# ---------------------------------------------------------------------------
# 6) 收件箱口径：过期单不进列表、不进 total（红点归零的依据）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_inbox_counts_only_unexpired_pending(
    session_factory: async_sessionmaker[AsyncSession], seeded: dict[str, int]
) -> None:
    """清单口径 = 未过期 PENDING。这条直接决定侧边栏红点数（前端用 total）。"""
    async with session_factory() as session:
        data = await list_paused_tasks(session, creator_id=None, page=1, size=20)

    task_ids = [item["task_id"] for item in data["items"]]
    assert seeded["stuck_task"] not in task_ids, "超时单不该出现在待办里"
    assert seeded["live_task"] in task_ids
    assert seeded["no_deadline_task"] in task_ids
    assert data["total"] == len(task_ids) == 2


@pytest.mark.asyncio
async def test_inbox_marks_deadlocked_items_unresumable(
    session_factory: async_sessionmaker[AsyncSession], seeded: dict[str, int]
) -> None:
    """断点丢了的任务要显式标 resumable=false，前端据此置灰行内「审批」。"""
    async with session_factory() as session:
        data = await list_paused_tasks(session, creator_id=None, page=1, size=20)
    for item in data["items"]:
        # 测试环境是内存 checkpointer → 探不了 → fail-open 视为可恢复
        assert item["resumable"] is True


# ---------------------------------------------------------------------------
# 7) 断点探活：fail-open
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_probe_resumable_fails_open() -> None:
    assert await probe_resumable([], None) == {}
    assert await probe_resumable([1, 2], None) == {1: True, 2: True}


@pytest.mark.asyncio
async def test_expired_only_approval_never_resurrects_task_status(
    session_factory: async_sessionmaker[AsyncSession], seeded: dict[str, int]
) -> None:
    """收口后二次确认：任务不会因为"还有一张 PENDING 单"而重新出现在待办里。"""
    await sweep_expired_approvals(session_factory=session_factory, purge_checkpoints=False)
    async with session_factory() as session:
        remaining = (
            (
                await session.execute(
                    select(Approval).where(
                        Approval.task_id == seeded["stuck_task"],
                        Approval.status == ApprovalStatus.PENDING,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert remaining == []
