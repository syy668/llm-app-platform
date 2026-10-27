"""MCP 工具注册面板（v1.1 任务 1）：工具元信息 + 启停开关。

三个端点，全部只读装配层、不触发任何工具调用：

- ``GET  /tools``         工具目录（入参 / 出参 / 能力说明 / 配置项 / 当前启用状态 / 最近变更）；
- ``GET  /tools/{name}``  单个工具详情；
- ``PATCH /tools/{name}`` 启用 / 禁用（写 Redis + 审计，下一次任务装配即生效）。

权限：读取 = ADMIN / APPROVER（与 ``/kb/status``、``/logs/search`` 一致的运维配套）；
写入 = 仅 ADMIN。开关状态**不影响已挂起任务**的断点续跑，只影响后续新建任务的工具集。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Path
from redis.asyncio import Redis

from app.core.deps import require_role
from app.core.logging import get_logger
from app.core.redis_client import get_redis
from app.schemas.auth import CurrentUser
from app.schemas.common import ApiResponse
from app.schemas.enums import UserRole
from app.schemas.tool import ToolInfo, ToolListData, ToolToggleRequest
from app.services.tool_registry import TOOL_CATALOG, ToolRegistry

logger = get_logger(__name__)

router = APIRouter(prefix="/tools", tags=["tools"])


def _registry(redis: Redis = Depends(get_redis)) -> ToolRegistry:
    return ToolRegistry(redis=redis)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@router.get("", summary="工具目录（元信息 + 启停状态）", response_model=ApiResponse[ToolListData])
async def list_tools(
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER)),
    registry: ToolRegistry = Depends(_registry),
) -> ApiResponse[ToolListData]:
    items = await registry.list_tools()
    enabled = sum(1 for item in items if item["enabled"])
    # 全部禁用时 filter_enabled_tools 会 fail-open 退回全量（安全兜底，有意设计）；
    # 这里如实回传该状态（缺陷 D-02 建议 b），避免运维/前端误判为"已无工具可用"。
    all_disabled = bool(items) and enabled == 0
    data = ToolListData(
        items=[ToolInfo(**item) for item in items],
        total=len(items),
        enabled=enabled,
        disabled=len(items) - enabled,
        generated_at=_now_iso(),
        audit=await registry.recent_audit(limit=20),
        fail_open=all_disabled,
    )
    return ApiResponse.ok(data)


@router.get("/{name}", summary="工具详情", response_model=ApiResponse[ToolInfo])
async def get_tool(
    name: str = Path(min_length=1, max_length=64),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER)),
    registry: ToolRegistry = Depends(_registry),
) -> ApiResponse[ToolInfo]:
    items = await registry.list_tools()
    for item in items:
        if item["name"] == name:
            return ApiResponse.ok(ToolInfo(**item))
    from app.core.exceptions import AppException, ErrorCode

    raise AppException(
        ErrorCode.NOT_FOUND,
        f"未注册的工具：{name}",
        detail={"available": sorted(TOOL_CATALOG.keys())},
    )


@router.patch("/{name}", summary="启用 / 禁用工具（仅 ADMIN）", response_model=ApiResponse[ToolInfo])
async def toggle_tool(
    payload: ToolToggleRequest,
    name: str = Path(min_length=1, max_length=64),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN)),
    registry: ToolRegistry = Depends(_registry),
) -> ApiResponse[ToolInfo]:
    if name not in TOOL_CATALOG:
        from app.core.exceptions import AppException, ErrorCode

        raise AppException(
            ErrorCode.NOT_FOUND,
            f"未注册的工具：{name}",
            detail={"available": sorted(TOOL_CATALOG.keys())},
        )
    item = await registry.set_enabled(
        name,
        bool(payload.enabled),
        operator=user.username,
        reason=payload.reason,
    )
    logger.info(
        "tool_toggle_api",
        extra={"tool": name, "enabled": payload.enabled, "operator": user.id, "reason": payload.reason[:100]},
    )
    return ApiResponse.ok(
        ToolInfo(**item),
        message=f"工具 {name} 已{'启用' if payload.enabled else '禁用'}（下一次新建任务生效）",
    )


__all__ = ["router"]
