"""任务服务（PRD FR-001 / FR-012）：创建任务 → 组装初始 TaskState → 驱动 graph。

职责边界：
- 任务落库（tasks / subtasks）与终态回写都在本服务，**节点保持无副作用**；
- graph 执行通过 checkpointer 的 ``thread_id = task_id`` 关联快照，进程重启后可恢复（W3 暴露 resume 接口）；
- 后台执行使用独立 Session（不复用请求 Session），避免请求结束后连接被回收。

状态收口（一条任务只有一个终态）：
    PLANNING → (AWAITING_APPROVAL) → RUNNING → SUMMARIZING → COMPLETED / FAILED
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.common import results_of, total_tokens
from app.agents.graph import AgentRuntime, create_runtime, pending_interrupts
from app.core.exceptions import AppException, ErrorCode
from app.core.guardrails import intent_guardrail, sanitize_user_input
from app.core.logging import get_logger
from app.schemas.enums import SubtaskStatus, TaskStatus
from app.schemas.graph_state import TaskState, TimeRange
from app.schemas.orm_models import Subtask, Task, TaskExecution
from app.services.checkpoint_service import CheckpointService
from app.services.plan_service import PlanService
from app.utils.ids import new_task_no, new_trace_id
from app.utils.time import utcnow

logger = get_logger(__name__)

GOAL_MAX_LEN = 500
# 提示注入黑名单（PRD FR-001 验收 2 / NFR-S5）
# 任务 10：条目已全部收编进 app/core/guardrails.py 的 JAILBREAK_PATTERNS（正则版），
# 拦截逻辑统一走 sanitize_user_input；本元组保留导出仅为兼容既有引用，不再参与匹配。
INJECTION_BLACKLIST: tuple[str, ...] = (
    "忽略之前所有指令",
    "忽略以上指令",
    "忽略前面的",
    "ignore previous instructions",
    "ignore all previous",
    "system prompt",
    "开发者模式",
    "越狱",
    "dan mode",
)

_runtime: AgentRuntime | None = None
_runtime_lock = asyncio.Lock()


async def get_runtime() -> AgentRuntime:
    """进程内共享 runtime（4 个 MCP 子进程只起一次）。"""
    global _runtime
    if _runtime is not None:
        return _runtime
    async with _runtime_lock:
        if _runtime is None:
            _runtime = await create_runtime()
        return _runtime


async def reset_runtime() -> None:
    global _runtime
    if _runtime is not None:
        await _runtime.aclose()
        _runtime = None


class TaskService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        runtime: AgentRuntime | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._runtime = runtime

    # ---------------- 基础设施 ----------------
    @property
    def bus(self) -> Any:
        """事件总线（惰性单例）：非 SSE 入口也要建，否则轨迹/事件无处可写。"""
        if getattr(self, "_bus", None) is None:
            from app.services.event_bus import EventBus

            self._bus = EventBus()
        return self._bus

    @property
    def trace(self) -> Any:
        if getattr(self, "_trace", None) is None:
            from app.services.trace_service import TraceService

            self._trace = TraceService(self._session_factory)
        return self._trace

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is not None:
            return self._session_factory
        from app.db.session import get_session_factory

        return get_session_factory()

    async def runtime(self) -> AgentRuntime:
        return self._runtime or await get_runtime()

    # ---------------- 校验 ----------------
    @staticmethod
    def validate_goal(goal: str) -> str:
        text = (goal or "").strip()
        if not text:
            raise AppException(ErrorCode.TASK_INVALID, "目标不能为空", detail={"reason": "GOAL_EMPTY"})
        if len(text) > GOAL_MAX_LEN:
            raise AppException(
                ErrorCode.TASK_INVALID,
                f"目标超过 {GOAL_MAX_LEN} 字",
                detail={"reason": "GOAL_TOO_LONG", "length": len(text)},
            )
        # 任务 10：注入拦截收编进 guardrails.sanitize_user_input——
        # 原 INJECTION_BLACKLIST 子串匹配升级为正则（挡大小写/空格/变体），
        # 黑名单条目全部收编进 JAILBREAK_PATTERNS，错误码统一为 INVALID_INPUT(400)
        return sanitize_user_input(text)

    @staticmethod
    def validate_time_range(value: TimeRange) -> TimeRange:
        if value.start > value.end:
            raise AppException(
                ErrorCode.TASK_INVALID,
                "时间范围非法：start 晚于 end",
                detail={"reason": "TIME_RANGE_INVALID"},
            )
        if (value.end - value.start).days > 366:
            raise AppException(
                ErrorCode.TASK_INVALID,
                "时间范围跨度过大（>366 天），请缩小",
                detail={"reason": "TIME_RANGE_TOO_WIDE"},
            )
        return value

    # ---------------- 创建 ----------------
    async def create_task(
        self,
        *,
        goal: str,
        time_range: TimeRange,
        creator_id: int,
        sync: bool = False,
        compress: bool = False,
        use_memory: bool = False,
        eval_tag: str | None = None,
    ) -> dict[str, Any]:
        # 任务 10：validate_goal 内部已委托 guardrails.sanitize_user_input（空值/长度校验保留）；
        # 改进 3：意图护栏兜底（正则特征组合，绕过第一道清洗的复杂变种在此拦截）
        goal_text = self.validate_goal(goal)
        await intent_guardrail.check(goal_text)
        window = self.validate_time_range(time_range)

        async with self.session_factory() as session:
            task_no = await self._next_task_no(session)
            task = Task(
                task_no=task_no,
                creator_id=creator_id,
                goal=goal_text,
                time_range={"start": window.start.isoformat(), "end": window.end.isoformat()},
                status=TaskStatus.PLANNING,
                replan_count=0,
                token_usage=0,
                trace_id=new_trace_id(),
                compress=bool(compress),
                use_memory=bool(use_memory),
                eval_tag=(eval_tag or "").strip() or None,
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id, trace_id = task.id, task.trace_id

        logger.info("task_created", extra={"task_id": task_id, "trace_id": trace_id, "creator_id": creator_id})

        if sync:
            await self.run(task_id)
        else:
            asyncio.create_task(self._run_safely(task_id))

        return {"task_id": task_id, "trace_id": trace_id, "status": TaskStatus.PLANNING.value, "task_no": task_no}

    async def _run_safely(self, task_id: int) -> None:
        try:
            await self.run(task_id)
        except Exception:  # noqa: BLE001 - 后台任务异常必须落库留痕
            logger.exception("task_background_failed", extra={"task_id": task_id})

    async def _next_task_no(self, session: AsyncSession) -> str:
        today = date.today().strftime("%Y%m%d")
        prefix = f"AF{today}-"
        total = (
            await session.execute(select(func.count(Task.id)).where(Task.task_no.like(f"{prefix}%")))
        ).scalar_one()
        return new_task_no(int(total) + 1)

    # ---------------- 执行 ----------------
    def _bind_offline_context(self, task_id: int, trace_id: str) -> tuple[Any, Any]:
        """给非流式执行绑定任务上下文，返回 (ctx, token)；已有上下文时不重复绑定。"""
        from app.services.stream_context import StreamContext, bind, current_stream

        existing = current_stream.get()
        if existing is not None:
            return existing, None
        ctx = StreamContext(task_id=task_id, trace_id=trace_id, bus=self.bus, trace=self.trace)
        return ctx, bind(ctx)

    @staticmethod
    def _unbind_offline_context(token: Any) -> None:
        if token is None:
            return
        from app.services.stream_context import unbind

        unbind(token)

    def build_initial_state(self, task: Task) -> TaskState:
        window = task.time_range or {}
        return TaskState(
            task_id=task.id,
            trace_id=task.trace_id,
            creator_id=task.creator_id,
            goal=task.goal,
            time_range={"start": window.get("start"), "end": window.get("end")},
            memory_hits=[],  # use_memory=True 时由 memory_recall 节点填充
            plan=[],
            subtask_results={},
            replan_count=int(task.replan_count or 0),
            approvals={},
            messages=[],
            token_ledger={},
            context_digest="",
            artifacts=[],
            status=TaskStatus.PLANNING.value,
            compress=bool(task.compress),
            use_memory=bool(task.use_memory),
            eval_tag=task.eval_tag,
        )

    async def run(self, task_id: int) -> dict[str, Any]:
        runtime = await self.runtime()
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise AppException(ErrorCode.NOT_FOUND, f"任务 {task_id} 不存在")
            initial = self.build_initial_state(task)
            trace_id = task.trace_id

        config = CheckpointService.config(task_id, trace_id=trace_id)
        final_state: dict[str, Any]
        # 轨迹 / 事件都依赖 ContextVar 里的任务上下文，节点级 enter/exit 更要消费事件流
        # 才拿得到。此前只有 SSE 入口做这两件事，普通 POST /tasks 创建的任务跑完后
        # task_executions 一条都没有——详情页轨迹时间线整页空白。这里补齐。
        ctx, token = self._bind_offline_context(task_id, trace_id)
        try:
            from app.services.stream_service import iterate_graph_events, translate_graph_event

            try:
                async for event in iterate_graph_events(runtime.graph, initial, config):
                    await translate_graph_event(event, ctx)
                snapshot = await runtime.graph.aget_state(config)
                final_state = dict(getattr(snapshot, "values", None) or {})
            except Exception as exc:  # noqa: BLE001 - 图执行异常统一落 FAILED
                logger.exception("task_graph_failed", extra={"task_id": task_id})
                # 任务 9：优先保留业务异常自带错误码（如 TOKEN_BUDGET_EXCEEDED / LLM_TIMEOUT）
                fail_code = str(getattr(exc, "code", "") or "TASK_EXECUTION_ERROR")
                await self._finalize_failed(task_id, fail_code, str(exc)[:500])
                from app.services.stream_context import emit

                await emit(
                    "complete",
                    {
                        "status": TaskStatus.FAILED.value,
                        "error": {"code": fail_code, "message": str(exc)[:300]},
                    },
                )
                raise

            # 快照才含 __interrupt__：ainvoke/astream 的返回值都不带，必须查状态
            interrupts = await pending_interrupts(runtime.graph, config)
            interrupted = bool(interrupts)
            if interrupted:
                logger.info(
                    "task_awaiting_approval",
                    extra={"task_id": task_id, "interrupts": len(interrupts)},
                )
            status = self._resolve_status(final_state, interrupted=interrupted)
            # 终止帧必须发：SSE 前端只认 complete/interrupt 收尾，缺了会一直停在"运行中"，
            # 直到空闲超时断开（此前非流式入口不发这两个事件，就是这个表现）。
            await self._emit_terminal(task_id, ctx, runtime, status, final_state, interrupted)
            await self._finalize(task_id, final_state, status)
        finally:
            self._unbind_offline_context(token)

        logger.info(
            "task_finished",
            extra={
                "task_id": task_id,
                "status": status,
                "replan_count": final_state.get("replan_count", 0),
                "tokens": total_tokens(final_state),
            },
        )
        return final_state

    async def _emit_terminal(
        self,
        task_id: int,
        ctx: Any,
        runtime: AgentRuntime,
        status: TaskStatus,
        final_state: dict[str, Any],
        interrupted: bool,
    ) -> None:
        """向事件总线补发终止帧（interrupt / complete），与流式路径保持一致。"""
        from app.services.stream_context import emit

        if interrupted:
            from app.services.stream_service import interrupt_context

            context = await interrupt_context(
                runtime.graph, task_id, trace_id=ctx.trace_id, task_status=TaskStatus.AWAITING_APPROVAL
            )
            await emit("interrupt", dict(context or {}, phase="awaiting_approval"))
            return

        from app.services.stream_service import artifact_dicts

        await emit(
            "complete",
            {
                "status": status.value,
                "token_usage": total_tokens(final_state),
                "artifacts": artifact_dicts(final_state),
                "degraded": bool(final_state.get("degraded")),
                "replan_count": int(final_state.get("replan_count") or 0),
                "elapsed_ms": getattr(ctx, "elapsed_ms", 0),
            },
        )
        return final_state

    @staticmethod
    def _resolve_status(state: dict[str, Any], *, interrupted: bool) -> TaskStatus:
        if interrupted:
            return TaskStatus.AWAITING_APPROVAL
        if state.get("status") == TaskStatus.FAILED.value or state.get("error"):
            return TaskStatus.FAILED
        if state.get("summary"):
            return TaskStatus.COMPLETED
        return TaskStatus.FAILED

    async def _finalize(self, task_id: int, state: dict[str, Any], status: TaskStatus) -> None:
        plan_service = PlanService()
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return

            from app.agents.common import plan_of

            plan = plan_of(state)  # type: ignore[arg-type]
            seq_to_id: dict[int, int] = {}
            if plan:
                seq_to_id = await plan_service.persist_plan(session, task_id, plan)
            if seq_to_id:
                await self._backfill_subtask_ids(session, task_id, seq_to_id)

            results = results_of(state)  # type: ignore[arg-type]
            if results:
                await plan_service.sync_results(session, task_id, results)

            task.status = status
            task.replan_count = min(int(state.get("replan_count") or 0), 2)
            task.token_usage = total_tokens(state)  # type: ignore[arg-type]
            # 加固任务 3：commit 前捕获归属与消耗（expire_on_commit 后访问会触发懒加载）
            creator_id = task.creator_id
            tokens_used = int(task.token_usage or 0)
            error = state.get("error")
            if error:
                task.error = {
                    "code": error.get("code"),
                    "message": error.get("message"),
                    "span_id": error.get("span_id"),
                }
            if status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                task.finished_at = utcnow()
                # 终态报告快照落库（见 orm_models.Task.result_payload 注释）：
                # 必须在 cleanup_task_artifacts 删除 Redis 快照之前写进 MySQL，
                # 否则详情页的摘要/评审/产物面板永远为空。任何序列化异常都不能影响终态落库。
                try:
                    task.result_payload = _jsonable_payload(state)
                except Exception as exc:  # noqa: BLE001 - 快照序列化失败只损失展示
                    logger.warning("task_result_payload_skipped", extra={"task_id": task_id, "error": str(exc)[:200]})
            await session.commit()
            logger.info("task_status_saved", extra={"task_id": task_id, "status": status.value})

        # 旁路动作放在事务外：日配额记账失败绝不影响任务状态（fail-open）
        if creator_id is not None and tokens_used > 0:
            from app.core.daily_quota import record_task_tokens

            await record_task_tokens(creator_id, tokens_used)

        # 记忆归档放在事务外：它是旁路增强，失败绝不能回滚任务状态
        await self._finalize_memory(task_id, state, status)

        # 加固任务 4：终态清理放在**最后**——MySQL 持久化成功 + 旁路动作收尾后，
        # 才回收 Redis 侧临时产物。只在真终态触发：AWAITING_APPROVAL 必须保留
        # 快照供 resume；CANCELLED 走 _apply_cancel 自己的清理口径。
        if status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            from app.services.cancel_service import CancelSignal
            from app.services.task_cleanup import cleanup_task_artifacts

            try:
                runtime = await self.runtime()
                checkpointer = getattr(getattr(runtime, "graph", None), "checkpointer", None)
            except Exception:  # noqa: BLE001 - 运行时不可用则交给 72h TTL 兜底
                checkpointer = None
            await cleanup_task_artifacts(task_id, checkpointer=checkpointer, cancel_signal=CancelSignal())

    async def _finalize_memory(self, task_id: int, state: dict[str, Any], status: TaskStatus) -> None:
        """分层记忆收尾：短期落 Redis、中期写摘要、长期按评审分归档。

        只有在 ``ENABLE_LAYERED_MEMORY=true`` 时才动作；关闭时直接返回，
        保持 W1~W4 行为逐字节不变（该开关的原始设计约束）。
        """
        try:
            from app.core.w5_config import layered_memory_enabled

            if not layered_memory_enabled():
                return

            from app.memory.layered_memory import LayeredMemoryService, review_score_of

            memory = LayeredMemoryService(session_factory=self.session_factory)
            if not memory.enabled:
                return
            user_id = state.get("creator_id")
            messages = [m for m in (state.get("messages") or []) if isinstance(m, dict)]

            # 4.1 短期层：会话原文进 Redis List（TTL 24h，超长自动裁剪）
            for message in messages:
                await memory.append_short(task_id, message, user_id=user_id)

            if status != TaskStatus.COMPLETED:
                logger.info("memory_finalize_skipped", extra={"task_id": task_id, "status": status.value})
                return

            # 4.2 / 4.3 中期摘要 + 长期归档（评审分不足时 archive_long 自身会跳过）
            summary = str(state.get("context_digest") or "")
            artifacts = state.get("artifacts") or []
            if not summary and artifacts:
                summary = json.dumps(artifacts[:2], ensure_ascii=False, default=str)
            result = await memory.finalize_task(
                task_id=task_id,
                user_id=user_id,
                goal=str(state.get("goal") or ""),
                messages=messages,
                summary=summary or None,
                review_score=review_score_of(state),
            )
            logger.info(
                "memory_finalized",
                extra={
                    "task_id": task_id,
                    "mid_id": result.get("mid_id"),
                    "long_id": result.get("long_id"),
                    "review_score": result.get("review_score"),
                },
            )
        except Exception as exc:  # noqa: BLE001 - 记忆是旁路，故障只留痕
            logger.warning("memory_finalize_failed", extra={"task_id": task_id, "error": str(exc)[:200]})

    @staticmethod
    async def _backfill_subtask_ids(
        session: AsyncSession, task_id: int, seq_to_id: dict[int, int]
    ) -> int:
        """G7：把执行期记录的子任务序号回填成 ``subtasks.id``。

        执行期计划还没落库（`persist_plan` 在收尾时才跑），轨迹行拿不到主键，只能把
        ``subtask_seq`` 记在 payload 里；拿到 seq→id 映射后在这里一次补齐，
        Trace 页即可按 ``subtask_id`` 精确归属子任务↔工具调用。
        只在 ``subtask_id IS NULL`` 的行上补，不覆盖已有值。
        """
        rows = (
            (
                await session.execute(
                    select(TaskExecution).where(
                        TaskExecution.task_id == task_id,
                        TaskExecution.subtask_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        patched = 0
        for row in rows:
            seq = (row.payload or {}).get("subtask_seq")
            if isinstance(seq, int) and seq in seq_to_id:
                row.subtask_id = seq_to_id[seq]
                patched += 1
        if patched:
            logger.info("trace_subtask_backfilled", extra={"task_id": task_id, "rows": patched})
        return patched

    async def _finalize_failed(self, task_id: int, code: str, message: str) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            task.status = TaskStatus.FAILED
            task.error = {"code": code, "message": message, "span_id": None}
            task.finished_at = utcnow()
            await session.commit()

    # ---------------- 查询 ----------------
    async def get_detail(self, task_id: int) -> dict[str, Any]:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise AppException(ErrorCode.NOT_FOUND, f"任务 {task_id} 不存在")
            subtasks = (
                (await session.execute(select(Subtask).where(Subtask.task_id == task_id).order_by(Subtask.seq)))
                .scalars()
                .all()
            )
            detail = {
                "task_id": task.id,
                "task_no": task.task_no,
                "goal": task.goal,
                "time_range": task.time_range,
                "status": task.status.value,
                "replan_count": task.replan_count,
                "token_usage": task.token_usage,
                "trace_id": task.trace_id,
                "creator_id": task.creator_id,
                "compress": bool(task.compress),
                "use_memory": bool(task.use_memory),
                "eval_tag": task.eval_tag,
                "error": task.error,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "finished_at": task.finished_at.isoformat() if task.finished_at else None,
                # 必须在 session 内取（expire_on_commit / 会话关闭后访问会触发懒加载报错）
                "_result_payload": dict(task.result_payload or {}),
                "subtasks": [
                    {
                        "id": row.id,
                        "seq": row.seq,
                        "name": row.name,
                        "description": row.description,
                        "tool_hint": row.tool_hint.value if hasattr(row.tool_hint, "value") else str(row.tool_hint),
                        "depends_on": list(row.depends_on or []),
                        "risk_level": row.risk_level.value if hasattr(row.risk_level, "value") else str(row.risk_level),
                        "status": row.status.value if hasattr(row.status, "value") else str(row.status),
                        "result_summary": row.result_summary,
                        "retry_count": row.retry_count,
                    }
                    for row in subtasks
                ],
            }

        # 快照里的运行期信息（summary / review / token_ledger）——失败不影响主查询
        # 注意顺序：加固任务 4 在终态后会删除 Redis 快照，此时 aget_state 返回空 state，
        # 必须回退到 tasks.result_payload（终态落库的那一份），否则详情页摘要/评审/产物永远为空。
        detail["summary"] = ""
        detail["review"] = None
        detail["review_intervention"] = None
        detail["eval_score"] = None
        detail["token_ledger"] = {}
        detail["approvals"] = {}
        detail["artifacts"] = []
        detail["payload_source"] = "checkpoint"
        try:
            runtime = await self.runtime()
            state = await CheckpointService(runtime.graph).restore(task_id)
            detail["summary"] = state.get("summary") or ""
            detail["review"] = state.get("review")
            # v1.1：REJECT 人工批注记录（未触发时为 None），供详情页展示"谁批注了什么"
            detail["review_intervention"] = state.get("review_intervention")
            detail["eval_score"] = state.get("eval_score")
            detail["token_ledger"] = state.get("token_ledger") or {}
            detail["approvals"] = state.get("approvals") or {}
            detail["artifacts"] = [
                a if isinstance(a, dict) else getattr(a, "model_dump", lambda **_: a)(mode="json")
                for a in (state.get("artifacts") or [])
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("task_detail_state_unavailable", extra={"task_id": task_id, "error": str(exc)[:200]})

        # 快照缺失/已被清理（终态清理策略）→ 用 MySQL 里的终态报告兜底
        stored: dict[str, Any] = detail.pop("_result_payload", None) or {}
        if stored and (not detail["summary"] or not detail["artifacts"]):
            detail["summary"] = detail["summary"] or stored.get("summary") or ""
            detail["review"] = detail["review"] or stored.get("review")
            detail["review_intervention"] = detail["review_intervention"] or stored.get("review_intervention")
            detail["eval_score"] = detail["eval_score"] if detail["eval_score"] is not None else stored.get("eval_score")
            detail["token_ledger"] = detail["token_ledger"] or stored.get("token_ledger") or {}
            detail["approvals"] = detail["approvals"] or stored.get("approvals") or {}
            detail["artifacts"] = detail["artifacts"] or list(stored.get("artifacts") or [])
            detail["payload_source"] = "mysql_fallback"

        return detail

    async def find_task(self, task_id: int) -> Task | None:
        async with self.session_factory() as session:
            return await session.get(Task, task_id)

    async def get_detail_by_ref(self, task_ref: str) -> dict[str, Any]:
        """按「数字主键 或 任务编号」查详情。

        前端各列表（工作台/看板/收藏）一律只展示 ``task_no``（AF2026xxxx-xxxx），
        数字主键 ``task.id`` 从不渲染——单任务评测分查询如果只收 int，用户拿着
        界面上唯一可见的编号却查不到。数字 → 主键直查；其余 → 按 task_no 查。
        """
        ref = task_ref.strip()
        if ref.isdigit():
            return await self.get_detail(int(ref))
        async with self.session_factory() as session:
            task = (
                await session.execute(select(Task).where(Task.task_no == ref))
            ).scalar_one_or_none()
        if task is None:
            raise AppException(ErrorCode.NOT_FOUND, f"任务 {ref} 不存在")
        return await self.get_detail(task.id)


def _jsonable_payload(state: dict[str, Any]) -> dict[str, Any]:
    """抽取终态展示字段并转成可 JSON 序列化的结构（写 Task.result_payload 用）。

    只保留详情页真正渲染的键，避免把整份 state（含 messages / 原始工具结果）写进 DB。
    """
    from fastapi.encoders import jsonable_encoder

    payload: dict[str, Any] = {}
    for key in ("summary", "review", "review_intervention", "eval_score", "token_ledger", "approvals", "artifacts"):
        value = state.get(key)
        if value in (None, "", [], {}):
            continue
        try:
            payload[key] = jsonable_encoder(value)
        except Exception:  # noqa: BLE001 - 单字段编码失败降级为字符串，不阻断落库
            payload[key] = str(value)[:2000]
    return payload


__all__ = [
    "GOAL_MAX_LEN",
    "INJECTION_BLACKLIST",
    "TaskService",
    "get_runtime",
    "reset_runtime",
    "SubtaskStatus",
]
