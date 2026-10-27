"""W5-Part2 新增用例：边界加固（SSE 守护 / 异常捕获 / 一致性校验 / 取消回收）。"""

from __future__ import annotations

import asyncio

import pytest

from app.core.resilience_guard import (
    SseGuardStats,
    async_guard,
    check_task_consistency,
    guard,
    release_task_resources,
    sse_guard,
    task_redis_keys,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1) SSE 守护
# ---------------------------------------------------------------------------
async def _collect(inner, **kwargs) -> tuple[list[str], SseGuardStats]:
    stats = SseGuardStats()
    frames = [frame async for frame in sse_guard(inner, stats=stats, **kwargs)]
    return frames, stats


async def test_sse_guard_passes_frames_through_in_order() -> None:
    async def inner():
        for i in range(3):
            yield f"data: {i}\n\n"

    frames, stats = await _collect(inner(), heartbeat_seconds=1, idle_timeout_seconds=1, max_seconds=5)

    assert frames[0].startswith("retry: ")
    assert [f for f in frames if f.startswith("data: ")] == ["data: 0\n\n", "data: 1\n\n", "data: 2\n\n"]
    assert frames[-1].startswith("event: close")
    assert '"reason": "inner_end"' in frames[-1]
    assert stats.frames == 3


async def test_sse_guard_emits_heartbeat_when_idle() -> None:
    async def inner():
        yield "data: first\n\n"
        await asyncio.sleep(0.25)
        yield "data: second\n\n"

    frames, stats = await _collect(inner(), heartbeat_seconds=0.05, idle_timeout_seconds=0.05, max_seconds=5)

    assert stats.pings >= 2
    assert frames.count(": ping\n\n") == stats.pings
    assert "data: first\n\n" in frames and "data: second\n\n" in frames


async def test_sse_guard_closes_on_max_seconds() -> None:
    async def inner():
        while True:
            await asyncio.sleep(0.05)
            yield "data: tick\n\n"

    frames, stats = await _collect(inner(), heartbeat_seconds=0.05, idle_timeout_seconds=0.05, max_seconds=0.3)

    assert stats.closed_reason == "max_seconds"
    assert '"reason": "max_seconds"' in frames[-1]


async def test_sse_guard_handles_inner_exception() -> None:
    closed = {"called": False}

    async def inner():
        yield "data: one\n\n"
        raise RuntimeError("上游炸了")

    def on_close(state: SseGuardStats) -> None:
        closed["called"] = True

    frames, stats = await _collect(
        inner(), heartbeat_seconds=1, idle_timeout_seconds=1, max_seconds=5, on_close=on_close
    )

    assert stats.closed_reason == "inner_error"
    assert frames[-1].startswith("event: close")
    assert closed["called"] is True


async def test_sse_guard_closes_inner_generator() -> None:
    state = {"closed": False}

    async def inner():
        try:
            yield "data: x\n\n"
            await asyncio.sleep(5)
            yield "data: never\n\n"
        finally:
            state["closed"] = True

    stream = sse_guard(inner(), heartbeat_seconds=0.05, idle_timeout_seconds=0.05, max_seconds=0.2)
    async for _ in stream:
        pass

    assert state["closed"] is True


# ---------------------------------------------------------------------------
# 2) 异常捕获装饰器
# ---------------------------------------------------------------------------
async def test_async_guard_returns_fallback() -> None:
    @async_guard(event="test_guard", fallback={"ok": False})
    async def boom() -> dict[str, bool]:
        raise ValueError("x")

    assert await boom() == {"ok": False}


async def test_async_guard_never_swallows_cancel() -> None:
    @async_guard(event="test_guard_cancel")
    async def cancel() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await cancel()


async def test_async_guard_raise_on_passthrough() -> None:
    @async_guard(event="test_guard_raise", raise_on=(KeyError,))
    async def boom() -> None:
        raise KeyError("k")

    with pytest.raises(KeyError):
        await boom()


def test_sync_guard_returns_fallback() -> None:
    @guard(event="test_guard_sync", fallback=0)
    def boom() -> int:
        raise RuntimeError("x")

    assert boom() == 0


# ---------------------------------------------------------------------------
# 3) 任务状态一致性校验
# ---------------------------------------------------------------------------
def _snapshot(**overrides):
    base = {
        "thread_id": "task-1",
        "step": 4,
        "next_nodes": ["human_gate"],
        "interrupt_count": 1,
        "values": {"plan": [{"seq": 1, "name": "sql-query"}, {"seq": 2, "name": "notify"}]},
    }
    base.update(overrides)
    return base


def test_consistency_ok_for_identical_snapshots() -> None:
    report = check_task_consistency(_snapshot(), _snapshot())
    assert report.ok is True
    assert report.mismatches == []


def test_consistency_allows_small_step_lag() -> None:
    report = check_task_consistency(_snapshot(step=4), _snapshot(step=5), max_step_lag=1)
    assert report.ok is True


def test_consistency_detects_thread_mismatch() -> None:
    report = check_task_consistency(_snapshot(), _snapshot(thread_id="task-2"))
    assert report.ok is False
    assert any(item.startswith("thread_id") for item in report.mismatches)


def test_consistency_detects_plan_mismatch() -> None:
    broken = _snapshot(values={"plan": [{"seq": 1, "name": "sql-query"}]})
    report = check_task_consistency(_snapshot(), broken)
    assert report.ok is False
    assert any(item.startswith("plan_signature") for item in report.mismatches)


def test_consistency_detects_step_lag_beyond_limit() -> None:
    report = check_task_consistency(_snapshot(step=2), _snapshot(step=9), max_step_lag=1)
    assert report.ok is False
    assert any(item.startswith("step_lag") for item in report.mismatches)


def test_consistency_detects_interrupt_mismatch() -> None:
    report = check_task_consistency(_snapshot(interrupt_count=1), _snapshot(interrupt_count=0))
    assert report.ok is False
    assert any(item.startswith("interrupt_count") for item in report.mismatches)


def test_consistency_missing_snapshot() -> None:
    report = check_task_consistency(None, _snapshot())
    assert report.ok is False
    assert report.mismatches == ["snapshot_missing"]


# ---------------------------------------------------------------------------
# 4) 取消后的资源回收
# ---------------------------------------------------------------------------
def test_task_redis_keys_cover_cancel_stream_memory() -> None:
    keys = task_redis_keys(42)
    assert any(key.endswith(":cancel:42") for key in keys)
    assert any(key.endswith(":events:42") for key in keys)
    assert any(key.endswith(":memory:short:42") for key in keys)


async def test_release_task_resources_deletes_keys(fake_redis) -> None:
    keys = task_redis_keys(42)
    for key in keys:
        await fake_redis.set(key, "x")

    report = await release_task_resources(42, redis=fake_redis)

    assert sorted(report["deleted"]) == sorted(keys)
    assert report["errors"] == []
    for key in keys:
        assert await fake_redis.exists(key) == 0


async def test_release_task_resources_keeps_checkpoint(fake_redis) -> None:
    """断点 key 不在清理清单里：取消不应影响审计与回放。"""
    from app.services.checkpoint_store import checkpoint_key

    ckpt = checkpoint_key(42)
    await fake_redis.set(ckpt, "snapshot")
    await release_task_resources(42, redis=fake_redis)
    assert await fake_redis.exists(ckpt) == 1


async def test_release_task_resources_can_be_disabled(fake_redis) -> None:
    key = task_redis_keys(42)[0]
    await fake_redis.set(key, "x")

    report = await release_task_resources(42, redis=fake_redis, enabled=False)

    assert report["deleted"] == []
    assert await fake_redis.exists(key) == 1


async def test_release_task_resources_survives_redis_error() -> None:
    class Broken:
        async def delete(self, key):
            raise ConnectionError("redis down")

    report = await release_task_resources(42, redis=Broken())
    assert report["deleted"] == []
    assert any("redis" in item.lower() or "ConnectionError" in item for item in report["errors"])
