"""端到端冒烟（Mock 全链路）：planner → executor → reviewer → summarizer → [notifier]。

目的不是验证"分析质量"，而是验证**装配正确性**：
1. 7 节点图在 Mock 依赖下能跑通到终态；
2. 唯一 interrupt 挂起点确实在 human_gate，``Command(resume=...)`` 能续跑；
3. 审批未通过时 notifier 被双重闸门拦住（节点 + MCP Server）；
4. state 关键字段（plan / results / review / summary / artifacts / token_ledger）齐全。

依赖全部进程内：LLM=Mock、MCP=InMemoryTransport、DB=SQLite 内存、checkpointer=MemorySaver，
因此可以在 CI 里稳定跑。
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agents.graph import build_graph, pending_interrupts
from app.schemas.enums import ApprovalStatus, ApprovalType
from app.schemas.orm_models import Approval

pytestmark = pytest.mark.unit


def status_of(value: Any) -> str:
    return str(value["status"] if isinstance(value, dict) else value.status)


def _state(goal: str) -> dict[str, Any]:
    return {
        "task_id": 1,
        "trace_id": "trace-e2e",
        "creator_id": 1,
        "goal": goal,
        "time_range": {"start": "2026-10-12", "end": "2026-10-18"},
        "messages": [],
        "token_ledger": {},
        "subtask_results": {},
        "artifacts": [],
        "status": "PENDING",
    }


def _config(thread: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread}}


# ---------------------------------------------------------------------------
# 1) 无高危动作：一气跑到 COMPLETED
# ---------------------------------------------------------------------------
async def test_pipeline_completes_without_approval(mcp_pool, mock_llm, context_service) -> None:
    graph = build_graph(
        llm=mock_llm,
        pool=mcp_pool,
        context=context_service,
        tools=mcp_pool.tools,
        checkpointer=MemorySaver(),
        force_local_review=True,
    )

    final = await graph.ainvoke(_state("分析上周 GMV 下跌原因并检索历史周报佐证"), _config("no-gate"))

    assert final["status"] == "COMPLETED"
    assert final.get("summary")
    # 规划 3~15 个子任务，且全部有终态
    assert 3 <= len(final["plan"]) <= 15
    assert {status_of(r) for r in final["subtask_results"].values()} <= {"DONE", "SKIPPED"}
    # 评审通过（本地规则评分）
    review = final["review"]
    assert float(review["total"] if isinstance(review, dict) else review.total) >= 3.5
    # 报告产物
    types = {a["type"] if isinstance(a, dict) else a.type for a in final["artifacts"]}
    assert "report" in types
    # Token 记账覆盖 planner / executor / reviewer / summarizer
    assert {"planner", "executor", "reviewer", "summarizer"} <= set(final["token_ledger"])
    assert final["context_digest"]


async def test_pipeline_records_context_meta(mcp_pool, mock_llm, context_service) -> None:
    graph = build_graph(
        llm=mock_llm,
        pool=mcp_pool,
        context=context_service,
        tools=mcp_pool.tools,
        checkpointer=MemorySaver(),
        force_local_review=True,
    )

    final = await graph.ainvoke(_state("分析上周 GMV 下跌原因"), _config("ctx-meta"))

    meta = final["context_meta"]
    assert meta["node"]
    assert meta["budget"] > 0
    assert meta["estimated"] >= 0


# ---------------------------------------------------------------------------
# 2) 含 notify：interrupt → 批准 → 完成
# ---------------------------------------------------------------------------
async def test_notify_flow_interrupts_then_completes(
    mcp_pool, mock_llm, context_service, session_factory
) -> None:
    created: list[dict[str, Any]] = []
    decisions: list[tuple[int, str]] = []

    async def fake_creator(payload: dict[str, Any]) -> dict[str, Any]:
        created.append(payload)
        return {"approval_id": 42, "status": "PENDING", "expires_at": None}

    async def fake_updater(approval_id: int, decision: str, comment: str, decided_by: int | None) -> None:
        decisions.append((approval_id, decision))

    graph = build_graph(
        llm=mock_llm,
        pool=mcp_pool,
        context=context_service,
        tools=mcp_pool.tools,
        checkpointer=MemorySaver(),
        force_local_review=True,
        approval_creator=fake_creator,
        approval_updater=fake_updater,
    )
    config = _config("gate-approve")

    paused = await graph.ainvoke(_state("分析上周 GMV 并通知运营群"), config)

    # 唯一挂起点：human_gate（ainvoke 返回值不含 __interrupt__，需查快照）
    interrupts = await pending_interrupts(graph, config)
    assert interrupts, "含 notify 子任务必须停在 human_gate"
    interrupt_payload = interrupts[0].value
    assert interrupt_payload["approval_id"] == 42
    assert interrupt_payload["preview"]["subtasks"]
    assert paused["status"] == "PLANNING"  # 尚未进入 executor
    assert created and created[0]["task_id"] == 1

    # 审批库写一条 APPROVED，供 MCP Server 侧第二道闸门校验
    async with session_factory() as session:
        session.add(
            Approval(
                id=42,
                task_id=1,
                type=ApprovalType.HIGH_RISK_TOOL,
                reason="推送运营群",
                status=ApprovalStatus.APPROVED,
                requested_by=0,
            )
        )
        await session.commit()

    final = await graph.ainvoke(Command(resume={"decision": "approve", "comment": "同意"}), config)

    assert decisions == [(42, "APPROVED")]
    assert final["status"] == "COMPLETED"
    assert final["notify_result"]["mock"] is True
    assert final["notify_result"]["message_id"]


# ---------------------------------------------------------------------------
# 3) 含 notify：驳回 → 重规划 → notifier 被拦截
# ---------------------------------------------------------------------------
async def test_notify_flow_rejection_blocks_sending(
    mcp_pool, mock_llm, context_service
) -> None:
    async def fake_creator(payload: dict[str, Any]) -> dict[str, Any]:
        return {"approval_id": 7, "status": "PENDING", "expires_at": None}

    async def fake_updater(approval_id: int, decision: str, comment: str, decided_by: int | None) -> None:
        return None

    graph = build_graph(
        llm=mock_llm,
        pool=mcp_pool,
        context=context_service,
        tools=mcp_pool.tools,
        checkpointer=MemorySaver(),
        force_local_review=True,
        approval_creator=fake_creator,
        approval_updater=fake_updater,
    )
    config = _config("gate-reject")

    paused = await graph.ainvoke(_state("分析上周 GMV 并通知运营群"), config)
    assert await pending_interrupts(graph, config)

    final = await graph.ainvoke(
        Command(resume={"decision": "reject", "comment": "结论缺少证据，不要推送"}), config
    )

    assert final["status"] == "FAILED"
    assert final["error"]["code"] == "APPROVAL_MISSING"
    assert final.get("notify_result") is None
    # 驳回促成了一次重规划
    assert int(final.get("replan_count") or 0) >= 1
