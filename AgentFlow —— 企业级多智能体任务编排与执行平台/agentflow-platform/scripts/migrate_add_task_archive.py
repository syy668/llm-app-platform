"""幂等迁移：给 ``tasks`` 补 ``archived_at`` 列（归档能力，第一阶段）。

为什么需要单独一个脚本：
    项目没有 alembic 版本目录，建表走 ``Base.metadata.create_all``，而 create_all
    只补表、不补列 —— 已有的库上新增 ORM 列不会自动生效。``tasks.result_payload``
    当年也是手工 ALTER 加的，这里把同样的事固化成脚本，换库/重建库时可直接重跑。

用法::

    # 打 MySQL（读 .env 的 MYSQL_*，默认 127.0.0.1:3307/agentflow）
    python scripts/migrate_add_task_archive.py

    # 打本地演示库（dev_serve.py 用的 SQLite）
    python scripts/migrate_add_task_archive.py --sqlite storage/_dev_serve.db

    # 只看要执行什么，不落库
    python scripts/migrate_add_task_archive.py --dry-run

幂等：列已存在则直接跳过；索引按 MySQL / SQLite 各自的语法创建。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLUMN = "archived_at"
INDEX = "ix_tasks_archived_at"
#: 归档列：NULL=未归档；有值=归档时间。与后续 deleted_at（软删除）语义不同。
COLUMN_DDL_MYSQL = f"ALTER TABLE tasks ADD COLUMN {COLUMN} DATETIME NULL COMMENT '归档时间；NULL=未归档'"
COLUMN_DDL_SQLITE = f"ALTER TABLE tasks ADD COLUMN {COLUMN} DATETIME NULL"
INDEX_DDL = f"CREATE INDEX {INDEX} ON tasks ({COLUMN})"


def _load_env() -> None:
    """复用项目 .env（与运行时同一套 MYSQL_* 配置）。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv 是运行期依赖
        return
    load_dotenv(env_file)


def _column_exists(cur, table: str, column: str, *, sqlite: bool) -> bool:
    if sqlite:
        cur.execute(f"PRAGMA table_info({table})")
        return any(row[1] == column for row in cur.fetchall())
    cur.execute(
        """
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return bool(cur.fetchone()[0])


def _index_exists(cur, table: str, index: str, *, sqlite: bool) -> bool:
    if sqlite:
        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index,))
        return cur.fetchone() is not None
    cur.execute(
        """
        SELECT COUNT(*) FROM information_schema.statistics
        WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s
        """,
        (table, index),
    )
    return bool(cur.fetchone()[0])


def migrate_mysql(dry_run: bool) -> int:
    import pymysql

    host = os.getenv("MYSQL_HOST", "127.0.0.1")
    port = int(os.getenv("MYSQL_PORT", "3307"))
    user = os.getenv("MYSQL_USER", "agentflow")
    password = os.getenv("MYSQL_PASSWORD", "")
    database = os.getenv("MYSQL_DB", "agentflow")

    print(f"[mysql] {host}:{port}/{database}")
    conn = pymysql.connect(host=host, port=port, user=user, password=password, database=database, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            if _column_exists(cur, "tasks", COLUMN, sqlite=False):
                print(f"[skip] tasks.{COLUMN} 已存在")
            elif dry_run:
                print(f"[dry-run] {COLUMN_DDL_MYSQL}")
            else:
                cur.execute(COLUMN_DDL_MYSQL)
                print(f"[ok] 已新增 tasks.{COLUMN}")

            if _index_exists(cur, "tasks", INDEX, sqlite=False):
                print(f"[skip] 索引 {INDEX} 已存在")
            elif dry_run:
                print(f"[dry-run] {INDEX_DDL}")
            else:
                cur.execute(INDEX_DDL)
                print(f"[ok] 已创建索引 {INDEX}")
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return 0


def migrate_sqlite(path: str, dry_run: bool) -> int:
    import sqlite3

    db = Path(path)
    if not db.exists():
        print(f"[error] {db} 不存在（先跑一次 dev_serve.py 建库）")
        return 1

    print(f"[sqlite] {db}")
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.cursor()
        if _column_exists(cur, "tasks", COLUMN, sqlite=True):
            print(f"[skip] tasks.{COLUMN} 已存在")
        elif dry_run:
            print(f"[dry-run] {COLUMN_DDL_SQLITE}")
        else:
            cur.execute(COLUMN_DDL_SQLITE)
            print(f"[ok] 已新增 tasks.{COLUMN}")

        if _index_exists(cur, "tasks", INDEX, sqlite=True):
            print(f"[skip] 索引 {INDEX} 已存在")
        elif dry_run:
            print(f"[dry-run] {INDEX_DDL}")
        else:
            cur.execute(INDEX_DDL)
            print(f"[ok] 已创建索引 {INDEX}")
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="给 tasks 补 archived_at 列（幂等）")
    parser.add_argument("--sqlite", help="改为迁移指定 SQLite 文件（演示库）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的 DDL")
    args = parser.parse_args()

    if args.sqlite:
        return migrate_sqlite(args.sqlite, args.dry_run)
    _load_env()
    return migrate_mysql(args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
