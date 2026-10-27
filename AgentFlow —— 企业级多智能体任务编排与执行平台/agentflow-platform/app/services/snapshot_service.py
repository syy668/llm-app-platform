"""快照与轨迹回放（FR-015）：把 checkpoint 历史 + task_executions 组装成前端可回放的数据。

两个接口：
- ``GET /tasks/{id}/snapshot``：当前状态快照（运行期实时状态，含 next / interrupt）；
- ``GET /tasks/{id}/trace``：全量回放（span 序列 + checkpoint 时间轴）。

只读实现，绝不调用 ``aupdate_state`` / ``invoke``，保证"回放不影响正在跑的任务"。
"""

from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder

from app.core.logging import get_logger
from app.core.w3_config import w3
from app.schemas.w3 import isoformat
from app.services.checkpoint_service import CheckpointService
from app.services.interrupt_service import collect_interrupts
from app.services.trace_service import TraceService

logger = get_logger(__name__)


def _jsonable(values: dict[str, Any]) -> dict[str, Any]:
    """把 State（含 Pydantic 模型 / datetime / 枚举）转成纯 JSON 结构。"""
    try:
        return jsonable_encoder(values, custom_encoder={bytes: lambda b: f"<{len(b)} bytes>"})
    except Exception:  # noqa: BLE001 - 编码失败退回字符串化，保证接口不 500
        import json

        try:
            return json.loads(json.dumps(values, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            return {"_unserializable": True}


def _plan_stats(values: dict[str, Any]) -> tuple[int, int]:
    plan = values.get("plan") or []
    results = values.get("subtask_results") or {}
    done = 0
    for item in results.values():
        status = getattr(item, "status", None) if not isinstance(item, dict) else item.get("status")
        if str(status) == "DONE":
            done += 1
    return len(plan), done


class SnapshotService:
    """状态快照 + 轨迹回放。"""

    def __init__(
        self,
        graph: Any,
        *,
        trace: TraceService | None = None,
        session_factory: Any = None,
    ) -> None:
        self.graph = graph
        self.trace = trace or TraceService()
        # 加固任务 4：终态任务的 checkpointer 快照已被清理（MySQL 为权威），
        # snapshot 需要回退 DB 状态，所以这里要能拿到 session_factory
        self.session_factory = session_factory

    async def _db_status(self, task_id: int | str) -> str | None:
        """读 MySQL 里的任务状态（快照缺失时的权威回退；fail-open 返回 None）。"""
        if self.session_factory is None:
            return None
        try:
            from app.schemas.orm_models import Task

            async with self.session_factory() as session:
                task = await session.get(Task, int(task_id))
                return task.status.value if task is not None else None
        except Exception:  # noqa: BLE001 - 回退失败不阻塞快照返回
            return None

    async def _db_fallback_values(self, task_id: int | str) -> dict[str, Any]:
        """终态任务快照已清理后的 MySQL 平替数据（加固任务 4 收尾）。

        把任务对象 + 子任务列表填进 ``values``，前端"原始快照 JSON 查看器"无需
        任何改动即可渲染有意义的结构（字段带 ``_source: mysql_fallback`` 标记）。
        fail-open：任何异常返回空 dict，只损失查看器内容。
        """
        if self.session_factory is None:
            return {}
        try:
            from sqlalchemy import select

            from app.schemas.orm_models import Subtask, Task

            async with self.session_factory() as session:
                task = await session.get(Task, int(task_id))
                if task is None:
                    return {}
                subtasks = (
                    (await session.execute(select(Subtask).where(Subtask.task_id == int(task_id)).order_by(Subtask.seq.asc())))
                    .scalars()
                    .all()
                )
                return {
                    "_source": "mysql_fallback",
                    "_note": "Redis 快照已按终态清理策略删除，以下为 MySQL 权威数据平替",
                    "goal": task.goal,
                    "status": task.status.value,
                    "error": task.error,
                    "token_usage": int(task.token_usage or 0),
                    "replan_count": int(task.replan_count or 0),
                    "created_at": task.created_at.isoformat() if task.created_at else None,
                    "finished_at": task.finished_at.isoformat() if task.finished_at else None,
                    "plan": [
                        {
                            "seq": s.seq,
                            "name": s.name,
                            "description": s.description,
                            "tool_hint": s.tool_hint.value,
                            "risk_level": s.risk_level.value,
                            "status": s.status.value,
                            "result_summary": s.result_summary,
                        }
                        for s in subtasks
                    ],
                }
        except Exception:  # noqa: BLE001 - 平替失败不阻塞快照返回
            return {}

    async def snapshot(self, task_id: int | str, *, trace_id: str | None = None) -> dict[str, Any]:
        config = CheckpointService.config(task_id)
        snapshot = await self.graph.aget_state(config)

        raw_values = dict(getattr(snapshot, "values", None) or {})
        messages = raw_values.get("messages") or []
        values = _jsonable({k: v for k, v in raw_values.items() if k != "messages"})
        recent = _jsonable(messages[-w3.SNAPSHOT_RECENT_MESSAGES :]) if messages else []
        if recent:
            values["recent_messages"] = recent

        interrupts = collect_interrupts(snapshot)
        # 加固任务 4：快照已清理的终态任务，status 回退 MySQL 权威状态；
        # 平替方案（加固任务 4 收尾）：values 用 DB 的任务对象+子任务列表填充，
        # 前端"原始快照查看器"无需改动即可展示 MySQL 权威数据
        status_value = values.get("status")
        if not raw_values:
            fallback = await self._db_fallback_values(task_id)
            if fallback:
                values.update(fallback)
                status_value = values.get("status") or status_value
            elif status_value is None:
                status_value = await self._db_status(task_id)
        elif status_value is None:
            status_value = await self._db_status(task_id)
        return {
            "task_id": int(task_id),
            "trace_id": trace_id or raw_values.get("trace_id"),
            "status": status_value,
            "next_nodes": [str(n) for n in (getattr(snapshot, "next", ()) or ())],
            "created_at": isoformat(getattr(snapshot, "created_at", None)),
            "step": (getattr(snapshot, "metadata", None) or {}).get("step"),
            "values": values,
            "messages_count": len(messages),
            "interrupt": interrupts[0] if interrupts else None,
            "interrupt_count": len(interrupts),
        }

    async def checkpoints(self, task_id: int | str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """checkpoint 时间轴（按 step 升序）。"""
        config = CheckpointService.config(task_id)
        max_items = limit or w3.TRACE_REPLAY_MAX_SNAPSHOTS
        timeline: list[dict[str, Any]] = []
        try:
            async for snapshot in self.graph.aget_state_history(config):
                values = dict(getattr(snapshot, "values", None) or {})
                plan_size, done = _plan_stats(values)
                metadata = getattr(snapshot, "metadata", None) or {}
                timeline.append(
                    {
                        "step": metadata.get("step"),
                        "source": metadata.get("source"),
                        "status": values.get("status"),
                        "next_nodes": [str(n) for n in (getattr(snapshot, "next", ()) or ())],
                        "plan_size": plan_size,
                        "done_count": done,
                        "created_at": isoformat(getattr(snapshot, "created_at", None)),
                    }
                )
                if len(timeline) >= max_items:
                    break
        except Exception as exc:  # noqa: BLE001 - 后端不支持 history 时降级为空时间轴
            logger.warning("checkpoint_history_unavailable", extra={"task_id": task_id, "error": str(exc)[:200]})
            return []
        timeline.sort(key=lambda item: (item.get("step") if item.get("step") is not None else -1))
        return timeline

    async def replay(self, task_id: int | str, *, trace_id: str | None = None, after_id: int = 0) -> dict[str, Any]:
        steps = await self.trace.list_steps(task_id=int(task_id), after_id=after_id)
        checkpoints = await self.checkpoints(task_id)
        try:
            snapshot = await self.snapshot(task_id, trace_id=trace_id)
            interrupt = snapshot.get("interrupt")
            resolved_trace_id = trace_id or snapshot.get("trace_id")
        except Exception as exc:  # noqa: BLE001
            logger.warning("replay_snapshot_failed", extra={"task_id": task_id, "error": str(exc)[:200]})
            interrupt = None
            resolved_trace_id = trace_id
        return {
            "task_id": int(task_id),
            "trace_id": resolved_trace_id,
            "steps": steps,
            "checkpoints": checkpoints,
            "interrupt": interrupt,
            "total_steps": len(steps),
        }


__all__ = ["SnapshotService"]
