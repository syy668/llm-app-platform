"""W3 接口契约：SSE 事件、审批决策、断点续跑、快照与轨迹回放。

与 W2 的 ``agent_io.py`` 分工一致：
- 请求体强校验（extra="forbid"），避免前端拼错字段被静默忽略；
- 响应体以 dict 为主（运行期数据形态多变），此处只固定**协议层**结构（SSE 帧、回放步骤）。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# SSE 事件协议
# ---------------------------------------------------------------------------
class SseEventType(str, Enum):
    """对外 4 类事件（PRD 7.2）；心跳走 SSE 注释帧，不计入事件类型。"""

    STATE_UPDATE = "state_update"
    TOOL_CALL = "tool_call"
    INTERRUPT = "interrupt"
    COMPLETE = "complete"


#: 收到即代表"本次连接可收尾"的终止帧（前端据此关闭 EventSource）
TERMINAL_EVENTS: frozenset[str] = frozenset({SseEventType.INTERRUPT.value, SseEventType.COMPLETE.value})


class SseEvent(BaseModel):
    """一条 SSE 帧。``id`` 即 Redis Stream ID，用作 Last-Event-ID 去重游标。"""

    id: str | None = Field(default=None, description="事件 ID（Redis Stream ID），断线重连时回传")
    event: str = Field(description="事件类型：state_update / tool_call / interrupt / complete")
    data: dict[str, Any] = Field(default_factory=dict, description="事件负载")
    trace_id: str | None = Field(default=None, description="链路追踪 ID")
    retry_ms: int | None = Field(default=None, description="仅首帧建议的重连间隔")

    def to_frame(self) -> str:
        """渲染为 SSE 文本帧（``id:`` / ``event:`` / ``data:`` + 空行）。"""
        import json

        lines: list[str] = []
        if self.retry_ms is not None:
            lines.append(f"retry: {int(self.retry_ms)}")
        if self.id:
            lines.append(f"id: {self.id}")
        lines.append(f"event: {self.event}")
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        # SSE 规范：data 中不允许裸换行，逐行拆分
        for chunk in payload.split("\n"):
            lines.append(f"data: {chunk}")
        return "\n".join(lines) + "\n\n"


class SseHeartbeat(BaseModel):
    """心跳注释帧（``: ping``），不触发 EventSource 的 message 事件。"""

    text: str = "ping"

    def to_frame(self) -> str:
        return f": {self.text}\n\n"


# ---------------------------------------------------------------------------
# 审批 / 断点续跑
# ---------------------------------------------------------------------------
DecisionLiteral = Literal["APPROVED", "REJECTED", "APPROVE", "REJECT", "approve", "reject"]


class ResumeRequest(BaseModel):
    """POST /tasks/{id}/resume 请求体（审批通过或驳回，二选一）。"""

    model_config = ConfigDict(extra="forbid")

    decision: DecisionLiteral = Field(description="APPROVED / REJECTED（大小写不敏感）")
    comment: str = Field(default="", max_length=500, description="审批意见；驳回时作为重规划的硬约束")
    approval_id: int | None = Field(default=None, ge=1, description="可选：显式指定审批单，缺省取当前挂起单")
    sync: bool | None = Field(default=None, description="True=阻塞到终态/下次挂起，None=走配置默认值")

    @field_validator("comment")
    @classmethod
    def _strip_comment(cls, value: str) -> str:
        return (value or "").strip()

    @property
    def normalized_decision(self) -> str:
        return "APPROVED" if self.decision.upper().startswith("APPROVE") else "REJECTED"


class CancelRequest(BaseModel):
    """POST /tasks/{id}/cancel 请求体。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=255, description="终止原因，写入 task.error.message")


class InterruptContext(BaseModel):
    """GET /tasks/{id}/interrupt 的 data 结构（非暂停任务 paused=false）。"""

    model_config = ConfigDict(extra="forbid")

    paused: bool = Field(description="是否处于挂起等待审批")
    task_id: int
    trace_id: str | None = None
    status: str | None = None
    approval_id: int | None = None
    type: str | None = Field(default=None, description="审批类型 HIGH_RISK_TOOL / LARGE_SCAN")
    reason: str | None = Field(default=None, description="阻断原因（收件箱展示）")
    preview: dict[str, Any] = Field(default_factory=dict, description="高危子任务预览（脱敏）")
    interrupted_at: str | None = Field(default=None, description="挂起时间（ISO8601）")
    next_node: list[str] = Field(default_factory=list, description="恢复后将要执行的节点")
    resumable: bool = Field(default=True, description="interrupt 是否可 resume")
    resume_endpoint: str | None = Field(default=None, description="恢复接口路径，便于前端直接调用")


# ---------------------------------------------------------------------------
# 审批收件箱
# ---------------------------------------------------------------------------
class ApprovalInboxItem(BaseModel):
    """待审批条目：任务信息 + 审批单 + 阻断原因，一次查询给前端够用。"""

    task_id: int
    task_no: str | None = None
    goal: str
    status: str
    creator_id: int | None = None
    approval_id: int | None = None
    approval_type: str | None = None
    reason: str | None = Field(default=None, description="阻断原因")
    risky_subtasks: list[dict[str, Any]] = Field(default_factory=list, description="高危子任务预览")
    created_at: str | None = None
    expires_at: str | None = None


class ApprovalInboxData(BaseModel):
    items: list[ApprovalInboxItem] = Field(default_factory=list)
    page: int = 1
    size: int = 20
    total: int = 0


# ---------------------------------------------------------------------------
# 轨迹回放
# ---------------------------------------------------------------------------
class TraceStep(BaseModel):
    """task_executions 的一行（回放按 id 顺序，等价于 started_at 顺序）。"""

    id: int
    trace_id: str
    task_id: int
    node: str
    event: str
    tool_name: str | None = None
    tool_error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    payload: dict[str, Any] | None = None
    started_at: str | None = None


class CheckpointStep(BaseModel):
    """一次 checkpoint 快照的摘要（前端 DAG 回放的时间轴节点）。"""

    step: int | None = None
    source: str | None = None
    status: str | None = None
    next_nodes: list[str] = Field(default_factory=list)
    plan_size: int = 0
    done_count: int = 0
    created_at: str | None = None


class TraceReplayData(BaseModel):
    task_id: int
    trace_id: str | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    checkpoints: list[CheckpointStep] = Field(default_factory=list)
    interrupt: dict[str, Any] | None = None
    total_steps: int = 0


class SnapshotData(BaseModel):
    task_id: int
    trace_id: str | None = None
    status: str | None = None
    next_nodes: list[str] = Field(default_factory=list)
    created_at: str | None = None
    step: int | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    messages_count: int = 0
    interrupt: dict[str, Any] | None = None


class ResumeResult(BaseModel):
    task_id: int
    resumed: bool
    status: str
    approval_id: int | None = None
    decision: str | None = None
    next_nodes: list[str] = Field(default_factory=list)


class CancelResult(BaseModel):
    task_id: int
    status: str
    canceled: bool
    reason: str | None = None


def isoformat(value: Any) -> str | None:
    """datetime / str / None 统一转 ISO8601 字符串（checkpointer 两种后端返回类型不一致）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


__all__ = [
    "SseEventType",
    "TERMINAL_EVENTS",
    "SseEvent",
    "SseHeartbeat",
    "ResumeRequest",
    "CancelRequest",
    "InterruptContext",
    "ApprovalInboxItem",
    "ApprovalInboxData",
    "TraceStep",
    "CheckpointStep",
    "TraceReplayData",
    "SnapshotData",
    "ResumeResult",
    "CancelResult",
    "isoformat",
]
