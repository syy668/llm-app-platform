"""初始化管理员账号脚本（运维脚本，非业务接口）。

用法：
  python scripts/seed.py --username admin --password '你的密码' [--display-name 系统管理员]
  # 账号已存在时重置密码（仅 UPDATE users.password_hash 一列，不动任何业务表）
  python scripts/seed.py --username admin --password '新密码' --reset-password

密码也可通过环境变量 ADMIN_INIT_PASSWORD 提供；若两者都不提供，
则自动生成随机密码并打印到终端。密码将以 bcrypt 哈希写入数据库，不存明文。

安全说明：本脚本只会 INSERT 一个新账号，或在显式传入 --reset-password 时
UPDATE 目标账号的 password_hash 一列；不 DROP、不 TRUNCATE、不重建表，
不触碰 documents / chunks / traces / eval_* 等任何业务数据。
"""
import argparse
import os
import secrets
import sys
from pathlib import Path

# 将项目根目录加入 sys.path，保证脚本可从任意目录执行（跨平台）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.core import security  # noqa: E402
from app.core.database import SessionLocal, create_tables  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.models import ROLE_ADMIN, ROLE_USER, User  # noqa: E402


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="初始化管理员账号")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--display-name", default="系统管理员")
    parser.add_argument("--email", default=None)
    parser.add_argument("--role", default=ROLE_ADMIN, choices=[ROLE_ADMIN, ROLE_USER])
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="账号已存在时重置其密码（仅更新该账号的 password_hash，不改角色/状态/业务数据）",
    )
    args = parser.parse_args()

    password = args.password or os.getenv("ADMIN_INIT_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(12)
        print(f"[提示] 未提供密码，已生成随机密码：{password}")

    # 确保表存在（幂等；数据库本身需已通过 scripts/init_db.sql 创建）
    create_tables()

    db = SessionLocal()
    try:
        exists = db.query(User).filter(User.username == args.username).first()
        if exists:
            if not args.reset_password:
                print(f"账号 {args.username} 已存在，跳过创建。（如需重置密码请追加 --reset-password）")
                return

            # 仅改动 password_hash 一列；role / status / 其它业务数据保持原样
            exists.password_hash = security.hash_password(password)
            db.add(exists)
            db.commit()
            print(f"已重置账号 {args.username} 的密码（用户 ID={exists.id}，角色={exists.role}，状态={exists.status}）")
            if len(password) < 8:
                print("[提醒] 当前密码长度 < 8 位，仅适用于本地演示环境；生产请使用强密码。")
            print("[说明] 已登录的会话保存在 Redis（kb:session:*），不随密码变更自动失效。")
            return

        user = User(
            username=args.username,
            password_hash=security.hash_password(password),
            display_name=args.display_name,
            email=args.email,
            role=args.role,
            status="active",
        )
        db.add(user)
        db.commit()
        print(f"成功创建账号：{args.username}（角色：{args.role}）")
    finally:
        db.close()


if __name__ == "__main__":
    main()
