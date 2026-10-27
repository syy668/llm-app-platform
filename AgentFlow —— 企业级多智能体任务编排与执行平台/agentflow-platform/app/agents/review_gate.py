"""review_gate 节点（v1.1 任务 3）：Reviewer 判 REJECT 时的人工批注挂起点。

背景：Reviewer 的 ``verdict`` 由服务端计算（分数 + 证据覆盖 + 来源系数），
判 REJECT 意味着「连重规划都未必救得回来」。纯自动重规划在真实场景里经常是
"用同样的证据再猜一次"，人看一眼往往一句"口径换成环比、把断货 SKU 单独列出来"
就能救活。本节点把这一步变成可操作的挂起点：

```
reviewer ──(PASS)──────────────────────────────→ summarizer
        └──(REVISE / REJECT，仍有重规划预算)──→ review_gate ──→ replan
                                                  │  REJECT 时 interrupt
                                                  └─(人工放行)──→ summarizer
```

三条硬约束（保证不改动已验收的评审逻辑）：

1. **不碰 Reviewer**：``compute_confidence`` / ``decide_verdict`` / ``route_after_review``
   一行不改；本节点只消费它们产出的 ``verdict``。
2. **只对 REJECT 挂起**：REVISE 直接透传到 replan（它是"改改就行"的常规路径，
   拦下来会显著拖慢正常任务）。
3. **仅在有重规划预算时才挂起**：路由已经把"预算用尽"分流到 summarizer，
   本节点拿到的 REJECT 一定还有至少一次 replan 机会，批注才有落点。

恢复方式复用既有 ``POST /tasks/{id}/resume``（``Command(resume=...)``）：
- ``decision=REJECTED`` + ``comment``：批注作为**硬约束**写入 ``replan_context.human_comment``，
  优先级高于评审建议（replan 的 _build_context 已有该优先级）；
- ``decision=APPROVED``：人工放行，跳过重规划直接汇总（报告里标注"人工放行"）。
"""

from __future__ import annotations

from typing import Any, Callable

from langgraph.types import interrupt

from app.agents.common import plan_of
from app.core.logging import get_logger
from app.schemas.graph_state import TaskState

logger = get_logger(__name__)

AGENT_NAME = "review_gate"

#: interrupt 的 type 标识：前端据此渲染「评审驳回 · 人工批注」卡片而不是审批抽屉
INTERVENTION_TYPE = "REVIEW_INTERVENTION"


def _parse_intervention(value: Any) -> tuple[str, str, int | None]:
    """解析恢复值：``APPROVED`` 放行 / ``REJECTED`` 批注重规划。

    兼容三种形态（与 human_gate 的 ``_parse_decision`` 口径一致，便于前端复用同一套提交逻辑）：
    dict（标准）、bool、纯字符串。
    """
    if isinstance(value, dict):
        raw = str(value.get("decision") or value.get("status") or "").lower()
        comment = str(value.get("comment") or value.get("annotation") or "").strip()
        decided_by = value.get("decided_by")
    elif isinstance(value, bool):
        raw, comment, decided_by = ("approve" if value else "reject"), "", None
    else:
        raw, comment, decided_by = str(value or "").strip().lower(), "", None

    approved = raw in {"approve", "approved", "true", "yes", "1", "pass"}
    return ("APPROVED" if approved else "REJECTED"), comment[:500], decided_by


def _preview(state: TaskState) -> dict[str, Any]:
    review = state.get("review") or {}
    suggestions = review.get("suggestions") or []
    return {
        "verdict": review.get("verdict"),
        "total": review.get("total"),
        "confidence": review.get("confidence"),
        "confidence_reason": review.get("confidence_reason"),
        "scores": review.get("scores") or {},
        "suggestions": suggestions[:5],
        "replan_count": int(state.get("replan_count") or 0),
        "goal": str(state.get("goal") or "")[:200],
        "subtask_count": len(plan_of(state)),
        "report_excerpt": str(state.get("context_digest") or state.get("summary") or "")[:600],
    }


def _route(intervention: dict[str, Any], state: TaskState) -> dict[str, Any]:
    """按人工决策返回增量状态。"""
    status = str(intervention.get("status") or "").upper()
    comment = str(intervention.get("comment") or "")
    review = state.get("review") or {}

    if status == "APPROVED":
        # 人工放行：跳过重规划，直接进汇总；把干预记录留给报告与详情页
        return {
            "review_intervention": intervention,
            "degraded": True,
            "messages": [
                {
                    "role": "assistant",
                    "content": f"人工放行（评审 {review.get('verdict')}，总分 {review.get('total')}）：按当前结论降级交付",
                }
            ],
            "status": "SUMMARIZING",
        }

    suggestions = review.get("suggestions") or []
    review_comment = "; ".join(
        f"#{item.get('seq')}: {item.get('advice')}" for item in suggestions[:3] if isinstance(item, dict)
    )
    human_comment = comment or "人工驳回：未填写批注，按评审建议重新规划"
    return {
        "review_intervention": intervention,
        "replan_context": {
            "cause": "human_rejected",
            "failed_seqs": [],
            "human_comment": human_comment,
            "review_comment": review_comment[:500],
            "completed_summary": {},
        },
        "messages": [
            {
                "role": "assistant",
                "content": f"人工批注已注入重规划约束：{human_comment[:120]}",
            }
        ],
        "status": "RUNNING",
    }


def route_after_review_gate(state: TaskState) -> str:
    """review_gate 之后：人工放行 → summarizer，其余 → replan。"""
    if state.get("status") == "FAILED" or state.get("error"):
        return "summarizer"
    intervention = state.get("review_intervention") or {}
    if str(intervention.get("status") or "").upper() == "APPROVED":
        return "summarizer"
    return "replan"


def make_review_gate_node(*, enabled: bool = True) -> Callable[[TaskState], Any]:
    """构造节点。``enabled=False`` 时纯放行（等价于 v1.1 之前的行为）。"""

    async def review_gate_node(state: TaskState) -> dict[str, Any]:
        review = state.get("review") or {}
        verdict = str(review.get("verdict") or "").upper()

        if not enabled or verdict != "REJECT":
            # REVISE / 无 verdict（老 checkpoint）：直接放行到 replan，保持原行为
            return {}

        payload = {
            "type": INTERVENTION_TYPE,
            "reason": (
                f"评审判定 REJECT（总分 {review.get('total')}/5，可信度 "
                f"{float(review.get('confidence') or 0):.0%}），请人工批注后重新规划"
            ),
            "preview": _preview(state),
        }
        logger.info(
            "review_intervention_waiting",
            extra={
                "task_id": state.get("task_id"),
                "total": review.get("total"),
                "confidence": review.get("confidence"),
                "replan_count": state.get("replan_count"),
            },
        )

        decision_value = interrupt(payload)
        status, comment, decided_by = _parse_intervention(decision_value)
        # 语义对齐：REJECTED = 提交批注并重规划；APPROVED = 人工放行
        intervention_status = "APPROVED" if status == "APPROVED" else "ANNOTATED"
        intervention = {
            "status": intervention_status,
            "decision": status,
            "comment": comment,
            "decided_by": decided_by,
            "verdict": verdict,
        }
        logger.info(
            "review_intervention_resolved",
            extra={
                "task_id": state.get("task_id"),
                "status": intervention_status,
                "has_comment": bool(comment),
                "decided_by": decided_by,
            },
        )
        return _route(intervention, state)

    review_gate_node.__name__ = "review_gate_node"
    return review_gate_node


__all__ = [
    "AGENT_NAME",
    "INTERVENTION_TYPE",
    "make_review_gate_node",
    "route_after_review_gate",
]
