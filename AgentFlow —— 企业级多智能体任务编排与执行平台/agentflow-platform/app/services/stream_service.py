"""流式任务执行（FR-009 / FR-012）：``astream_events`` → 事件总线 → SSE。

为什么用「子类覆盖 run」而不是改 W2 的 ``TaskService``：
- W2 的 create_task / _finalize / _resolve_status 等全部复用，零改动（红线性约束）；
- 只把 ``run`` 从 ``ainvoke`` 换成 ``astream_events``，并把每个 LangGraph 事件翻译成
  4 类 SSE 事件 + 轨迹行；
- ``resume`` 与 ``run`` 共用 ``_drive``，因此「首次执行」与「断点续跑」的事件口径完全一致。

事件翻译规则：
- 节点 ``on_chain_start`` / ``on_chain_end`` → ``state_update``（phase=enter/exit，带 output_keys）；
- MCP 层 ``tool.call`` / ``tool.result`` → ``tool_call``（phase=call/result，由 ``mcp_event_hook`` 产生）；
- 图停在 human_gate（``snapshot.tasks[*].interrupts`` 非空）→ ``interrupt``（终止帧）；
- 图正常结束 / 失败 / 被取消 → ``complete``（终止帧）。

版本协商：
``w3.SSE_EVENTS_VERSION`` 默认 v3（需求指定），但当前 langgraph 0.3.34 的实现只支持 v1/v2，
调用会抛 ``NotImplementedError``。因此这里做"先试首选版本、失败按 v2 → v1 退让"的协商，
并在首次成功后缓存实际版本，既满足协议要求也不依赖特定小版本。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from langgraph.types import Command

from app.agents.common import total_tokens
from app.agents.graph import NODE_NAMES, AgentRuntime
from app.core.exceptions import AppException, ErrorCode
from app.core.logging import get_logger
from app.core.redis_lock import task_lock
from app.core.w3_config import w3
from app.core.w3_errors import (
    INTERRUPT_MISSING,
    TASK_NOT_CANCELABLE,
    TERMINAL_STATUSES,
    conflict,
    normalize_status,
    not_found,
)
from app.schemas.enums import TaskStatus
from app.schemas.orm_models import Task
from app.schemas.w3 import TERMINAL_EVENTS
from app.services.cancel_service import CancelSignal
from app.services.checkpoint_service import CheckpointService
from app.services.event_bus import START_ID, EventBus
from app.services.interrupt_service import (
    clear_interrupts,
    collect_interrupts,
    ensure_resumable,
    expire_pending_approvals,
    interrupt_context,
    load_snapshot,
)
from app.services.stream_context import StreamContext, bind, emit, unbind
from app.services.task_service import TaskService
from app.services.trace_service import TraceService, brief
from app.services.w3_runtime import get_w3_runtime
from app.utils.time import utcnow

logger = get_logger(__name__)

_TERMINAL_STATUS_VALUES = {item.value for item in TERMINAL_STATUSES}
#: 首次成功后缓存实际可用的事件协议版本
_RESOLVED_EVENTS_VERSION: str | None = None


def version_candidates(preferred: str | None = None) -> list[str]:
    """协议版本探测顺序：首选 → v2 → v1（去重保序）。"""
    ordered = [preferred or w3.SSE_EVENTS_VERSION or "v2", "v2", "v1"]
    seen: set[str] = set()
    result: list[str] = []
    for item in ordered:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


async def iterate_graph_events(
    graph: Any,
    graph_input: Any,
    config: dict[str, Any],
    *,
    preferred: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """``astream_events`` 的版本协商包装（不改变事件语义，只做降级）。"""
    global _RESOLVED_EVENTS_VERSION
    candidates = version_candidates(_RESOLVED_EVENTS_VERSION or preferred)
    last_error: Exception | None = None
    for version in candidates:
        emitted = False
        try:
            async for event in graph.astream_events(graph_input, config=config, version=version):
                emitted = True
                if _RESOLVED_EVENTS_VERSION is None:
                    _RESOLVED_EVENTS_VERSION = version
                    if version != (preferred or w3.SSE_EVENTS_VERSION):
                        logger.warning(
                            "sse_events_version_negotiated",
                            extra={"requested": preferred or w3.SSE_EVENTS_VERSION, "actual": version},
                        )
                yield event
            return
        except (NotImplementedError, ValueError) as exc:
            last_error = exc
            if emitted:  # 已经开始产出事件，说明不是版本问题，直接抛出
                raise
            logger.warning("sse_events_version_unsupported", extra={"version": version, "error": str(exc)[:160]})
            continue
    if last_error is not None:
        raise last_error


def _artifact_dicts(state: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for artifact in state.get("artifacts") or []:
        if isinstance(artifact, dict):
            items.append(artifact)
        else:
            dump = getattr(artifact, "model_dump", None)
            items.append(dump(mode="json") if callable(dump) else {"value": str(artifact)})
    return items


#: 公开别名：非流式入口（TaskService.run）补发 complete 帧时需要同一套 artifact 序列化
artifact_dicts = _artifact_dicts


async def translate_graph_event(event: dict[str, Any], ctx: StreamContext) -> bool:
    """把一个 LangGraph 事件翻译成 SSE 事件 + 轨迹行；返回是否为本节点的 enter 事件。

    抽成模块级函数的原因：SSE 入口（``StreamingTaskService._handle_event``）与普通
    ``POST /tasks`` 入口（``TaskService.run``）都要靠它落轨迹。此前只有前者消费事件，
    后者用 ``ainvoke`` 一次性跑完，导致普通创建的任务 ``task_executions`` 全空——
    详情页轨迹时间线整页空白。现在两条路径共用同一套翻译，语义完全一致。
    """
    kind = event.get("event")
    name = event.get("name")
    metadata = event.get("metadata") or {}
    node = metadata.get("langgraph_node")
    # 只处理"图节点本身"的 chain 事件，滤掉节点内部的子 chain（如 LLM、工具）：
    # metadata.langgraph_node 是图的节点名（planner/context_compress…），
    # 而 event.name 是 Runnable 名字——对 G8 的可选节点来说是函数名 `xxx_node`，
    # 因此两者要按 "节点名" 或 "节点名_node" 匹配，否则分支节点的事件会被整段丢弃，
    # Trace 里就看不到 memory_recall / context_compress（表现为"开关开了但轨迹没有该节点"）。
    if not node or node not in NODE_NAMES or str(name) not in {str(node), f"{node}_node"}:
        return False

    node_name = str(node)
    if kind == "on_chain_start":
        ctx.node = node_name
        await emit("state_update", {"phase": "enter", "node": node_name, "status": TaskStatus.RUNNING.value})
        await ctx.trace.record(task_id=ctx.task_id, trace_id=ctx.trace_id, node=node_name, event="enter")
        return True

    if kind == "on_chain_end":
        output = (event.get("data") or {}).get("output")
        output_dict = output if isinstance(output, dict) else {}
        status = str(output_dict.get("status") or TaskStatus.RUNNING.value)
        await emit(
            "state_update",
            {
                "phase": "exit",
                "node": node_name,
                "status": status,
                "output_keys": sorted(str(key) for key in output_dict)[:20],
            },
        )
        await ctx.trace.record(
            task_id=ctx.task_id,
            trace_id=ctx.trace_id,
            node=node_name,
            event="exit",
            payload={key: output_dict[key] for key in list(output_dict)[:12]},
        )
        return False

    if kind == "on_chain_error":
        error = (event.get("data") or {}).get("error")
        await emit(
            "state_update",
            {
                "phase": "error",
                "node": node_name,
                "status": TaskStatus.FAILED.value,
                "error": brief(error),
            },
        )
        await ctx.trace.record(
            task_id=ctx.task_id,
            trace_id=ctx.trace_id,
            node=node_name,
            event="error",
            payload={"error": brief(error)},
        )
    return False


class StreamingTaskService(TaskService):
    """在 W2 TaskService 之上，把执行过程变成可订阅的事件流。"""

    def __init__(
        self,
        *,
        bus: EventBus,
        trace: TraceService | None = None,
        cancel: CancelSignal | None = None,
        session_factory: Any = None,
        runtime: AgentRuntime | None = None,
    ) -> None:
        super().__init__(session_factory=session_factory, runtime=runtime)
        self._bus = bus
        self._trace = trace or TraceService(session_factory)
        # 复用事件总线同一个 Redis 客户端：少一条连接池，且测试里 fake redis 自动生效
        self._cancel = cancel or CancelSignal(bus.redis_client)

    # ---------------- 基础设施 ----------------
    async def runtime(self) -> AgentRuntime:  # type: ignore[override]
        if self._runtime is not None:
            return self._runtime
        return await get_w3_runtime()

    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def trace(self) -> TraceService:
        return self._trace

    # ---------------- 提交 ----------------
    async def submit(
        self,
        *,
        goal: str,
        time_range: Any,
        creator_id: int,
        sync: bool = False,
        compress: bool = False,
        use_memory: bool = False,
        eval_tag: str | None = None,
    ) -> dict[str, Any]:
        """创建任务 + 触发流式执行，返回带 ``stream_url`` 的提交结果。"""
        result = await self.create_task(
            goal=goal,
            time_range=time_range,
            creator_id=creator_id,
            sync=sync,
            compress=compress,
            use_memory=use_memory,
            eval_tag=eval_tag,
        )
        result["stream_url"] = f"/api/v1/tasks/{result['task_id']}/stream"
        result["stream_accept"] = "text/event-stream"
        return result

    # ---------------- 执行 ----------------
    async def run(self, task_id: int) -> dict[str, Any]:  # type: ignore[override]
        return await self._drive(task_id, resume_payload=None)

    async def resume(
        self,
        task_id: int,
        *,
        decision: str,
        comment: str = "",
        decided_by: int | None = None,
        approval_id: int | None = None,
        sync: bool | None = None,
    ) -> dict[str, Any]:
        """审批决策 → ``Command(resume=...)`` 从 human_gate 断点继续。"""
        runtime = await self.runtime()
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise not_found(f"任务 {task_id} 不存在", task_id=task_id)
            status = normalize_status(task.status)
            trace_id = task.trace_id
            creator_id = int(task.creator_id)

        if (
            not w3.APPROVAL_ALLOW_SELF_DECIDE
            and decided_by is not None
            and int(decided_by) == creator_id
        ):
            raise AppException(
                ErrorCode.FORBIDDEN,
                "不允许审批本人创建的任务",
                detail={"task_id": task_id, "creator_id": creator_id},
            )

        context = await ensure_resumable(
            runtime.graph, task_id, status, trace_id=trace_id, approval_id=approval_id
        )
        payload = {
            "decision": decision,
            "comment": comment,
            "decided_by": decided_by,
            "approval_id": context.get("approval_id"),
        }
        should_sync = w3.RESUME_SYNC_DEFAULT if sync is None else bool(sync)

        if not should_sync:
            asyncio.create_task(self._resume_safely(task_id, payload))
            return {
                "task_id": task_id,
                "resumed": True,
                "status": TaskStatus.RUNNING.value,
                "approval_id": context.get("approval_id"),
                "decision": decision,
                "next_nodes": [],
                "sync": False,
            }

        # 任务 1：task 粒度 Redis 分布式锁。checkpointer 直写 Redis 绕过 ORM，
        # 并发审批（如两个管理员同时同意）会各自驱动图执行、互相覆盖状态——
        # 必须在驱动前串行化；抢锁失败快速返回 409 TASK_LOCKED，不排队。
        async with task_lock(self._bus.redis_client, task_id):
            await self._drive(task_id, resume_payload=payload)
            snapshot = await load_snapshot(runtime.graph, task_id)
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                final_status = normalize_status(task.status) if task is not None else None
        return {
            "task_id": task_id,
            "resumed": True,
            "status": (final_status or TaskStatus.RUNNING).value,
            "approval_id": context.get("approval_id"),
            "decision": decision,
            "next_nodes": [str(n) for n in (getattr(snapshot, "next", ()) or ())],
            "sync": True,
        }

    async def _resume_safely(self, task_id: int, payload: dict[str, Any]) -> None:
        try:
            # 异步（非阻塞）恢复路径同样持锁：后台驱动与下一次人工操作互斥
            async with task_lock(self._bus.redis_client, task_id):
                await self._drive(task_id, resume_payload=payload)
        except AppException as exc:
            # 业务冲突（典型：抢锁失败 TASK_LOCKED）：任务仍在 AWAITING_APPROVAL，
            # 用户可重试；后台任务只留痕，不允许炸掉事件循环
            logger.warning(
                "task_resume_background_conflict",
                extra={"task_id": task_id, "code": str(getattr(exc, "code", ""))},
            )
        except Exception:  # noqa: BLE001 - 后台恢复异常必须留痕，但不能炸掉事件循环
            logger.exception("task_resume_background_failed", extra={"task_id": task_id})

    async def _drive(self, task_id: int, *, resume_payload: dict[str, Any] | None) -> dict[str, Any]:
        runtime = await self.runtime()
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise not_found(f"任务 {task_id} 不存在", task_id=task_id)
            trace_id = task.trace_id
            if resume_payload is None:
                graph_input: Any = self.build_initial_state(task)
                task.status = TaskStatus.RUNNING
                task.finished_at = None
                await session.commit()
            else:
                graph_input = Command(resume=resume_payload)
                task.status = TaskStatus.RUNNING
                await session.commit()

        config = CheckpointService.config(task_id, trace_id=trace_id)
        ctx = StreamContext(task_id=task_id, trace_id=trace_id, bus=self._bus, trace=self._trace)
        token = bind(ctx)
        try:
            # 事件必须绑定在上下文存活期间发布：interrupt / complete 是"终止帧"，
            # 一旦提前 unbind，emit 会静默跳过，前端就永远等不到收尾信号。
            return await self._drive_bound(
                task_id=task_id,
                runtime=runtime,
                config=config,
                ctx=ctx,
                graph_input=graph_input,
                resume_payload=resume_payload,
            )
        finally:
            unbind(token)

    async def _drive_bound(
        self,
        *,
        task_id: int,
        runtime: AgentRuntime,
        config: dict[str, Any],
        ctx: StreamContext,
        graph_input: Any,
        resume_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """真正跑图的段落（此时 ContextVar 已绑定，事件可正常发布）。"""
        cancelled = False
        final_state: dict[str, Any] = {}
        started = time.perf_counter()

        try:
            mode = "resume" if resume_payload is not None else "fresh"
            await emit("state_update", {"phase": "run", "status": TaskStatus.RUNNING.value, "mode": mode})
            async for event in iterate_graph_events(runtime.graph, graph_input, config):
                if await self._handle_event(event, ctx):
                    cancelled = True
                    break
                if await self._cancel.is_set(task_id):
                    cancelled = True
                    break

            snapshot = await load_snapshot(runtime.graph, task_id)
            final_state = dict(getattr(snapshot, "values", None) or {})
            interrupts = collect_interrupts(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 图执行异常 → 落 FAILED + complete 事件
            logger.exception("task_stream_failed", extra={"task_id": task_id})
            # 任务 9：优先保留业务异常自带错误码（如 TOKEN_BUDGET_EXCEEDED / LLM_TIMEOUT）
            fail_code = str(getattr(exc, "code", "") or "TASK_EXECUTION_ERROR")
            await self._finalize_failed(task_id, fail_code, str(exc)[:500])
            await emit(
                "complete",
                {
                    "status": TaskStatus.FAILED.value,
                    "error": {"code": fail_code, "message": brief(exc)},
                },
            )
            return {}

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        if cancelled or await self._cancel.is_set(task_id):
            await self._apply_cancel(task_id, final_state, reason=await self._cancel.reason(task_id))
            return final_state

        if interrupts:
            context = await interrupt_context(
                runtime.graph, task_id, trace_id=ctx.trace_id, task_status=TaskStatus.AWAITING_APPROVAL
            )
            await emit("interrupt", dict(context or {}, phase="awaiting_approval", elapsed_ms=elapsed_ms))
            status = TaskStatus.AWAITING_APPROVAL
        else:
            status = self._resolve_status(final_state, interrupted=False)
            await emit(
                "complete",
                {
                    "status": status.value,
                    "token_usage": total_tokens(final_state),  # type: ignore[arg-type]
                    "artifacts": _artifact_dicts(final_state),
                    "degraded": bool(final_state.get("degraded")),
                    "replan_count": int(final_state.get("replan_count") or 0),
                    "elapsed_ms": elapsed_ms,
                },
            )

        await self._finalize(task_id, final_state, status)
        return final_state

    # ---------------- 事件翻译 ----------------
    async def _handle_event(self, event: dict[str, Any], ctx: StreamContext) -> bool:
        """返回 True 表示需要中断执行（收到取消信号）。"""
        entered = await translate_graph_event(event, ctx)
        return bool(entered) and await self._cancel.is_set(ctx.task_id)

    # ---------------- 取消 ----------------
    async def cancel(self, task_id: int, *, reason: str = "", user_id: int | None = None) -> dict[str, Any]:
        """终止任务：写取消信号 → 清断点 → 置 CANCELLED。"""
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise not_found(f"任务 {task_id} 不存在", task_id=task_id)
            current = normalize_status(task.status)
            trace_id = task.trace_id

        if current is not None and current.value in _TERMINAL_STATUS_VALUES:
            raise conflict(
                TASK_NOT_CANCELABLE,
                "任务已结束，无需取消",
                task_id=task_id,
                status=current.value,
            )

        reason_text = reason or (f"operator:{user_id}" if user_id is not None else "manual_cancel")
        await self._cancel.set(task_id, reason=reason_text)
        runtime = await self.runtime()

        if current == TaskStatus.AWAITING_APPROVAL:
            # 挂起态收口会经 clear_interrupts → aupdate_state 直写 checkpointer，
            # 与 resume 写路径互斥（否则"取消的瞬间有人恢复"会状态竞态）。
            # 抢不到锁 = 有人正在恢复本任务 → 409 TASK_LOCKED。
            snapshot = await load_snapshot(runtime.graph, task_id)
            state = dict(getattr(snapshot, "values", None) or {})
            async with task_lock(
                self._bus.redis_client,
                task_id,
                message="任务正在执行/恢复中，无法取消，请稍后再试",
            ):
                await self._apply_cancel(task_id, state, reason=reason_text, trace_id=trace_id)
                return {
                    "task_id": task_id,
                    "status": TaskStatus.CANCELLED.value,
                    "canceled": True,
                    "reason": reason_text,
                }

        # 运行中：先落状态给出即时反馈，执行体在下一个节点边界自行收口
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is not None:
                task.status = TaskStatus.CANCELLED
                task.error = {"code": "TASK_CANCELED", "message": reason_text[:255], "span_id": None}
                await session.commit()
        await self._publish(
            task_id,
            trace_id,
            "state_update",
            {"phase": "cancel_requested", "status": TaskStatus.CANCELLED.value, "node": None},
        )
        logger.info("task_cancel_requested", extra={"task_id": task_id, "reason": reason_text})
        return {
            "task_id": task_id,
            "status": TaskStatus.CANCELLED.value,
            "canceled": True,
            "reason": reason_text,
            "cancel_requested": True,
        }

    async def _apply_cancel(
        self,
        task_id: int,
        state: dict[str, Any],
        *,
        reason: str | None,
        trace_id: str | None = None,
    ) -> None:
        runtime = await self.runtime()
        await clear_interrupts(runtime.graph, task_id, status=TaskStatus.CANCELLED.value)
        expired = await expire_pending_approvals(task_id, session_factory=self.session_factory)
        reason_text = reason or "任务已取消"
        creator_id: int | None = None
        tokens_used = 0
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is not None:
                task.status = TaskStatus.CANCELLED
                task.error = {"code": "TASK_CANCELED", "message": reason_text[:255], "span_id": None}
                task.finished_at = utcnow()
                if state:
                    task.token_usage = total_tokens(state)  # type: ignore[arg-type]
                # 加固任务 3：commit 前捕获归属与消耗（expire_on_commit 后访问会触发懒加载）
                creator_id = task.creator_id
                tokens_used = int(task.token_usage or 0)
                await session.commit()
                trace_id = trace_id or task.trace_id
        if creator_id is not None and tokens_used > 0:
            # 旁路记账：取消的任务实际烧掉的 Token 也要计入当日预算（fail-open）
            from app.core.daily_quota import record_task_tokens

            await record_task_tokens(creator_id, tokens_used)
        # 加固任务 4：终态清理。CANCELLED 故意**不删** checkpoint——resume 校验
        # （ensure_resumable / interrupt 上下文）与运维追溯仍要读快照，且其有
        # 72h TTL 兜底；这里只清取消信号（终态后永久失去用途）。
        from app.services.task_cleanup import cleanup_task_artifacts

        await cleanup_task_artifacts(task_id, cancel_signal=self._cancel)
        if trace_id:
            await self._trace.record(
                task_id=task_id,
                trace_id=trace_id,
                node="executor",
                event="exit",
                payload={"canceled": True, "reason": reason_text, "expired_approvals": expired},
            )
        await self._publish(
            task_id,
            trace_id,
            "complete",
            {
                "status": TaskStatus.CANCELLED.value,
                "reason": reason_text,
                "expired_approvals": expired,
            },
        )
        await self._cancel.clear(task_id)
        logger.info("task_canceled", extra={"task_id": task_id, "expired_approvals": expired})

    async def _publish(self, task_id: int, trace_id: str | None, event: str, data: dict[str, Any]) -> str | None:
        """不依赖流式上下文的直接发布（取消等"请求侧"动作使用）。

        ``emit`` 依赖 ContextVar（只在图执行期间存在），而取消接口跑在 HTTP 请求协程里，
        没有上下文；若复用 emit，事件会被静默丢弃，前端就等不到终止帧。
        """
        payload = {
            "task_id": task_id,
            "trace_id": trace_id,
            "ts": utcnow().isoformat(),
            **data,
        }
        try:
            return await self._bus.publish(task_id, event, payload, trace_id=trace_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sse_publish_failed", extra={"task_id": task_id, "event": event, "error": str(exc)[:200]})
            return None

    # ---------------- 查询辅助 ----------------
    async def status_of(self, task_id: int) -> TaskStatus:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise not_found(f"任务 {task_id} 不存在", task_id=task_id)
            return normalize_status(task.status) or TaskStatus.PLANNING

    async def interrupt_of(self, task_id: int, *, trace_id: str | None = None) -> dict[str, Any]:
        runtime = await self.runtime()
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise not_found(f"任务 {task_id} 不存在", task_id=task_id)
            status = normalize_status(task.status)
            trace_id = trace_id or task.trace_id
        context = await interrupt_context(runtime.graph, task_id, trace_id=trace_id, task_status=status)
        if context is None:
            return {
                "paused": False,
                "task_id": task_id,
                "trace_id": trace_id,
                "status": status.value if status else None,
                "next_node": [],
                "resumable": False,
            }
        if status != TaskStatus.AWAITING_APPROVAL:
            # checkpoint 有断点但 DB 已终态（如已取消）→ 对外一律按"不可恢复"呈现
            context["paused"] = False
            context["resumable"] = False
        return context

    async def ensure_interrupt(self, task_id: int) -> dict[str, Any]:
        context = await self.interrupt_of(task_id)
        if not context.get("paused"):
            raise conflict(INTERRUPT_MISSING, "当前任务没有挂起的审批断点", task_id=task_id)
        return context


# ---------------------------------------------------------------------------
# SSE 帧生成（HTTP 层只做包装，便于单测直接驱动）
# ---------------------------------------------------------------------------
async def sse_event_frames(
    *,
    bus: EventBus,
    task_id: int,
    trace_id: str | None = None,
    start_id: str | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    block_ms: int | None = None,
    max_duration: float | None = None,
    idle_timeout: float | None = None,
):
    """把事件总线上的事件渲染成 SSE 文本帧。

    - 首帧发 ``retry:`` 提示浏览器重连间隔；
    - 无事件时发 ``: ping`` 注释帧保活（不触发 message 事件）；
    - 收到 ``interrupt`` / ``complete`` 立即收尾（前端据此关闭 EventSource，审批后重连）；
    - 客户端断开 / 空闲超时 / 总时长超限都会安静退出，不抛异常。
    """
    cursor = start_id or START_ID
    block = w3.SSE_BLOCK_MS if block_ms is None else block_ms
    max_seconds = w3.SSE_MAX_DURATION_SECONDS if max_duration is None else max_duration
    idle_seconds = w3.SSE_IDLE_TIMEOUT_SECONDS if idle_timeout is None else idle_timeout
    started = time.monotonic()
    last_activity = started
    first_frame = True

    yield f": connected task_id={task_id} trace_id={trace_id or '-'}\n\n"

    while True:
        if is_disconnected is not None:
            try:
                if await is_disconnected():
                    logger.info("sse_client_disconnected", extra={"task_id": task_id})
                    return
            except Exception:  # noqa: BLE001 - 断开探测失败按已断开处理
                return

        try:
            events = await bus.read(task_id, last_id=cursor, count=w3.SSE_READ_COUNT, block_ms=block)
        except Exception as exc:  # noqa: BLE001 - 总线异常不能 500，只收尾
            logger.warning("sse_read_failed", extra={"task_id": task_id, "error": str(exc)[:200]})
            return

        now = time.monotonic()
        if events:
            for event in events:
                cursor = event.id
                yield event.to_sse(retry_ms=w3.SSE_RETRY_MS if first_frame else None)
                first_frame = False
                if event.event in TERMINAL_EVENTS:
                    logger.info(
                        "sse_stream_finished",
                        extra={"task_id": task_id, "terminal": event.event, "cursor": cursor},
                    )
                    return
            last_activity = now
        else:
            yield ": ping\n\n"
            if block <= 0:
                await asyncio.sleep(max(0.01, w3.SSE_POLL_INTERVAL_MS / 1000))
            if now - last_activity > idle_seconds:
                logger.info("sse_idle_timeout", extra={"task_id": task_id})
                return

        if time.monotonic() - started > max_seconds:
            logger.info("sse_max_duration", extra={"task_id": task_id})
            return


__all__ = [
    "StreamingTaskService",
    "iterate_graph_events",
    "version_candidates",
    "sse_event_frames",
]
