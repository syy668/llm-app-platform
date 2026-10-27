"""用户管理接口（仅管理员）。"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ..deps import CurrentUser, require_admin
from ...core.database import get_db
from ...core.response import ok
from ...schemas.user import ResetPasswordRequest, UserCreate, UserUpdate
from ...services import user_service

router = APIRouter(prefix="/users", tags=["用户管理"])


def _client_ip(request: Request) -> str | None:
    """获取客户端 IP，兼容反向代理场景。"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get("")
def list_users(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    role: str | None = None,
    _: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """分页查询用户列表。"""
    return ok(user_service.list_users(db, page, size, keyword, role))


@router.post("")
def create_user(
    payload: UserCreate,
    request: Request,
    operator: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """新建用户。"""
    return ok(user_service.create_user(db, payload, operator.id, _client_ip(request)))


@router.put("/{user_id}")
def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    operator: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """更新用户信息。"""
    return ok(user_service.update_user(db, user_id, payload, operator.id, _client_ip(request)))


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    request: Request,
    operator: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """删除用户（软删除：禁用账号）。"""
    return ok(user_service.delete_user(db, user_id, operator.id, _client_ip(request)))


@router.put("/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: ResetPasswordRequest,
    request: Request,
    operator: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """重置指定用户密码。"""
    return ok(user_service.reset_password(db, user_id, payload.new_password, operator.id, _client_ip(request)))
