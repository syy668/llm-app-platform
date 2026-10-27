"""导出库表结构为 JSON（供截图脚本渲染）。

为什么不用 DESCRIBE：MySQL 8 的 DESCRIBE 只返回 6 列
(Field/Type/Null/Key/Default/Extra)，**不含列注释**，
拿它做交付截图会得到一整列空的「说明」，看起来像没写注释。
注释必须走 information_schema.COLUMNS.COLUMN_COMMENT。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.database import engine  # noqa: E402

TABLES = ["eval_datasets", "eval_items", "eval_runs", "eval_scores"]

COL_SQL = text(
    """
    SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT,
           EXTRA, COLUMN_COMMENT
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t
    ORDER BY ORDINAL_POSITION
    """
)

TBL_SQL = text(
    """
    SELECT TABLE_COMMENT, ENGINE, TABLE_COLLATION
    FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t
    """
)


def main() -> None:
    out: dict = {"database": "eval_agent_db", "tables": []}
    with engine.connect() as conn:
        out["mysql_version"] = conn.execute(text("SELECT VERSION()")).scalar_one()
        out["database"] = conn.execute(text("SELECT DATABASE()")).scalar_one()
        for table in TABLES:
            cols = []
            for r in conn.execute(COL_SQL, {"t": table}).fetchall():
                cols.append(
                    {
                        "field": r[0],
                        "type": r[1],
                        "null": r[2],
                        "key": r[3],
                        "default": r[4],
                        "extra": r[5],
                        "comment": r[6],
                    }
                )
            meta = conn.execute(TBL_SQL, {"t": table}).fetchone()
            idx = sorted({r[2] for r in conn.execute(text(f"SHOW INDEX FROM `{table}`")).fetchall()})
            cnt = conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar_one()
            out["tables"].append(
                {
                    "name": table,
                    "comment": meta[0] if meta else "",
                    "engine": meta[1] if meta else "",
                    "collation": meta[2] if meta else "",
                    "rows": int(cnt),
                    "columns": cols,
                    "indexes": idx,
                }
            )

    target = Path(__file__).resolve().parent.parent / "logs" / "schema.json"
    target.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"导出 {len(out['tables'])} 张表 -> {target}")


if __name__ == "__main__":
    main()
