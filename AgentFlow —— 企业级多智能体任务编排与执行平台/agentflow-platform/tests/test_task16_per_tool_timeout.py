"""任务 16：per-tool 独立超时预算。

背景：统一 5s 硬超时对 RAG 检索（项目一 /kbs/6/query 实测 8~12s）必然全灭——
改为按工具名读配置：kb-search=KB_TIMEOUT_SECONDS(20)、sql-query=SQL_EXEC_TIMEOUT_SECONDS(10)、
chart-render=CHART_RENDER_TIMEOUT_SECONDS(15)、notify=NOTIFY_TIMEOUT_SECONDS(5)，
未命中映射用 DEFAULT_TOOL_TIMEOUT_SECONDS(5) 兜底；显式 tool_timeout（测试注入）优先。

熔断器与健康哨兵语义不变：哨兵只管传输层瞬断（MCP_PROTOCOL_ERROR，见 test_task15），
超时仍是 executor 层 MCP_TIMEOUT 走重试矩阵，熔断器照常计数。
"""

from __future__ import annotations

import asyncio
from typing import Any
from types import SimpleNamespace

import pytest

from app.agents.executor import Executor
from app.core import w2_config
from app.schemas.graph_state import SubtaskPlan
from app.tools.mcp_client import McpErrorCode

TIME_RANGE = {"start": "2026-10-12", "end": "2026-10-18"}


def subtask(seq: int, tool: str = "kb_search") -> SubtaskPlan:
    return SubtaskPlan(
        seq=seq,
        name=f"子任务{seq}"[:64],
        description="检索知识库复盘证据",
        tool_hint=tool,
        depends_on=[],
        risk_level="normal",
        acceptance="返回数据或结论",
    )


class SlowPool:
    """每次调用都睡固定时长后成功的连接池桩。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.tools: list[Any] = []
        self.calls = 0

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return {"columns": ["c"], "rows": [["x"]]}


class RecordingCircuit:
    """熔断器桩：记录 success/failure 调用（接口对齐 executor 用法）。"""

    def __init__(self) -> None:
        self.cooldown = 60
        self.successes: list[str] = []
        self.failures: list[str] = []

    async def is_open(self, tool: str) -> bool:
        return False

    async def record_success(self, tool: str) -> None:
        self.successes.append(tool)

    async def record_failure(self, tool: str) -> None:
        self.failures.append(tool)


# ---------------------------------------------------------------------------
# 1. 预算映射
# ---------------------------------------------------------------------------
def test_per_tool_budget_mapping(mock_llm, context_service) -> None:
    executor = Executor(SimpleNamespace(tools=[]), mock_llm, context_service)
    assert executor._tool_budget("kb-search") == pytest.approx(float(w2_config.w2.KB_TIMEOUT_SECONDS))
    assert executor._tool_budget("sql-query") == pytest.approx(float(w2_config.w2.SQL_EXEC_TIMEOUT_SECONDS))
    assert executor._tool_budget("chart-render") == pytest.approx(float(w2_config.w2.CHART_RENDER_TIMEOUT_SECONDS))
    assert executor._tool_budget("notify") == pytest.approx(float(w2_config.w2.NOTIFY_TIMEOUT_SECONDS))


def test_unknown_tool_falls_back_to_default(mock_llm, context_service, monkeypatch) -> None:
    monkeypatch.setattr(w2_config.w2, "DEFAULT_TOOL_TIMEOUT_SECONDS", 3)
    executor = Executor(SimpleNamespace(tools=[]), mock_llm, context_service)
    assert executor._tool_budget("custom-tool") == pytest.approx(3.0)


def test_explicit_tool_timeout_overrides_mapping(mock_llm, context_service) -> None:
    """显式注入（测试路径）优先于 per-tool 映射，保持向后兼容。"""
    executor = Executor(SimpleNamespace(tools=[]), mock_llm, context_service, tool_timeout=0.05)
    for tool in ("kb-search", "sql-query", "notify", "whatever"):
        assert executor._tool_budget(tool) == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# 2. 行为：慢检索不再被一刀切 5s 杀死
# ---------------------------------------------------------------------------
async def test_slow_kb_call_survives_with_kb_budget(mock_llm, context_service) -> None:
    """0.3s 的慢 kb 调用：旧一刀切 5s 其实也能过——用 DEFAULT 收紧到 0.05 反向证明
    kb-search 走的是 KB 预算（20s）而不是 default。"""
    monkey_default = pytest.MonkeyPatch()
    try:
        monkey_default.setattr(w2_config.w2, "DEFAULT_TOOL_TIMEOUT_SECONDS", 0.05)
        pool = SlowPool(delay=0.3)
        executor = Executor(pool, mock_llm, context_service, backoff_enabled=False)

        run = await executor.run_subtask(
            subtask(1, "kb_search"), siblings=[], digest="", time_range=TIME_RANGE
        )
        assert run.execution.status == "DONE"  # 0.3s < KB 预算 20s，不受 default 0.05s 影响
    finally:
        monkey_default.undo()


async def test_timeout_error_message_carries_budget(mock_llm, context_service) -> None:
    """超时报错文案必须带实际预算值，方便运维直接看出是哪档超时。"""
    pool = SlowPool(delay=0.3)
    executor = Executor(pool, mock_llm, context_service, tool_timeout=0.05, backoff_enabled=False)

    run = await executor.run_subtask(subtask(1), siblings=[], digest="", time_range=TIME_RANGE)

    assert run.execution.status == "FAILED"
    assert run.execution.outcome is not None
    assert run.execution.outcome.error_code == McpErrorCode.MCP_TIMEOUT.value
    assert "预算 0.05s" in (run.execution.outcome.error_message or "")


# ---------------------------------------------------------------------------
# 3. 熔断器配合不变：超时仍计失败、成功仍计成功
# ---------------------------------------------------------------------------
async def test_circuit_counts_timeout_as_failure(mock_llm, context_service) -> None:
    circuit = RecordingCircuit()
    pool = SlowPool(delay=0.3)
    executor = Executor(
        pool, mock_llm, context_service, tool_timeout=0.05, backoff_enabled=False, circuit=circuit
    )

    run = await executor.run_subtask(subtask(1), siblings=[], digest="", time_range=TIME_RANGE)

    assert run.execution.status == "FAILED"
    assert circuit.failures == ["kb-search"] * 3  # 1 原始 + 2 矩阵重试（kb-search 重试上限 3）
    assert circuit.successes == []


async def test_circuit_counts_success_after_slow_but_within_budget(mock_llm, context_service) -> None:
    circuit = RecordingCircuit()
    pool = SlowPool(delay=0.3)
    executor = Executor(pool, mock_llm, context_service, backoff_enabled=False, circuit=circuit)

    run = await executor.run_subtask(
        subtask(1, "kb_search"), siblings=[], digest="", time_range=TIME_RANGE
    )

    assert run.execution.status == "DONE"
    assert circuit.successes == ["kb-search"]
    assert circuit.failures == []
