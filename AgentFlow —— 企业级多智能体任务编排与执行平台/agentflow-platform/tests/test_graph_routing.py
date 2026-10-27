"""图装配与条件路由单测（PRD 5.2 边表）。

路由函数全部是纯函数，因此可以脱离 LLM / DB 单独回归——
这是保证"7 节点固定拓扑 + 单挂起点"不被后续改动破坏的第一道防线。
"""

from __future__ import annotations

import pytest
from langgraph.graph import END

from app.agents.graph import (
    NODE_NAMES,
    build_graph,
    route_after_compress,
    route_after_executor,
    route_after_gate,
    route_after_planner,
    route_after_review,
    route_after_summary,
    route_from_start,
)
from app.core.w2_config import w2

pytestmark = pytest.mark.unit


def plan(tool: str = "none", risk: str = "normal") -> list[dict]:
    return [
        {
            "seq": 1,
            "name": "子任务",
            "description": "描述",
            "tool_hint": tool,
            "depends_on": [],
            "risk_level": risk,
            "acceptance": "有产物",
        }
    ]


# ---------------------------------------------------------------------------
# planner 之后
# ---------------------------------------------------------------------------
def test_route_after_planner_to_executor() -> None:
    assert route_after_planner({"plan": plan("sql_query")}) == "executor"


def test_route_after_planner_to_human_gate() -> None:
    """含 notify（高危）子任务 → 必须经过 human_gate 挂起。"""
    assert route_after_planner({"plan": plan("notify", "high")}) == "human_gate"


def test_route_after_planner_high_risk_flag_alone_triggers_gate() -> None:
    assert route_after_planner({"plan": plan("chart_render", "high")}) == "human_gate"


def test_route_after_planner_failed_goes_end() -> None:
    assert route_after_planner({"status": "FAILED", "plan": plan()}) == END
    assert route_after_planner({"error": {"code": "PLAN_INVALID"}, "plan": plan()}) == END


# ---------------------------------------------------------------------------
# G8：START 之后（memory_recall）与 planner 之后（context_compress）
# ---------------------------------------------------------------------------
def test_route_from_start_default_to_planner() -> None:
    """默认（无 use_memory）→ 直入 planner，等价原 START→planner。"""
    assert route_from_start({}) == "planner"
    assert route_from_start({"use_memory": False}) == "planner"


def test_route_from_start_use_memory_to_recall() -> None:
    assert route_from_start({"use_memory": True}) == "memory_recall"


def test_route_from_start_failed_goes_end() -> None:
    assert route_from_start({"status": "FAILED"}) == END


def test_route_after_planner_compress_true_to_compress() -> None:
    """compress=True → context_compress，与是否高危无关。"""
    assert route_after_planner({"compress": True, "plan": plan("sql_query")}) == "context_compress"
    assert route_after_planner({"compress": True, "plan": plan("notify", "high")}) == "context_compress"


def test_route_after_planner_compress_default_unchanged() -> None:
    """compress 缺省（False）→ 行为与改造前完全一致。"""
    assert route_after_planner({"plan": plan("sql_query")}) == "executor"
    assert route_after_planner({"plan": plan("notify", "high")}) == "human_gate"


def test_route_after_compress_splits_to_gate_or_executor() -> None:
    assert route_after_compress({"plan": plan("sql_query")}) == "executor"
    assert route_after_compress({"plan": plan("notify", "high")}) == "human_gate"
    assert route_after_compress({"status": "FAILED"}) == END


# ---------------------------------------------------------------------------
# human_gate 之后
# ---------------------------------------------------------------------------
def test_route_after_gate_approved_goes_executor() -> None:
    assert route_after_gate({"approvals": {"status": "APPROVED"}}) == "executor"


def test_route_after_gate_rejected_goes_replan() -> None:
    assert route_after_gate({"approvals": {"status": "REJECTED"}}) == "replan"


def test_route_after_gate_missing_state_defaults_executor() -> None:
    assert route_after_gate({}) == "executor"


def test_route_after_gate_failed_goes_end() -> None:
    assert route_after_gate({"status": "FAILED"}) == END


# ---------------------------------------------------------------------------
# executor / reviewer / summarizer 之后
# ---------------------------------------------------------------------------
def test_route_after_executor() -> None:
    assert route_after_executor({"status": "RUNNING"}) == "reviewer"
    assert route_after_executor({"status": "FAILED"}) == END


def test_route_after_review_pass_goes_summarizer() -> None:
    assert route_after_review({"review": {"total": 4.2, "pass": True}}) == "summarizer"


def test_route_after_review_high_total_without_pass_flag() -> None:
    assert route_after_review({"review": {"total": 3.6}}) == "summarizer"


def test_route_after_review_low_score_goes_replan() -> None:
    state = {"review": {"total": 2.4, "pass": False}, "replan_count": 0}

    assert route_after_review(state) == "replan"


def test_route_after_review_skipped_always_passes() -> None:
    """REVIEW_SKIPPED 必须放行，不能卡死主流程。"""
    state = {"review": {"total": 3.5, "skipped": True}, "replan_count": 2}

    assert route_after_review(state) == "summarizer"


def test_route_after_review_exhausted_degrades_to_summarizer() -> None:
    state = {"review": {"total": 2.0, "pass": False}, "replan_count": w2.REPLAN_MAX_COUNT}

    assert route_after_review(state) == "summarizer"


def test_route_after_review_alias_key_supported() -> None:
    """model_dump() 落库的是 pass_，路由必须两种键都认。"""
    assert route_after_review({"review": {"total": 1.9, "pass_": True}}) == "summarizer"


def test_route_after_summary() -> None:
    assert route_after_summary({"plan": plan("notify")}) == "notifier"
    assert route_after_summary({"plan": plan("sql_query")}) == END
    assert route_after_summary({"status": "FAILED", "plan": plan("notify")}) == END


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
def test_graph_has_all_nodes(mcp_pool, mock_llm, context_service) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    graph = build_graph(
        llm=mock_llm,
        pool=mcp_pool,
        context=context_service,
        tools=mcp_pool.tools,
        checkpointer=MemorySaver(),
        force_local_review=True,
    )

    assert NODE_NAMES == (
        "planner",
        "human_gate",
        "executor",
        "reviewer",
        # v1.1：REJECT 人工批注闸门（reviewer 的 "replan" 出边经过它再进 replan）
        "review_gate",
        "replan",
        "summarizer",
        "notifier",
        "memory_recall",
        "context_compress",
    )
    assert set(graph.get_graph().nodes) >= set(NODE_NAMES)
