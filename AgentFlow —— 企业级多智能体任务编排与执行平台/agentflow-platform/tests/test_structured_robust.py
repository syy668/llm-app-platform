"""加固任务 6：LLM 输出解析兜底（防程序崩溃）。

覆盖点：
1. ``extract_json`` 畸形输出矩阵：```json 围栏 / 前后废话 / 尾逗号 / Python 字面量 /
   缺右括号（截断）/ 未闭合字符串——本地轻量修复，省一次 LLM 回喂；
2. ``complete_structured`` 重试机制：垃圾输出回喂"你上次输出的格式错误"类修复提示，
   最多 1+2 次内恢复；穷尽时 ERROR 级结构化日志 + ``StructuredOutputError``；
3. Planner 节点兜底：LLM 永远垃圾 → 节点返回 ``status=FAILED``（PLAN_INVALID），
   绝不抛未捕获异常炸图；
4. Reviewer 节点两级兜底：第 1 次失败 → 本地规则降级（REVIEW_INVALID 不炸图）；
   连续第 2 次 → ``REVIEW_SKIPPED`` 放行（3.5 分及格 + 可信度上限 0.4 + ERROR 日志）。

说明：Planner 的安全默认状态是 FAILED 而不是"空计划"——空计划会让 Executor 无事可做、
Summarizer 产出静默空报告，而 FAILED(PLAN_INVALID) 是显式可重试的终态，语义更安全；
"解析不崩溃"的硬性要求由节点层 try/except 保证（本文件 3/4 组用例验证）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from app.agents.llm import MockLLMClient
from app.agents.planner import make_planner_node
from app.agents.reviewer import make_reviewer_node
from app.agents.structured import (
    StructuredOutputError,
    complete_structured,
    extract_json,
)
from app.core.w2_config import w2
from app.schemas.agent_io import PlannerInput
from app.schemas.graph_state import SubtaskPlan, SubtaskResult
from app.services.context_service import ContextService
from pydantic import BaseModel

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. extract_json：畸形输出矩阵
# ---------------------------------------------------------------------------
class _Box(BaseModel):
    a: int
    b: str = "x"


def test_extract_json_plain_and_fenced() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}  # 围栏
    assert extract_json('好的，结果如下：```json\n{"a": 2}\n``` 请查收。') == {"a": 2}  # 废话包裹
    assert extract_json('前置说明 {"a": 3} 后缀') == {"a": 3}  # 无围栏废话


def test_extract_json_trailing_comma() -> None:
    assert extract_json('{"a": 1, "b": "y",}') == {"a": 1, "b": "y"}  # 尾逗号
    assert extract_json('{"a": 1, "items": [1, 2,],}') == {"a": 1, "items": [1, 2]}  # 嵌套尾逗号


def test_extract_json_python_literals() -> None:
    assert extract_json('{"a": 1, "flag": True, "x": None}') == {"a": 1, "flag": True, "x": None}


def test_extract_json_missing_closing_brace() -> None:
    """缺右括号（输出被截断）：本地补齐，不再抛给 LLM 重试。"""
    assert extract_json('{"a": 1, "b": "ok"') == {"a": 1, "b": "ok"}
    assert extract_json('{"a": 1, "list": [1, 2') == {"a": 1, "list": [1, 2]}
    # 截断在字符串中间：先闭合字符串再补括号
    assert extract_json('{"a": 1, "b": "trunca') == {"a": 1, "b": "trunca"}


def test_extract_json_garbage_still_raises() -> None:
    with pytest.raises(ValueError):
        extract_json("完全没有结构的自由文本")
    with pytest.raises(ValueError):
        extract_json("")
    with pytest.raises(ValueError):
        extract_json('{"a": 1, "b": [}')  # 括号交错，保守修复不碰


# ---------------------------------------------------------------------------
# 2. complete_structured：重试机制 + 穷尽日志
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_complete_structured_repairs_with_feedback_prompt() -> None:
    """第 1 次围栏内语法错（不可本地修复）→ 回喂重试；第 2 次缺右括号被**本地轻量修复**
    直接救回——不再消耗第 3 次 LLM 调用（修复机制省 token 的直接证明）。"""
    mock = MockLLMClient()
    mock.push('我说完了：```json\n{"a": oops}\n```')  # 围栏内语法错
    mock.push('{"a": 1, "b": "repair"')  # 缺右括号 → 第 4 级本地补齐
    mock.push('{"a": 7, "b": "final"}')  # 永远不该被消费

    result = await complete_structured(mock, [{"role": "user", "content": "给个 JSON"}], _Box)

    assert result.value.a == 1  # 本地修复救回的是第 2 次输出
    assert result.attempts == 2  # 只调了 2 次 LLM（修复省掉了第 3 次）
    assert len(result.repair_errors) == 1  # 第 1 次失败记录了错误
    assert mock._queue  # 第 3 条预置响应仍在队列里 = 未被调用


@pytest.mark.asyncio
async def test_complete_structured_retry_prompt_contains_error_feedback() -> None:
    """回喂的重试 Prompt 必须含错误信息与"严格 JSON/不要围栏"要求（任务 6 核心要求）。"""
    mock = MockLLMClient()
    mock.push("随便一段不是 JSON 的话")
    mock.push('{"a": 1, "b": "ok"}')

    captured: list[list[dict[str, Any]]] = []
    original = mock.complete

    async def spy(messages, **kwargs):  # noqa: ANN001, ANN202
        captured.append(list(messages))
        return await original(messages, **kwargs)

    mock.complete = spy  # type: ignore[method-assign]

    result = await complete_structured(mock, [{"role": "user", "content": "给我 JSON"}], _Box)

    assert result.value.b == "ok"
    assert len(captured) == 2
    retry_prompt = str(captured[1][-1]["content"])
    assert "不符合要求" in retry_prompt and "JSON" in retry_prompt
    assert "JSON 解析失败" in retry_prompt  # 错误信息回喂（Observation）


@pytest.mark.asyncio
async def test_complete_structured_exhausted_raises_with_error_log(caplog) -> None:  # noqa: ANN001
    mock = MockLLMClient()
    mock.push("垃圾1", "垃圾2", "垃圾3")  # 1 + 2 次重试全失败

    with caplog.at_level(logging.ERROR, logger="app.agents.structured"):
        with pytest.raises(StructuredOutputError) as exc_info:
            await complete_structured(mock, [{"role": "user", "content": "x"}], _Box, use_fallback_once=False)

    assert exc_info.value.attempts == 1 + w2.STRUCTURED_REPAIR_MAX_RETRY  # 3
    assert any(r.message == "structured_output_exhausted" for r in caplog.records)
    record = next(r for r in caplog.records if r.message == "structured_output_exhausted")
    assert record.attempts == 3  # ERROR 级结构化日志带结构化字段


@pytest.mark.asyncio
async def test_complete_structured_retry_budget_is_two() -> None:
    """任务 6 口径：最多重试 2 次（共 3 次调用），第 3 次成功即恢复。"""
    mock = MockLLMClient()
    mock.push("垃圾1", "垃圾2", '{"a": 3}')
    result = await complete_structured(mock, [{"role": "user", "content": "x"}], _Box, use_fallback_once=False)
    assert result.value.a == 3 and result.attempts == 3


# ---------------------------------------------------------------------------
# 3. Planner 节点兜底：永不崩溃
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_planner_node_fails_safe_on_garbage_llm() -> None:
    """LLM 永远输出垃圾（含修复重试+备用模型穷尽）：节点返回 FAILED，不抛异常。"""
    mock = MockLLMClient()
    mock.push("垃圾")  # 队列耗尽后 mock 会按 marker 返回合法 JSON → 需要另一种永久垃圾源

    class _AlwaysGarbage(MockLLMClient):
        async def complete(self, messages, **kwargs):  # noqa: ANN001, ANN202
            from app.agents.llm import LLMResponse

            return LLMResponse(content="这不是 JSON", model="garbage", provider="mock")

    node = make_planner_node(_AlwaysGarbage(), ContextService())
    state: dict[str, Any] = {
        "goal": "分析上周 GMV",
        "time_range": {"start": "2026-10-12", "end": "2026-10-18"},
        "messages": [],
    }
    update = await node(state)  # 不应抛出任何异常

    assert update["status"] == "FAILED"
    assert update["error"]["code"] == "PLAN_INVALID"
    assert "规划失败" in update["messages"][0]["content"]


@pytest.mark.asyncio
async def test_planner_node_recovers_after_markdown_output() -> None:
    """第一次输出带 ```json 围栏的合法计划 → extract_json 直接救回，节点正常 PLANNING。"""
    mock = MockLLMClient()
    mock.push('这是计划：```json\n{"subtasks": [], "assumptions": [], "replan_count": 0}\n```')
    mock.push(
        json.dumps(
            {
                "subtasks": [
                    {"seq": 1, "name": "取数", "description": "d", "tool_hint": "sql_query",
                     "depends_on": [], "risk_level": "normal", "acceptance": "a"},
                    {"seq": 2, "name": "归因", "description": "d", "tool_hint": "none",
                     "depends_on": [1], "risk_level": "normal", "acceptance": "a"},
                    {"seq": 3, "name": "汇报", "description": "d", "tool_hint": "none",
                     "depends_on": [2], "risk_level": "normal", "acceptance": "a"},
                ],
                "assumptions": [],
                "replan_count": 0,
            },
            ensure_ascii=False,
        )
    )

    payload = PlannerInput(
        goal="分析上周 GMV", time_range={"start": "2026-10-12", "end": "2026-10-18"}
    )
    # 围栏内是合法 JSON 但 subtasks=[] → DAG 校验失败回喂重试 → 第 2 次合法
    node = make_planner_node(mock, ContextService())
    update = await node(
        {"goal": payload.goal, "time_range": payload.time_range, "messages": []}
    )
    assert update["status"] == "PLANNING"
    assert len(update["plan"]) == 3


# ---------------------------------------------------------------------------
# 4. Reviewer 节点兜底：两级降级
# ---------------------------------------------------------------------------
def _review_state(failures: int = 0) -> dict[str, Any]:
    return {
        "goal": "分析上周 GMV",
        "context_digest": "【分析】GMV 100 万，环比 -5%",
        "plan": [
            SubtaskPlan(
                seq=1, name="取数", description="d", tool_hint="sql_query",
                depends_on=[], risk_level="normal", acceptance="a",
            )
        ],
        "subtask_results": {
            "1": SubtaskResult(
                subtask_seq=1, status="DONE", tool="sql-query",
                data_digest="查询返回 12 行指标数据，GMV 100 万",
            )
        },
        "review_failures": failures,
        "messages": [],
    }


class _AlwaysGarbageLLM(MockLLMClient):
    async def complete(self, messages, **kwargs):  # noqa: ANN001, ANN202
        from app.agents.llm import LLMResponse

        return LLMResponse(content="依旧不是 JSON", model="garbage", provider="mock")


class _FakeGateway:
    """外部评测桩：恒定 faithfulness=4，让 review_deliverable 走 external 路径调 LLM。"""

    async def faithfulness(self, digest: str, evidence: list):  # noqa: ANN001, ANN202
        from types import SimpleNamespace

        return SimpleNamespace(faithfulness=4, degraded=False)


@pytest.mark.asyncio
async def test_reviewer_first_failure_degrades_to_local() -> None:
    """第 1 次 Reviewer 输出不合规：走本地规则降级评分（REVIEW_SKIPPED 之前），不炸图。"""
    node = make_reviewer_node(_AlwaysGarbageLLM(), ContextService(), gateway=_FakeGateway(), force_local=False)
    update = await node(_review_state(failures=0))

    assert "error" not in update  # 不炸图
    assert update["review_failures"] == 1
    assert update["eval_score"]["source"] == "local_fallback"
    assert update["eval_score"]["total"] > 0


@pytest.mark.asyncio
async def test_reviewer_second_failure_skips_with_passing_default() -> None:
    """连续第 2 次不合规：REVIEW_SKIPPED 放行——默认及格 3.5 分 + 可信度 ≤0.4 + ERROR 日志。"""
    node = make_reviewer_node(_AlwaysGarbageLLM(), ContextService(), gateway=_FakeGateway(), force_local=False)
    update = await node(_review_state(failures=1))

    assert "error" not in update
    assert update["review"]["skipped"] is True
    assert update["review"]["total"] == 3.5  # 默认及格分
    assert update["review"]["verdict"] == "PASS"
    assert update["eval_score"]["confidence"] <= 0.4
    assert "REVIEW_SKIPPED" in update["review"]["skip_reason"]


@pytest.mark.asyncio
async def test_reviewer_recovers_on_second_attempt() -> None:
    """第 1 次垃圾、第 2 次合法：complete_structured 修复重试直接救回，走 external 正常评分。"""
    from types import SimpleNamespace

    class _FakeGateway:
        async def faithfulness(self, digest: str, evidence: list):  # noqa: ANN001, ANN202
            return SimpleNamespace(faithfulness=4, degraded=False)

    mock = MockLLMClient()
    mock.push("不是 JSON 的话")
    mock.push(
        json.dumps(
            {
                "scores": {"evidence": 4, "faithfulness": 4, "consistency": 4, "completeness": 4, "format": 4},
                "total": 4.0,
                "pass": True,
                "suggestions": [],
            },
            ensure_ascii=False,
        )
    )
    node = make_reviewer_node(mock, ContextService(), gateway=_FakeGateway(), force_local=False)
    update = await node(_review_state(failures=0))

    assert update["review_failures"] == 0  # 成功即清零
    assert update["eval_score"]["source"] == "external"
    assert update["eval_score"]["total"] == 4.0  # faithfulness 取外部评测分 4
