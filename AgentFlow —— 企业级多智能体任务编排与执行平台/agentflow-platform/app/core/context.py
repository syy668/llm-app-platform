"""请求级上下文变量（ContextVar）。

单独成模块，避免 logging <-> trace 之间的循环导入。
"""

from __future__ import annotations

from contextvars import ContextVar

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
span_id_var: ContextVar[str] = ContextVar("span_id", default="")
user_id_var: ContextVar[int | None] = ContextVar("user_id", default=None)
#: 当前正在执行的任务 id —— 由 stream_context.bind() 在进入执行图前绑定，
#: logging 格式化器读取它给每条日志自动带上 task_id（任务 7）。
task_id_var: ContextVar[int | None] = ContextVar("task_id", default=None)


def get_trace_id() -> str:
    return trace_id_var.get()


def get_span_id() -> str:
    return span_id_var.get()


def get_task_id() -> int | None:
    return task_id_var.get()
