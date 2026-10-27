"""任务 7：结构化日志 task_id 注入闭环。

覆盖点：
1. JsonFormatter：绑定执行上下文后，每条 JSON 日志自动携带 task_id；
   未绑定时为 null；record 显式 ``extra={"task_id": ...}`` 优先于 ContextVar。
2. PlainFormatter：绑定时形如 ``[task_id:12]``，未绑定时显示 ``[task_id:-]``。
3. 并发隔离：两个 asyncio 任务各自 bind/unbind（复刻 _drive 的绑定模式），
   日志交错产出，task_id 各自隔离不串台；执行结束上下文必须干净复位。
"""

from __future__ import annotations

import asyncio
import json
import logging

from app.core.context import get_task_id, task_id_var
from app.core.logging import JsonFormatter, PlainFormatter
from app.services.stream_context import StreamContext, bind, get_context, unbind


def _ctx(task_id: int) -> StreamContext:
    """构造最小 StreamContext：bind/unbind 只消费 task_id，bus/trace 传 None 即可。"""
    return StreamContext(task_id=task_id, trace_id=f"trace-{task_id}", bus=None, trace=None)


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="t7",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class _Capture(logging.Handler):
    """内存 handler：按当前格式化器落行，用于并发日志取证。"""

    def __init__(self, fmt: logging.Formatter) -> None:
        super().__init__()
        self.setFormatter(fmt)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


# ---------------------------------------------------------------------------
# 1. JsonFormatter 注入
# ---------------------------------------------------------------------------
def test_json_injects_task_id_from_contextvar():
    tokens = bind(_ctx(42))
    try:
        payload = json.loads(JsonFormatter().format(_record()))
        assert payload["task_id"] == 42
        # trace_id_var 由 HTTP 中间件管理，bind 不越权改写：保持空值（序列化为 null）
        assert payload["trace_id"] is None
    finally:
        unbind(tokens)


def test_json_without_binding_is_null():
    assert get_task_id() is None
    payload = json.loads(JsonFormatter().format(_record()))
    assert payload["task_id"] is None


def test_json_explicit_extra_overrides_contextvar():
    tokens = bind(_ctx(1))
    try:
        # 与 trace_id 同口径：record 显式属性优先于 ContextVar 兜底
        payload = json.loads(JsonFormatter().format(_record(task_id=99)))
        assert payload["task_id"] == 99
    finally:
        unbind(tokens)


# ---------------------------------------------------------------------------
# 2. PlainFormatter 注入
# ---------------------------------------------------------------------------
def test_plain_shows_task_id_segment_when_bound():
    tokens = bind(_ctx(12))
    try:
        line = PlainFormatter().format(_record())
        assert "[task_id:12]" in line
    finally:
        unbind(tokens)


def test_plain_shows_dash_when_unbound():
    line = PlainFormatter().format(_record())
    assert "[task_id:-]" in line


# ---------------------------------------------------------------------------
# 3. bind/unbind 生命周期
# ---------------------------------------------------------------------------
def test_unbind_resets_task_id_and_allows_rebind():
    tokens = bind(_ctx(7))
    assert get_task_id() == 7
    assert get_context() is not None
    unbind(tokens)
    assert get_task_id() is None
    assert get_context() is None

    # 模拟协程复用：立刻绑定下一个任务，无残留、无异常
    tokens2 = bind(_ctx(8))
    assert get_task_id() == 8
    unbind(tokens2)
    assert get_task_id() is None


def test_unbind_none_is_noop():
    # task_service._bind_offline_context 复用外层上下文时返回 token=None
    unbind(None)
    assert get_task_id() is None


def test_raw_contextvar_roundtrip():
    token = task_id_var.set(5)
    try:
        assert get_task_id() == 5
    finally:
        task_id_var.reset(token)
    assert get_task_id() is None


# ---------------------------------------------------------------------------
# 4. 并发隔离（核心验收：两个任务日志交错，task_id 不串台）
# ---------------------------------------------------------------------------
async def _run_task(task_id: int, lg: logging.Logger, results: list[tuple[str, int, int | None]]) -> None:
    """复刻 stream_service._drive 的绑定模式：bind → 执行（穿插 await 制造交错）→ unbind。"""
    tokens = bind(_ctx(task_id))
    try:
        await asyncio.sleep(0)  # 让出控制权，两个任务的日志段交错产出
        assert get_task_id() == task_id
        lg.info("phase-1", extra={"phase": task_id})
        await asyncio.sleep(0)
        assert get_task_id() == task_id  # 交错回来后仍是自己的 task_id
        lg.info("phase-2", extra={"phase": task_id})
        results.append(("done", task_id, get_task_id()))
    finally:
        unbind(tokens)
    lg.info("after")  # unbind 后的日志不得再带任何 task_id
    results.append(("clean", task_id, get_task_id()))


async def test_concurrent_tasks_task_id_isolated():
    lg = logging.getLogger("t7.concurrency")
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.INFO)  # root 有效级别可能是 WARNING，必须显式放开
    handler = _Capture(JsonFormatter())
    lg.addHandler(handler)
    try:
        results: list[tuple[str, int, int | None]] = []
        await asyncio.gather(_run_task(101, lg, results), _run_task(202, lg, results))

        assert len(handler.lines) == 6  # 2 任务 × (phase-1 + phase-2 + after)
        rows = [json.loads(line) for line in handler.lines]

        # phase-1 / phase-2：两条交错日志各自携带正确的 task_id
        for phase in ("phase-1", "phase-2"):
            matched = [r for r in rows if r["msg"] == phase]
            assert {r["task_id"] for r in matched} == {101, 202}
            for r in matched:
                assert r["task_id"] == r["phase"], "task_id 串台：日志归属与任务不符"

        # after（unbind 之后）：不得残留任何 task_id
        afters = [r for r in rows if r["msg"] == "after"]
        assert len(afters) == 2
        assert all(r["task_id"] is None for r in afters)

        # 协程内部全程自洽 + 结束后上下文干净（交错顺序随调度变化，不做顺序断言）
        done = [r for r in results if r[0] == "done"]
        clean = [r for r in results if r[0] == "clean"]
        assert len(done) == 2 and len(clean) == 2
        assert all(tid == expected for _, expected, tid in done), "执行期间 task_id 被他人覆盖"
        assert all(tid is None for _, _, tid in clean), "unbind 后 task_id 未清理"

        # 主上下文不受并发任务影响
        assert get_task_id() is None
        assert get_context() is None
    finally:
        lg.removeHandler(handler)
