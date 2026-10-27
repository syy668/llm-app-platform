"""任务 A：SQL 修复循环的列名保留 + 真实表结构（DDL）注入。

验证四件事：
1. sanitize_tool_error 对 1054 类错误保留**标识符**（列名/表名），
   原文其余部分（堆栈 / 潜在 PII 如手机号、身份证）绝不进 Prompt；
2. 非结构化 SQL 错误维持 v2.1 固定 hint 行为（回归）；
3. build_executor_messages / _llm_rewrite_sql 的 Prompt 注入真实表结构段，
   schema 不可得（None/空串）时整段不出现（降级回归）；
4. get_mall_schema_summary 拉取失败降级 None + 进程级缓存生效。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.executor import Executor
from app.agents.prompts import build_executor_messages
from app.schemas.agent_io import ExecutorInput
from app.schemas.mcp import McpErrorCode


# ---------------------------------------------------------------------------
# 测试基建（与 test_tool_resilience.py 同款最小桩）
# ---------------------------------------------------------------------------
class FakeToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _make_subtask() -> Any:
    from app.schemas.agent_io import SubtaskPlan

    return SubtaskPlan(
        seq=1,
        name="取数",
        description="按渠道聚合转化率",
        tool_hint="sql_query",
        depends_on=[],
        risk_level="normal",
        acceptance="返回数据",
    )


def _make_tools() -> list[Any]:
    from app.schemas.agent_io import ToolDef

    return [
        ToolDef(
            name="sql-query",
            description="在只读电商数据仓库执行单条 SELECT 查询。",
            input_schema={"type": "object", "properties": {"sql": {"type": "string"}}},
            server="sql-query",
        )
    ]


# ---------------------------------------------------------------------------
# 1. sanitize_tool_error：列名保留 + PII 不泄
# ---------------------------------------------------------------------------
def test_sanitize_keeps_unknown_column_identifiers():
    """1054 原文里的列名必须保留——修复循环的生命线。"""
    err = FakeToolError(
        McpErrorCode.SQL_SYNTAX.value,
        "(1054, \"Unknown column 't.visitors' in 'field list'\")",
    )
    out = Executor.sanitize_tool_error(err)
    assert "'t.visitors'" in out, f"列名必须进回喂提示，实际: {out}"
    assert "真实表结构" in out


def test_sanitize_sql_error_strips_pii_and_stack():
    """原文混入手机号/堆栈时，只有标识符放行，其余全部丢弃。"""
    err = FakeToolError(
        McpErrorCode.SQL_SYNTAX.value,
        "Unknown column 'visitors' in 'field list'; user phone 13800138000, "
        "idcard 110101199003077758, at conn=0x7f3a pool#12",
    )
    out = Executor.sanitize_tool_error(err)
    assert "'visitors'" in out
    assert "13800138000" not in out
    assert "110101199003077758" not in out
    assert "pool#12" not in out
    assert len(out) <= 200


def test_sanitize_keeps_unknown_table_identifier():
    """1146 表不存在同样保留表名。"""
    err = FakeToolError(
        McpErrorCode.SQL_SYNTAX.value,
        "(1146, \"Table 'mall.orders_backup' doesn't exist\")",
    )
    out = Executor.sanitize_tool_error(err)
    assert "'mall.orders_backup'" in out


def test_sanitize_sql_syntax_without_identifiers_regresses():
    """无结构化模式（如「SQL 不能为空」）→ 维持 v2.1 固定 hint，不杜撰标识符。"""
    err = FakeToolError(McpErrorCode.SQL_SYNTAX.value, "SQL 不能为空")
    out = Executor.sanitize_tool_error(err)
    assert out.startswith("[系统提示]")
    assert "SQL 语法有误" in out
    assert "不存在的标识符" not in out
    # v2.1 回归断言：原文不外泄
    assert "SQL 不能为空" not in out


def test_sanitize_sql_syntax_stack_regression():
    """v2.1 既有断言保持：'Unknown column' 原文短语不进提示（只有标识符进）。"""
    out = Executor.sanitize_tool_error(FakeToolError("SQL_SYNTAX", "Unknown column 'foo' ... 堆栈"))
    assert "Unknown column" not in out
    assert "'foo'" in out
    assert "堆栈" not in out


def test_extract_sql_idents_dedup_and_cap():
    raw = "Unknown column 'a' ... unknown column 'a' ... no such column: b ... unknown column 'c'"
    idents = Executor._extract_sql_idents(raw)
    assert idents == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# 2. Prompt 注入真实表结构
# ---------------------------------------------------------------------------
def test_build_executor_messages_injects_schema():
    payload = ExecutorInput(
        subtask=_make_subtask(),
        context_digest="",
        sibling_summaries=[],
        tools=_make_tools(),
        sql_schema="orders(order_id, channel, paid_orders, dt)；\ntraffic_records(dt, channel, visitors)",
    )
    messages = build_executor_messages(payload)
    user = str(messages[-1]["content"])
    assert "数据库真实表结构" in user
    assert "traffic_records(dt, channel, visitors)" in user
    assert "禁止使用未列出的表或列" in user


def test_build_executor_messages_no_schema_no_block():
    """schema 不可得（空串）→ 整段不出现，Prompt 与升级前等价。"""
    payload = ExecutorInput(
        subtask=_make_subtask(),
        context_digest="",
        sibling_summaries=[],
        tools=_make_tools(),
        sql_schema="",
    )
    messages = build_executor_messages(payload)
    user = str(messages[-1]["content"])
    assert "数据库真实表结构" not in user


# ---------------------------------------------------------------------------
# 3. _llm_rewrite_sql：修复循环注入 schema + 列名提示
# ---------------------------------------------------------------------------
async def test_llm_rewrite_sql_injects_schema_and_ident_hint(monkeypatch) -> None:
    """修复 Prompt 同时包含：真实表结构段 + sanitize 后的列名提示。"""
    import app.agents.executor as exec_mod
    from app.agents.executor import SqlRewriteRequest

    captured: list[list[dict[str, Any]]] = []

    async def fake_complete_structured(llm, messages, model_cls, **kwargs):
        captured.append(messages)
        return SimpleNamespace(value=SqlRewriteRequest(sql="SELECT 1", max_rows=100))

    monkeypatch.setattr(exec_mod, "complete_structured", fake_complete_structured)

    async def fake_schema_summary(self):
        return "orders(order_id, channel, paid_orders, dt)"

    monkeypatch.setattr(exec_mod.Executor, "_safe_schema_summary", fake_schema_summary)

    executor = exec_mod.Executor(
        pool=SimpleNamespace(tools=[]),
        llm=SimpleNamespace(),
        context=SimpleNamespace(),
        tools=[],
        backoff_enabled=False,
    )
    subtask = _make_subtask()
    result = await executor._llm_rewrite_sql(
        {"sql": "SELECT visitors FROM orders"},
        McpErrorCode.SQL_SYNTAX.value,
        Executor.sanitize_tool_error(
            FakeToolError(McpErrorCode.SQL_SYNTAX.value, "Unknown column 'visitors' in 'field list'")
        ),
        subtask,
    )
    assert result is not None and result["sql"] == "SELECT 1"
    user = str(captured[0][-1]["content"])
    assert "orders(order_id, channel, paid_orders, dt)" in user, "修复 Prompt 必须带真实表结构"
    assert "'visitors'" in user, "修复 Prompt 必须带 1054 列名提示"


def _async(coro):
    async def inner(self):
        return await coro

    return inner


# ---------------------------------------------------------------------------
# 4. sql_schema 拉取：降级 + 缓存
# ---------------------------------------------------------------------------
async def test_get_mall_schema_summary_degrades_on_engine_failure(monkeypatch) -> None:
    """库不可达 → None，绝不抛出（executor 拿到空串、Prompt 不注入）。"""
    from app.tools import sql_schema

    sql_schema.clear_schema_cache()

    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(sql_schema, "_dsn", lambda: "mysql+aiomysql://x")
    import sqlalchemy.ext.asyncio as sa_asyncio

    monkeypatch.setattr(sa_asyncio, "create_async_engine", boom)
    assert await sql_schema.get_mall_schema_summary(force_refresh=True) is None
    sql_schema.clear_schema_cache()


async def test_get_mall_schema_summary_caches(monkeypatch) -> None:
    """缓存生效：两次调用只 fetch 一次。"""
    from app.tools import sql_schema

    sql_schema.clear_schema_cache()
    calls = {"n": 0}

    async def fake_fetch():
        calls["n"] += 1
        return "orders(id, dt)"

    monkeypatch.setattr(sql_schema, "_fetch_schema", fake_fetch)
    first = await sql_schema.get_mall_schema_summary()
    second = await sql_schema.get_mall_schema_summary()
    assert first == "orders(id, dt)" and second == first
    assert calls["n"] == 1
    sql_schema.clear_schema_cache()


def test_render_schema_sorted_and_compact():
    from app.tools.sql_schema import _render_schema

    text = _render_schema({"traffic_records": ["dt", "channel", "visitors"], "orders": ["id", "dt"]})
    lines = [seg.split("(")[0] for seg in text.split("；\n")]
    assert lines == ["orders", "traffic_records"]  # 字典序，确定性输出
