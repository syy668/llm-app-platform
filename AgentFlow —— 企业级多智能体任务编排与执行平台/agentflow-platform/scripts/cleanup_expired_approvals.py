"""存量修复：把"超期 / 孤儿"的 PENDING 审批单收口成 EXPIRED（默认只读，不落库）。

背景（2026-10 实测）：库里有 5 张 PENDING 单全部超期 3 天，其中 task#10 更已经死锁——
任务还挂在 AWAITING_APPROVAL，但 Redis 断点早被 72h TTL 清空，于是"批准必 409、
又永不消失"，侧边栏红点常亮。定时器上线后新单会自动收口，但**存量**需要一次性修。

默认 dry-run：只打印"哪些单会被 kill、哪些任务会被置 FAILED"，不做任何写操作。
确认无误后加 ``--apply`` 真正落库（幂等，可重复跑）。

用法::

    # 1) 先看（默认行为，等价于 --dry-run）
    python scripts/cleanup_expired_approvals.py

    # 2) 确认无误后执行
    python scripts/cleanup_expired_approvals.py --apply

    # 3) 演示库（dev_serve.py 的 SQLite）
    python scripts/cleanup_expired_approvals.py --sqlite storage/_dev_serve.db --apply

收口口径与运行时定时器**完全一致**（直接复用 ``sweep_expired_approvals``），
避免"脚本改一套、服务改另一套"的口径分叉。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(env_file)


def _safe_stdout() -> None:
    """Windows 控制台默认 GBK，中文/emoji 会 UnicodeEncodeError。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


async def _inspect(session_factory: Any, now: Any) -> list[dict[str, Any]]:
    """只读扫描：列出所有需要收口的 PENDING 单及其将产生的动作。"""
    from sqlalchemy import select

    from app.schemas.enums import ApprovalStatus, TaskStatus
    from app.schemas.orm_models import Approval, Task

    rows: list[dict[str, Any]] = []
    async with session_factory() as session:
        pending = (
            (await session.execute(select(Approval).where(Approval.status == ApprovalStatus.PENDING)))
            .scalars()
            .all()
        )
        if not pending:
            return rows
        task_ids = {int(a.task_id) for a in pending}
        tasks = {
            int(t.id): t
            for t in (await session.execute(select(Task).where(Task.id.in_(task_ids)))).scalars().all()
        }
        for approval in pending:
            task = tasks.get(int(approval.task_id))
            overdue = approval.expires_at is not None and approval.expires_at < now
            awaiting = task is not None and task.status == TaskStatus.AWAITING_APPROVAL
            orphan = not awaiting
            if not (overdue or orphan):
                continue
            overdue_days = (now - approval.expires_at).days if overdue and approval.expires_at else None
            if overdue and awaiting:
                action = "approval→EXPIRED, task→FAILED(APPROVAL_EXPIRED), 清断点"
            elif overdue:
                action = f"approval→EXPIRED（任务已是 {task.status.value if task else 'MISSING'}，不动）"
            else:
                action = f"approval→EXPIRED（孤儿单，任务已是 {task.status.value if task else 'MISSING'}）"
            rows.append(
                {
                    "approval_id": int(approval.id),
                    "task_id": int(approval.task_id),
                    "task_no": task.task_no if task else "—",
                    "task_status": task.status.value if task else "MISSING",
                    "expires_at": approval.expires_at.isoformat() if approval.expires_at else "—",
                    "overdue_days": overdue_days,
                    "action": action,
                }
            )
    return rows


async def _run(args: argparse.Namespace) -> int:
    from app.utils.time import utcnow

    engine = None
    if args.sqlite:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        path = Path(args.sqlite)
        if not path.exists():
            print(f"[error] {path} 不存在（先跑一次 dev_serve.py 建库）")
            return 1
        engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        print(f"[sqlite] {path}")
    else:
        from app.db.session import get_session_factory

        session_factory = get_session_factory()
        print(f"[db] {_dsn_hint()}")

    try:
        now = utcnow()
        rows = await _inspect(session_factory, now)
        if not rows:
            print("[ok] 没有需要收口的审批单（库是干净的）")
            return 0

        print(f"\n发现 {len(rows)} 张需要收口的 PENDING 审批单（now={now.isoformat()}）：\n")
        header = f"{'审批单':>8}  {'任务ID':>7}  {'任务编号':<18} {'任务状态':<18} {'过期时间':<20} {'超期':>5}  动作"
        print(header)
        print("-" * len(header) + "-" * 40)
        for row in rows:
            overdue = f"{row['overdue_days']}天" if row["overdue_days"] is not None else "—"
            print(
                f"{row['approval_id']:>8}  {row['task_id']:>7}  {row['task_no']:<18} "
                f"{row['task_status']:<18} {row['expires_at']:<20} {overdue:>5}  {row['action']}"
            )

        if not args.apply:
            print("\n[dry-run] 未做任何修改。确认上面列表无误后，加 --apply 执行收口。")
            return 0

        from app.services.interrupt_service import sweep_expired_approvals

        result = await sweep_expired_approvals(
            now=now, session_factory=session_factory, purge_checkpoints=not args.no_purge
        )
        print("\n[applied] 收口结果：")
        print(f"  审批单置 EXPIRED : {result['expired']}")
        print(f"  任务置 FAILED    : {result['tasks_failed']}")
        print(f"  孤儿单（仅过期） : {result['orphans']}")
        print(f"  清理断点键数     : {result['checkpoint_keys_deleted']}")
        return 0
    finally:
        # 显式收池：否则 aiomysql 连接在 asyncio.run 结束后才被 GC，
        # 会刷一串 "RuntimeError: Event loop is closed" 噪音，看着像失败。
        if engine is not None:
            await engine.dispose()
        else:
            from app.db.session import dispose_engine

            await dispose_engine()


def _dsn_hint() -> str:
    try:
        from app.core.config import settings

        return f"{settings.MYSQL_HOST}:{settings.MYSQL_PORT}/{settings.MYSQL_DB}"
    except Exception:  # noqa: BLE001
        return "（按 .env 配置）"


def main() -> int:
    _safe_stdout()
    parser = argparse.ArgumentParser(description="收口超期/孤儿的 PENDING 审批单（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="真正落库（缺省只打印）")
    parser.add_argument("--dry-run", action="store_true", help="显式声明只读（与缺省一致）")
    parser.add_argument("--sqlite", help="改为操作指定 SQLite 文件（演示库）")
    parser.add_argument("--no-purge", action="store_true", help="不清 Redis 断点键（仅改库状态）")
    args = parser.parse_args()
    args.apply = bool(args.apply and not args.dry_run)

    _load_env()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
