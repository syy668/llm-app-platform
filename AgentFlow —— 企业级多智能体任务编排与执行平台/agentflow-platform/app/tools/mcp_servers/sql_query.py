"""MCP Server：sql-query（PRD FR-005 / NFR-S1）。

安全五道闸门（缺一不可）：
1. **只读账号**：连接使用 ``mall_ro``（仅 SELECT on mall），从配置读取，不落日志；
2. **AST 解析**：sqlglot 解析，语句类型白名单仅 ``Select``，同时拒绝树内任何 DML/DDL 节点、
   ``SELECT ... INTO``、``FOR UPDATE``、多语句与注释；
3. **表白名单**：仅 ``orders / traffic_records / ad_campaigns / skus / categories``，禁止跨库；
4. **强制 LIMIT**：无 LIMIT 自动追加，超限改写为上限值，``max_rows`` 强制 ≤1000；
5. **扫描量预估**：``EXPLAIN`` 行数 > 100 万 → ``SQL_TOO_LARGE``，建议缩小时间窗。

超时 10s，错误码 SQL_SYNTAX / SQL_TIMEOUT / SQL_TOO_LARGE / DB_UNAVAILABLE。
"""

from __future__ import annotations

import asyncio
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
# 注意：这里必须用别名——模块级 `time` 是计时器（perf_counter），
# 直接 from datetime import time 会把它遮蔽成 datetime.time，工具一调用就 AttributeError。
from datetime import date as _date, datetime as _datetime, time as _time
from decimal import Decimal
from typing import Any
from urllib.parse import quote_plus

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.mcp import McpErrorCode, SqlQueryInput, SqlQueryOutput
from app.tools.mcp_stdio import FunctionTool, MCPServerApp, ToolError, run_server

logger = get_logger(__name__)

SERVER_NAME = "sql-query"
TOOL_NAME = "sql-query"

_COMMENT_RE = re.compile(r"(--|/\*|\*/)|((^|\s)#)", re.DOTALL)
FORBIDDEN_NODE_TYPES = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter)


@dataclass(slots=True)
class ValidatedSql:
    sql: str
    applied_limit: int
    truncated: bool
    tables: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int


def _jsonify(value: Any) -> Any:
    """把驱动返回的 Python 原生类型收敛为 JSON 原生类型（协议层的唯一真相）。

    背景（真实事故）：MySQL 的 SUM/AVG 经 aiomysql 返回 ``decimal.Decimal``、时间列返回
    ``datetime``，而 stdio 协议用 ``json.dumps(..., default=str)`` 兜底 → **数字变成字符串**。
    下游 chart-render 只认 int/float，判定「无数值列」→ 整条出图分支失败；
    这类失败还不报错码，只留一个空白的 FAILED 子任务。

    口径：Decimal 整数位无损转 int，其余转 float；日期转 ISO 字符串；
    其余不可序列化对象退化为 str（保持原 default=str 的行为，不会比升级前更差）。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (_datetime, _date, _time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


# ---------------------------------------------------------------------------
# 1) 静态校验（纯函数，可单测，不触数据库）
# ---------------------------------------------------------------------------
def validate_sql(sql: str, *, max_rows: int = 200) -> ValidatedSql:
    """AST 白名单校验 + 强制 LIMIT。失败抛 ToolError(SQL_SYNTAX)。"""
    raw = (sql or "").strip()
    if not raw:
        raise ToolError(McpErrorCode.SQL_SYNTAX, "SQL 不能为空")

    if _COMMENT_RE.search(raw):
        raise ToolError(McpErrorCode.SQL_SYNTAX, "禁止 SQL 注释")
    if ";" in raw.rstrip(";"):
        raise ToolError(McpErrorCode.SQL_SYNTAX, "禁止多语句执行")

    try:
        statements = sqlglot.parse(raw, read="mysql")
    except ParseError as exc:
        raise ToolError(McpErrorCode.SQL_SYNTAX, f"SQL 无法解析: {exc}") from exc

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise ToolError(McpErrorCode.SQL_SYNTAX, f"仅允许单条 SELECT，实际 {len(statements)} 条语句")

    statement = statements[0]
    if not isinstance(statement, exp.Select):
        raise ToolError(McpErrorCode.SQL_SYNTAX, f"仅允许 SELECT，实际为 {type(statement).__name__.upper()}")

    # 树内任何写操作节点一律拒绝（含 CTE 内嵌套写入、UNION 分支等）
    for node in statement.walk():
        if isinstance(node, exp.Into):
            raise ToolError(McpErrorCode.SQL_SYNTAX, "禁止 SELECT ... INTO")
        if isinstance(node, exp.Lock):
            raise ToolError(McpErrorCode.SQL_SYNTAX, "禁止 SELECT ... FOR UPDATE")
        if isinstance(node, FORBIDDEN_NODE_TYPES):
            raise ToolError(McpErrorCode.SQL_SYNTAX, f"禁止 {type(node).__name__.upper()} 语句")

    allowed = w2.sql_allowed_tables
    tables: list[str] = []
    for table in statement.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name:
            continue
        db = (table.db or "").lower()
        catalog = (table.catalog or "").lower()
        if db and db != w2.SQL_RO_DB.lower():
            raise ToolError(McpErrorCode.SQL_SYNTAX, f"禁止跨库访问：{db}.{name}")
        if catalog:
            raise ToolError(McpErrorCode.SQL_SYNTAX, f"禁止跨实例访问：{catalog}")
        if name not in allowed:
            raise ToolError(McpErrorCode.SQL_SYNTAX, f"表 {name} 不在白名单内（允许：{sorted(allowed)}）")
        tables.append(name)

    if not tables:
        raise ToolError(McpErrorCode.SQL_SYNTAX, "未识别到任何业务表，拒绝无表查询")

    effective_max = max(1, min(int(max_rows), w2.SQL_MAX_ROWS))
    existing_limit: int | None = None
    limit_node = statement.args.get("limit")
    if limit_node is not None:
        try:
            existing_limit = int(limit_node.expression.this)
        except (AttributeError, TypeError, ValueError):
            existing_limit = None

    truncated = False
    if existing_limit is None:
        applied = min(w2.SQL_DEFAULT_LIMIT, effective_max)
        statement = statement.limit(applied)
    elif existing_limit > effective_max:
        applied = effective_max
        statement = statement.limit(effective_max)
        truncated = True
    else:
        applied = existing_limit

    return ValidatedSql(
        sql=statement.sql(dialect="mysql"),
        applied_limit=applied,
        truncated=truncated,
        tables=tables,
    )


# ---------------------------------------------------------------------------
# 2) 执行器（只读账号 / 可注入）
# ---------------------------------------------------------------------------
class SqlExecutor(ABC):
    @abstractmethod
    async def execute(self, sql: str, timeout: float) -> QueryResult: ...

    async def estimate_scan_rows(self, sql: str, timeout: float) -> int | None:
        """默认不预估（SQLite 等无 EXPLAIN rows 语义的实现直接返回 None）。"""
        return None


class MySqlReadOnlyExecutor(SqlExecutor):
    """MySQL 只读连接：独立 engine，账号仅 SELECT，语句级超时。"""

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None

    def _dsn(self) -> str:
        return (
            f"mysql+aiomysql://{w2.SQL_RO_USER}:{quote_plus(w2.SQL_RO_PASSWORD)}"
            f"@{w2.SQL_RO_HOST}:{w2.SQL_RO_PORT}/{w2.SQL_RO_DB}?charset=utf8mb4"
        )

    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self._dsn(), pool_pre_ping=True, pool_size=2, max_overflow=2, echo=False
            )
        return self._engine

    async def execute(self, sql: str, timeout: float) -> QueryResult:
        async def _run() -> QueryResult:
            async with self.engine().connect() as conn:
                result = await conn.execute(text(sql))
                columns = list(result.keys())
                rows = [list(row) for row in result.fetchall()]
                return QueryResult(columns=columns, rows=rows, row_count=len(rows))

        return await asyncio.wait_for(_run(), timeout=timeout)

    async def estimate_scan_rows(self, sql: str, timeout: float) -> int | None:
        async def _run() -> int | None:
            async with self.engine().connect() as conn:
                result = await conn.execute(text(f"EXPLAIN {sql}"))
                total = 0
                for row in result.fetchall():
                    raw = dict(row._mapping).get("rows")
                    if raw is not None:
                        total += int(raw)
                return total

        try:
            return await asyncio.wait_for(_run(), timeout=timeout)
        except Exception:  # noqa: BLE001 - 预估失败不阻断主查询
            return None


# ---------------------------------------------------------------------------
# 3) 工具
# ---------------------------------------------------------------------------
class SqlQueryTool:
    name = TOOL_NAME
    description = (
        "在只读电商数据仓库执行单条 SELECT 查询，返回列与行。"
        "仅允许查询 orders / traffic_records / ad_campaigns / skus / categories，"
        "自动追加 LIMIT，禁止任何写操作。"
    )

    def __init__(self, executor: SqlExecutor | None = None, *, timeout: float | None = None) -> None:
        self.executor = executor or MySqlReadOnlyExecutor()
        self.timeout = timeout or float(w2.SQL_EXEC_TIMEOUT_SECONDS)

    async def __call__(self, args: SqlQueryInput) -> dict[str, Any]:
        started = time.perf_counter()
        validated = validate_sql(args.sql, max_rows=args.max_rows)

        # 扫描量预估：超 100 万行直接拒绝，提示缩小时间窗
        scanned = await self.executor.estimate_scan_rows(validated.sql, self.timeout)
        if scanned is not None and scanned > w2.SQL_MAX_SCAN_ROWS:
            raise ToolError(
                McpErrorCode.SQL_TOO_LARGE,
                f"预估扫描 {scanned} 行超过上限 {w2.SQL_MAX_SCAN_ROWS}，请缩小时间窗或降低粒度",
                data={"scanned_rows": scanned, "sql": validated.sql},
            )

        try:
            result = await self.executor.execute(validated.sql, self.timeout)
        except asyncio.TimeoutError as exc:
            raise ToolError(
                McpErrorCode.SQL_TIMEOUT, f"SQL 执行超过 {self.timeout}s", data={"sql": validated.sql}
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 区分语法与连接故障
            message = str(exc)
            code = (
                McpErrorCode.SQL_SYNTAX if _looks_like_syntax_error(message) else McpErrorCode.DB_UNAVAILABLE
            )
            logger.warning("sql_exec_failed", extra={"code": code.value, "error": message[:300]})
            raise ToolError(code, message[:300], data={"sql": validated.sql}) from exc

        max_rows = max(1, min(args.max_rows, w2.SQL_MAX_ROWS))
        rows = result.rows
        truncated = validated.truncated or result.row_count > max_rows
        if result.row_count > max_rows:
            rows = rows[:max_rows]

        output = SqlQueryOutput(
            columns=result.columns,
            rows=[[_jsonify(cell) for cell in row] for row in rows],
            row_count=len(rows),
            truncated=truncated,
            exec_ms=int((time.perf_counter() - started) * 1000),
            scanned_rows=scanned,
            applied_limit=validated.applied_limit,
        )
        return output.model_dump()


def _looks_like_syntax_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in ("syntax", "unknown column", "doesn't exist", "no such table", "no such column")
    )


def build_app(executor: SqlExecutor | None = None) -> MCPServerApp:
    tool = FunctionTool(
        name=TOOL_NAME,
        description=SqlQueryTool.description,
        input_model=SqlQueryInput,
        handler=SqlQueryTool(executor),
    )
    return MCPServerApp(SERVER_NAME, [tool])


def main() -> None:  # pragma: no cover - 子进程入口
    run_server(build_app())


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "SERVER_NAME",
    "TOOL_NAME",
    "ValidatedSql",
    "QueryResult",
    "validate_sql",
    "SqlExecutor",
    "MySqlReadOnlyExecutor",
    "SqlQueryTool",
    "build_app",
    "main",
]
