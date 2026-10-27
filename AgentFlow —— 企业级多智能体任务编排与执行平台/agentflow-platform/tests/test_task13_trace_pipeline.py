"""任务 13：PII 脱敏 + Trace 异步削峰（缓冲批量落库）专项测试。

- mask_pii 纯函数：手机号 / 邮箱 / 身份证 / bearer / 混合文本；
- TraceService 落库前脱敏：dict 与纯字符串 payload 两条路都要过 mask；
- TraceBuffer：入队不阻塞、flush 排干、批量触发、定时触发、stop 不丢数据、
  写失败 fail-open、shutdown 钩子清空注册表。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.orm_models import TaskExecution
from app.services.trace_service import (
    _ACTIVE_BUFFERS,
    TraceBuffer,
    TraceService,
    _safe_payload,
    shutdown_trace_writer,
)
from app.utils.masking import mask_pii


# ---------------------------------------------------------------------------
# mask_pii 纯函数
# ---------------------------------------------------------------------------
def test_mask_pii_phone():
    assert mask_pii("联系我 13812345678 谢谢") == "联系我 138****5678 谢谢"


def test_mask_pii_email():
    assert mask_pii("发送到 zhangsan@example.com 即可") == "发送到 zha***@example.com 即可"


def test_mask_pii_idcard_keeps_head_and_tail():
    masked = mask_pii("身份证 110101199003077512 号")
    assert "110101199003077512" not in masked
    assert "110101********7512" in masked  # 生日与顺序码全遮


def test_mask_pii_bearer_token():
    assert mask_pii("Authorization: Bearer abc.def-123") == "Authorization: Bearer ***"


def test_mask_pii_mixed_and_plain_untouched():
    text = "订单 87654321，手机 13998765432，邮箱 li@example.com，身份证 440301198001011234"
    masked = mask_pii(text)
    assert "13998765432" not in masked and "138****" not in masked  # 只遮出现的号码
    assert "li***@example.com" in masked
    assert "440301********1234" in masked
    assert "87654321" in masked  # 普通数字不动
    assert mask_pii("今天 GMV 增长 12.5%") == "今天 GMV 增长 12.5%"


# ---------------------------------------------------------------------------
# TraceService 落库前脱敏
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_record_masks_string_payload(session_factory: async_sessionmaker[AsyncSession]):
    """此前缺口：纯字符串 payload 没过脱敏。现在必须打码后才入库。"""
    trace = TraceService(session_factory)
    row_id = await trace.record(
        task_id=1,
        trace_id="t" * 8,
        node="planner",
        event="llm.call",
        payload="用户手机号 13812345678，邮箱 bob@test.com",
    )
    assert row_id is not None
    steps = await trace.list_steps(task_id=1)
    payload_text = str(steps[-1]["payload"])
    assert "13812345678" not in payload_text
    assert "138****5678" in payload_text
    assert "bob@test.com" not in payload_text


@pytest.mark.asyncio
async def test_record_masks_dict_payload_and_idcard(session_factory: async_sessionmaker[AsyncSession]):
    trace = TraceService(session_factory)
    await trace.record(
        task_id=2,
        trace_id="t" * 8,
        node="executor",
        event="tool.result",
        payload={"note": "持证人 440301198001011234", "token": "super-secret"},
    )
    steps = await trace.list_steps(task_id=2)
    payload_text = str(steps[-1]["payload"])
    assert "440301198001011234" not in payload_text
    assert "440301********1234" in payload_text
    assert "super-secret" not in payload_text  # 敏感 key 全量打码（既有口径）


def test_safe_payload_masks_non_dict_scalar_strings():
    """_shrink 的 preview 路径也必须脱敏：超长字符串 → 截断前先 mask。"""
    long_text = "手机 13812345678，" + "x" * 6000
    shrunk = _safe_payload(long_text, max_chars=200)
    assert shrunk is not None and shrunk.get("_truncated") is True
    assert "13812345678" not in str(shrunk)


# ---------------------------------------------------------------------------
# TraceBuffer 异步削峰
# ---------------------------------------------------------------------------
def _row(task_id: int, seq: int) -> dict:
    from app.services.trace_service import _build_row

    return _build_row(
        task_id=task_id,
        trace_id=f"trace-{seq:04d}",
        node_value="planner",
        event="enter",
        tool_name=None,
        tool_error=None,
        payload={"seq": seq},
        input_tokens=1,
        output_tokens=2,
        latency_ms=3,
        subtask_id=None,
        parent_span_id=None,
    )


async def _count(session_factory: async_sessionmaker[AsyncSession], task_id: int) -> int:
    async with session_factory() as session:
        rows = (await session.execute(select(TaskExecution).where(TaskExecution.task_id == task_id))).scalars().all()
        return len(rows)


@pytest.mark.asyncio
async def test_buffer_flush_persists_rows(session_factory: async_sessionmaker[AsyncSession]):
    """入队后数据不立即可见，flush 后全部落库。"""
    buffer = TraceBuffer(session_factory)
    for seq in range(5):
        buffer.enqueue(_row(101, seq))
    assert await _count(session_factory, 101) == 0  # 未 flush 不可见
    assert buffer.pending == 5
    assert await buffer.flush() == 0
    assert await _count(session_factory, 101) == 5


@pytest.mark.asyncio
async def test_buffer_batch_size_triggers_write(session_factory: async_sessionmaker[AsyncSession]):
    """攒满 batch_size 由守护协程自动批量写，无需显式 flush。"""
    buffer = TraceBuffer(session_factory, batch_size=3, flush_interval=60.0)
    for seq in range(5):
        buffer.enqueue(_row(102, seq))
    await asyncio.sleep(0.05)  # 让 worker 跑一批
    written = await _count(session_factory, 102)
    assert written == 3  # 攒满 3 条的批次已落库
    assert await buffer.flush() == 0  # 排干剩余 2 条
    assert await _count(session_factory, 102) == 5
    await buffer.stop()


@pytest.mark.asyncio
async def test_buffer_flush_interval_timer(session_factory: async_sessionmaker[AsyncSession]):
    """低流量下首条滞留超过 flush_interval 也会自动落库（2 秒定时语义）。"""
    buffer = TraceBuffer(session_factory, batch_size=50, flush_interval=0.1)
    buffer.enqueue(_row(103, 1))
    await asyncio.sleep(0.3)
    assert await _count(session_factory, 103) == 1
    await buffer.stop()


@pytest.mark.asyncio
async def test_buffer_stop_flushes_tail_and_is_idempotent(session_factory: async_sessionmaker[AsyncSession]):
    """stop = flush 尾部 + 停协程；重复调用安全；closed 后入队被忽略。"""
    buffer = TraceBuffer(session_factory)
    buffer.enqueue(_row(104, 1))
    await buffer.stop()
    assert await _count(session_factory, 104) == 1
    await buffer.stop()  # 幂等
    buffer.enqueue(_row(104, 2))  # closed → 忽略
    assert await _count(session_factory, 104) == 1


@pytest.mark.asyncio
async def test_buffer_write_failure_is_fail_open(session_factory: async_sessionmaker[AsyncSession]):
    """批量写失败只告警不外抛，队列照常排干（观测不得阻断业务）。"""
    buffer = TraceBuffer(session_factory)
    buffer.enqueue(_row(105, 1))

    async def broken(*_args, **_kwargs):  # 打断真实写入
        raise RuntimeError("db down")

    buffer._write = broken  # type: ignore[method-assign]
    assert await buffer.flush() == 0  # 不抛异常，队列已排干
    assert buffer.pending == 0


@pytest.mark.asyncio
async def test_buffered_trace_service_record_and_shutdown(session_factory: async_sessionmaker[AsyncSession]):
    """TraceService 缓冲模式：record 即返（主键 None），shutdown 钩子全量落库。"""
    trace = TraceService(session_factory, buffered=True)
    assert trace._buffer in _ACTIVE_BUFFERS  # 已注册，供 lifespan shutdown flush
    for seq in range(3):
        result = await trace.record(
            task_id=106, trace_id=f"t{seq}", node="planner", event="enter", payload={"seq": seq}
        )
        assert result is None  # 缓冲模式拿不到主键（调用方均未使用）
    assert await _count(session_factory, 106) == 0
    await shutdown_trace_writer()
    assert await _count(session_factory, 106) == 3
    assert trace._buffer not in _ACTIVE_BUFFERS  # 注册表已清


@pytest.mark.asyncio
async def test_unbuffered_service_stays_sync(session_factory: async_sessionmaker[AsyncSession]):
    """兼容回归：buffered=False（e2e 的 conftest 路径）行为与改造前一致。"""
    trace = TraceService(session_factory, buffered=False)
    assert trace._buffer is None
    row_id = await trace.record(task_id=107, trace_id="t1", node="planner", event="enter")
    assert row_id is not None  # 同步路径立刻返回主键
    assert await _count(session_factory, 107) == 1
