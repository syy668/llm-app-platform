"""结构化日志（JSON），每条日志自动携带 trace_id（PRD NFR-M2）。"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.core.context import get_span_id, get_task_id, get_trace_id

_CONFIGURED = False

_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """单行 JSON；异常信息序列化到 exc 字段。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None) or get_trace_id() or None,
            "span_id": getattr(record, "span_id", None) or get_span_id() or None,
            # 任务 7：执行图内的日志自动携带 task_id（record 显式 extra 优先）
            "task_id": getattr(record, "task_id", None) or get_task_id(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class PlainFormatter(logging.Formatter):
    """本地可读格式，保留 trace_id 与 task_id 便于排查。"""

    def format(self, record: logging.LogRecord) -> str:
        trace_id = get_trace_id() or "-"
        # 任务 7：无绑定上下文时显示 "-"，有绑定时形如 [task_id:12]
        task_id = getattr(record, "task_id", None) or get_task_id()
        base = (
            f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} [{trace_id[:8]}] "
            f"[task_id:{task_id if task_id is not None else '-'}] "
            f"{record.name}: {record.getMessage()}"
        )
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def setup_logging(level: str | None = None) -> None:
    """幂等初始化根 logger（Uvicorn 默认 handler 会被接管）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.LOG_JSON else PlainFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel((level or settings.LOG_LEVEL).upper())

    # uvicorn 自带 access log 与 TraceID 中间件的 http_request 重复，这里关闭前者
    logging.getLogger("uvicorn.access").propagate = False

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
