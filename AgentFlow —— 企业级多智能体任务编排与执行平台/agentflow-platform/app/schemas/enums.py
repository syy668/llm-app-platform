"""领域枚举：ORM 与 Pydantic 共用，取值严格对齐 PRD 第六章。"""

from __future__ import annotations

import enum


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    APPROVER = "approver"
    VIEWER = "viewer"


class TaskStatus(str, enum.Enum):
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING = "RUNNING"
    SUMMARIZING = "SUMMARIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SubtaskToolHint(str, enum.Enum):
    SQL_QUERY = "sql_query"
    CHART_RENDER = "chart_render"
    KB_SEARCH = "kb_search"
    NOTIFY = "notify"
    NONE = "none"


class SubtaskRiskLevel(str, enum.Enum):
    NORMAL = "normal"
    HIGH = "high"


class SubtaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class ExecutionNode(str, enum.Enum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    REPLAN = "replan"
    SUMMARIZER = "summarizer"
    NOTIFIER = "notifier"
    HUMAN_GATE = "human_gate"
    # G8 任务级可选步骤节点：只有开关打开时才会进入，故历史枚举里没有；
    # 需与 MySQL ENUM 列同步（ALTER TABLE ... MODIFY node ENUM(...) 追加这两个值）。
    MEMORY_RECALL = "memory_recall"
    CONTEXT_COMPRESS = "context_compress"


class ExecutionEvent(str, enum.Enum):
    """task_executions.event 取值（PRD 6.4）；字段类型 VARCHAR(32)。"""

    ENTER = "enter"
    EXIT = "exit"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    REPLAN = "replan"
    HUMAN_EDIT = "human_edit"
    ERROR = "error"


class ApprovalType(str, enum.Enum):
    HIGH_RISK_TOOL = "HIGH_RISK_TOOL"
    LARGE_SCAN = "LARGE_SCAN"


class ApprovalStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class MemoryScope(str, enum.Enum):
    SHORT = "short"
    MID = "mid"
    LONG = "long"


class EvalSource(str, enum.Enum):
    REVIEWER_GATE = "reviewer_gate"
    REGRESSION = "regression"
    AB_TEST = "ab_test"


class EvalVariant(str, enum.Enum):
    COMPRESS_ON = "compress_on"
    COMPRESS_OFF = "compress_off"


class EvalRunStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
