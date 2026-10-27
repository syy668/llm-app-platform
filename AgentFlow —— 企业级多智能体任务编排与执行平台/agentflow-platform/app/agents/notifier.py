"""notifier 节点（PRD 5.2 / FR-008）：审批通过后调用 notify 工具（Mock，不出网）。

前置硬闸门：``approval_state.status == APPROVED``。
- 未批准（PENDING / REJECTED / 缺失）→ 直接返回 ``APPROVAL_MISSING``，任务 FAILED，
  **不发起 MCP 调用**（MCP Server 侧还有第二道闸门，双重保险）；
- 调用成功 → 结果瘦身为 message_id + status 回填，产物状态置 DONE。
"""

from __future__ import annotations

from typing import Any, Callable

from app.agents.common import ledger, notify_subtask, plan_of
from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.enums import ApprovalStatus
from app.schemas.graph_state import SubtaskResult, TaskState
from app.services.context_service import ContextService
from app.tools.mcp_client import MCPClientPool, MCPTransportError, ToolInvocationError

logger = get_logger(__name__)

AGENT_NAME = "notifier"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def make_notifier_node(pool: MCPClientPool, context: ContextService) -> Callable[[TaskState], Any]:
    async def notifier_node(state: TaskState) -> dict[str, Any]:
        plan = plan_of(state)
        subtask = notify_subtask(plan)
        if subtask is None:
            return {"status": "COMPLETED"}

        approval = state.get("approvals") or {}
        approval_id = _as_int(approval.get("approval_id"))
        approved = approval.get("status") == ApprovalStatus.APPROVED.value
        if not approved:
            logger.error("notify_blocked", extra={"approval_status": approval.get("status")})
            return {
                "error": {
                    "code": "APPROVAL_MISSING",
                    "message": "notify 前置条件不满足：审批未通过，已阻断发送",
                    "node": AGENT_NAME,
                },
                "subtask_results": {
                    str(subtask.seq): SubtaskResult(
                        subtask_seq=subtask.seq,
                        status="FAILED",
                        tool="notify",
                        error={"code": "APPROVAL_MISSING", "message": "无 APPROVED 审批记录"},
                    )
                },
                "status": "FAILED",
                "messages": [{"role": "assistant", "content": "notify 被拦截：缺少审批"}],
            }

        summary = str(state.get("summary") or "")
        arguments = {
            "channel": "feishu",
            "title": f"{subtask.name}"[:50],
            "content": (summary or subtask.description or subtask.name)[: min(2000, w2.NOTIFY_MAX_CONTENT_LEN)],
            "receivers": ["ops_group"],
            "task_id": _as_int(state.get("task_id")),
            "approval_id": approval_id,
        }

        try:
            payload = await pool.call("notify", arguments)
        except (ToolInvocationError, MCPTransportError) as exc:
            code = getattr(exc, "code", "NOTIFY_FAILED")
            logger.error("notify_failed", extra={"code": code})
            return {
                "error": {"code": code, "message": str(exc)[:300], "node": AGENT_NAME},
                "subtask_results": {
                    str(subtask.seq): SubtaskResult(
                        subtask_seq=subtask.seq,
                        status="FAILED",
                        tool="notify",
                        error={"code": code, "message": str(exc)[:300]},
                    )
                },
                "status": "FAILED",
            }

        slim = context.slim_tool_result("notify", payload)
        logger.info(
            "notify_sent",
            extra={"message_id": payload.get("message_id"), "mock": payload.get("mock", True), "egress": False},
        )
        return {
            "notify_result": payload,
            "subtask_results": {
                str(subtask.seq): SubtaskResult(
                    subtask_seq=subtask.seq,
                    status="DONE",
                    data_digest=slim.digest,
                    tool="notify",
                    attempts=1,
                )
            },
            "token_ledger": ledger(AGENT_NAME, 0, 0),
            "messages": [
                {
                    "role": "assistant",
                    "content": f"通知已发送（Mock）：message_id={payload.get('message_id')}",
                }
            ],
            "status": "COMPLETED",
        }

    notifier_node.__name__ = "notifier_node"
    return notifier_node


__all__ = ["AGENT_NAME", "make_notifier_node"]
