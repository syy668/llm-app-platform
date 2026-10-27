"""W3 业务错误码与状态别名。

为什么单独成文：W1 ``app/core/exceptions.py`` 的 ``ErrorCode`` 枚举已冻结（红线性约束），
W3 新增错误码不改枚举，而是以「字符串码 + 显式 HTTP 状态」的方式复用 ``AppException``——
``AppException`` 本身支持 ``code: ErrorCode | str`` 与 ``status_code=`` 入参，无需修改 W1。
"""

from __future__ import annotations

from app.core.exceptions import AppException
from app.schemas.enums import TaskStatus

# ---------------- 错误码 ----------------
TASK_NOT_PAUSED = "TASK_NOT_PAUSED"          # 任务不在 AWAITING_APPROVAL，不能 resume
TASK_NOT_CANCELABLE = "TASK_NOT_CANCELABLE"  # 任务已终态，不能 cancel
TASK_NOT_RUNNING = "TASK_NOT_RUNNING"        # 任务没有可恢复的运行时
APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"    # 审批单不存在
APPROVAL_STALE = "APPROVAL_STALE"            # 审批单已被处理（过期/重复提交）
APPROVAL_EXPIRED = "APPROVAL_EXPIRED"        # 审批单已超时（PRD L245：24h 未处理即失效，禁止再批）
STREAM_DISABLED = "STREAM_DISABLED"          # SSE 开关关闭
STREAM_UNAVAILABLE = "STREAM_UNAVAILABLE"    # 事件总线不可用
TRACE_DISABLED = "TRACE_DISABLED"            # 轨迹回放关闭
INTERRUPT_MISSING = "INTERRUPT_MISSING"      # checkpoint 中查不到挂起上下文
TASK_LOCKED = "TASK_LOCKED"                  # 同一任务的写路径被其他请求持有（Redis 分布式锁）
INVALID_INPUT = "INVALID_INPUT"              # 输入包含违规内容（越狱特征词，任务 10）
INTENT_BLOCKED = "INTENT_BLOCKED"            # 输入疑似攻击意图（意图护栏兜底，任务 10）

# ---------------- 任务状态 ----------------
# W1 的 TaskStatus 已包含 AWAITING_APPROVAL / CANCELLED，本阶段不新增枚举值（红线）。
# 需求文档中的 "CANCELED" 为同一语义的另一种拼写，统一归一化为 CANCELLED 对外输出。
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
# 允许 resume 的源状态
RESUMABLE_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.AWAITING_APPROVAL})

_STATUS_ALIASES: dict[str, TaskStatus] = {
    "CANCELED": TaskStatus.CANCELLED,
    "CANCELLED": TaskStatus.CANCELLED,
    "AWAITING": TaskStatus.AWAITING_APPROVAL,
    "CREATED": TaskStatus.PLANNING,  # W1 枚举用 PLANNING 表达「已创建待执行」
}


def normalize_status(value: str | TaskStatus | None) -> TaskStatus | None:
    """把外部传入的状态串归一化为 W1 枚举（兼容 CANCELED / CREATED 等别名）。"""
    if value is None:
        return None
    if isinstance(value, TaskStatus):
        return value
    text = str(value).strip().upper()
    if text in _STATUS_ALIASES:
        return _STATUS_ALIASES[text]
    try:
        return TaskStatus(text)
    except ValueError:
        return None


# ---------------- 便捷异常构造 ----------------
def conflict(code: str, message: str, **detail: object) -> AppException:
    """409 冲突：状态机不允许的操作（resume 非暂停任务 / cancel 终态任务）。"""
    return AppException(code, message, status_code=409, detail=dict(detail) or None)


def not_found(message: str, **detail: object) -> AppException:
    return AppException("NOT_FOUND", message, status_code=404, detail=dict(detail) or None)


def bad_request(code: str, message: str, **detail: object) -> AppException:
    return AppException(code, message, status_code=400, detail=dict(detail) or None)


def unavailable(code: str, message: str, **detail: object) -> AppException:
    return AppException(code, message, status_code=503, detail=dict(detail) or None)


__all__ = [
    "TASK_NOT_PAUSED",
    "TASK_NOT_CANCELABLE",
    "TASK_NOT_RUNNING",
    "APPROVAL_NOT_FOUND",
    "APPROVAL_STALE",
    "APPROVAL_EXPIRED",
    "STREAM_DISABLED",
    "STREAM_UNAVAILABLE",
    "TRACE_DISABLED",
    "INTERRUPT_MISSING",
    "TASK_LOCKED",
    "INVALID_INPUT",
    "INTENT_BLOCKED",
    "TERMINAL_STATUSES",
    "RESUMABLE_STATUSES",
    "normalize_status",
    "conflict",
    "not_found",
    "bad_request",
    "unavailable",
]
