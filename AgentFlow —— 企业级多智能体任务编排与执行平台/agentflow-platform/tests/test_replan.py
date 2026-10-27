"""replan 节点单测（FR-003）。

覆盖：
1. 重规划上下文的原因优先级：人工驳回 > 评审未过 > 工具失败；
2. 已达 ``REPLAN_MAX_COUNT`` → ``REPLAN_EXHAUSTED`` + FAILED（不无限重规划）；
3. 重规划保留 DONE 结果，失败/跳过子任务用 SKIPPED 占位以便重跑；
4. 重规划成功后次数 +1、状态回到 RUNNING。
"""

from __future__ import annotations

import pytest

from app.agents.llm import LLMClient
from app.agents.replan import _build_context, make_replan_node
from app.core.w2_config import w2

pytestmark = pytest.mark.unit

TIME_RANGE = {"start": "2026-10-12", "end": "2026-10-18"}


def plan_dict(seq: int, name: str, tool: str = "none") -> dict:
    return {
        "seq": seq,
        "name": name,
        "description": f"{name}：按目标时间窗取数与归因",
        "tool_hint": tool,
        "depends_on": [] if seq == 1 else [seq - 1],
        "risk_level": "normal",
        "acceptance": "有数据或结论",
    }


def base_state(**overrides) -> dict:
    state = {
        "task_id": 1,
        "goal": "分析上周 GMV 下跌原因",
        "time_range": TIME_RANGE,
        "plan": [plan_dict(1, "取数", "sql_query"), plan_dict(2, "归因")],
        "subtask_results": {
            "1": {"subtask_seq": 1, "status": "DONE", "data_digest": "GMV 125000.5，环比 -8.2%", "tool": "sql-query"},
            "2": {"subtask_seq": 2, "status": "FAILED", "data_digest": "", "tool": "none"},
        },
        "replan_count": 0,
        "messages": [],
        "artifacts": [],
        "token_ledger": {},
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# 1) 上下文原因优先级
# ---------------------------------------------------------------------------
def test_context_cause_human_rejected() -> None:
    state = base_state(
        approvals={"status": "REJECTED", "comment": "结论缺少证据，重做"},
        review={"pass": True},
    )

    ctx = _build_context(state)

    assert ctx.cause == "human_rejected"
    assert "缺少证据" in ctx.human_comment
    assert ctx.completed_summary == {1: "GMV 125000.5，环比 -8.2%"}
    assert ctx.failed_seqs == [2]


def test_context_cause_review_rejected() -> None:
    state = base_state(
        review={
            "pass": False,
            "total": 2.8,
            "suggestions": [{"seq": 2, "advice": "补充品类下钻证据"}],
        }
    )

    ctx = _build_context(state)

    assert ctx.cause == "review_rejected"
    assert "#2" in ctx.review_comment
    assert "补充品类下钻证据" in ctx.review_comment


def test_context_cause_defaults_to_tool_failed() -> None:
    ctx = _build_context(base_state())

    assert ctx.cause == "tool_failed"
    assert ctx.failed_seqs == [2]


def test_context_review_comment_falls_back_to_total() -> None:
    state = base_state(review={"pass": False, "total": 2.5, "suggestions": []})

    ctx = _build_context(state)

    assert "2.5" in ctx.review_comment


# ---------------------------------------------------------------------------
# 2) 次数上限
# ---------------------------------------------------------------------------
async def test_replan_exhausted(mock_llm: LLMClient, context_service) -> None:
    node = make_replan_node(mock_llm, context_service)

    result = await node(base_state(replan_count=w2.REPLAN_MAX_COUNT))

    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "REPLAN_EXHAUSTED"


# ---------------------------------------------------------------------------
# 3) 结果保留策略
# ---------------------------------------------------------------------------
async def test_replan_preserves_done_and_requeues_failed(mock_llm: LLMClient, context_service) -> None:
    node = make_replan_node(mock_llm, context_service)

    result = await node(base_state())

    assert result["status"] == "RUNNING"
    assert result["replan_count"] == 1
    preserved = result["subtask_results"]
    assert preserved["1"].status == "DONE"
    assert preserved["2"].status == "SKIPPED"  # 占位，executor 会重跑并覆盖
    assert preserved["2"].data_digest == "重规划待重跑"
    # 新计划已写入 state
    assert len(result["plan"]) >= 2
    assert result["messages"]


async def test_replan_keeps_replan_count_within_limit(mock_llm: LLMClient, context_service) -> None:
    node = make_replan_node(mock_llm, context_service)

    result = await node(base_state(replan_count=1))

    assert result["replan_count"] == 2


async def test_replan_records_token_ledger(mock_llm: LLMClient, context_service) -> None:
    node = make_replan_node(mock_llm, context_service)

    result = await node(base_state())

    assert "replan" in result["token_ledger"]
    assert result["token_ledger"]["replan"]["input"] > 0
