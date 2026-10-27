"""加固任务 2：工具调用硬超时 / SQL 报错回喂 LLM 自反思 / 工具熔断器。

三条防线：
1. **强制 5s 硬超时**：Executor 外层 asyncio.wait_for，超时转 MCP_TIMEOUT 走可重试矩阵；
2. **SQL 自反思**：确定性错误（SQL_SYNTAX/SQL_TOO_LARGE）把错误作为 Observation 回喂
   LLM 重新生成 SQL（≤3 次），LLM 失败回退确定性缩窗；3 次后子任务 FAILED；
3. **熔断**：同一工具连续失败 5 次（Redis 计数）→ 冷却 60s 内快速拒绝（TOOL_CIRCUIT_OPEN），
   注册表 GET /tools 同步展示 circuit_open。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.executor import Executor, SqlRewriteRequest
from app.core.circuit_breaker import ToolCircuitBreaker
from app.core.config import settings
from app.schemas.graph_state import SubtaskPlan
from app.tools.mcp_client import ToolInvocationError

TIME_RANGE = {"start": "2026-10-12", "end": "2026-10-18"}


class SlowPool:
    """每次调用都超过给定时长的连接池桩（验证硬超时）。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.tools: list[Any] = []
        self.calls = 0

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return {}


class ScriptedPool:
    """按脚本执行的连接池桩（脚本元素：异常=抛错后睡一段制造超时，dict=正常返回）。"""

    def __init__(self, script: list[Any], *, slow_error_delay: float = 0.0) -> None:
        self.script = list(script)
        self.slow_error_delay = slow_error_delay
        self.tools: list[Any] = []
        self.calls = 0

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            if self.slow_error_delay > 0:
                await asyncio.sleep(self.slow_error_delay)  # 超过硬超时
            raise step
        return step


class SequencePool:
    """按脚本依次返回 / 抛错的连接池桩（验证 SQL 自反思后重试成功）。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.tools: list[Any] = []
        self.calls: list[dict[str, Any]] = []

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append({"tool": tool, "arguments": dict(arguments)})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class FailingPool:
    """始终抛业务错误的连接池桩。"""

    def __init__(self, code: str = "SQL_SYNTAX") -> None:
        self.code = code
        self.tools: list[Any] = []
        self.calls = 0

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        self.calls += 1
        raise ToolInvocationError(self.code, "模拟工具失败")


def subtask(seq: int, tool: str = "sql_query") -> SubtaskPlan:
    return SubtaskPlan(
        seq=seq,
        name=f"子任务{seq}"[:64],
        description="按目标时间窗取数与归因",
        tool_hint=tool,
        depends_on=[],
        risk_level="normal",
        acceptance="返回数据或结论",
    )


def _patch_sql_rewrite(monkeypatch, sql: str | None, *, raise_error: bool = False) -> list[Any]:
    """把 executor 的 complete_structured 换成可控桩，返回捕获到的 (model_cls, messages)。

    注意 _build_sql_arguments 也会调 complete_structured（生成 ToolCallRequest），
    所以断言 SQL 回喂次数时要按 model_cls 过滤。
    """
    import app.agents.executor as exec_mod

    captured: list[Any] = []

    async def fake_complete_structured(llm, messages, model_cls, **kwargs):
        captured.append((model_cls, messages))
        if raise_error:
            raise RuntimeError("LLM 不可用")
        return SimpleNamespace(value=SqlRewriteRequest(sql=sql or "", max_rows=100))

    monkeypatch.setattr(exec_mod, "complete_structured", fake_complete_structured)
    return captured


# ---------------------------------------------------------------------------
# 1. 硬超时
# ---------------------------------------------------------------------------
async def test_hard_timeout_converts_to_retryable_mcp_timeout(mock_llm, context_service) -> None:
    """调用超过硬超时 → 转 MCP_TIMEOUT（可重试），按矩阵重试后 FAILED，绝不挂死。"""
    pool = SlowPool(delay=0.3)
    executor = Executor(
        pool, mock_llm, context_service, tool_timeout=0.05, backoff_enabled=False
    )

    run = await executor.run_subtask(subtask(1), siblings=[], digest="", time_range=TIME_RANGE)

    assert run.execution.status == "FAILED"
    assert run.execution.outcome is not None
    assert run.execution.outcome.error_code == "MCP_TIMEOUT"
    assert run.execution.outcome.attempts == 4  # 1 原始 + 3 矩阵重试（sql-query）
    assert pool.calls == 4


async def test_hard_timeout_recovers_when_tool_recovers(mock_llm, context_service) -> None:
    """前两次超时、第三次成功：硬超时不阻断恢复，outcome.ok=True。"""
    pool = ScriptedPool(
        [TimeoutError(), TimeoutError(), {"columns": ["c"], "rows": [["x"]]}],
        slow_error_delay=0.05,
    )
    executor = Executor(
        pool, mock_llm, context_service, tool_timeout=0.02, backoff_enabled=False
    )

    run = await executor.run_subtask(subtask(1), siblings=[], digest="", time_range=TIME_RANGE)

    assert run.execution.status == "DONE"
    assert run.execution.outcome is not None and run.execution.outcome.ok
    assert run.execution.outcome.attempts == 3


# ---------------------------------------------------------------------------
# 2. SQL 报错回喂 LLM 自反思
# ---------------------------------------------------------------------------
async def test_sql_syntax_error_fed_back_to_llm(mock_llm, context_service, monkeypatch) -> None:
    """SQL_SYNTAX → 错误作为 Observation 进 Prompt → LLM 产出合法 SQL → 重试成功。"""
    ok_payload = {"columns": ["category_id"], "rows": [[1]]}
    pool = SequencePool([ToolInvocationError("SQL_SYNTAX", "near 'form': syntax error"), ok_payload])
    captured = _patch_sql_rewrite(
        monkeypatch,
        "SELECT category_id, SUM(amount) AS gmv FROM orders "
        "WHERE order_date BETWEEN '2026-10-12' AND '2026-10-14' GROUP BY category_id",
    )
    executor = Executor(pool, mock_llm, context_service, backoff_enabled=False)

    run = await executor.run_subtask(subtask(1), siblings=[], digest="", time_range=TIME_RANGE)

    assert run.execution.status == "DONE"
    assert run.execution.outcome is not None and run.execution.outcome.ok
    assert run.execution.outcome.attempts == 2
    # 回喂的 Prompt 里必须带错误码与脱敏后的业务提示（Observation）；
    # v2.1 起 Observation 只给 [系统提示]，原始报错细节不再进 Prompt
    rewrite_calls = [msgs for model_cls, msgs in captured if model_cls is SqlRewriteRequest]
    assert len(rewrite_calls) == 1
    user_text = str(rewrite_calls[0][-1]["content"])
    assert "[SQL_SYNTAX]" in user_text and "[系统提示]" in user_text
    assert "SQL 语法有误" in user_text
    assert "near 'form'" not in user_text, "v2.1 脱敏：原始报错细节不得喂给 LLM"
    assert "上次生成的 SQL" in user_text
    # 第二次调用用的就是 LLM 重新生成的 SQL
    assert "2026-10-14" in pool.calls[1]["arguments"]["sql"]


async def test_sql_rewrite_llm_failure_falls_back_to_deterministic(mock_llm, context_service, monkeypatch) -> None:
    """LLM 挂掉/产出非法时回退确定性缩窗，重试预算与 FAILED 语义不变。"""
    pool = FailingPool("SQL_SYNTAX")
    _patch_sql_rewrite(monkeypatch, None, raise_error=True)
    executor = Executor(pool, mock_llm, context_service, backoff_enabled=False)

    run = await executor.run_subtask(subtask(1), siblings=[], digest="", time_range=TIME_RANGE)

    assert run.execution.status == "FAILED"
    assert run.execution.outcome is not None and run.execution.outcome.attempts == 4  # 预算不变


async def test_sql_rewrite_exhausted_marks_subtask_failed(mock_llm, context_service, monkeypatch) -> None:
    """LLM 3 次都产出非法 SQL（无 select）→ 全部走回退 → 3 次重写后 FAILED。"""
    pool = FailingPool("SQL_SYNTAX")
    _patch_sql_rewrite(monkeypatch, "这不是 SQL")
    executor = Executor(pool, mock_llm, context_service, backoff_enabled=False)

    run = await executor.run_subtask(subtask(1), siblings=[], digest="", time_range=TIME_RANGE)

    assert run.execution.status == "FAILED"
    assert run.execution.outcome is not None and run.execution.outcome.attempts == 4
    assert pool.calls == 4  # 1 原始 + 3 次重写，之后不再调用


# ---------------------------------------------------------------------------
# 3. 熔断器
# ---------------------------------------------------------------------------
async def test_circuit_opens_after_consecutive_failures(fake_redis) -> None:
    """连续失败达到阈值 → 熔断打开且带冷却 TTL；成功清零计数。"""
    breaker = ToolCircuitBreaker(fake_redis, failure_threshold=3, cooldown_seconds=60)

    assert await breaker.record_failure("sql-query") is False
    assert await breaker.record_failure("sql-query") is False
    assert await breaker.is_open("sql-query") is False
    assert await breaker.record_failure("sql-query") is True  # 第 3 次触发
    assert await breaker.is_open("sql-query") is True

    ttl = await fake_redis.ttl(settings.key("tool:circuit", "sql-query"))
    assert 0 < ttl <= 60

    await breaker.record_success("sql-query")
    assert int(await fake_redis.get(settings.key("tool:circuit_fail", "sql-query")) or 0) == 0


async def test_circuit_fail_open_when_redis_broken() -> None:
    """Redis 故障 → 熔断器 fail-open（视为未熔断），不阻塞业务调用。"""

    class BrokenClient:
        async def get(self, key: str):
            raise ConnectionError("redis down")

        async def incr(self, key: str):
            raise ConnectionError("redis down")

        async def expire(self, key: str, seconds: int):
            raise ConnectionError("redis down")

        async def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

        async def delete(self, key: str):
            raise ConnectionError("redis down")

    breaker = ToolCircuitBreaker(BrokenClient())
    assert await breaker.is_open("sql-query") is False
    assert await breaker.record_failure("sql-query") is False


async def test_circuit_fast_rejects_tool_calls(mock_llm, context_service, fake_redis) -> None:
    """熔断打开后：后续子任务不再真正调用工具，直接 TOOL_CIRCUIT_OPEN 快速失败。

    注意：SubtaskPlan.tool_name 会把 sql_query 归一化为 sql-query，熔断 key 必须用同一名字。
    """
    # 阈值调大：确保第一个子任务的重试过程中不会中途熔断（行为确定性）
    breaker = ToolCircuitBreaker(fake_redis, failure_threshold=100, cooldown_seconds=60)
    pool = FailingPool("SQL_TIMEOUT")  # 可重试错误，按矩阵重试
    executor = Executor(
        pool, mock_llm, context_service, backoff_enabled=False, circuit=breaker
    )

    first = await executor.run_subtask(subtask(1), siblings=[], digest="", time_range=TIME_RANGE)
    assert first.execution.status == "FAILED"
    assert first.execution.outcome is not None and first.execution.outcome.error_code == "SQL_TIMEOUT"
    calls_after_first = pool.calls
    assert calls_after_first >= 2
    assert await breaker.is_open("sql-query") is False

    # 手动补足连续失败到阈值 → 熔断打开（key 与 executor 实际使用的一致）
    for _ in range(100):
        await breaker.record_failure("sql-query")
    assert await breaker.is_open("sql-query") is True

    second = await executor.run_subtask(subtask(2), siblings=[], digest="", time_range=TIME_RANGE)
    assert second.execution.status == "FAILED"
    assert second.execution.outcome is not None
    assert second.execution.outcome.error_code == "TOOL_CIRCUIT_OPEN"
    assert pool.calls == calls_after_first  # 熔断期零真实调用


# ---------------------------------------------------------------------------
# 4. 注册表展示熔断状态
# ---------------------------------------------------------------------------
async def test_tool_registry_surfaces_circuit_state(client, fake_redis) -> None:
    """GET /tools：熔断中的工具回传 circuit_open=true（与人工开关独立）。"""
    token_resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin_user", "password": "Passw0rd!"}
    )
    token = token_resp.json()["data"]["access_token"]
    await fake_redis.set(settings.key("tool:circuit", "sql-query"), "open")

    response = await client.get("/api/v1/tools", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    items = {item["name"]: item for item in response.json()["data"]["items"]}
    assert items["sql-query"]["circuit_open"] is True
    assert items["kb-search"]["circuit_open"] is False
    # 人工开关不受熔断影响
    assert items["sql-query"]["enabled"] is True
