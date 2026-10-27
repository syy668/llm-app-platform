"""工具执行 trace 上下文（供重试包装器写入 trace_tool_calls）。"""
from contextvars import ContextVar

# 由 TraceCallbackHandler 在节点开始时写入：{trace_id, step_name, tool_name}
tool_trace_context_var: ContextVar = ContextVar("tool_trace_context", default=None)
