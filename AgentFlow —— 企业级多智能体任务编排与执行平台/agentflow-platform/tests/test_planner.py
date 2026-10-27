"""Planner 单测（FR-002 / FR-003）。

覆盖点：
1. 正常规划：输出可被 DAG 校验（3~15 子任务、无环、依赖合法）；
2. **JSON 解析失败 → 修复重试成功**（PRD FR-002 验收 2）；
3. **DAG 校验失败 → 回喂错误重排成功**；
4. 修复 2 次仍失败 → ``PlannerError``（不静默产出坏计划）；
5. temperature 固定 0.2（PRD 8.2 硬性要求）；
6. 重规划入口必须携带 ``replan_context``，且允许少于 3 个子任务。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.agents.llm import BaseLLMClient, LLMClient, LLMResponse, MockLLMClient
from app.agents.planner import PLANNER_TEMPERATURE, PlannerError, plan, replan, run_planner
from app.schemas.agent_io import PlannerInput, ReplanContext, ToolDef
from app.schemas.graph_state import TimeRange
from app.utils.dag import validate_plan

pytestmark = pytest.mark.unit

RANGE = TimeRange(start=date(2026, 10, 12), end=date(2026, 10, 18))


def make_payload(goal: str = "分析上周 GMV 下跌原因并输出报告", replan_context=None) -> PlannerInput:
    return PlannerInput(goal=goal, time_range=RANGE, replan_context=replan_context)


def st(seq: int, name: str, tool: str = "none", deps: tuple[int, ...] = (), risk: str = "normal") -> dict:
    return {
        "seq": seq,
        "name": name,
        "description": f"{name}：按目标时间窗取数与归因",
        "tool_hint": tool,
        "depends_on": list(deps),
        "risk_level": risk,
        "acceptance": "返回可核对的数据或结论",
    }


def plan_json(subtasks: list[dict], replan_count: int = 0) -> str:
    return json.dumps(
        {"subtasks": subtasks, "assumptions": ["时间范围以任务入参为准"], "replan_count": replan_count},
        ensure_ascii=False,
    )


VALID_THREE = [
    st(1, "取数：核心指标", "sql_query"),
    st(2, "归因：品类下钻", "none", (1,)),
    st(3, "汇总报告", "none", (1, 2)),
]


class SpyLLM(BaseLLMClient):
    """记录调用的 LLM 桩：用于断言 temperature / max_tokens 等硬性参数。"""

    model = "spy-llm"

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    async def complete(self, messages, *, temperature=None, max_tokens=None, json_mode=True):  # noqa: ANN001
        self.calls.append(
            {"messages": messages, "temperature": temperature, "max_tokens": max_tokens, "json_mode": json_mode}
        )
        return LLMResponse(content=self.content, model=self.model, prompt_tokens=11, completion_tokens=7)


# ---------------------------------------------------------------------------
# 1) 正常路径
# ---------------------------------------------------------------------------
async def test_mock_planner_produces_valid_dag(mock_llm: LLMClient, context_service) -> None:
    run = await run_planner(mock_llm, context_service, make_payload())

    assert len(run.plan.subtasks) >= 3
    report = validate_plan(run.plan.subtasks, min_subtasks=3, max_subtasks=15)
    assert report.ok, report.error_text
    assert run.attempts == 1
    # 预算与记账可用
    assert run.context.budget > 0
    assert run.input_tokens > 0


async def test_planner_passes_tools_to_prompt(mock_llm: LLMClient, context_service) -> None:
    tools = [ToolDef(name="sql-query", description="只读查询", inputSchema={"type": "object"})]
    payload = PlannerInput(goal="分析上周 GMV", time_range=RANGE, tools=tools)

    run = await run_planner(mock_llm, context_service, payload)

    assert run.plan.subtasks
    assert any(item.tool_name == "sql-query" for item in run.plan.subtasks)


# ---------------------------------------------------------------------------
# 2) JSON 解析失败 → 修复重试
# ---------------------------------------------------------------------------
async def test_planner_repairs_malformed_json(
    mock_llm_client: MockLLMClient, context_service
) -> None:
    """第一次返回非 JSON 文本，第二次返回合法计划：应重试一次后成功。"""
    mock_llm_client.push("抱歉，我无法给出计划。", plan_json(VALID_THREE))
    llm = LLMClient(primary=mock_llm_client, mock=True)

    run = await run_planner(llm, context_service, make_payload())

    assert run.attempts == 2
    assert len(run.repair_errors) == 1
    assert "JSON 解析失败" in run.repair_errors[0]
    assert validate_plan(run.plan.subtasks, min_subtasks=3, max_subtasks=15).ok


async def test_planner_repairs_fenced_json(mock_llm_client: MockLLMClient, context_service) -> None:
    """带 ```json 围栏 + 前后废话也应被容忍，不计入修复次数。"""
    fenced = "好的，计划如下：\n```json\n" + plan_json(VALID_THREE) + "\n```\n以上。"
    mock_llm_client.push(fenced)
    llm = LLMClient(primary=mock_llm_client, mock=True)

    run = await run_planner(llm, context_service, make_payload())

    assert run.attempts == 1
    assert run.repair_errors == []


# ---------------------------------------------------------------------------
# 3) DAG 校验失败 → 回喂重排
# ---------------------------------------------------------------------------
async def test_planner_repairs_dag_cycle(mock_llm_client: MockLLMClient, context_service) -> None:
    cyclic = [st(1, "A", deps=(3,)), st(2, "B", "sql_query", (1,)), st(3, "C", deps=(2,))]
    mock_llm_client.push(plan_json(cyclic), plan_json(VALID_THREE))
    llm = LLMClient(primary=mock_llm_client, mock=True)

    run = await run_planner(llm, context_service, make_payload())

    assert run.attempts == 2
    assert any("环" in err or "环" in err or "cycle" in err.lower() for err in run.repair_errors)
    assert validate_plan(run.plan.subtasks, min_subtasks=3, max_subtasks=15).ok


async def test_planner_repairs_missing_dependency(mock_llm_client: MockLLMClient, context_service) -> None:
    broken = [st(1, "A", "sql_query"), st(2, "B", deps=(9,)), st(3, "C", deps=(1, 2))]
    mock_llm_client.push(plan_json(broken), plan_json(VALID_THREE))
    llm = LLMClient(primary=mock_llm_client, mock=True)

    run = await run_planner(llm, context_service, make_payload())

    assert run.attempts == 2
    assert validate_plan(run.plan.subtasks, min_subtasks=3, max_subtasks=15).ok


# ---------------------------------------------------------------------------
# 4) 修复用尽 → 明确失败
# ---------------------------------------------------------------------------
async def test_planner_raises_after_repair_exhausted(
    mock_llm_client: MockLLMClient, context_service
) -> None:
    mock_llm_client.push("not json", "still not json", "definitely not json")
    llm = LLMClient(primary=mock_llm_client, mock=True)

    with pytest.raises(PlannerError) as excinfo:
        await run_planner(llm, context_service, make_payload())

    assert excinfo.value.code == "PLAN_INVALID"
    assert excinfo.value.attempts == 3  # 1 次原始 + 2 次修复（STRUCTURED_REPAIR_MAX_RETRY=2）


async def test_planner_raises_when_dag_still_invalid(
    mock_llm_client: MockLLMClient, context_service
) -> None:
    """DAG 两轮仍不合规（这里故意只给 1 个子任务，低于 3 的下限）。"""
    too_few = [st(1, "只有一个", "sql_query")]
    mock_llm_client.push(plan_json(too_few), plan_json(too_few))
    llm = LLMClient(primary=mock_llm_client, mock=True)

    with pytest.raises(PlannerError) as excinfo:
        await run_planner(llm, context_service, make_payload())

    assert excinfo.value.code == "PLAN_INVALID"


# ---------------------------------------------------------------------------
# 5) 硬性参数
# ---------------------------------------------------------------------------
async def test_planner_temperature_is_0_2(context_service) -> None:
    spy = SpyLLM(plan_json(VALID_THREE))
    llm = LLMClient(primary=spy, mock=True)

    await run_planner(llm, context_service, make_payload())

    assert PLANNER_TEMPERATURE == 0.2
    assert spy.calls[0]["temperature"] == 0.2
    assert spy.calls[0]["json_mode"] is True


# ---------------------------------------------------------------------------
# 6) 重规划入口
# ---------------------------------------------------------------------------
async def test_replan_requires_context(mock_llm: LLMClient, context_service) -> None:
    with pytest.raises(PlannerError) as excinfo:
        await replan(mock_llm, context_service, make_payload())

    assert excinfo.value.code == "REPLAN_INVALID"


async def test_replan_allows_smaller_plan(mock_llm_client: MockLLMClient, context_service) -> None:
    """重规划只重排受影响分支，允许少于 3 个子任务（min_subtasks=1）。"""
    smaller = [st(1, "重跑取数", "sql_query"), st(2, "重写结论", deps=(1,))]
    mock_llm_client.push(plan_json(smaller))
    llm = LLMClient(primary=mock_llm_client, mock=True)

    ctx = ReplanContext(cause="review_rejected", failed_seqs=[2], review_comment="证据不足")
    run = await replan(llm, context_service, make_payload(replan_context=ctx))

    assert len(run.plan.subtasks) == 2


async def test_plan_alias_matches_run_planner(mock_llm: LLMClient, context_service) -> None:
    run = await plan(mock_llm, context_service, make_payload())
    assert run.plan.subtasks
