"""轨迹服务（FR-015）：把节点进出、工具调用、LLM 入参出参写入 ``task_executions``。

边界说明（为什么不改 trace 中间件）：
- W1 的 ``app/core/trace.py`` 只负责 HTTP 层 trace_id 透传，保持不动；
- W3 的轨迹落库全部发生在本服务，由流式执行器在事件循环里显式调用；
- 写入失败只告警、不抛出——轨迹是观测能力，不能反向影响主流程（PRD 4.2）。

字段复用（不新增 ORM 表，红线性约束）：
``task_executions(trace_id, span_id, task_id, node, event, tool_name, tool_error, latency_ms, payload, started_at)``
其中 ``event`` 列是 VARCHAR(32) 自由文本，W3 在 PRD 取值（enter/exit/tool.call/tool.result）
之外补充了 ``llm.call``，用于满足"LLM 入参出参全部入库"的要求。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import timezone
from typing import Any, AsyncIterator

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.core.w3_config import w3
from app.schemas.enums import ExecutionNode
from app.schemas.orm_models import TaskExecution
from app.utils.ids import new_span_id
from app.utils.masking import mask_mapping, mask_text
from app.utils.time import utcnow

logger = get_logger(__name__)

VALID_NODES: frozenset[str] = frozenset(item.value for item in ExecutionNode)
_TEXT_LIMIT = 200


def _factory(session_factory: async_sessionmaker[AsyncSession] | None) -> async_sessionmaker[AsyncSession]:
    if session_factory is not None:
        return session_factory
    from app.db.session import get_session_factory

    return get_session_factory()


def _shrink(payload: Any, max_chars: int) -> dict[str, Any] | None:
    """把任意 payload 收成可入库的 JSON：超长转 preview，不可序列化则标记。"""
    if payload is None:
        return None
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return {"_unserializable": True, "type": type(payload).__name__}
    if len(text) <= max_chars:
        try:
            shrunk = json.loads(text)
        except ValueError:
            return {"preview": text[:max_chars]}
        return shrunk if isinstance(shrunk, dict) else {"value": shrunk}
    return {"_truncated": True, "chars": len(text), "preview": text[:max_chars]}


def _safe_payload(payload: Any, max_chars: int) -> dict[str, Any] | None:
    """先脱敏再截断（顺序不能反：先截断可能把敏感串切一半，正则失效）。

    任务 13 PII 补口：此前只有 dict 走 ``mask_mapping``，纯字符串 payload
    （LLM 原文、工具结果片段）完全没过脱敏——它们恰恰是最容易携带
    手机号/邮箱/身份证的自由文本。
    """
    if payload is None:
        return None
    if isinstance(payload, dict):
        masked: Any = mask_mapping(payload)
    elif isinstance(payload, str):
        masked = mask_text(payload)
    else:
        masked = payload
    return _shrink(masked, max_chars)


def _build_row(
    *,
    task_id: int,
    trace_id: str,
    node_value: str,
    event: str,
    tool_name: str | None,
    tool_error: str | None,
    payload: Any,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    subtask_id: int | None,
    parent_span_id: str | None,
    max_chars: int = 4000,
) -> dict[str, Any]:
    """构造一条 span 的列字典（脱敏 / 截断 / 类型归一都在这里）。

    同步路径 ``TaskExecution(**row)`` 与缓冲路径 ``insert(...).values 批量``
    共用同一份构造逻辑，保证两条路的脱敏口径完全一致。
    """
    return {
        "trace_id": str(trace_id)[:32],
        "span_id": new_span_id(),
        "parent_span_id": parent_span_id,
        "task_id": int(task_id),
        "subtask_id": subtask_id,
        "node": ExecutionNode(node_value),
        "event": str(event)[:32],
        "tool_name": (str(tool_name)[:32] if tool_name else None),
        "tool_error": (str(tool_error)[:64] if tool_error else None),
        "input_tokens": max(0, int(input_tokens or 0)),
        "output_tokens": max(0, int(output_tokens or 0)),
        "latency_ms": max(0, int(latency_ms or 0)),
        "payload": _safe_payload(payload, max_chars),
        "started_at": utcnow(),
    }


class TraceBuffer:
    """Trace 异步削峰缓冲（任务 13）：``asyncio.Queue`` + 守护协程攒批落库。

    - 业务侧 ``enqueue`` 只做 ``put_nowait``，永不阻塞执行图（队列满丢弃 + 告警）；
    - 守护协程攒满 ``batch_size`` 条或首条滞留超过 ``flush_interval`` 秒即批量写；
    - 在途批次持有在 ``self._pending``（``_lock`` 保护所有权转移），``flush`` /
      ``stop`` 能一并排干，不会出现"数据卡在协程局部变量里 flush 不到"的暗仓；
    - ``flush`` / ``stop`` 供 FastAPI lifespan shutdown 钩子调用，不丢尾部数据；
    - 一切写失败只告警（观测能力不得反向影响主流程，与既有口径一致）。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        batch_size: int = 50,
        flush_interval: float = 2.0,
        maxsize: int = 10000,
    ) -> None:
        self._session_factory = session_factory
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.05, float(flush_interval))
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max(1, int(maxsize)))
        self._pending: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def pending(self) -> int:
        """尚未落库的条数 = 队列内 + 守护协程在途批次。"""
        return self._queue.qsize() + len(self._pending)

    def _resolve_factory(self) -> async_sessionmaker[AsyncSession]:
        return _factory(self._session_factory)

    def enqueue(self, row: dict[str, Any]) -> None:
        """入队（非阻塞）；懒启动守护协程。队列满或已关闭则丢弃并告警。"""
        if self._closed:
            return
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:  # pragma: no cover - 上限 10000，极难触达
            logger.warning("trace_buffer_full_drop", extra={"task_id": row.get("task_id")})
            return
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        loop = asyncio.get_running_loop()
        self._worker = loop.create_task(self._run())

    async def _run(self) -> None:
        """守护循环：攒满 batch_size 或首条滞留 flush_interval 秒即批量写。"""
        while True:
            if self._closed and self._queue.empty() and not self._pending:
                return
            if self._pending:
                timeout: float | None = self.flush_interval
            elif self._closed:
                timeout = 0.05  # 关闭后快速轮询，让出口条件尽快命中
            else:
                timeout = None
            try:
                self._pending.append(await asyncio.wait_for(self._queue.get(), timeout=timeout))
            except asyncio.TimeoutError:
                if self._pending:  # 滞留超时：有多少写多少（定时 flush 语义）
                    await asyncio.shield(self._drain_and_write())
                continue
            while len(self._pending) < self.batch_size:
                try:
                    self._pending.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if len(self._pending) >= self.batch_size:
                # shield：外部 stop 的 cancel 不会打断在途 DB 写（不丢数据）
                await asyncio.shield(self._drain_and_write())

    async def _drain_and_write(self) -> None:
        """原子接管在途批次并落库（_lock 保证 flush 与 worker 不重复写同一批）。"""
        async with self._lock:
            batch, self._pending = self._pending, []
        await self._write(batch)

    async def _write(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        try:
            session_factory = self._resolve_factory()
            async with session_factory() as session:
                await session.execute(insert(TaskExecution), batch)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - 观测写失败只告警
            logger.warning(
                "trace_batch_write_failed",
                extra={"count": len(batch), "error": str(exc)[:200]},
            )

    async def flush(self) -> int:
        """同步排干在途批次 + 队列并批量写（测试 / shutdown 用）。返回剩余队列长度。"""
        async with self._lock:
            batch, self._pending = self._pending, []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        with contextlib.suppress(Exception):  # noqa: BLE001 - fail-open
            await self._write(batch)
        return self._queue.qsize()

    async def stop(self) -> None:
        """关闭：先排干尾部数据再停守护协程（不丢数据）。可重复调用。"""
        if self._closed and self._worker is None:
            return
        self._closed = True
        await self.flush()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None


#: 存活的缓冲实例（生产链路 shutdown 时统一 flush）。测试关闭缓冲后为空。
_ACTIVE_BUFFERS: list[TraceBuffer] = []


async def shutdown_trace_writer() -> None:
    """FastAPI lifespan shutdown 钩子：flush 所有存活缓冲，不丢尾部轨迹。"""
    for buffer in list(_ACTIVE_BUFFERS):
        with contextlib.suppress(Exception):  # noqa: BLE001 - shutdown 阶段不抛
            await buffer.stop()
    _ACTIVE_BUFFERS.clear()


class TraceService:
    """执行轨迹读写。所有写方法都吞异常（观测能力不得阻断业务）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        enabled: bool | None = None,
        max_chars: int | None = None,
        buffered: bool | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.enabled = w3.TRACE_ENABLED if enabled is None else enabled
        self.max_chars = max_chars or w3.TRACE_PAYLOAD_MAX_CHARS
        # 任务 13 异步削峰：显式参数优先，否则读配置；e2e 测试由 conftest autouse 关闭
        use_buffer = w3.TRACE_ASYNC_BUFFER_ENABLED if buffered is None else buffered
        self._buffer: TraceBuffer | None = None
        if use_buffer and self.enabled:
            self._buffer = TraceBuffer(
                session_factory,
                batch_size=w3.TRACE_BUFFER_BATCH_SIZE,
                flush_interval=w3.TRACE_BUFFER_FLUSH_INTERVAL,
                maxsize=w3.TRACE_BUFFER_MAXSIZE,
            )
            _ACTIVE_BUFFERS.append(self._buffer)

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return _factory(self._session_factory)

    async def record(
        self,
        *,
        task_id: int,
        trace_id: str,
        node: str,
        event: str,
        tool_name: str | None = None,
        tool_error: str | None = None,
        payload: Any = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        subtask_id: int | None = None,
        parent_span_id: str | None = None,
    ) -> int | None:
        """写一条 span，返回主键；关闭或失败时返回 None。

        任务 13 异步削峰：缓冲模式（``TRACE_ASYNC_BUFFER_ENABLED``）下只做
        ``put_nowait`` 入队，批量落库由后台守护协程完成——业务侧零等待；
        主键此时不可得，统一返回 None（调用方均未使用返回值）。
        """
        if not self.enabled:
            return None
        node_value = str(node or "").lower()
        if node_value not in VALID_NODES:
            logger.debug("trace_node_skipped", extra={"node": node_value, "event": event})
            return None

        row = _build_row(
            task_id=task_id,
            trace_id=trace_id,
            node_value=node_value,
            event=event,
            tool_name=tool_name,
            tool_error=tool_error,
            payload=payload,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            subtask_id=subtask_id,
            parent_span_id=parent_span_id,
            max_chars=self.max_chars,
        )
        if self._buffer is not None:
            self._buffer.enqueue(row)
            return None
        try:
            async with self.session_factory() as session:
                obj = TaskExecution(**row)
                session.add(obj)
                await session.commit()
                await session.refresh(obj)
                return int(obj.id)
        except Exception as exc:  # noqa: BLE001 - 轨迹写失败不影响主流程
            logger.warning(
                "trace_write_failed",
                extra={"task_id": task_id, "node": node_value, "event": event, "error": str(exc)[:200]},
            )
            return None

    # ---------------- 读取（回放） ----------------
    async def list_steps(
        self,
        *,
        task_id: int,
        after_id: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        max_steps = limit or w3.TRACE_REPLAY_MAX_STEPS
        async with self.session_factory() as session:
            stmt = (
                select(TaskExecution)
                .where(TaskExecution.task_id == int(task_id), TaskExecution.id > int(after_id))
                .order_by(TaskExecution.id.asc())
                .limit(max_steps)
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [self._to_dict(row) for row in rows]

    async def count(self, task_id: int, *, event: str | None = None) -> int:
        """统计任务的 span 数量；``event`` 给定时只统计该事件（导出 metrics 用）。"""
        async with self.session_factory() as session:
            stmt = select(func.count(TaskExecution.id)).where(TaskExecution.task_id == int(task_id))
            if event is not None:
                stmt = stmt.where(TaskExecution.event == str(event))
            return int((await session.execute(stmt)).scalar_one())

    async def iter_steps(
        self,
        *,
        task_id: int,
        after_id: int = 0,
        limit: int | None = None,
        batch: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """分批游标读取轨迹 span（加固任务 5：导出防 OOM 的数据源）。

        采用 **keyset 分页**（``WHERE id > cursor ORDER BY id LIMIT batch``）而非
        ``yield_per``：每批一个独立查询，不长期占用连接/事务，兼容 SQLite 与 MySQL，
        且导出期间其他连接写入不会使游标失效。内存占用 = 单批（默认 100 条）。
        """
        remaining = (limit or w3.TRACE_REPLAY_MAX_STEPS) - 0
        cursor = int(after_id)
        batch = max(1, int(batch))
        while remaining > 0:
            size = min(batch, remaining)
            async with self.session_factory() as session:
                stmt = (
                    select(TaskExecution)
                    .where(TaskExecution.task_id == int(task_id), TaskExecution.id > cursor)
                    .order_by(TaskExecution.id.asc())
                    .limit(size)
                )
                rows = (await session.execute(stmt)).scalars().all()
            if not rows:
                return
            for row in rows:
                yield self._to_dict(row)
            remaining -= len(rows)
            cursor = int(rows[-1].id)
            if len(rows) < size:  # 已读尽
                return

    async def stats(self, task_id: int) -> dict[str, Any]:
        """按节点/事件聚合，供前端 DAG 回放侧栏展示。"""
        async with self.session_factory() as session:
            stmt = (
                select(TaskExecution.node, TaskExecution.event, func.count(TaskExecution.id))
                .where(TaskExecution.task_id == int(task_id))
                .group_by(TaskExecution.node, TaskExecution.event)
            )
            rows = (await session.execute(stmt)).all()
        by_node: dict[str, int] = {}
        by_event: dict[str, int] = {}
        for node, event, count in rows:
            node_value = node.value if hasattr(node, "value") else str(node)
            by_node[node_value] = by_node.get(node_value, 0) + int(count)
            by_event[str(event)] = by_event.get(str(event), 0) + int(count)
        return {"by_node": by_node, "by_event": by_event, "total": sum(by_node.values())}

    @staticmethod
    def _to_dict(row: TaskExecution) -> dict[str, Any]:
        node = row.node.value if hasattr(row.node, "value") else str(row.node)
        return {
            "id": int(row.id),
            "trace_id": row.trace_id,
            "task_id": int(row.task_id),
            "span_id": row.span_id,
            "parent_span_id": row.parent_span_id,
            "subtask_id": int(row.subtask_id) if row.subtask_id else None,
            "node": node,
            "event": row.event,
            "tool_name": row.tool_name,
            "tool_error": row.tool_error,
            "input_tokens": int(row.input_tokens or 0),
            "output_tokens": int(row.output_tokens or 0),
            "latency_ms": int(row.latency_ms or 0),
            "payload": row.payload,
            # 表里是 UTC naive（本服务写入时用 utcnow()），序列化补偏移，避免前端按本地时间误读 8 小时
            "started_at": (
                row.started_at.replace(tzinfo=timezone.utc).isoformat() if row.started_at else None
            ),
        }


def brief(text: Any, limit: int = _TEXT_LIMIT) -> str:
    """把任意对象压成一行脱敏短文本（用于 payload 摘要，避免整段提示词入库）。"""
    return mask_text(str(text or ""))[:limit]


__all__ = ["TraceService", "VALID_NODES", "TraceBuffer", "brief", "shutdown_trace_writer"]
