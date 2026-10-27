"""human_gate 节点（PRD FR-011）：唯一 interrupt 挂起点。

行为：
1. 识别 ``risk_level=high`` 或 ``tool_hint=notify`` 的子任务；
2. 首次进入：写 approvals 记录（PENDING，24h 过期）→ ``interrupt({approval_id, reason, preview})``；
3. 恢复时（``Command(resume=...)``）：拿到审批决策，落库为 APPROVED / REJECTED；
4. 状态回写：批准 → 继续 executor；驳回 → 边路由到 replan，驳回意见作为硬约束。

注意：LangGraph 在恢复时会**重新执行整个节点**，因此必须幂等——
已有 ``approval_id`` 时不再重复建单，避免审批记录膨胀。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from langgraph.types import interrupt

from app.agents.common import high_risk_subtasks, needs_approval, plan_of
from app.core.logging import get_logger
from app.schemas.enums import ApprovalStatus, ApprovalType
from app.schemas.graph_state import ApprovalState, TaskState

logger = get_logger(__name__)

AGENT_NAME = "human_gate"

# 依赖注入签名：入参是待建单据信息，返回 {"approval_id", "status", "expires_at"}
ApprovalCreator = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
# 更新签名：(approval_id, decision, comment, decided_by) -> None
ApprovalUpdater = Callable[[int, str, str, int | None], Awaitable[None]]


async def _default_creator(payload: dict[str, Any]) -> dict[str, Any]:
    from app.services.approval_service import create_pending_approval

    return await create_pending_approval(**payload)


async def _default_updater(approval_id: int, decision: str, comment: str, decided_by: int | None) -> None:
    from app.services.approval_service import apply_decision

    await apply_decision(approval_id, decision=decision, comment=comment, decided_by=decided_by)


def _parse_decision(value: Any) -> tuple[str, str, int | None]:
    """兼容多种恢复值形态：dict / 'approve' / bool。"""
    if isinstance(value, dict):
        raw = str(value.get("decision") or value.get("status") or "").lower()
        comment = str(value.get("comment") or value.get("decide_comment") or "")
        decided_by = value.get("decided_by")
        approved = raw in {"approve", "approved", "true", "yes", "1"}
        return ("APPROVED" if approved else "REJECTED"), comment, decided_by
    if isinstance(value, bool):
        return ("APPROVED" if value else "REJECTED"), "", None
    text = str(value or "").strip().lower()
    return ("APPROVED" if text in {"approve", "approved", "true", "yes", "1"} else "REJECTED"), "", None


def _preview(state: TaskState, high_risk: list[Any]) -> dict[str, Any]:
    return {
        "goal": str(state.get("goal") or "")[:200],
        "time_range": state.get("time_range"),
        "subtasks": [
            {
                "seq": item.seq,
                "name": item.name,
                # `tool` 是审批单既有契约（测试依赖），`tool_hint` 与其同值，
                # 供前端按子任务字段名直接消费，避免两套命名对不上。
                "tool": item.tool_name,
                "tool_hint": item.tool_name,
                # 枚举需取 .value：str(SubtaskRiskLevel.HIGH) 会得到 "SubtaskRiskLevel.HIGH"，
                # 前端按 'high' 判断会失效（审批抽屉的「高危」标签不显示）。
                "risk_level": (
                    item.risk_level.value if hasattr(item.risk_level, "value") else str(item.risk_level)
                ),
                "description": item.description[:200],
            }
            for item in high_risk
        ],
    }


def make_human_gate_node(
    *,
    creator: ApprovalCreator | None = None,
    updater: ApprovalUpdater | None = None,
) -> Callable[[TaskState], Any]:
    create = creator or _default_creator
    update = updater or _default_updater

    async def human_gate_node(state: TaskState) -> dict[str, Any]:
        plan = plan_of(state)
        if not needs_approval(plan):
            # 理论上条件边不会把无风险任务送进来；兜底直接放行
            return {"approvals": ApprovalState(status=ApprovalStatus.APPROVED.value)}

        high_risk = high_risk_subtasks(plan)
        current: ApprovalState = dict(state.get("approvals") or {})  # type: ignore[assignment]

        # 已裁决：直接按结论路由（恢复后二次进入的场景）
        if current.get("status") in {ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value}:
            return _route_by_status(current, state)

        approval_id = current.get("approval_id")
        if approval_id is None:
            if len(high_risk) == 1 and high_risk[0].tool_name == "notify":
                approval_type = ApprovalType.HIGH_RISK_TOOL.value
            else:
                approval_type = ApprovalType.HIGH_RISK_TOOL.value
            reason = "、".join(f"#{item.seq} {item.name}" for item in high_risk)[:255]
            try:
                record = await create(
                    {
                        "task_id": state.get("task_id"),
                        "subtask_id": None,
                        "type": approval_type,
                        "reason": f"高危动作需审批：{reason}",
                        "payload": _preview(state, high_risk),
                    }
                )
                approval_id = int(record["approval_id"])
            except Exception as exc:  # noqa: BLE001 - 建单失败不能静默放行
                logger.exception("approval_create_failed")
                return {
                    "error": {"code": "APPROVAL_CREATE_FAILED", "message": str(exc)[:300], "node": AGENT_NAME},
                    "status": "FAILED",
                }
            logger.info("approval_created", extra={"approval_id": approval_id, "task_id": state.get("task_id")})

        # ---- 唯一挂起点：恢复值为审批决策 ----
        decision_value = interrupt(
            {
                "approval_id": approval_id,
                "type": ApprovalType.HIGH_RISK_TOOL.value,
                "reason": f"高危动作需审批：{'、'.join(f'#{i.seq} {i.name}' for i in high_risk)}"[:255],
                "preview": _preview(state, high_risk),
            }
        )

        status, comment, decided_by = _parse_decision(decision_value)
        try:
            await update(int(approval_id), status, comment, decided_by)
        except Exception:  # noqa: BLE001 - 落库失败不影响本次内存决策，但需留痕
            logger.exception("approval_update_failed", extra={"approval_id": approval_id})

        logger.info(
            "approval_resolved",
            extra={"approval_id": approval_id, "decision": status, "has_comment": bool(comment)},
        )
        approval_state = ApprovalState(
            approval_id=int(approval_id),
            status=status,  # type: ignore[typeddict-item]
            comment=comment,
            decided_by=decided_by,
        )
        return _route_by_status(approval_state, state)

    human_gate_node.__name__ = "human_gate_node"
    return human_gate_node


def _route_by_status(approval_state: ApprovalState, state: TaskState) -> dict[str, Any]:
    if approval_state.get("status") == ApprovalStatus.REJECTED.value:
        comment = str(approval_state.get("comment") or "审批驳回，请调整方案")
        return {
            "approvals": approval_state,
            "replan_context": {
                "cause": "human_rejected",
                "failed_seqs": [],
                "human_comment": comment,
                "review_comment": "",
            },
            "messages": [{"role": "assistant", "content": f"审批驳回：{comment}"}],
            "status": "RUNNING",
        }
    return {
        "approvals": approval_state,
        "messages": [{"role": "assistant", "content": "审批已通过，继续执行"}],
        "status": "RUNNING",
    }


__all__ = ["AGENT_NAME", "make_human_gate_node", "ApprovalCreator", "ApprovalUpdater"]
