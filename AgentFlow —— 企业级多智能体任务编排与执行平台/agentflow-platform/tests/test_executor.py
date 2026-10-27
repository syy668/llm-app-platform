"""Executor 单测（FR-004）：工具选择、重试/降级矩阵、结果瘦身、循环上限。

矩阵验证（PRD 4.2）：
- 确定性错误（SQL_SYNTAX / SQL_TOO_LARGE）：不重试，回喂改写最多 3 次；
- 可重试错误（SQL_TIMEOUT 等）：按 2s/4s 退避重试 3 次；
- chart-render 失败：降级为 Markdown 表格，不阻塞；
- 工具循环上限 8 次，防死循环。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.executor import Executor, ExecutorError
from app.core.w2_config import w2
from app.schemas.graph_state import SubtaskPlan
from app.tools.mcp_client import ToolInvocationError

pytestmark = pytest.mark.unit

TIME_RANGE = {"start": "2026-10-12", "end": "2026-10-18"}


class FailingPool:
    """始终抛业务错误的连接池桩（tools 供 Executor 构造 Function Calling 定义）。"""

    def __init__(self, code: str = "SQL_SYNTAX") -> None:
        self.code = code
        self.tools: list[Any] = []
        self.calls: list[dict[str, Any]] = []

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append({"tool": tool, "arguments": arguments})
        raise ToolInvocationError(self.code, "模拟工具失败")


def subtask(seq: int, tool: str = "none", name: str | None = None) -> SubtaskPlan:
    return SubtaskPlan(
        seq=seq,
        name=(name or f"子任务{seq}")[:64],
        description="按目标时间窗取数与归因",
        tool_hint=tool,
        depends_on=[] if seq == 1 else [seq - 1],
        risk_level="normal",
        acceptance="返回数据或结论",
    )


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
async def test_sql_subtask_success_is_slimmed(mcp_pool, mock_llm, context_service) -> None:
    executor = Executor(mcp_pool, mock_llm, context_service, tools=mcp_pool.tools)

    run = await executor.run_subtask(subtask(1, "sql_query"), siblings=[], digest="", time_range=TIME_RANGE)

    assert run.execution.status == "DONE"
    assert run.execution.outcome is not None and run.execution.outcome.ok
    assert run.execution.outcome.attempts == 1
    # 进入上下文的是瘦身后的 digest，而不是原始行数据
    digest = run.execution.result.data_digest
    assert "列:" in digest and "统计:" in digest
    assert "category_id" in digest
    assert len(digest) < 2000


async def test_kb_subtask_success(mcp_pool, mock_llm, context_service) -> None:
    executor = Executor(mcp_pool, mock_llm, context_service, tools=mcp_pool.tools)
    task = subtask(2, "kb_search")
    task.description = "检索历史周报中与库存周转相关的记录"

    run = await executor.run_subtask(task, siblings=[], digest="")

    assert run.execution.status == "DONE"
    assert "kb-" in run.execution.result.data_digest


async def test_none_subtask_needs_no_tool(fake_sql_executor, mock_llm, context_service) -> None:
    executor = Executor(FailingPool(), mock_llm, context_service, tools=[])

    run = await executor.run_subtask(subtask(2, "none"), siblings=["#1 GMV 125000.5"], digest="")

    assert run.execution.status == "DONE"
    assert "依据上游结果" in run.execution.result.data_digest
    assert "125000.5" in run.execution.result.data_digest


async def test_none_subtask_without_siblings_flags_missing_data(mock_llm, context_service) -> None:
    executor = Executor(FailingPool(), mock_llm, context_service, tools=[])

    run = await executor.run_subtask(subtask(2, "none"), siblings=[], digest="")

    assert "缺少上游数据" in run.execution.result.data_digest


# ---------------------------------------------------------------------------
# 确定性错误：不重试，改写 3 次后放弃
# ---------------------------------------------------------------------------
async def test_deterministic_sql_error_rewrites_then_fails(mock_llm, context_service) -> None:
    pool = FailingPool("SQL_SYNTAX")
    executor = Executor(pool, mock_llm, context_service, backoff_enabled=False)

    run = await executor.run_subtask(subtask(1, "sql_query"), siblings=[], digest="", time_range=TIME_RANGE)

    assert run.execution.status == "FAILED"
    assert run.execution.result.error["code"] == "SQL_SYNTAX"
    assert run.execution.outcome.attempts == 4  # 1 次原始 + 3 次改写
    assert run.execution.degraded is True
    # 每次改写都改变了 SQL（缩窗 / 降 max_rows），不是原样重放
    sqls = {call["arguments"]["sql"] for call in pool.calls}
    assert len(sqls) >= 2


# ---------------------------------------------------------------------------
# 可重试错误：按矩阵退避
# ---------------------------------------------------------------------------
async def test_retryable_error_retries_per_matrix(mock_llm, context_service) -> None:
    pool = FailingPool("SQL_TIMEOUT")
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    executor = Executor(pool, mock_llm, context_service, sleeper=fake_sleep, backoff_enabled=True)

    run = await executor.run_subtask(subtask(1, "sql_query"), siblings=[], digest="", time_range=TIME_RANGE)

    assert run.execution.status == "FAILED"
    assert run.execution.outcome.error_code == "SQL_TIMEOUT"
    assert run.execution.outcome.attempts == 4  # 1 + 3 次重试
    assert slept == [2.0, 4.0, 4.0]  # PRD 4.2 退避序列


async def test_loop_limit_guards_endless_retry(mock_llm, context_service) -> None:
    pool = FailingPool("SQL_SYNTAX")
    executor = Executor(pool, mock_llm, context_service, loop_limit=1, backoff_enabled=False)

    with pytest.raises(ExecutorError) as excinfo:
        await executor.run_subtask(subtask(1, "sql_query"), siblings=[], digest="", time_range=TIME_RANGE)

    assert excinfo.value.code == "TOOL_LOOP_LIMIT"
    assert w2.TOOL_LOOP_LIMIT == 8


# ---------------------------------------------------------------------------
# 图表降级
# ---------------------------------------------------------------------------
SOURCE_PAYLOAD = {
    1: {"columns": ["category_id", "gmv"], "rows": [[1001, 125000.5], [1002, 98000.0]]}
}


async def test_chart_failure_degrades_to_markdown_table(mock_llm, context_service) -> None:
    pool = FailingPool("CHART_DATA_INVALID")
    executor = Executor(pool, mock_llm, context_service)

    run = await executor.run_subtask(
        subtask(2, "chart_render", "生成品类对比图"),
        siblings=[],
        digest="",
        source_payloads=SOURCE_PAYLOAD,
    )

    assert run.execution.status == "DONE"  # 降级成功即算完成，不阻塞主流程
    assert run.execution.result.degraded is True
    assert "Markdown 表格" in run.execution.result.data_digest
    assert "125000.5" in run.execution.result.data_digest


async def test_chart_without_upstream_data_refuses_to_fabricate(mock_llm, context_service) -> None:
    executor = Executor(FailingPool(), mock_llm, context_service)

    with pytest.raises(ExecutorError) as excinfo:
        await executor.run_subtask(subtask(2, "chart_render"), siblings=[], digest="")

    assert excinfo.value.code == "CHART_DATA_INVALID"


async def test_chart_success_emits_artifact(mcp_pool, mock_llm, context_service) -> None:
    executor = Executor(mcp_pool, mock_llm, context_service, tools=mcp_pool.tools)

    run = await executor.run_subtask(
        subtask(2, "chart_render", "生成品类对比图"),
        siblings=[],
        digest="",
        source_payloads=SOURCE_PAYLOAD,
    )

    assert run.execution.status == "DONE"
    artifacts = run.execution.result.artifacts
    assert artifacts and artifacts[0].type == "chart"
    assert artifacts[0].url and artifacts[0].url.startswith("/static/charts/")


async def test_chart_accepts_decimal_aggregates(mcp_pool, mock_llm, context_service) -> None:
    """任务 B 收尾：MySQL SUM/AVG 经 aiomysql 返回 decimal.Decimal，
    数值列判定必须认 Decimal，否则聚合图误报「无上游数据可绘图」。"""
    from decimal import Decimal

    executor = Executor(mcp_pool, mock_llm, context_service, tools=mcp_pool.tools)
    decimal_payload = {
        1: {
            "columns": ["channel", "gmv", "order_cnt"],
            "rows": [[1, "direct", Decimal("125000.50"), 300], [2, "search", Decimal("98000.00"), 210]],
        }
    }

    run = await executor.run_subtask(
        subtask(2, "chart_render", "生成渠道 GMV 对比图"),
        siblings=[],
        digest="",
        source_payloads=decimal_payload,
    )

    assert run.execution.status == "DONE", "Decimal 聚合列不得触发「无上游数据」拒绝"


async def test_chart_accepts_numeric_strings_from_mcp_json(mcp_pool, mock_llm, context_service) -> None:
    """生产链路：MCP JSON 层把数值序列化成字符串（digest 实测 "3888138.87"/"3" 带引号），
    数值列判定必须认数字字符串，否则 chart-render 在生产环境永远「无上游数据」。
    （任务 40 实锤：chart-render 上线以来 0 次成功，全部因此拒绝）"""
    executor = Executor(mcp_pool, mock_llm, context_service, tools=mcp_pool.tools)
    string_payload = {
        1: {
            "columns": ["channel", "gmv_total", "order_cnt_total"],
            "rows": [["天猫", "515891919.11", "720"], ["京东", "407813438.47", "720"]],
        }
    }

    run = await executor.run_subtask(
        subtask(2, "chart_render", "生成渠道 GMV 对比图"),
        siblings=[],
        digest="",
        source_payloads=string_payload,
    )

    assert run.execution.status == "DONE", "数字字符串列不得触发「无上游数据」拒绝"
    args = executor._build_chart_arguments(subtask(2, "chart_render"), string_payload)
    assert args["y_fields"] == ["gmv_total", "order_cnt_total"]
    assert all(isinstance(row["gmv_total"], float) for row in args["data"])


# ---------------------------------------------------------------------------
# 整批执行
# ---------------------------------------------------------------------------
async def test_run_pass_skips_notify_and_blocked(mock_llm, context_service) -> None:
    """notify 归 notifier 节点处理；上游失败的下游直接 SKIPPED。"""
    executor = Executor(FailingPool("SQL_SYNTAX"), mock_llm, context_service, backoff_enabled=False)
    plan = [
        subtask(1, "sql_query"),
        subtask(2, "none"),
        SubtaskPlan(
            seq=3,
            name="推送运营群",
            description="推送报告",
            tool_hint="notify",
            depends_on=[2],
            risk_level="high",
            acceptance="返回 message_id",
        ),
    ]
    state = {
        "task_id": 1,
        "goal": "分析上周 GMV",
        "time_range": TIME_RANGE,
        "plan": plan,
        "subtask_results": {
            "1": {"subtask_seq": 1, "status": "FAILED", "data_digest": "", "tool": "sql-query"}
        },
    }

    result = await executor.run_pass(state)

    assert "3" not in result["subtask_results"]  # notify 不在 executor 阶段执行
    assert result["subtask_results"]["2"].status == "SKIPPED"
    assert result["status"] == "RUNNING"
    assert "digest" in result["context_digest"].lower() or "#1" in result["context_digest"]
    assert "executor" in result["token_ledger"]


async def test_run_pass_marks_failed_subtask(mcp_pool, mock_llm, context_service) -> None:
    executor = Executor(FailingPool("DB_UNAVAILABLE"), mock_llm, context_service, backoff_enabled=False)
    state = {
        "task_id": 1,
        "goal": "分析上周 GMV",
        "time_range": TIME_RANGE,
        "plan": [subtask(1, "sql_query")],
        "subtask_results": {},
    }

    result = await executor.run_pass(state)

    assert result["subtask_results"]["1"].status == "FAILED"
