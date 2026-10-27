"""边界加固：SSE 守护 / 一致性校验 / 取消资源回收 —— W5-Part2 任务 4。

**只新增，不改状态流转**。本模块对外提供四样东西：

1. ``sse_guard(inner)`` —— 用一个**泵任务 + 队列**包住既有 SSE 事件流，补齐：
   心跳帧、空闲超时、连接总时长上限、断线重连提示（``retry:``）、关闭时优雅回收；
2. ``@async_guard`` —— 通用异常捕获装饰器，把异常转成结构化日志 + 可选兜底返回值，
   用来包住「非主流程」的旁路调用（指标上报、审计落库、通知副作用）；
3. ``release_task_resources`` —— 任务取消后清理任务级 Redis 临时键
   （取消标记 / 事件流 / 短期记忆），避免脏数据残留；
4. ``check_task_consistency`` —— 多副本读同一任务快照时的状态一致性校验
   （断点错乱防护）：比较 thread_id、checkpoint、next 节点、interrupt 数、plan 签名。

## 为什么 SSE 用「泵 + 队列」而不是直接 ``wait_for(anext(inner))``

直接对 ``__anext__`` 加超时会取消协程，异步生成器在 ``yield`` 处收到
``CancelledError`` 后会**直接终止**，下一次再也取不到值 —— 表现为「超时一次，流就死了」。
泵任务把「产」和「消费超时」解耦：超时只影响队列读取，事件流本身继续正常推进。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable

from app.core.logging import get_logger
from app.core.w6_config import w6

logger = get_logger(__name__)

# SSE 协议字段（与 W3 前端约定的语义保持一致）
SSE_RETRY_FRAME = "retry: {ms}\n\n"
SSE_PING_FRAME = ": ping\n\n"
SSE_CLOSE_FRAME = "event: close\ndata: {payload}\n\n"


# ---------------------------------------------------------------------------
# 1) 通用异常捕获装饰器（旁路调用的安全网）
# ---------------------------------------------------------------------------
def async_guard(
    *,
    event: str,
    fallback: Any = None,
    raise_on: tuple[type[BaseException], ...] = (),
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """异步异常捕获：记录结构化日志后返回 ``fallback``。

    ``raise_on`` 里列出的异常会原样抛出（例如 ``asyncio.CancelledError``），
    避免把「取消」误当成业务失败吞掉。
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except raise_on:
                raise
            except Exception as exc:  # noqa: BLE001 - 旁路调用不允许影响主流程
                logger.warning(
                    event,
                    extra={
                        "func": getattr(func, "__qualname__", str(func))[:120],
                        "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    },
                )
                return fallback

        return wrapper

    return decorator


def guard(
    *,
    event: str,
    fallback: Any = None,
    raise_on: tuple[type[BaseException], ...] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """同步版异常捕获装饰器（与 ``async_guard`` 语义一致）。"""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except raise_on:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    event,
                    extra={
                        "func": getattr(func, "__qualname__", str(func))[:120],
                        "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    },
                )
                return fallback

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# 2) SSE 守护：心跳 / 空闲超时 / 连接上限 / 重连提示 / 优雅回收
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SseGuardStats:
    """守护期间的观测数据（写日志 / 自检用）。"""

    frames: int = 0
    pings: int = 0
    started_at: float = field(default_factory=time.monotonic)
    closed_reason: str = ""

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at


async def sse_guard(
    inner: AsyncIterator[str],
    *,
    heartbeat_seconds: float | None = None,
    idle_timeout_seconds: float | None = None,
    max_seconds: float | None = None,
    retry_ms: int | None = None,
    stats: SseGuardStats | None = None,
    on_close: Callable[[SseGuardStats], Any] | None = None,
) -> AsyncIterator[str]:
    """包装既有 SSE 事件流，补齐心跳 / 超时 / 回收；**不改变事件内容与顺序**。

    - 首帧前插入 ``retry: N``（浏览器 EventSource 断线重连间隔）；
    - 空闲超过 ``idle_timeout_seconds`` → 发 ``: ping``，不判定断线；
    - 连接总时长超过 ``max_seconds`` → 发 ``event: close`` 主动收尾（客户端带
      ``Last-Event-ID`` 重连即可续上，事件总线里的事件仍在）；
    - 内层抛异常 → 记录日志后以 ``close`` 帧收尾，不把半截流留给客户端。
    """
    heartbeat = heartbeat_seconds if heartbeat_seconds is not None else w6.SSE_HEARTBEAT_SECONDS
    idle = idle_timeout_seconds if idle_timeout_seconds is not None else w6.SSE_IDLE_TIMEOUT_SECONDS
    limit = max_seconds if max_seconds is not None else w6.SSE_CONNECTION_MAX_SECONDS
    retry = retry_ms if retry_ms is not None else w6.SSE_RETRY_INTERVAL_MS
    state = stats or SseGuardStats()

    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=512)
    cancelled = asyncio.Event()

    async def pump() -> None:
        try:
            async for item in inner:
                if cancelled.is_set():
                    break
                await queue.put(("item", item))
            await queue.put(("end", None))
        except asyncio.CancelledError:  # pragma: no cover - 由外层 cancel 触发
            raise
        except Exception as exc:  # noqa: BLE001 - 内层异常转成事件，避免半截流
            await queue.put(("error", exc))

    task = asyncio.create_task(pump(), name="sse-guard-pump")
    try:
        yield SSE_RETRY_FRAME.format(ms=retry)
        while True:
            if state.elapsed >= limit:
                state.closed_reason = "max_seconds"
                break
            # 等待时长 = min(心跳周期, 空闲上限, 剩余连接寿命)
            remaining = min(heartbeat, idle, max(0.05, limit - state.elapsed))
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                state.pings += 1
                yield SSE_PING_FRAME
                continue

            if kind == "end":
                state.closed_reason = "inner_end"
                break
            if kind == "error":
                state.closed_reason = "inner_error"
                logger.warning(
                    "sse_stream_error",
                    extra={"error": f"{type(payload).__name__}: {str(payload)[:200]}"},
                )
                break

            state.frames += 1
            yield str(payload)
    finally:
        cancelled.set()
        # 先把泵任务取消再 aclose 内层：顺序反了会在内层 yield 处抛 RuntimeError
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        aclose = getattr(inner, "aclose", None)
        if callable(aclose):
            with contextlib.suppress(Exception):
                await aclose()
        if on_close is not None:
            with contextlib.suppress(Exception):
                on_close(state)

    yield SSE_CLOSE_FRAME.format(
        payload=f'{{"reason": "{state.closed_reason or "done"}", "frames": {state.frames}, '
        f'"pings": {state.pings}, "elapsed_ms": {int(state.elapsed * 1000)}}}'
    )
    logger.info(
        "sse_stream_closed",
        extra={
            "reason": state.closed_reason or "done",
            "frames": state.frames,
            "pings": state.pings,
            "elapsed_ms": int(state.elapsed * 1000),
        },
    )


# ---------------------------------------------------------------------------
# 3) 取消后的资源回收
# ---------------------------------------------------------------------------
def task_redis_keys(task_id: int | str) -> list[str]:
    """任务级临时键清单（与 W3 各服务的 key 约定保持一致，只读引用常量）。"""
    from app.core.config import settings
    from app.core.w3_config import w3

    tid = str(task_id)
    return [
        settings.key("cancel", tid),  # 取消标记（CancelSignal）
        settings.key(w3.STREAM_KEY_PREFIX, tid),  # 事件流（EventBus）
        settings.key("memory", "short", tid),  # 短期记忆（W5-Part1，若开启）
    ]


async def release_task_resources(
    task_id: int | str,
    *,
    redis: Any = None,
    extra_keys: Iterable[str] | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """删除任务级 Redis 临时键；**只清理旁路数据，不碰任务主状态与断点**。

    为什么保留 ``ckpt:{task_id}``：断点是任务的可恢复资产，取消后仍可能需要
    审计/回放，且由 checkpoint TTL 自行过期，这里主动删会造成「取消即失忆」。
    """
    flag = w6.TASK_CANCEL_CLEANUP_ENABLED if enabled is None else bool(enabled)
    report: dict[str, Any] = {"enabled": flag, "deleted": [], "errors": []}
    if not flag:
        return report

    keys = list(task_redis_keys(task_id))
    if extra_keys:
        keys.extend(str(k) for k in extra_keys)

    client = redis
    if client is None:
        try:
            from app.core.redis_client import get_shared_redis

            client = get_shared_redis()
        except Exception as exc:  # noqa: BLE001 - 无 Redis 时静默跳过
            report["errors"].append(f"redis_unavailable: {type(exc).__name__}")
            return report

    for key in keys:
        try:
            removed = await client.delete(key)
            if removed:
                report["deleted"].append(key)
        except Exception as exc:  # noqa: BLE001 - 单个 key 失败不影响其余
            report["errors"].append(f"{key}: {type(exc).__name__}")

    logger.info(
        "task_resources_released",
        extra={"task_id": task_id, "deleted": report["deleted"], "errors": report["errors"]},
    )
    return report


# ---------------------------------------------------------------------------
# 4) 多副本任务状态一致性校验
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ConsistencyReport:
    """两个快照的一致性结论。``ok=False`` 时 ``mismatches`` 给出具体字段。"""

    ok: bool
    mismatches: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    left_step: int | None = None
    right_step: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mismatches": list(self.mismatches),
            "checked": list(self.checked),
            "left_step": self.left_step,
            "right_step": self.right_step,
        }


def _plan_signature(values: dict[str, Any]) -> tuple[tuple[int, str], ...]:
    plan = values.get("plan") or []
    signature: list[tuple[int, str]] = []
    for item in plan:
        if isinstance(item, dict):
            signature.append((int(item.get("seq") or 0), str(item.get("name") or item.get("tool") or "")))
        else:
            signature.append((int(getattr(item, "seq", 0) or 0), str(getattr(item, "name", ""))))
    return tuple(signature)


def check_task_consistency(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    *,
    max_step_lag: int | None = None,
    allow_step_lag: bool = True,
) -> ConsistencyReport:
    """比较同一任务在两个副本上读到的快照，防护「断点错乱」。

    判据（任一项不满足即 ``ok=False``）：

    1. ``thread_id`` 相同（不同 = 读到了别的任务的断点，最严重）；
    2. ``plan`` 签名（seq + 节点名）相同（不同 = replica 落后或状态被改写）；
    3. ``next_nodes`` 相同（不同 = 挂起点不一致）；
    4. ``interrupt_count`` 相同；
    5. ``step`` 差值 ≤ ``max_step_lag``（允许副本来不及同步的正常抖动）。
    """
    lag_limit = w6.TASK_CONSISTENCY_MAX_STEP_LAG if max_step_lag is None else max_step_lag
    report = ConsistencyReport(ok=True)

    if left is None or right is None:
        report.ok = False
        report.mismatches.append("snapshot_missing")
        return report

    left_values = left.get("values") or {}
    right_values = right.get("values") or {}
    report.left_step = left.get("step")
    report.right_step = right.get("step")

    def compare(name: str, lval: Any, rval: Any) -> None:
        report.checked.append(name)
        if lval != rval:
            report.ok = False
            report.mismatches.append(f"{name}: {lval!r} != {rval!r}")

    compare("thread_id", left.get("thread_id"), right.get("thread_id"))
    compare("next_nodes", left.get("next_nodes"), right.get("next_nodes"))
    compare("interrupt_count", left.get("interrupt_count"), right.get("interrupt_count"))
    compare("plan_signature", _plan_signature(left_values), _plan_signature(right_values))

    if allow_step_lag:
        report.checked.append("step_lag")
        try:
            lag = abs(int(report.left_step or 0) - int(report.right_step or 0))
        except (TypeError, ValueError):
            lag = 0
        if lag > lag_limit:
            report.ok = False
            report.mismatches.append(f"step_lag: {lag} > {lag_limit}")

    if not report.ok:
        logger.warning(
            "task_state_inconsistent",
            extra={"thread_id": left.get("thread_id"), "mismatches": report.mismatches},
        )
    return report


__all__ = [
    "ConsistencyReport",
    "SseGuardStats",
    "async_guard",
    "check_task_consistency",
    "guard",
    "release_task_resources",
    "sse_guard",
    "task_redis_keys",
]
