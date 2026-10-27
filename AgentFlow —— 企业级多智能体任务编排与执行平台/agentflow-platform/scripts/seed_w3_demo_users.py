#!/usr/bin/env python
"""W3 演示账号种子脚本（幂等）。

为什么需要它：
- W1 只建了 ``users`` 表，没有提供任何用户种子脚本；
- W3 的最小 MVP 前端「演示登录」按钮写死了 ``admin_user / Passw0rd!``（见
  ``frontend/src/main.js`` 的 ``demoLogin``），若不预先建号，点击必然 401；
- 本脚本只新增用户数据，**不改任何 W1 代码**，也不触碰其它表。

行为：
- 对 ``admin_user`` / ``approver_user`` / ``viewer_user`` 三个账号「不存在则创建」，
  已存在则只校验密码哈希是否需要刷新（默认不覆盖，避免把线上改过的密码重置）；
- 支持 ``--dsn`` 覆盖 ``DATABASE_URL``，便于对测试库播种；
- 支持 ``--dry-run`` 只打印将要做的事。

用法：
    python scripts/seed_w3_demo_users.py                 # 对 .env 里的库播种
    python scripts/seed_w3_demo_users.py --dry-run       # 只看计划
    python scripts/seed_w3_demo_users.py --dsn sqlite+aiosqlite:///./w3_demo.db --create-tables

账号与角色（与 PRD 2.1 角色矩阵一致）：
    admin_user     admin      建任务、审批、取消
    approver_user  approver   只看收件箱并审批（不能建任务）
    viewer_user    viewer     只读，用于验证 403
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

# 允许 `python scripts/seed_w3_demo_users.py` 直接运行（把项目根加入 sys.path）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASSWORD = "Passw0rd!"


@dataclass(frozen=True)
class DemoUser:
    username: str
    role: str
    note: str


DEMO_USERS: tuple[DemoUser, ...] = (
    DemoUser("admin_user", "admin", "建任务 / 审批 / 取消"),
    DemoUser("approver_user", "approver", "收件箱审批（不可建任务）"),
    DemoUser("viewer_user", "viewer", "只读，用于验证 403"),
)


async def seed(dsn: str | None, *, reset_password: bool, create_tables: bool, dry_run: bool) -> int:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings
    from app.core.security import hash_password
    from app.db.base import Base
    from app.schemas.enums import UserRole
    from app.schemas.orm_models import User

    # 自建 engine：不依赖 app.db.session 的全局单例，
    # 这样 --dsn 可以安全指向演示库/测试库，且不会污染进程内的连接池。
    url = dsn or settings.database_url
    engine = create_async_engine(url, future=True, echo=False)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    print(f"[seed] 目标库：{url.split('@')[-1] if '@' in url else url}")

    created = updated = skipped = 0
    try:
        if create_tables:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            print("[seed] 已按 ORM 元数据建表（仅演示/测试库使用，生产请走 alembic upgrade head）")

        async with factory() as session:
            for item in DEMO_USERS:
                existing = (
                    await session.execute(select(User).where(User.username == item.username))
                ).scalar_one_or_none()
                if existing is None:
                    if dry_run:
                        print(f"[plan] 新建 {item.username:<14} role={item.role:<9} ({item.note})")
                        created += 1
                        continue
                    session.add(
                        User(
                            username=item.username,
                            password_hash=hash_password(PASSWORD),
                            role=UserRole(item.role),
                            status=1,
                        )
                    )
                    created += 1
                    print(f"[new ] {item.username:<14} role={item.role:<9} ({item.note})")
                    continue

                if reset_password:
                    if dry_run:
                        print(f"[plan] 重置密码 {item.username}")
                        updated += 1
                        continue
                    existing.password_hash = hash_password(PASSWORD)
                    existing.status = 1
                    updated += 1
                    print(f"[upd ] {item.username:<14} 密码已重置为演示密码")
                else:
                    skipped += 1
                    print(f"[skip] {item.username:<14} 已存在（role={existing.role.value}），未覆盖")

            if dry_run:
                print("[seed] dry-run 结束，未写库")
                return 0
            await session.commit()
    finally:
        await engine.dispose()

    print(f"[seed] 完成：新建 {created} / 重置 {updated} / 跳过 {skipped}")
    print(f"[seed] 演示密码统一为 {PASSWORD!r}（仅本地演示环境使用）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="W3 演示账号种子（幂等）")
    parser.add_argument("--dsn", default=None, help="覆盖 DATABASE_URL，例如 sqlite+aiosqlite:///./w3_demo.db")
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="对已存在账号强制把密码重置为演示密码（默认不覆盖）",
    )
    parser.add_argument(
        "--create-tables",
        action="store_true",
        help="按 ORM 元数据建表（仅演示/测试库；生产请用 alembic upgrade head）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    args = parser.parse_args()
    return asyncio.run(
        seed(
            args.dsn,
            reset_password=args.reset_password,
            create_tables=args.create_tables,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
