"""SQL 生成/修复的真实表结构注入源（任务 A）。

背景（v2.1 遗留缺口）：
- LLM 生成 SQL 时只能凭工具描述里的**表名**猜列名，命中率低；
- 数据库 1054「Unknown column」报错又被 v2.1 脱敏机制擦掉列名，
  SQL 修复循环变成"盲修"，重试预算烧完即 FAILED。

本模块提供运行时**真实 DDL 摘要**，供 Executor 注入生成/修复 Prompt：
- 从只读库（``w2.SQL_RO_*``，与 sql-query / MallKbProvider 同一套凭证口径）
  inspect 白名单表的列名，输出紧凑单行 schema（如 ``orders(id, order_no, dt)``）；
- 进程级缓存（表结构运行期不变），TTL 兜底防长驻进程拿陈旧结构；
- **任何失败**（库不可达 / 驱动缺失 / 超时）一律返回 ``None`` 降级，
  绝不阻塞任务执行——拿不到结构就退回现状（工具描述里的表名提示）。

安全边界：
- 只执行 ``inspect``（元数据读取），不触碰业务数据行；
- 输出仅含表名/列名（标识符），不含任何行数据，无 PII 泄露面。
"""

from __future__ import annotations

import asyncio
import re
import time

from app.core.logging import get_logger
from app.core.w2_config import w2

logger = get_logger(__name__)

#: 摘要缓存 TTL（秒）：表结构运行期不变，长 TTL 足够；过期后下次调用重拉
_SCHEMA_TTL_SECONDS = 3600
#: 单表列数上限（防御性，正常表远小于此）
_MAX_COLUMNS_PER_TABLE = 60

#: 需要注入「取值域」的列名模式：低基数枚举列（状态 / 渠道 / 平台 / 类型 / 等级）。
#: 背景：真实事故——模型把 orders.pay_status 猜成 '支付成功'（库里实际是 paid/refunded/unpaid），
#: SQL 语法与列名全对，却静默返回 0 行；下游分析、绘图全部被拖垮，且错误码不报任何异常。
#: 只读这一类列的 DISTINCT 值（业务字典值，不含个人信息），自由文本列与数值列一律不碰。
_ENUM_COLUMN_PATTERN = re.compile(r"(status|state|channel|platform|type|level)$", re.IGNORECASE)
#: 单列取值域上限：超过即视为高基数（自由文本/ID），不注入
_ENUM_MAX_VALUES = 16
#: 单表参与取样列数上限（防御极端宽表把 schema 撑爆）
_ENUM_MAX_COLUMNS_PER_TABLE = 4
#: 标识符白名单（列/表名来自 inspector，仍再做一次形状校验，杜绝拼接面）
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

_cache: tuple[float, str] | None = None


def _dsn() -> str:
    """只读库 DSN：与 MallKbProvider 同口径（SQL_RO_*），支持 MALL_RO_DSN 覆盖。"""
    import os

    override = os.environ.get("MALL_RO_DSN", "").strip()
    if override:
        return override
    return (
        f"mysql+aiomysql://{w2.SQL_RO_USER}:{w2.SQL_RO_PASSWORD}"
        f"@{w2.SQL_RO_HOST}:{w2.SQL_RO_PORT}/{w2.SQL_RO_DB}?charset=utf8mb4"
    )


def _render_schema(tables: dict[str, list[str]], domains: dict[str, list[str]] | None = None) -> str:
    """把 {表名: [列名]} 渲染成紧凑单行 schema 文本（可选附枚举列取值域）。"""
    domains = domains or {}
    parts = []
    for table in sorted(tables):
        cols = tables[table]
        if not cols:
            continue
        line = f"{table}({', '.join(cols)})"
        pairs = [
            (key.split(".", 1)[1], vals)
            for key, vals in sorted(domains.items())
            if key.split(".", 1)[0] == table
        ]
        if pairs:
            line += "〔取值域：" + "；".join(f"{col}∈{{{', '.join(vals)}}}" for col, vals in pairs) + "〕"
        parts.append(line)
    return "；\n".join(parts)


async def get_mall_schema_summary(*, force_refresh: bool = False) -> str | None:
    """返回白名单表的真实列名摘要（进程级缓存）；任何失败返回 None。"""
    global _cache
    if not force_refresh and _cache is not None and (time.monotonic() - _cache[0]) < _SCHEMA_TTL_SECONDS:
        return _cache[1] or None

    summary = await _fetch_schema()
    _cache = (time.monotonic(), summary or "")
    return summary


async def _fetch_schema() -> str | None:
    """inspect 只读库拉白名单表列名；失败返回 None，绝不抛出。"""
    try:
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy.ext.asyncio import create_async_engine

        allowed = sorted(w2.sql_allowed_tables)
        if not allowed:
            return None
        engine = create_async_engine(_dsn(), future=True, pool_pre_ping=True)
        try:
            deadline = float(w2.SQL_EXEC_TIMEOUT_SECONDS)

            def _read(sync_conn: object) -> dict[str, list[str]]:
                inspector = sa_inspect(sync_conn)
                tables: dict[str, list[str]] = {}
                existing = set(inspector.get_table_names())
                for table in allowed:
                    if table not in existing:
                        continue
                    cols = [
                        str(col["name"])
                        for col in inspector.get_columns(table)[:_MAX_COLUMNS_PER_TABLE]
                    ]
                    tables[table] = cols
                return tables

            def _read_domains(sync_conn: object, tables: dict[str, list[str]]) -> dict[str, list[str]]:
                """枚举列取值域：低基数（≤16）才注入，超过视为高基数直接丢弃。"""
                domains: dict[str, list[str]] = {}
                for table, cols in tables.items():
                    if not _IDENTIFIER_RE.match(table):
                        continue
                    sampled = 0
                    for col in cols:
                        if sampled >= _ENUM_MAX_COLUMNS_PER_TABLE:
                            break
                        if not _IDENTIFIER_RE.match(col) or not _ENUM_COLUMN_PATTERN.search(col):
                            continue
                        sampled += 1
                        try:
                            rows = sync_conn.exec_driver_sql(  # noqa: S608 - 标识符已双重校验
                                f"SELECT DISTINCT `{col}` FROM `{table}` "
                                f"WHERE `{col}` IS NOT NULL LIMIT {_ENUM_MAX_VALUES + 1}"
                            ).fetchall()
                        except Exception as exc:  # noqa: BLE001 - 取样失败不影响主摘要
                            logger.debug("sql_schema_domain_skip", extra={"table": table, "column": col, "error": str(exc)[:120]})
                            continue
                        if 0 < len(rows) <= _ENUM_MAX_VALUES:
                            domains[f"{table}.{col}"] = [str(row[0]).strip() for row in rows]
                return domains

            async with engine.connect() as conn:
                tables = await asyncio.wait_for(conn.run_sync(_read), timeout=deadline)
                try:
                    domains = await asyncio.wait_for(conn.run_sync(_read_domains, tables), timeout=deadline)
                except Exception as exc:  # noqa: BLE001 - 值域是增强项，失败退化为纯列名摘要
                    logger.warning("sql_schema_domains_unavailable", extra={"error": str(exc)[:200]})
                    domains = {}
        finally:
            await engine.dispose()

        if not tables:
            logger.warning("sql_schema_empty", extra={"allowed": allowed})
            return None
        summary = _render_schema(tables, domains)
        logger.info(
            "sql_schema_loaded",
            extra={"tables": len(tables), "chars": len(summary), "domains": len(domains)},
        )
        return summary or None
    except Exception as exc:  # noqa: BLE001 - 降级：拿不到结构绝不阻塞执行
        logger.warning("sql_schema_unavailable", extra={"error": str(exc)[:200]})
        return None


def clear_schema_cache() -> None:
    """清空缓存（仅测试用）。"""
    global _cache
    _cache = None


__all__ = ["clear_schema_cache", "get_mall_schema_summary"]
