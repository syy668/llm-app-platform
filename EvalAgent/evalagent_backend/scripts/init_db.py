"""建库 + 建表 + 打印表结构（幂等，可反复执行）。

用法：
    .venv/Scripts/python.exe scripts/init_db.py          # 建库建表并打印结构
    .venv/Scripts/python.exe scripts/init_db.py --drop   # 先删库再重建（危险，仅开发）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base, engine  # noqa: E402


def split_db_url(url_str: str) -> tuple[str, str]:
    """把 mysql+pymysql://u:p@h:p/db?x 拆成 (server_url, db_name)。

    必须用 make_url 解析：手写 partition("/") 会把 "://" 的斜杠吃掉，
    拼出 "mysql+pymysql:/?charset=..." 这种无法解析的串（已踩过一次）。
    """
    url = make_url(url_str)
    db_name = url.database or ""
    # 注意：url.set(database=None) 在 SQLAlchemy 2.0 是空操作（库名仍保留），
    # 连过去会报 1049 Unknown database，必须先落到一个必然存在的系统库。
    server_url = url.set(database="mysql").render_as_string(hide_password=False)
    return server_url, db_name


def create_database() -> str:
    server_url, db_name = split_db_url(settings.DATABASE_URL)
    server = create_engine(server_url, future=True, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
    server.dispose()
    return db_name


def drop_database() -> None:
    server_url, db_name = split_db_url(settings.DATABASE_URL)
    server = create_engine(server_url, future=True, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{db_name}`"))
    server.dispose()


def print_schema() -> None:
    with engine.connect() as conn:
        tables = [
            r[0]
            for r in conn.execute(text("SHOW TABLES")).fetchall()
        ]
        print(f"\n共 {len(tables)} 张表: {', '.join(tables)}")
        for table in tables:
            rows = conn.execute(text(f"DESCRIBE `{table}`")).fetchall()
            count = conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar_one()
            idx = conn.execute(text(f"SHOW INDEX FROM `{table}`")).fetchall()
            print("\n" + "=" * 100)
            print(f"表 {table}    行数={count}")
            print("-" * 100)
            print(f"{'字段':<24}{'类型':<20}{'可空':<6}{'键':<5}{'默认':<8}说明")
            for r in rows:
                field, col_type, nullable, key, default, extra, comment = (
                    r[0], r[1], r[2], r[3], r[4], r[5], (r[6] if len(r) > 6 else "")
                )
                print(
                    f"{field:<24}{col_type:<20}{nullable:<6}{str(key or ''):<5}"
                    f"{str(default if default is not None else ''):<8}{comment or ''}"
                )
            print(f"-- 索引: {', '.join(sorted({i[2] for i in idx}))}")


if __name__ == "__main__":
    if "--drop" in sys.argv:
        print("!! 重建数据库（DROP + CREATE）")
        drop_database()

    name = create_database()
    print(f"数据库就绪: {name}")

    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    print("建表完成（create_all 幂等）")
    print_schema()
