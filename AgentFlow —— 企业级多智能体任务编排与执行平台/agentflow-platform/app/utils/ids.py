"""ID 生成（trace_id / span_id / 业务编号）。"""

from __future__ import annotations

import uuid
from datetime import datetime


def new_trace_id() -> str:
    """任务级追踪 ID：32 位 hex（对齐 PRD tasks.trace_id VARCHAR(32)）。"""
    return uuid.uuid4().hex


def new_span_id() -> str:
    """节点级 span ID：36 位 UUID 字符串（对齐 task_executions.span_id VARCHAR(36)）。"""
    return str(uuid.uuid4())


def new_uuid() -> str:
    return str(uuid.uuid4())


def new_task_no(seq: int, when: datetime | None = None) -> str:
    """业务编号：AF20261020-0001。"""
    day = (when or datetime.now()).strftime("%Y%m%d")
    return f"AF{day}-{seq:04d}"


def new_run_no(seq: int, when: datetime | None = None) -> str:
    """评测编号：AFRUN20261020-0001。"""
    day = (when or datetime.now()).strftime("%Y%m%d")
    return f"AFRUN{day}-{seq:04d}"
