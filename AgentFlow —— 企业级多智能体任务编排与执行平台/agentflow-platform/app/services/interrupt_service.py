"""审批断点服务（FR-011 / FR-012）：挂起上下文读取、恢复前置校验、审批收件箱查询。

状态判定口径（沿用 W2 已修复方案，禁止用 ``__interrupt__`` 字段）：
- 任务是否挂起 **只看** ``graph.aget_state(config).tasks[*].interrupts``；
- ``ainvoke`` / ``astream`` 的返回值里没有 ``__interrupt__``，用它判断必然误判成普通终态。

职责边界：
- 本模块只做"读上下文 + 校验 + 查询"，**不驱动图执行**（由 ``stream_service`` 负责），
  这样 resume 与初次执行共用同一条流式代码路径，事件口径不会分叉。
- 例外：``sweep_expired_approvals``（L1 超时收口）会写 ``approvals`` / ``tasks`` 两表，
  因为它是 PRD 明确要求的**状态机兜底**，不属于"用户发起的动作"，没有别的层适合承载。

审批超时语义（PRD L98 / L245 / L382，此前**完全未实现**）：
- 审批单 ``expires_at`` 是 24h 的硬期限，过期即 ``EXPIRED``，任务转 ``FAILED``；
- 收件箱与侧边栏红点只统计"PENDING 且未过期"的单据，过期单移到「已过期」页签；
- 批准入口二次校验，避免定时器还没扫到（最长一个扫描周期）时把过期单批掉。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.core.w3_config import w3
from app.core.w3_errors import (
    APPROVAL_EXPIRED,
    APPROVAL_NOT_FOUND,
    APPROVAL_STALE,
    RESUMABLE_STATUSES,
    TASK_NOT_PAUSED,
    conflict,
)
from app.schemas.enums import ApprovalStatus, TaskStatus
from app.schemas.orm_models import Approval, Task
from app.schemas.w3 import isoformat
from app.services.checkpoint_service import CheckpointService
from app.utils.time import utcnow

logger = get_logger(__name__)

# 断点键前缀：AsyncRedisSaver 以 thread_id(=task_id) 分片，三族键共用同一段 id。
# 收口时必须三族一起清——只清 checkpoint 会留下 blob/write 索引，语义上"还有断点"。
_CHECKPOINT_KEY_PREFIXES = ("checkpoint", "checkpoint_blob", "checkpoint_write")

# 探活/清理时最多扫多少个键，超了就放弃（fail-open），避免在超大 keyspace 上拖垮请求
_KEY_SCAN_CAP = 5000

APPROVAL_EXPIRED_MESSAGE = "审批超时未处理，任务已自动终止"


def _factory(session_factory: async_sessionmaker[AsyncSession] | None) -> async_sessionmaker[AsyncSession]:
    if session_factory is not None:
        return session_factory
    from app.db.session import get_session_factory

    return get_session_factory()


def _node_name(task: Any) -> str | None:
    name = getattr(task, "name", None)
    if name:
        return str(name)
    node = getattr(task, "node", None)
    return str(node) if node else None


async def load_snapshot(graph: Any, task_id: int | str) -> Any:
    return await graph.aget_state(CheckpointService.config(task_id))


def collect_interrupts(snapshot: Any) -> list[dict[str, Any]]:
    """从状态快照里摘出所有挂起项（含所属节点名）。"""
    items: list[dict[str, Any]] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        for interrupt in getattr(task, "interrupts", ()) or ():
            items.append(
                {
                    "node": _node_name(task),
                    "value": getattr(interrupt, "value", None),
                    "resumable": bool(getattr(interrupt, "resumable", True)),
                    "when": getattr(interrupt, "when", None),
                    "ns": list(getattr(interrupt, "ns", ()) or ()),
                }
            )
    return items


async def interrupt_context(
    graph: Any,
    task_id: int | str,
    *,
    trace_id: str | None = None,
    task_status: TaskStatus | str | None = None,
) -> dict[str, Any] | None:
    """返回挂起审批上下文；未挂起返回 None。"""
    try:
        snapshot = await load_snapshot(graph, task_id)
    except Exception as exc:  # noqa: BLE001 - 快照读取失败等价于"没有可恢复断点"
        logger.warning("interrupt_snapshot_failed", extra={"task_id": task_id, "error": str(exc)[:200]})
        return None

    interrupts = collect_interrupts(snapshot)
    if not interrupts:
        return None

    first = interrupts[0]
    value = first.get("value") if isinstance(first.get("value"), dict) else {}
    status_value = task_status.value if isinstance(task_status, TaskStatus) else (task_status or "")

    return {
        "paused": True,
        "task_id": int(task_id),
        "trace_id": trace_id,
        "status": status_value or TaskStatus.AWAITING_APPROVAL.value,
        "approval_id": value.get("approval_id"),
        "type": value.get("type"),
        "reason": value.get("reason"),
        "preview": value.get("preview") or {},
        "interrupted_at": isoformat(getattr(snapshot, "created_at", None)) or isoformat(first.get("when")),
        "next_node": [str(n) for n in (getattr(snapshot, "next", ()) or ())],
        "resumable": bool(first.get("resumable", True)),
        "resume_endpoint": f"/api/v1/tasks/{task_id}/resume",
        "node": first.get("node"),
        "interrupt_count": len(interrupts),
    }


async def ensure_resumable(
    graph: Any,
    task_id: int | str,
    task_status: TaskStatus | str | None,
    *,
    trace_id: str | None = None,
    approval_id: int | None = None,
) -> dict[str, Any]:
    """恢复前置校验：必须同时满足「DB 状态为 AWAITING_APPROVAL」+「checkpoint 有挂起项」。

    任一不满足 → 409 ``TASK_NOT_PAUSED``（需求：非暂停任务直接抛冲突）。
    """
    status_enum = task_status if isinstance(task_status, TaskStatus) else TaskStatus(str(task_status))
    if status_enum not in RESUMABLE_STATUSES:
        raise conflict(
            TASK_NOT_PAUSED,
            "任务当前不在等待审批状态，无法恢复",
            task_id=int(task_id),
            status=status_enum.value,
        )

    context = await interrupt_context(graph, task_id, trace_id=trace_id, task_status=status_enum)
    if context is None:
        raise conflict(
            TASK_NOT_PAUSED,
            "checkpoint 中不存在挂起断点，无法恢复",
            task_id=int(task_id),
            status=status_enum.value,
        )

    current_approval = context.get("approval_id")
    if approval_id is not None and current_approval is not None and int(approval_id) != int(current_approval):
        raise conflict(
            APPROVAL_STALE,
            "审批单与当前挂起断点不一致，请刷新后重试",
            task_id=int(task_id),
            expected_approval_id=current_approval,
            given_approval_id=int(approval_id),
        )
    return context


async def clear_interrupts(graph: Any, task_id: int | str, *, status: str = "CANCELLED") -> bool:
    """把挂起断点"打死"：写入终态并清空 next/interrupts（取消任务时调用）。

    实现要点：``aupdate_state`` 会生成新 checkpoint，``next`` 变为空、``tasks[*].interrupts``
    被清空（实测行为），因此后续任何 resume 都会因"无断点"被 409 拦截。
    """
    config = CheckpointService.config(task_id)
    try:
        snapshot = await graph.aget_state(config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("clear_interrupt_snapshot_failed", extra={"task_id": task_id, "error": str(exc)[:200]})
        return False

    if getattr(snapshot, "created_at", None) is None:
        return False

    pending_nodes = [str(n) for n in (getattr(snapshot, "next", ()) or ())]
    as_node = pending_nodes[0] if pending_nodes else None
    try:
        await graph.aupdate_state(config, {"status": status}, as_node=as_node)
        logger.info("interrupt_cleared", extra={"task_id": task_id, "as_node": as_node, "status": status})
        return True
    except Exception:  # noqa: BLE001 - as_node 不合法时退回无 as_node 写法
        try:
            await graph.aupdate_state(config, {"status": status})
            logger.info("interrupt_cleared_without_node", extra={"task_id": task_id})
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("interrupt_clear_failed", extra={"task_id": task_id, "error": str(exc)[:200]})
            return False


async def expire_pending_approvals(
    task_id: int,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """任务被取消时把 PENDING 审批单置 EXPIRED，避免遗留"可审批"脏单。"""
    async with _factory(session_factory)() as session:
        result = await session.execute(
            update(Approval)
            .where(Approval.task_id == int(task_id), Approval.status == ApprovalStatus.PENDING)
            .values(status=ApprovalStatus.EXPIRED)
        )
        await session.commit()
        return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# L1：审批超时收口（定时器 + 存量修复脚本共用同一实现）
# ---------------------------------------------------------------------------
async def _purge_task_checkpoints(task_ids: Sequence[int]) -> int:
    """删掉这些任务的 Redis 断点键，返回删除条数（任何异常都不抛，最多记一条日志）。

    为什么不复用 ``clear_interrupts``：它要 graph 实例，而后台定时器里
    ``get_w3_runtime()`` 会把 4 个 MCP 子进程拉起来（每 5 分钟一次不可接受）。
    直接删键能达成同样的两条不变量：
      1) 终态任务不可能再 resume（``ensure_resumable`` 另有状态闸，双保险）；
      2) ``GET /tasks/{id}/interrupt`` 不会再对已失败的任务报 ``paused=true``（僵尸视图）。
    代价是该任务无法"从最近 checkpoint 续跑"，退化为从头执行——PRD L317 允许此降级。
    """
    ids = [int(i) for i in task_ids]
    if not ids:
        return 0

    from app.core.redis_client import get_shared_redis

    try:
        redis = get_shared_redis()
    except Exception as exc:  # noqa: BLE001 - 收口是尽力而为，不能反过来炸掉任务状态
        logger.warning("checkpoint_purge_unavailable", extra={"error": str(exc)[:200]})
        return 0

    deleted = 0
    try:
        for task_id in ids:
            for prefix in _CHECKPOINT_KEY_PREFIXES:
                batch: list[str] = []
                async for key in redis.scan_iter(match=f"{prefix}:{task_id}:*", count=200):
                    batch.append(key)
                    if len(batch) >= 200:
                        deleted += int(await redis.delete(*batch))
                        batch.clear()
                if batch:
                    deleted += int(await redis.delete(*batch))
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpoint_purge_failed", extra={"task_ids": ids[:20], "error": str(exc)[:200]})
    return deleted


async def sweep_expired_approvals(
    *,
    now: datetime | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    purge_checkpoints: bool | None = None,
) -> dict[str, Any]:
    """L1 超时收口：一次性把"该失效的审批"和"卡住的任务"全部推到终态。

    收两类脏单，都置 ``EXPIRED``：
    1. **超时单**：``expires_at < now``（PRD 24h 硬期限）；
    2. **孤儿单**：任务早已不在 ``AWAITING_APPROVAL``，审批单却还是 PENDING
       （历史取消/异常路径遗留，实测库里 #5~#8 就是这种）。
    只有第 1 类且任务确实还挂在审批上时，才把任务置 ``FAILED`` —— 
    孤儿单的任务已经有自己的终态，不能被超时逻辑覆盖。

    并发说明：每个进程各起一个定时器，多 worker 部署时会同时扫到同一批单。
    由于写的是同一个目标状态（EXPIRED / FAILED），重复执行天然幂等，不需要分布式锁；
    真正的风险是"同一任务被两个 worker 同时 resume"——那由 ``task_lock`` 负责。
    """
    moment = now or utcnow()
    if purge_checkpoints is None:
        purge_checkpoints = bool(w3.APPROVAL_SWEEP_PURGE_CHECKPOINT)

    expired_ids: list[int] = []
    failed_task_ids: list[int] = []
    orphan_ids: list[int] = []

    async with _factory(session_factory)() as session:
        pending = (
            (await session.execute(select(Approval).where(Approval.status == ApprovalStatus.PENDING)))
            .scalars()
            .all()
        )
        if not pending:
            return {"scanned": 0, "expired": [], "tasks_failed": [], "orphans": [], "checkpoint_keys_deleted": 0}

        task_ids = {int(row.task_id) for row in pending}
        tasks = {
            int(task.id): task
            for task in (await session.execute(select(Task).where(Task.id.in_(task_ids)))).scalars().all()
        }

        for approval in pending:
            task = tasks.get(int(approval.task_id))
            overdue = approval.expires_at is not None and approval.expires_at < moment
            still_awaiting = task is not None and task.status == TaskStatus.AWAITING_APPROVAL
            orphan = not still_awaiting

            if not (overdue or orphan):
                continue

            approval.status = ApprovalStatus.EXPIRED
            # decided_at 复用作"失效时间"：EXPIRED 没有真实决策人，但需要一个排序锚点，
            # 否则「已处理」列表里超时单会全部沉到末尾（MySQL 中 NULL 在 DESC 里最靠后）。
            approval.decided_at = moment
            expired_ids.append(int(approval.id))

            if overdue and still_awaiting:
                task.status = TaskStatus.FAILED
                task.error = {
                    "code": APPROVAL_EXPIRED,
                    "message": APPROVAL_EXPIRED_MESSAGE,
                    "span_id": None,
                }
                task.finished_at = moment
                failed_task_ids.append(int(task.id))
            elif orphan:
                orphan_ids.append(int(approval.id))

        if expired_ids:
            await session.commit()

    deleted = 0
    if purge_checkpoints and failed_task_ids:
        deleted = await _purge_task_checkpoints(failed_task_ids)

    if expired_ids:
        logger.warning(
            "approval_sweep_done",
            extra={
                "expired_approval_ids": expired_ids[:50],
                "failed_task_ids": failed_task_ids[:50],
                "orphan_approval_ids": orphan_ids[:50],
                "checkpoint_keys_deleted": deleted,
            },
        )

    return {
        "scanned": len(expired_ids),
        "expired": expired_ids,
        "tasks_failed": failed_task_ids,
        "orphans": orphan_ids,
        "checkpoint_keys_deleted": deleted,
    }


# 后台收口协程（进程内单例；lifespan 启动、关闭时取消）
_sweeper_task: asyncio.Task[None] | None = None


async def _sweep_loop(interval_seconds: int) -> None:
    logger.info("approval_sweeper_started", extra={"interval_seconds": interval_seconds})
    while True:
        # 启动即扫一次：重启后现存脏单立刻收口，不用等第一个周期
        try:
            result = await sweep_expired_approvals()
            if result["expired"]:
                logger.warning(
                    "approval_sweeper_hit",
                    extra={"expired": len(result["expired"]), "tasks_failed": len(result["tasks_failed"])},
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 定时器绝不能因单次失败而退出
            logger.warning("approval_sweep_failed", extra={"error": str(exc)[:200]})
        await asyncio.sleep(interval_seconds)


async def start_approval_sweeper(interval_seconds: int | None = None) -> asyncio.Task[None] | None:
    """启动后台收口协程；已启动则复用。``APPROVAL_SWEEP_ENABLED=false`` 时不启动。"""
    global _sweeper_task
    if not w3.APPROVAL_SWEEP_ENABLED:
        logger.info("approval_sweeper_disabled")
        return None
    if _sweeper_task is not None and not _sweeper_task.done():
        return _sweeper_task
    interval = max(5, int(interval_seconds or w3.APPROVAL_SWEEP_INTERVAL_SECONDS))
    _sweeper_task = asyncio.create_task(_sweep_loop(interval), name="approval-sweeper")
    return _sweeper_task


async def stop_approval_sweeper() -> None:
    global _sweeper_task
    task, _sweeper_task = _sweeper_task, None
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    logger.info("approval_sweeper_stopped")


async def probe_resumable(task_ids: Sequence[int], redis_client: Any = None) -> dict[int, bool]:
    """粗粒度探活：Redis 里是否还有这些任务的断点键。

    fail-open 原则：探不出来（无 Redis 客户端 / 非 Redis checkpointer / 扫描异常 / 键太多）
    一律当作"可恢复"，宁可让用户点进去看到抽屉里的真实报错，也不要凭空禁用审批入口。
    """
    ids = {int(i) for i in task_ids}
    if not ids:
        return {}
    unknown = {i: True for i in ids}
    if redis_client is None:
        return unknown
    try:
        from app.db.checkpointer import checkpointer_status

        backend = str(checkpointer_status().get("backend") or "")
        if backend not in {"auto", "redis"}:
            return unknown  # 内存 checkpointer，Redis 里本来就没有键，探了必误判
    except Exception:  # noqa: BLE001
        return unknown

    alive: set[int] = set()
    try:
        seen = 0
        async for key in redis_client.scan_iter(match="checkpoint:*", count=500):
            seen += 1
            if seen > _KEY_SCAN_CAP:
                logger.warning("resumable_probe_cap_reached", extra={"cap": _KEY_SCAN_CAP})
                return unknown
            parts = str(key).split(":")
            if len(parts) >= 3 and parts[1].isdigit():
                candidate = int(parts[1])
                if candidate in ids:
                    alive.add(candidate)
    except Exception as exc:  # noqa: BLE001
        logger.warning("resumable_probe_failed", extra={"error": str(exc)[:200]})
        return unknown
    return {i: i in alive for i in ids}


async def find_pending_approval(
    task_id: int,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any] | None:
    async with _factory(session_factory)() as session:
        stmt = (
            select(Approval)
            .where(Approval.task_id == int(task_id), Approval.status == ApprovalStatus.PENDING)
            .order_by(Approval.id.desc())
            .limit(1)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        return _approval_to_dict(row) if row is not None else None


async def get_approval_by_id(
    approval_id: int,
    *,
    session: AsyncSession | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any] | None:
    """读取审批单；优先用请求级 Session（避免额外连接池），否则自建。"""
    if session is not None:
        row = await session.get(Approval, int(approval_id))
        if row is None:
            raise conflict(APPROVAL_NOT_FOUND, f"审批单 {approval_id} 不存在", approval_id=int(approval_id))
        return _approval_to_dict(row)
    async with _factory(session_factory)() as own_session:
        row = await own_session.get(Approval, int(approval_id))
        if row is None:
            raise conflict(APPROVAL_NOT_FOUND, f"审批单 {approval_id} 不存在", approval_id=int(approval_id))
        return _approval_to_dict(row)


async def list_paused_tasks(
    session: AsyncSession,
    *,
    creator_id: int | None = None,
    page: int = 1,
    size: int = 20,
    redis_client: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """审批收件箱数据源：AWAITING_APPROVAL 任务 + 最新 PENDING 审批单 + 阻断原因。

    **口径（2026-10 修订，勿改回）**：待处理 = 任务卡在 AWAITING_APPROVAL **且**审批单未过期。
    过期单不算待办——它已经批不动了，把它计入 ``total`` 会让侧边栏红点永远清不掉
    （历史现象：5 张超期 3 天的僵尸单让红点常亮）。过期单归「已过期」页签（只读）。
    审批单缺失（``expires_at IS NULL``）视为无期限，仍算待办，避免历史数据突然消失。
    """
    moment = now or utcnow()

    base = select(Task).where(Task.status == TaskStatus.AWAITING_APPROVAL)
    if creator_id is not None:
        base = base.where(Task.creator_id == int(creator_id))

    # 子查询：只要存在一条"未过期的 PENDING 审批单"才算待办（同任务多单时以最新为准由下方取）
    live_approval = select(Approval.id).where(
        Approval.task_id == Task.id,
        Approval.status == ApprovalStatus.PENDING,
        (Approval.expires_at.is_(None)) | (Approval.expires_at >= moment),
    )
    base = base.where(live_approval.exists())

    total = int((await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    rows = (
        (
            await session.execute(
                base.order_by(Task.created_at.desc(), Task.id.desc()).offset((page - 1) * size).limit(size)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {"items": [], "page": page, "size": size, "total": total}

    task_ids = [row.id for row in rows]
    approvals = (
        (
            await session.execute(
                select(Approval)
                .where(Approval.task_id.in_(task_ids), Approval.status == ApprovalStatus.PENDING)
                .order_by(Approval.id.desc())
            )
        )
        .scalars()
        .all()
    )
    latest: dict[int, Approval] = {}
    latest_live: dict[int, Approval] = {}
    for approval in approvals:
        owner = int(approval.task_id)
        latest.setdefault(owner, approval)
        if approval.expires_at is None or approval.expires_at >= moment:
            latest_live.setdefault(owner, approval)

    alive_map = await probe_resumable(task_ids, redis_client)

    items: list[dict[str, Any]] = []
    for task in rows:
        # 优先展示"未过期"的那张单：同任务可能存在多张 PENDING（历史重规划），
        # 展示已过期的那张会让人以为"能批但没入口"。
        approval = latest_live.get(int(task.id)) or latest.get(int(task.id))
        payload = (approval.payload if approval is not None else None) or {}
        subtasks = payload.get("subtasks") if isinstance(payload, dict) else None
        items.append(
            {
                "task_id": int(task.id),
                "task_no": task.task_no,
                "goal": task.goal,
                "status": task.status.value,
                "creator_id": int(task.creator_id),
                "approval_id": int(approval.id) if approval is not None else None,
                "approval_type": approval.type.value if approval is not None else None,
                "reason": approval.reason if approval is not None else None,
                "risky_subtasks": subtasks or [],
                "created_at": isoformat(task.created_at),
                "expires_at": isoformat(approval.expires_at) if approval is not None else None,
                # 断点还在不在：false = 死锁（批了也 409 TASK_NOT_PAUSED），前端据此禁用行内审批
                "resumable": bool(alive_map.get(int(task.id), True)),
            }
        )
    return {"items": items, "page": page, "size": size, "total": total}


def _approval_to_dict(row: Approval) -> dict[str, Any]:
    return {
        "approval_id": int(row.id),
        "task_id": int(row.task_id),
        "subtask_id": int(row.subtask_id) if row.subtask_id else None,
        "type": row.type.value if hasattr(row.type, "value") else str(row.type),
        "reason": row.reason,
        "status": row.status.value if hasattr(row.status, "value") else str(row.status),
        "payload": row.payload or {},
        "decided_by": row.decided_by,
        "decide_comment": row.decide_comment,
        "decided_at": isoformat(row.decided_at),
        "expires_at": isoformat(row.expires_at),
    }


async def list_processed_approvals(
    session: AsyncSession,
    *,
    creator_id: int | None = None,
    statuses: tuple[ApprovalStatus, ...] = (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """已处理审批列表（缺口 G4）：数据源 ``approvals`` 表按终态过滤。

    与待处理收件箱（``list_paused_tasks``）互补：
    - 待处理 = 任务卡在 AWAITING_APPROVAL + 最新 PENDING 审批单（阻断上下文）；
    - 已处理 = 审批单自身已到终态（APPROVED / REJECTED / EXPIRED），含审核人/意见/时间。

    ADMIN 只看本人任务，APPROVER 看全量（与 PRD 2.1 角色矩阵一致），由路由层传入 creator_id。
    """
    base = select(Approval).where(Approval.status.in_(statuses))
    if creator_id is not None:
        base = base.join(Task, Task.id == Approval.task_id).where(Task.creator_id == int(creator_id))

    total = int((await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    rows = (
        (
            await session.execute(
                base.order_by(Approval.decided_at.desc(), Approval.id.desc()).offset((page - 1) * size).limit(size)
            )
        )
        .scalars()
        .all()
    )

    items: list[dict[str, Any]] = []
    for approval in rows:
        item = _approval_to_dict(approval)
        item["task_no"] = None
        item["goal"] = None
        items.append(item)

    # 批量补 task_no / goal，避免 N+1 查询
    if items:
        task_ids = [int(item["task_id"]) for item in items]
        tasks = (
            (await session.execute(select(Task).where(Task.id.in_(task_ids)))).scalars().all()
        )
        by_id = {int(task.id): task for task in tasks}
        for item in items:
            task = by_id.get(int(item["task_id"]))
            if task is not None:
                item["task_no"] = task.task_no
                item["goal"] = task.goal

    return {"items": items, "page": page, "size": size, "total": total}


__all__ = [
    "load_snapshot",
    "collect_interrupts",
    "interrupt_context",
    "ensure_resumable",
    "clear_interrupts",
    "expire_pending_approvals",
    "sweep_expired_approvals",
    "start_approval_sweeper",
    "stop_approval_sweeper",
    "probe_resumable",
    "find_pending_approval",
    "get_approval_by_id",
    "list_paused_tasks",
    "list_processed_approvals",
    "APPROVAL_EXPIRED_MESSAGE",
]
