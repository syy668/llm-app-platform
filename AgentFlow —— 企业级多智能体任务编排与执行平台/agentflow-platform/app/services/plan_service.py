"""规划服务（PRD FR-002 / FR-003）：DAG 校验 + 子任务持久化 + 结果回写。

节点内部**不直接写库**（保持纯函数、可单测、可重放）；落库统一由本服务在
graph 执行前后完成，避免"节点半途崩溃留下脏数据"。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.schemas.agent_io import PlanValidationReport
from app.schemas.enums import SubtaskStatus
from app.schemas.graph_state import SubtaskPlan, SubtaskResult
from app.schemas.orm_models import Subtask
from app.utils.dag import validate_plan

logger = get_logger(__name__)


class PlanService:
    def __init__(self, session: AsyncSession | None = None) -> None:
        self.session = session

    # ---------------- 校验 ----------------
    @staticmethod
    def validate(subtasks: list[SubtaskPlan], *, min_subtasks: int = 1) -> PlanValidationReport:
        return validate_plan(subtasks, min_subtasks=min_subtasks, max_subtasks=15)

    # ---------------- 持久化 ----------------
    async def persist_plan(self, session: AsyncSession, task_id: int, plan: list[SubtaskPlan]) -> dict[int, int]:
        """按 (task_id, seq) upsert 子任务，返回 seq -> subtask.id。"""
        stmt = select(Subtask).where(Subtask.task_id == task_id)
        existing = {item.seq: item for item in (await session.execute(stmt)).scalars().all()}

        seq_to_id: dict[int, int] = {}
        for item in plan:
            row = existing.get(item.seq)
            if row is None:
                row = Subtask(task_id=task_id, seq=item.seq, name=item.name[:128])
                session.add(row)
            row.name = item.name[:128]
            row.description = item.description
            row.tool_hint = item.tool_hint if isinstance(item.tool_hint, str) else item.tool_hint.value
            row.depends_on = list(item.depends_on)
            row.risk_level = item.risk_level if isinstance(item.risk_level, str) else item.risk_level.value
            if row.id is None:
                row.status = SubtaskStatus.PENDING
            await session.flush()
            seq_to_id[item.seq] = row.id

        await session.commit()
        logger.info("plan_persisted", extra={"task_id": task_id, "subtasks": len(plan)})
        return seq_to_id

    async def sync_results(
        self,
        session: AsyncSession,
        task_id: int,
        results: dict[str, SubtaskResult],
        *,
        status_map: dict[int, str] | None = None,
    ) -> None:
        """把 state 中的子任务结果回写到 subtasks 表。"""
        stmt = select(Subtask).where(Subtask.task_id == task_id)
        rows = {item.seq: item for item in (await session.execute(stmt)).scalars().all()}

        for seq_str, result in results.items():
            row = rows.get(int(seq_str))
            if row is None:
                continue
            row.status = result.status if result.status in {s.value for s in SubtaskStatus} else SubtaskStatus.DONE.value
            summary = (result.data_digest or "")[:4000]
            if not summary and result.status == "FAILED" and result.error:
                # 失败子任务必须带文案落库：否则列表里只有一个红色 FAILED，看不出为什么
                code = str(result.error.get("code") or "TOOL_ERROR")
                message = str(result.error.get("message") or "")[:300]
                summary = f"[{code}] {message}"[:4000]
            row.result_summary = summary or None
            row.artifact_ids = [
                int(a.artifact_id) if a.artifact_id is not None else 0 for a in result.artifacts
            ]
            row.retry_count = min(int(result.attempts or 0), 255)

        if status_map:
            for seq, status in status_map.items():
                row = rows.get(int(seq))
                if row is not None:
                    row.status = status

        await session.commit()

    async def load_plan(self, session: AsyncSession, task_id: int) -> list[SubtaskPlan]:
        """从 DB 还原 DAG（GET /tasks/{id} 与断点恢复使用）。"""
        stmt = select(Subtask).where(Subtask.task_id == task_id).order_by(Subtask.seq)
        rows = (await session.execute(stmt)).scalars().all()
        plan: list[SubtaskPlan] = []
        for row in rows:
            plan.append(
                SubtaskPlan(
                    seq=row.seq,
                    name=row.name,
                    description=row.description or "",
                    tool_hint=row.tool_hint.value if hasattr(row.tool_hint, "value") else str(row.tool_hint),
                    depends_on=list(row.depends_on or []),
                    risk_level=row.risk_level.value if hasattr(row.risk_level, "value") else str(row.risk_level),
                    acceptance="",
                )
            )
        return plan

    # ---------------- 序列化辅助 ----------------
    @staticmethod
    def dump_plan(plan: list[SubtaskPlan]) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in plan]

    @staticmethod
    def dump_results(results: dict[str, SubtaskResult]) -> dict[str, Any]:
        return {seq: result.model_dump(mode="json") for seq, result in results.items()}

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)


__all__ = ["PlanService"]
