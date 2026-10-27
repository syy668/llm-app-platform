"""记忆管理 API（FR-014，W5-Part1 任务 2.5）。

路由清单（全部挂在 ``/api/v1`` 下，全部经鉴权）：

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | ``/memories`` | ADMIN/APPROVER/VIEWER | 分页查询（ADMIN 看全量，其他角色只看本人） |
| GET | ``/memories/meta`` | ADMIN/APPROVER/VIEWER | 分层配置自检（开关 / 存储 / TTL） |
| POST | ``/memories/recall`` | ADMIN/APPROVER/VIEWER | 相似度召回演示（只读，会累加 hit_count） |
| POST | ``/memories`` | ADMIN | 手工新增 mid / long |
| GET | ``/memories/{memory_id}`` | ADMIN/APPROVER/VIEWER | 单条详情 |
| DELETE | ``/memories/{memory_id}`` | ADMIN | 删除（long 同时失去召回资格） |

设计取舍：
- **写操作只给 ADMIN**（PRD NFR-S2「所有写接口必须挂 Depends(require_role(...))」）；
- ``ENABLE_LAYERED_MEMORY=false`` 时：查询返回空列表 + ``enabled=false``，
  写操作返回 503 ``SERVICE_UNAVAILABLE``——明确告诉调用方"功能未启用"，而不是静默成功；
- 短期层（Redis）不出现在管理 API 中：它是任务会话的运行时缓存，无人工干预语义。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_role
from app.core.exceptions import AppException, ErrorCode
from app.core.redis_client import get_redis
from app.core.w5_config import w5
from app.db.session import get_db
from app.memory.layered_memory import LayeredMemoryService, MemoryDisabledError
from app.schemas.auth import CurrentUser
from app.schemas.common import ApiResponse
from app.schemas.enums import MemoryScope, UserRole
from app.schemas.w5 import MemoryCreateRequest, MemoryRecallRequest

router = APIRouter(prefix="/memories", tags=["memories"])

READ_ROLES = (UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)


async def get_memory_service(
    session: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> LayeredMemoryService:
    """每请求一个实例（复用请求级 Session 与 Redis 连接；测试可整体 override）。"""
    return LayeredMemoryService(session=session, redis=redis)


def _owner_filter(user: CurrentUser) -> int | None:
    """ADMIN 看全量；其他角色只看本人（记忆是个人上下文，不能跨用户串味）。"""
    return None if user.role == UserRole.ADMIN else int(user.id)


def _ensure_enabled() -> None:
    if not w5.ENABLE_LAYERED_MEMORY:
        raise AppException(
            ErrorCode.SERVICE_UNAVAILABLE,
            "分层记忆未启用：请设置 ENABLE_LAYERED_MEMORY=true",
            detail={"feature": "FR-014", "switch": "ENABLE_LAYERED_MEMORY"},
        )


# ---------------------------------------------------------------------------
# 1) 查询
# ---------------------------------------------------------------------------
@router.get("", summary="查询记忆（分页 + scope/task 过滤）", response_model=ApiResponse[dict])
async def list_memories(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    scope: MemoryScope | None = Query(default=None, description="mid / long"),
    task_id: int | None = Query(default=None, ge=1),
    user: CurrentUser = Depends(require_role(*READ_ROLES)),
    service: LayeredMemoryService = Depends(get_memory_service),
) -> ApiResponse[dict]:
    enabled = w5.ENABLE_LAYERED_MEMORY
    if not enabled:
        return ApiResponse.ok(
            {"items": [], "meta": {"page": page, "size": size, "total": 0}, "enabled": False}
        )

    items, total = await service.list_memories(
        user_id=_owner_filter(user), scope=scope, task_id=task_id, page=page, size=size
    )
    return ApiResponse.ok(
        {
            "items": [item.to_dict() for item in items],
            "meta": {"page": page, "size": size, "total": total},
            "enabled": True,
        }
    )


@router.get("/meta", summary="分层记忆配置自检", response_model=ApiResponse[dict])
async def memory_meta(
    user: CurrentUser = Depends(require_role(*READ_ROLES)),
    service: LayeredMemoryService = Depends(get_memory_service),
) -> ApiResponse[dict]:
    return ApiResponse.ok(
        {
            **service.describe(),
            "layers": {
                "short": {"backend": "redis", "ttl_seconds": w5.MEMORY_SHORT_TTL_SECONDS, "summarized": False},
                "mid": {"backend": "mysql.memories", "ttl_days": w5.MEMORY_MID_TTL_DAYS, "summarized": True},
                "long": {
                    "backend": "mysql.memories + rag",
                    "min_review_score": w5.MEMORY_LONG_MIN_REVIEW_SCORE,
                    "ttl_days": None,
                },
            },
            "viewer": {"user_id": int(user.id), "role": user.role.value, "scope_filter": _owner_filter(user) is not None},
        }
    )


@router.post("/recall", summary="相似度召回（演示用）", response_model=ApiResponse[dict])
async def recall_memories(
    payload: MemoryRecallRequest,
    user: CurrentUser = Depends(require_role(*READ_ROLES)),
    service: LayeredMemoryService = Depends(get_memory_service),
) -> ApiResponse[dict]:
    if not w5.ENABLE_LAYERED_MEMORY:
        return ApiResponse.ok({"enabled": False, "goal": payload.goal, "hits": []})

    hits = await service.recall(payload.goal, user_id=_owner_filter(user), top_k=payload.top_k)
    return ApiResponse.ok(
        {
            "enabled": True,
            "goal": payload.goal,
            "store": service.store.name,
            # 检索通道自省：scorer=local-keyword 时 score 是关键词重合度，
            # 不是语义相似度（degraded=True 表示配置的向量通道故障后走了兜底）。
            "retrieval": service.last_retrieval,
            "hits": [hit.to_dict() for hit in hits],
            "injection": [hit.as_prompt(max_chars=w5.MEMORY_INJECT_MAX_CHARS) for hit in hits],
        }
    )


# ---------------------------------------------------------------------------
# 2) 新增 / 删除（写操作 → 仅 ADMIN）
# ---------------------------------------------------------------------------
@router.post("", summary="手工新增记忆（mid / long）", response_model=ApiResponse[dict])
async def create_memory(
    payload: MemoryCreateRequest,
    user: CurrentUser = Depends(require_role(UserRole.ADMIN)),
    service: LayeredMemoryService = Depends(get_memory_service),
) -> ApiResponse[dict]:
    _ensure_enabled()
    try:
        record = await service.create_memory(
            user_id=int(user.id),
            scope=payload.scope,
            content=payload.content,
            task_id=payload.task_id,
            importance=payload.importance,
        )
    except MemoryDisabledError as exc:
        raise AppException(ErrorCode.SERVICE_UNAVAILABLE, str(exc)) from exc
    except ValueError as exc:
        raise AppException(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    return ApiResponse.ok(record.to_dict(), message="已创建")


@router.get("/{memory_id}", summary="记忆详情", response_model=ApiResponse[dict])
async def get_memory(
    memory_id: int = Path(ge=1),
    user: CurrentUser = Depends(require_role(*READ_ROLES)),
    service: LayeredMemoryService = Depends(get_memory_service),
) -> ApiResponse[dict]:
    _ensure_enabled()
    record = await service.get_memory(memory_id, user_id=_owner_filter(user))
    if record is None:
        raise AppException(ErrorCode.NOT_FOUND, f"记忆 {memory_id} 不存在或无权访问")
    return ApiResponse.ok(record.to_dict())


@router.delete("/{memory_id}", summary="删除记忆", response_model=ApiResponse[dict])
async def delete_memory(
    memory_id: int = Path(ge=1),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN)),
    service: LayeredMemoryService = Depends(get_memory_service),
) -> ApiResponse[dict]:
    _ensure_enabled()
    deleted = await service.delete_memory(memory_id)
    if not deleted:
        raise AppException(ErrorCode.NOT_FOUND, f"记忆 {memory_id} 不存在")
    return ApiResponse.ok({"memory_id": memory_id, "deleted": True}, message="已删除")


__all__ = ["router", "get_memory_service"]
