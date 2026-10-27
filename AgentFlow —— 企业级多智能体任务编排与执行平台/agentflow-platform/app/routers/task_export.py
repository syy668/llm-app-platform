"""任务与轨迹导出（缺口 G10；加固任务 5：流式化防 OOM）。

两个只读导出端点，**只读数据库与快照，不触发任何执行**：

- ``GET /tasks/{task_id}/export``：整任务归档（任务基本信息 + 子任务 + 运行时结论/评审 +
  审批单 + 体积精简后的轨迹），用于复盘、归档、外部分析；
- ``GET /tasks/{task_id}/trace/export``：纯轨迹（steps + checkpoints + interrupt），
  用于把一次执行回放交给别人复现，或喂给评测工具。

**流式实现（加固任务 5）**：旧版把整个 payload（含全部 span）读进内存后一次性
``json.dumps``，超长任务（数万 span）会撑爆 worker 内存。现改为
``StreamingResponse`` + ``TraceService.iter_steps`` 分批游标（默认每批 100 条），
生成器内任何时刻只持有**头部对象 + 当前批**。

JSON 拼接正确性由构造保证：先用占位符 ``"@@AGENTFLOW_STEPS@@"`` 生成完整头部
JSON 文本，``split`` 成 prefix/suffix，流式输出 ``prefix + "[" + step,step,... + "]" +
suffix``——括号/逗号永远成对，不依赖手写拼接逻辑。schema 字段与 v1 保持兼容
（``steps`` 仍在 ``trace.steps``）。

导出体首行带 ``schema`` 版本号，后续字段演进时读取方可以据此兼容；文件名统一 ASCII，
避免浏览器下载时因中文名被截断。权限与 ``GET /tasks/{id}`` 完全一致
（复用 ``assert_visible``：VIEWER 仅已完成任务、ADMIN 仅本人任务）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Path
from fastapi.responses import StreamingResponse
from starlette.responses import Response

from app.core.deps import require_role
from app.core.logging import get_logger
from app.core.w3_config import w3
from app.routers.tasks_ext import assert_visible, get_stream_service
from app.schemas.auth import CurrentUser
from app.schemas.enums import UserRole
from app.services.snapshot_service import SnapshotService
from app.services.stream_service import StreamingTaskService
from app.services.trace_service import TraceService

logger = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["export"])

SCHEMA_TASK = "agentflow.task_export.v1"
SCHEMA_TRACE = "agentflow.trace_export.v1"

#: 归档里保留的轨迹条数上限（防止超长任务的导出文件把浏览器卡死）
_MAX_EXPORT_STEPS = 2000

#: 流式读取的每批 span 数（内存占用 ≈ 单批大小）
_EXPORT_BATCH = 100

#: steps 数组在头部 JSON 里的占位符（split 后 prefix/suffix 拼接，构造性保证合法）
_STEPS_PLACEHOLDER = "@@AGENTFLOW_STEPS@@"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _duration_seconds(created_at: str | None, finished_at: str | None) -> float | None:
    """按 UTC naive 口径计算耗时（与 duration_s 的库内口径一致）。"""
    if not created_at or not finished_at:
        return None
    try:
        start = datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).replace(tzinfo=None)
        end = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
    return round((end - start).total_seconds(), 3)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _split_at_steps(head: dict[str, Any]) -> tuple[str, str]:
    """把含占位符的头部对象拆成 (prefix, suffix)。

    占位符在 ``trace.steps`` 的位置上，序列化后整体 split 成两段：
    流式输出 ``prefix + "[" + steps... + "]" + suffix`` 即为合法 JSON。
    """
    marker = json.dumps(_STEPS_PLACEHOLDER, ensure_ascii=False)
    text = json.dumps(head, ensure_ascii=False, default=str)
    prefix, suffix = text.split(marker, 1)
    return prefix, suffix


async def _steps_stream(
    trace: TraceService,
    task_id: int,
    prefix: str,
    suffix: str,
    *,
    limit: int,
    batch: int = _EXPORT_BATCH,
) -> AsyncIterator[str]:
    """steps 数组的流式生成器：``prefix[step,step,...]suffix``。"""
    yield prefix + "["
    first = True
    async for step in trace.iter_steps(task_id=task_id, limit=limit, batch=batch):
        body = json.dumps(step, ensure_ascii=False, default=str)
        yield body if first else "," + body
        first = False
    yield "]" + suffix


def _stream_response(
    generator: AsyncIterator[str], *, schema: str, filename: str, total_steps: int
) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-AgentFlow-Schema": schema,
            "X-AgentFlow-Total-Steps": str(total_steps),
        },
    )


async def _snapshot_quietly(
    service: StreamingTaskService, task_id: int, trace_id: str | None
) -> dict[str, Any]:
    """取快照（interrupt / trace_id 解析用）；快照缺失或异常一律静默降级。"""
    try:
        runtime = await service.runtime()
        snap = SnapshotService(
            runtime.graph, trace=service.trace, session_factory=service.session_factory
        )
        return await snap.snapshot(task_id, trace_id=trace_id)
    except Exception as exc:  # noqa: BLE001 - 终态任务快照已清理是常态，不能影响导出
        logger.warning("export_snapshot_unavailable", extra={"task_id": task_id, "error": str(exc)[:200]})
        return {}


@router.get("/{task_id}/export", summary="任务导出（流式 JSON 归档：详情+子任务+评审+审批+轨迹）")
async def export_task(
    task_id: int = Path(ge=1),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    service: StreamingTaskService = Depends(get_stream_service),
) -> Response:
    detail = await service.get_detail(task_id)
    assert_visible(detail, user)

    snapshot = await _snapshot_quietly(service, task_id, detail.get("trace_id"))
    interrupt = snapshot.get("interrupt")
    trace_id = detail.get("trace_id") or snapshot.get("trace_id")

    total = await service.trace.count(task_id)
    query_limit = min(w3.TRACE_REPLAY_MAX_STEPS, _MAX_EXPORT_STEPS)
    emitted = min(total, query_limit)
    tool_calls = await service.trace.count(task_id, event="tool.call")

    head = {
        "schema": SCHEMA_TASK,
        "exported_at": _utcnow_iso(),
        "exported_by": {"user_id": user.id, "username": user.username, "role": user.role.value},
        "task": {k: v for k, v in detail.items() if k != "subtasks"},
        "subtasks": detail.get("subtasks") or [],
        "review": detail.get("review"),
        "eval_score": detail.get("eval_score"),
        "summary": detail.get("summary") or "",
        "approvals": detail.get("approvals") or {},
        "artifacts": detail.get("artifacts") or [],
        "trace": {
            "trace_id": trace_id,
            "total_steps": emitted,
            "truncated": total > emitted,
            "steps": _STEPS_PLACEHOLDER,
            "interrupt": interrupt,
        },
        "metrics": {
            "duration_s": _duration_seconds(detail.get("created_at"), detail.get("finished_at")),
            "token_usage": detail.get("token_usage"),
            "replan_count": detail.get("replan_count"),
            "subtask_total": len(detail.get("subtasks") or []),
            "subtask_done": sum(1 for s in (detail.get("subtasks") or []) if s.get("status") == "DONE"),
            "tool_calls": tool_calls,
        },
    }
    prefix, suffix = _split_at_steps(head)
    logger.info(
        "task_exported",
        extra={"task_id": task_id, "steps": emitted, "total": total, "operator": user.id},
    )
    return _stream_response(
        _steps_stream(service.trace, task_id, prefix, suffix, limit=query_limit),
        schema=SCHEMA_TASK,
        filename=f"task-{task_id}-export-{_stamp()}.json",
        total_steps=total,
    )


@router.get("/{task_id}/trace/export", summary="轨迹导出（流式 JSON：steps + checkpoints + interrupt）")
async def export_trace(
    task_id: int = Path(ge=1),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    service: StreamingTaskService = Depends(get_stream_service),
) -> Response:
    detail = await service.get_detail(task_id)
    assert_visible(detail, user)

    snapshot = await _snapshot_quietly(service, task_id, detail.get("trace_id"))
    interrupt = snapshot.get("interrupt")
    trace_id = detail.get("trace_id") or snapshot.get("trace_id")
    checkpoints: list[dict[str, Any]] = []
    if trace_id or detail.get("status") not in ("COMPLETED", "FAILED", "CANCELLED"):
        # checkpoint 时间轴有界（≤ TRACE_REPLAY_MAX_SNAPSHOTS），仅非终态或可读时才取；
        # 终态任务快照已清理（加固任务 4），读了也是空，省一次图查询
        try:
            runtime = await service.runtime()
            snap = SnapshotService(runtime.graph, trace=service.trace, session_factory=service.session_factory)
            checkpoints = await snap.checkpoints(task_id)
        except Exception:  # noqa: BLE001 - 快照不可用不影响导出主体
            checkpoints = []

    total = await service.trace.count(task_id)
    query_limit = min(w3.TRACE_REPLAY_MAX_STEPS, _MAX_EXPORT_STEPS)
    emitted = min(total, query_limit)

    head = {
        "schema": SCHEMA_TRACE,
        "exported_at": _utcnow_iso(),
        "task_id": task_id,
        "task_no": detail.get("task_no"),
        "goal": detail.get("goal"),
        "status": detail.get("status"),
        "trace_id": trace_id,
        "total_steps": emitted,
        "truncated": total > emitted,
        "steps": _STEPS_PLACEHOLDER,
        "checkpoints": checkpoints,
        "interrupt": interrupt,
    }
    prefix, suffix = _split_at_steps(head)
    logger.info(
        "trace_exported",
        extra={"task_id": task_id, "steps": emitted, "total": total, "operator": user.id},
    )
    return _stream_response(
        _steps_stream(service.trace, task_id, prefix, suffix, limit=query_limit),
        schema=SCHEMA_TRACE,
        filename=f"task-{task_id}-trace-{_stamp()}.json",
        total_steps=total,
    )


__all__ = [
    "router",
    "SCHEMA_TASK",
    "SCHEMA_TRACE",
    "_MAX_EXPORT_STEPS",
    "_EXPORT_BATCH",
    "_split_at_steps",
    "_steps_stream",
]
