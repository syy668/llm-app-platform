"""工具调用失败重试装饰器。

- 可配置最大尝试次数与重试间隔：系统配置页覆盖值优先（热更新），
  未覆盖时回退 .env（TOOL_MAX_ATTEMPTS 默认 3 / TOOL_RETRY_DELAY_SECONDS 默认 0.5）；
- 每次失败尝试写入 trace_tool_calls（status=failed），完整记录重试过程；
- 重试耗尽后重新抛出最后一个异常；成功链路行为不变。
"""
import functools
import logging
import time

from ...core import runtime_config
from ...core.database import SessionLocal
from ...core.tool_context import tool_trace_context_var
from ...models.trace_tool_call import TraceToolCall

logger = logging.getLogger(__name__)


def _max_attempts() -> int:
    """最大尝试次数（含首次）：配置页覆盖值 > .env: TOOL_MAX_ATTEMPTS（默认 3）。"""
    return int(runtime_config.get_value("tool_max_attempts"))


def _retry_delay() -> float:
    """重试间隔（秒）：配置页覆盖值 > .env: TOOL_RETRY_DELAY_SECONDS（默认 0.5）。"""
    return float(runtime_config.get_value("tool_retry_delay_seconds"))


def _record_failed_attempt(tool_name, step_name, tool_input, exc, latency_ms) -> None:
    """把失败尝试写入 trace_tool_calls（写入失败仅记日志，不阻断重试）。"""
    ctx = tool_trace_context_var.get()
    if not ctx or not ctx.get("trace_id"):
        return
    db = SessionLocal()
    try:
        db.add(TraceToolCall(
            trace_id=ctx["trace_id"],
            step_name=step_name,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=None,
            latency_ms=latency_ms,
            status="failed",
            error_msg=str(exc)[:1000],
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("记录重试失败明细失败", exc_info=True)
    finally:
        db.close()


def with_retry(fn):
    """工具函数重试装饰器：失败时按配置重试，重试耗尽才抛出。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        max_attempts = _max_attempts()
        ctx = tool_trace_context_var.get() or {}
        tool_name = ctx.get("tool_name") or getattr(fn, "__name__", "unknown")
        step_name = ctx.get("step_name") or tool_name
        tool_input = {"question": str(args[0]) if args else None}
        last_exc = None

        for attempt in range(1, max_attempts + 1):
            start = time.time()
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                latency_ms = int((time.time() - start) * 1000)
                if attempt < max_attempts:
                    logger.warning("工具调用失败，第 %s/%s 次重试: %s -> %s", attempt, max_attempts, tool_name, exc)
                    _record_failed_attempt(tool_name, step_name, tool_input, exc, latency_ms)
                    time.sleep(_retry_delay())
                else:
                    logger.error("工具重试耗尽(%s 次): %s -> %s", max_attempts, tool_name, exc)

        raise last_exc

    return wrapper
