"""流式执行上下文：把"当前正在跑哪个任务"绑定到 ContextVar，供工具层/LLM 层回写事件。

为什么用 ContextVar 而不是显式传参：
- W2 的 MCP 客户端（``MCPClient._emit``）与 LLM 客户端（``LLMClient.complete``）签名已冻结，
  W3 不能改它们的入参；
- 但 MCP 的 ``on_event`` 钩子与 LLM 的调用都发生在**同一条 asyncio 任务链**里，
  ContextVar 天然随 await 传播，因此钩子内能拿到当前 task_id / trace_id / node；
- 事务边界也干净：一次 graph 执行 = 一次 ``bind`` + 一次 ``reset``，任务之间零串扰。
"""

from __future__ import annotations

import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from app.agents.llm import LLMResponse
from app.core.context import task_id_var
from app.core.logging import get_logger
from app.services.event_bus import EventBus
from app.services.trace_service import TraceService, brief
from app.utils.time import utcnow

logger = get_logger(__name__)


@dataclass(slots=True)
class StreamContext:
    """一次图执行期间的事件上下文。"""

    task_id: int
    trace_id: str
    bus: EventBus
    trace: TraceService
    node: str = "planner"
    seq: int = 0
    started_at: float = field(default_factory=time.perf_counter)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started_at) * 1000)


current_stream: ContextVar[StreamContext | None] = ContextVar("agentflow_stream_ctx", default=None)

#: 当前正在执行的子任务**序号**（executor 在每次 run_subtask 前后 bind/unbind）。
#:
#: 为什么记 seq 而不是 subtask_id：`subtask_id` 是 `subtasks.id` 数据库主键，而计划直到
#: ``TaskService._finalize`` 才落库（persist_plan 返回 seq→id 映射），执行期根本没有 id。
#: 因此这里先记 seq 到 span payload，收尾落库后由 `_finalize` 用映射把 `subtask_id` 回填，
#: 这样 Trace 页就能按 subtask_id 精确归属（缺口 G7），不必再按 tool_name 猜。
current_subtask_seq: ContextVar[int | None] = ContextVar("agentflow_subtask_seq", default=None)


def bind_subtask(seq: int | None) -> Token:
    """绑定当前子任务序号，返回 Token 供 unbind 还原。"""
    return current_subtask_seq.set(seq)


def unbind_subtask(token: Token) -> None:
    try:
        current_subtask_seq.reset(token)
    except (ValueError, RuntimeError):  # pragma: no cover - 跨上下文 reset 的兜底
        current_subtask_seq.set(None)


def _with_subtask(payload: dict[str, Any]) -> dict[str, Any]:
    """把当前子任务序号并入 payload（无子任务上下文时不加键，保持 payload 干净）。"""
    seq = current_subtask_seq.get()
    if seq is not None:
        payload["subtask_seq"] = seq
    return payload


def bind(ctx: StreamContext) -> tuple[Token, Token]:
    """绑定流上下文，同时把 task_id 写入日志 ContextVar（任务 7）。

    返回 ``(stream_token, task_token)``，调用方原样透传给 ``unbind`` 即可——
    两个 token 的生命周期完全一致：一次图执行 = 一次 bind + 一次 unbind。
    """
    stream_token = current_stream.set(ctx)
    task_token = task_id_var.set(ctx.task_id)
    return stream_token, task_token


def unbind(tokens: tuple[Token, Token] | None) -> None:
    """还原 bind 绑定的全部 ContextVar；token 为 None（复用外层上下文）时跳过。

    双 try 兜底：跨上下文 reset 会抛 ValueError，此时退化为显式置空，
    保证异步协程复用后不会残留上一个任务的 task_id（串台）。
    """
    if tokens is None:
        return
    stream_token, task_token = tokens
    try:
        current_stream.reset(stream_token)
    except (ValueError, RuntimeError):  # pragma: no cover - 跨上下文 reset 的兜底
        current_stream.set(None)
    try:
        task_id_var.reset(task_token)
    except (ValueError, RuntimeError):  # pragma: no cover - 跨上下文 reset 的兜底
        task_id_var.set(None)


def get_context() -> StreamContext | None:
    return current_stream.get()


# ---------------------------------------------------------------------------
# SSE 事件发布
# ---------------------------------------------------------------------------
async def emit(event: str, data: dict[str, Any], *, node: str | None = None) -> str | None:
    """发布一条 SSE 事件；无上下文（离线单测 / 非流式执行）时静默跳过。"""
    ctx = current_stream.get()
    if ctx is None:
        return None
    payload: dict[str, Any] = {
        "task_id": ctx.task_id,
        "trace_id": ctx.trace_id,
        "seq": ctx.next_seq(),
        "node": node or ctx.node,
        "ts": utcnow().isoformat(),
        "elapsed_ms": ctx.elapsed_ms,
    }
    payload.update(data)
    try:
        return await ctx.bus.publish(ctx.task_id, event, payload, trace_id=ctx.trace_id)
    except Exception as exc:  # noqa: BLE001 - 推事件失败不能打断执行
        logger.warning("sse_publish_failed", extra={"task_id": ctx.task_id, "event": event, "error": str(exc)[:200]})
        return None


# ---------------------------------------------------------------------------
# MCP 工具事件钩子（由 MCPClientPool.create_default(on_event=...) 注入）
# ---------------------------------------------------------------------------
#: 工具 → 归属节点的确定性映射（比"当前节点"更可靠：图中节点与事件消费是并发推进的，
#: ``ctx.node`` 可能已经跑到下一个节点，而工具调用实际发生在上一节点里）
TOOL_NODE_HINT: dict[str, str] = {
    "sql-query": "executor",
    "chart-render": "executor",
    "kb-search": "executor",
    "notify": "notifier",
}


async def mcp_event_hook(event: str, payload: dict[str, Any]) -> None:
    """把 MCP 层的 ``tool.call`` / ``tool.result`` 转成 SSE 的 ``tool_call`` 事件 + 轨迹行。

    MCP 层已经做过参数脱敏（``mask_mapping``）与结果摘要（``result_digest``），
    这里只做「协议转换 + 落库」，不重复处理业务错误（错误由 Executor 决定重试策略）。
    """
    ctx = current_stream.get()
    if ctx is None:
        return

    tool = payload.get("tool")
    server = payload.get("server")
    node = TOOL_NODE_HINT.get(str(tool), ctx.node)
    if event == "tool.call":
        params = payload.get("params") or {}
        await emit(
            "tool_call",
            {
                "phase": "call",
                "tool": tool,
                "server": server,
                "params": params,
            },
            node=node,
        )
        await ctx.trace.record(
            task_id=ctx.task_id,
            trace_id=ctx.trace_id,
            node=node,
            event="tool.call",
            tool_name=tool,
            payload=_with_subtask({"server": server, "params": params}),
        )
        return

    if event == "tool.result":
        ok = bool(payload.get("ok"))
        error_code = payload.get("error_code")
        # 错误文案（MCP 层已脱敏）一并进 SSE 与轨迹，回放时不必再猜错误码含义
        error_message = payload.get("error_message")
        latency_ms = int(payload.get("latency_ms") or 0)
        await emit(
            "tool_call",
            {
                "phase": "result",
                "tool": tool,
                "server": server,
                "ok": ok,
                "latency_ms": latency_ms,
                "error_code": error_code,
                "error_message": error_message,
                "result_digest": payload.get("result_digest"),
            },
            node=node,
        )
        await ctx.trace.record(
            task_id=ctx.task_id,
            trace_id=ctx.trace_id,
            node=node,
            event="tool.result",
            tool_name=tool,
            tool_error=error_code,
            latency_ms=latency_ms,
            payload=_with_subtask(
                {
                    "server": server,
                    "ok": ok,
                    "error_code": error_code,
                    "error_message": error_message,
                    "result_digest": payload.get("result_digest"),
                }
            ),
        )


# ---------------------------------------------------------------------------
# LLM 装饰器：入参出参落库（不改变 LLMClient 契约）
# ---------------------------------------------------------------------------
class InstrumentedLLM:
    """透明代理：``LLMClient`` 的等价替代，额外把每次调用写入 task_executions。

    - 用 ``__getattr__`` 透传 ``mock_mode`` / ``primary`` 等属性，节点侧完全无感；
    - 异常也落库（``ok=false`` + 错误码），便于回放时定位"哪一步模型挂了"。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
    ) -> LLMResponse:
        ctx = current_stream.get()
        started = time.perf_counter()
        request_digest = {
            "messages": len(messages or []),
            "system": brief(self._system_text(messages)),
            "user": brief(self._last_user(messages)),
            "json_mode": json_mode,
        }
        try:
            response = await self._inner.complete(
                messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
            )
        except Exception as exc:
            if ctx is not None:
                await ctx.trace.record(
                    task_id=ctx.task_id,
                    trace_id=ctx.trace_id,
                    node=ctx.node,
                    event="llm.call",
                    payload=_with_subtask(
                        {
                            "ok": False,
                            "error": brief(exc),
                            "error_type": type(exc).__name__,
                            "request": request_digest,
                            "latency_ms": int((time.perf_counter() - started) * 1000),
                        }
                    ),
                )
            raise

        if ctx is not None:
            await ctx.trace.record(
                task_id=ctx.task_id,
                trace_id=ctx.trace_id,
                node=ctx.node,
                event="llm.call",
                payload=_with_subtask(
                    {
                        "ok": True,
                        "model": getattr(response, "model", None),
                        "provider": getattr(response, "provider", None),
                        "request": request_digest,
                        "response": brief(getattr(response, "content", "")),
                    }
                ),
                input_tokens=int(getattr(response, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(response, "completion_tokens", 0) or 0),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        return response

    @staticmethod
    def _system_text(messages: list[dict[str, Any]]) -> str:
        return next((str(m.get("content") or "") for m in messages or [] if m.get("role") == "system"), "")

    @staticmethod
    def _last_user(messages: list[dict[str, Any]]) -> str:
        return next(
            (str(m.get("content") or "") for m in reversed(messages or []) if m.get("role") == "user"),
            "",
        )


__all__ = [
    "StreamContext",
    "current_stream",
    "current_subtask_seq",
    "bind",
    "unbind",
    "bind_subtask",
    "unbind_subtask",
    "get_context",
    "emit",
    "mcp_event_hook",
    "InstrumentedLLM",
]
