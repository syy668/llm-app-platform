"""W3 任务接口：SSE 进度流、审批断点查询/恢复、取消、快照与轨迹回放。

路由清单（全部挂在 ``/api/v1`` 下，全部经过鉴权依赖）：
- ``POST /tasks/stream``              创建任务并驱动流式执行（返回 stream_url）
- ``GET  /tasks/{id}/stream``         SSE 进度流（state_update / tool_call / interrupt / complete）
- ``GET  /tasks/{id}/interrupt``      查询挂起审批上下文
- ``POST /tasks/{id}/resume``         审批通过/驳回 → Command(resume) 断点续跑
- ``POST /tasks/{id}/cancel``         终止任务（置 CANCELLED）
- ``GET  /tasks/{id}/snapshot``       完整状态快照（轨迹回放用）
- ``GET  /tasks/{id}/trace``          历史 span + checkpoint 时间轴

为什么不复用 W2 的 ``POST /tasks``：W2 契约要求「一行不改」，其执行路径是 ``ainvoke``
（无事件）。W3 以新增路径提供流式超集入口，避免覆盖既有路由，也不影响 W2 的 132 条用例。

SSE 鉴权说明：浏览器 ``EventSource`` **不能设置自定义请求头**，因此本接口除标准
``Authorization: Bearer`` 外，额外接受 ``?access_token=`` 查询参数，二者校验逻辑完全一致。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import bearer_scheme, get_current_user, get_token_store, require_role
from app.core.exceptions import AppException, ErrorCode
from app.core.logging import get_logger
from app.core.w3_errors import STREAM_DISABLED, unavailable
from app.core.w3_config import w3
from app.db.session import get_db
from app.schemas.agent_io import TaskCreateRequest
from app.schemas.auth import CurrentUser
from app.schemas.common import ApiResponse
from app.schemas.enums import TaskStatus, UserRole
from app.schemas.w3 import CancelRequest, ResumeRequest
from app.services.event_bus import EventBus
from app.services.snapshot_service import SnapshotService
from app.services.stream_service import StreamingTaskService, sse_event_frames
from app.services.token_store import TokenStore

logger = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ---------------------------------------------------------------------------
# 依赖注入点（测试通过 app.state 或 dependency_overrides 替换）
# ---------------------------------------------------------------------------
async def get_event_bus(request: Request) -> EventBus:
    """进程内单例事件总线（Redis Stream，失败降级内存）。"""
    bus = getattr(request.app.state, "event_bus", None)
    if bus is None:
        bus = EventBus()
        request.app.state.event_bus = bus
    return bus


async def get_stream_service(
    request: Request,
    bus: EventBus = Depends(get_event_bus),
) -> StreamingTaskService:
    service = getattr(request.app.state, "stream_service", None)
    if service is not None:
        return service
    service = StreamingTaskService(bus=bus)
    request.app.state.stream_service = service
    return service


async def sse_current_user(
    request: Request,
    access_token: str | None = Query(default=None, description="EventSource 无法带头时的 Token 传递方式"),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
    store: TokenStore = Depends(get_token_store),
) -> CurrentUser:
    """SSE 专用鉴权：Bearer 头优先，其次 ``?access_token=``，复用 W1 的校验实现。"""
    token = credentials.credentials if credentials is not None else access_token
    if not token:
        raise AppException(ErrorCode.AUTH_TOKEN_MISSING, "缺少 Authorization: Bearer <access_token>")
    return await get_current_user(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), db=db, store=store
    )


def assert_visible(detail: dict[str, Any], user: CurrentUser) -> None:
    """与 W2 ``GET /tasks/{id}`` 完全一致的可见性规则。"""
    if user.role == UserRole.VIEWER and detail.get("status") != TaskStatus.COMPLETED.value:
        raise AppException(ErrorCode.FORBIDDEN, "访客仅可查看已完成任务")
    if user.role == UserRole.ADMIN and int(detail.get("creator_id") or 0) != int(user.id):
        raise AppException(ErrorCode.FORBIDDEN, "只能查看本人创建的任务")


async def _visible_detail(service: StreamingTaskService, task_id: int, user: CurrentUser) -> dict[str, Any]:
    detail = await service.get_detail(task_id)
    assert_visible(detail, user)
    return detail


async def _snapshot_service(service: StreamingTaskService) -> SnapshotService:
    runtime = await service.runtime()
    # 加固任务 4：终态任务快照清理后 snapshot 回退 MySQL，需要 session_factory
    return SnapshotService(runtime.graph, trace=service.trace, session_factory=service.session_factory)


# ---------------------------------------------------------------------------
# 1) 创建 + 流式执行
# ---------------------------------------------------------------------------
@router.post(
    "/stream",
    summary="创建任务并驱动流式执行（W3 演示控制台入口）",
    response_model=ApiResponse[dict],
)
async def create_task_stream(
    payload: TaskCreateRequest,
    user: CurrentUser = Depends(require_role(UserRole.ADMIN)),
    service: StreamingTaskService = Depends(get_stream_service),
) -> ApiResponse[dict]:
    """与 ``POST /tasks`` 同一套校验与落库，差别是执行走 ``astream_events`` 并推事件。"""
    result = await service.submit(
        goal=payload.goal,
        time_range=payload.time_range,
        creator_id=user.id,
        sync=payload.sync,
        compress=payload.compress,
        use_memory=payload.use_memory,
        eval_tag=payload.eval_tag,
    )
    return ApiResponse.ok(result)


# ---------------------------------------------------------------------------
# 2) SSE 进度流
# ---------------------------------------------------------------------------
@router.get(
    "/{task_id}/stream",
    summary="任务进度流（SSE）",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "state_update / tool_call / interrupt / complete 四类事件",
        }
    },
)
async def stream_task(
    request: Request,
    task_id: int = Path(ge=1),
    last_event_id: str | None = None,
    user: CurrentUser = Depends(sse_current_user),
    service: StreamingTaskService = Depends(get_stream_service),
    bus: EventBus = Depends(get_event_bus),
) -> StreamingResponse:
    if not w3.SSE_ENABLED:
        raise unavailable(STREAM_DISABLED, "SSE 进度流已关闭")

    detail = await _visible_detail(service, task_id, user)
    # 断线重连：优先标准头 Last-Event-ID，其次查询参数（部分代理会吃掉自定义头）
    resume_from = request.headers.get("Last-Event-ID") or last_event_id
    trace_id = detail.get("trace_id")

    async def _frames():
        async for frame in sse_event_frames(
            bus=bus,
            task_id=task_id,
            trace_id=trace_id,
            start_id=resume_from,
            is_disconnected=request.is_disconnected,
        ):
            yield frame

    return StreamingResponse(
        _frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Trace-ID": str(trace_id or ""),
        },
    )


# ---------------------------------------------------------------------------
# 3) 挂起审批上下文
# ---------------------------------------------------------------------------
@router.get(
    "/{task_id}/interrupt",
    summary="查询当前任务的审批挂起上下文",
    response_model=ApiResponse[dict],
)
async def get_interrupt(
    task_id: int = Path(ge=1),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    service: StreamingTaskService = Depends(get_stream_service),
) -> ApiResponse[dict]:
    await _visible_detail(service, task_id, user)
    context = await service.interrupt_of(task_id)
    return ApiResponse.ok(context)


# ---------------------------------------------------------------------------
# 4) 断点续跑（审批通过 / 驳回）
# ---------------------------------------------------------------------------
@router.post(
    "/{task_id}/resume",
    summary="审批决策并恢复 LangGraph 断点",
    response_model=ApiResponse[dict],
)
async def resume_task(
    payload: ResumeRequest,
    task_id: int = Path(ge=1),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER)),
    service: StreamingTaskService = Depends(get_stream_service),
) -> ApiResponse[dict]:
    result = await service.resume(
        task_id,
        decision=payload.normalized_decision,
        comment=payload.comment,
        decided_by=user.id,
        approval_id=payload.approval_id,
        sync=payload.sync,
    )
    return ApiResponse.ok(result, message="已恢复执行" if result.get("resumed") else "未恢复")


# ---------------------------------------------------------------------------
# 5) 取消
# ---------------------------------------------------------------------------
@router.post(
    "/{task_id}/cancel",
    summary="终止任务（置 CANCELLED，不再执行）",
    response_model=ApiResponse[dict],
)
async def cancel_task(
    payload: CancelRequest | None = None,
    task_id: int = Path(ge=1),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER)),
    service: StreamingTaskService = Depends(get_stream_service),
) -> ApiResponse[dict]:
    reason = (payload.reason if payload is not None else "") or f"operator:{user.id}"
    result = await service.cancel(task_id, reason=reason, user_id=user.id)
    return ApiResponse.ok(result, message="任务已终止")


# ---------------------------------------------------------------------------
# 6) 状态快照 / 7) 轨迹回放
# ---------------------------------------------------------------------------
@router.get(
    "/{task_id}/snapshot",
    summary="任务完整状态快照（轨迹回放）",
    response_model=ApiResponse[dict],
)
async def get_snapshot(
    task_id: int = Path(ge=1),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    service: StreamingTaskService = Depends(get_stream_service),
) -> ApiResponse[dict]:
    detail = await _visible_detail(service, task_id, user)
    snapshot_service = await _snapshot_service(service)
    data = await snapshot_service.snapshot(task_id, trace_id=detail.get("trace_id"))
    return ApiResponse.ok(data)


@router.get(
    "/{task_id}/trace",
    summary="历史步骤与工具调用（只读回放）",
    response_model=ApiResponse[dict],
)
async def get_trace(
    task_id: int = Path(ge=1),
    after_id: int = Query(default=0, ge=0, description="增量拉取：只返回该 span id 之后的记录"),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    service: StreamingTaskService = Depends(get_stream_service),
) -> ApiResponse[dict]:
    detail = await _visible_detail(service, task_id, user)
    snapshot_service = await _snapshot_service(service)
    data = await snapshot_service.replay(task_id, trace_id=detail.get("trace_id"), after_id=after_id)
    return ApiResponse.ok(data)


__all__ = [
    "router",
    "get_event_bus",
    "get_stream_service",
    "sse_current_user",
    "assert_visible",
]
