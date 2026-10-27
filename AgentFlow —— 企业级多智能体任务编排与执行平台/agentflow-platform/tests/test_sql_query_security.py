"""sql-query 安全单测（FR-005 / NFR-S1）。

五道闸门逐条验证：
1. 仅允许单条 SELECT（DML / DDL / 多语句 / 注释 / ``SELECT INTO`` / ``FOR UPDATE`` 全部拒绝）；
2. 表白名单 + 禁止跨库 / 跨实例；
3. 强制 LIMIT（无则追加、超限则改写并标记 truncated、``max_rows`` 再夹到配置上限）；
4. 扫描量预估超 100 万行 → ``SQL_TOO_LARGE``；
5. 超时 / 语法错误 / 连接故障的错误码分类。

所有静态用例都在**不触数据库**的纯函数层完成，保证安全逻辑可单独回归。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.w2_config import w2
from app.schemas.mcp import SqlQueryInput
from app.tools.mcp_stdio import ToolError
from app.tools.mcp_servers.sql_query import SqlQueryTool, validate_sql

pytestmark = pytest.mark.unit


async def _run(executor, sql: str, max_rows: int = 200) -> dict:  # noqa: ANN001
    return await SqlQueryTool(executor)(SqlQueryInput(sql=sql, max_rows=max_rows))


# ---------------------------------------------------------------------------
# 闸门 3：强制 LIMIT
# ---------------------------------------------------------------------------
def test_appends_limit_when_absent() -> None:
    out = validate_sql("SELECT id, gmv FROM orders", max_rows=200)

    assert out.tables == ["orders"]
    assert out.applied_limit == 200
    assert "limit 200" in out.sql.lower()


def test_rewrites_oversized_limit_and_flags_truncated() -> None:
    out = validate_sql("SELECT id FROM orders LIMIT 5000", max_rows=200)

    assert out.truncated is True
    assert out.applied_limit == 200
    assert "limit 200" in out.sql.lower()


def test_keeps_smaller_existing_limit() -> None:
    out = validate_sql("SELECT id FROM orders LIMIT 10", max_rows=200)

    assert out.truncated is False
    assert out.applied_limit == 10


def test_max_rows_capped_by_config() -> None:
    """即使调用方传 5000，也只能拿到配置上限 1000（PRD 硬上限）。"""
    out = validate_sql("SELECT id FROM orders", max_rows=5000)

    assert out.applied_limit == w2.SQL_MAX_ROWS


# ---------------------------------------------------------------------------
# 闸门 1：语句类型 / 写法
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sql", "hint"),
    [
        ("INSERT INTO orders (id) VALUES (1)", "写操作"),
        ("UPDATE orders SET gmv = 0 WHERE id = 1", "写操作"),
        ("DELETE FROM orders WHERE id = 1", "写操作"),
        ("DROP TABLE orders", "DDL"),
        ("ALTER TABLE orders ADD COLUMN x INT", "DDL"),
        ("CREATE TABLE t AS SELECT * FROM orders", "DDL"),
        ("TRUNCATE TABLE orders", "DDL"),
    ],
)
def test_rejects_dml_and_ddl(sql: str, hint: str) -> None:
    with pytest.raises(ToolError) as excinfo:
        validate_sql(sql)

    assert excinfo.value.code == "SQL_SYNTAX", hint


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders; SELECT id FROM skus",  # 多语句
        "SELECT id FROM orders -- 偷偷追加",  # 行注释
        "SELECT id FROM orders /* 块注释 */",  # 块注释
        "SELECT id INTO backup_orders FROM orders",  # SELECT ... INTO
        "SELECT id FROM orders FOR UPDATE",  # 行锁
        "",  # 空语句
        "   ",  # 空白
    ],
)
def test_rejects_unsafe_forms(sql: str) -> None:
    with pytest.raises(ToolError) as excinfo:
        validate_sql(sql)

    assert excinfo.value.code == "SQL_SYNTAX"


def test_rejects_unparsable_sql() -> None:
    with pytest.raises(ToolError) as excinfo:
        validate_sql("SELEKT * FROM orders")

    assert excinfo.value.code == "SQL_SYNTAX"


# ---------------------------------------------------------------------------
# 闸门 2：表白名单与跨库
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", ["users", "payments", "order_items", "admin_logs"])
def test_rejects_table_outside_whitelist(table: str) -> None:
    with pytest.raises(ToolError) as excinfo:
        validate_sql(f"SELECT * FROM {table}")

    assert "白名单" in excinfo.value.message


def test_rejects_cross_database_access() -> None:
    with pytest.raises(ToolError) as excinfo:
        validate_sql("SELECT * FROM other_db.orders")

    assert "跨库" in excinfo.value.message


def test_allows_whitelisted_join() -> None:
    out = validate_sql(
        "SELECT o.category_id, SUM(o.amount) AS gmv FROM orders o "
        "JOIN skus s ON s.id = o.sku_id GROUP BY o.category_id"
    )

    assert set(out.tables) == {"orders", "skus"}


def test_rejects_select_without_table() -> None:
    with pytest.raises(ToolError) as excinfo:
        validate_sql("SELECT 1")

    assert "无表" in excinfo.value.message


def test_allows_same_database_prefix() -> None:
    out = validate_sql("SELECT id FROM mall.orders")

    assert out.tables == ["orders"]


# ---------------------------------------------------------------------------
# 工具层：闸门 4 / 5（扫描量、错误码分类）
# ---------------------------------------------------------------------------
async def test_invalid_sql_never_touches_database(fake_sql_executor) -> None:
    with pytest.raises(ToolError):
        await _run(fake_sql_executor, "DELETE FROM orders")

    assert fake_sql_executor.calls == []


async def test_scan_rows_over_limit_raises_too_large(fake_sql_executor) -> None:
    fake_sql_executor.scan_rows = w2.SQL_MAX_SCAN_ROWS + 1

    with pytest.raises(ToolError) as excinfo:
        await _run(fake_sql_executor, "SELECT id FROM orders")

    assert excinfo.value.code == "SQL_TOO_LARGE"
    assert fake_sql_executor.calls == []  # 拒绝在执行前，未产生真实查询


async def test_scan_rows_none_is_not_rejected(fake_sql_executor) -> None:
    fake_sql_executor.scan_rows = None

    payload = await _run(fake_sql_executor, "SELECT id FROM orders")

    assert payload["row_count"] == 3


async def test_timeout_maps_to_sql_timeout(fake_sql_executor) -> None:
    fake_sql_executor.raise_exc = asyncio.TimeoutError()

    with pytest.raises(ToolError) as excinfo:
        await _run(fake_sql_executor, "SELECT id FROM orders")

    assert excinfo.value.code == "SQL_TIMEOUT"


async def test_syntax_error_from_db_is_classified(fake_sql_executor) -> None:
    fake_sql_executor.raise_exc = RuntimeError("Unknown column 'foo' in 'field list'")

    with pytest.raises(ToolError) as excinfo:
        await _run(fake_sql_executor, "SELECT foo FROM orders")

    assert excinfo.value.code == "SQL_SYNTAX"


async def test_connection_error_is_db_unavailable(fake_sql_executor) -> None:
    fake_sql_executor.raise_exc = RuntimeError("Can't connect to MySQL server on '127.0.0.1'")

    with pytest.raises(ToolError) as excinfo:
        await _run(fake_sql_executor, "SELECT id FROM orders")

    assert excinfo.value.code == "DB_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 工具层：正常返回与行数截断
# ---------------------------------------------------------------------------
async def test_success_returns_rows_and_metrics(fake_sql_executor) -> None:
    payload = await _run(fake_sql_executor, "SELECT category_id, gmv FROM orders")

    assert payload["columns"] == fake_sql_executor.columns
    assert payload["row_count"] == 3
    assert payload["truncated"] is False
    assert payload["applied_limit"] == 200
    assert payload["exec_ms"] >= 0
    # 真正执行的是被 AST 改写后的 SQL（带 LIMIT）
    assert len(fake_sql_executor.calls) == 1
    assert "limit" in fake_sql_executor.calls[0].lower()


async def test_rows_truncated_by_max_rows(fake_sql_executor) -> None:
    fake_sql_executor.rows = [[i, float(i), i] for i in range(10)]

    payload = await _run(fake_sql_executor, "SELECT id FROM orders", max_rows=5)

    assert payload["row_count"] == 5
    assert payload["truncated"] is True
