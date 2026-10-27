"""认证接口：登录、刷新、当前用户、登出、修改密码。"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..deps import CurrentUser, get_current_user
from ...core import security
from ...core.database import get_db
from ...core.openapi import AUTH_ERRORS, std_errors
from ...core.response import ok
from ...schemas.user import ChangePasswordRequest, LoginRequest, LogoutRequest, RefreshRequest
from ...services import auth_service

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    """用户登录，签发 session 访问令牌与刷新令牌。"""
    result = auth_service.login(db, payload.username, payload.password)
    return ok(result)


def _do_refresh(payload: RefreshRequest, db: Session):
    """【双Token改造】刷新公共逻辑：旧 /auth/refresh 与新 /auth/refresh-token 共用。"""
    return ok(auth_service.refresh(db, payload.refresh_token))


@router.post("/refresh", responses=std_errors(401, 500))
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    """使用刷新令牌换发新令牌（一次性轮换，旧接口保留向后兼容）。

    - 入参：{"refresh_token": "登录时签发的刷新令牌"}；
    - 返回示例：{"code": 0, "message": "ok", "data": {"access_token": "...", "refresh_token": "..."}}；
    - 错误：401 刷新令牌无效 / 过期 / 已拉黑。
    """
    return _do_refresh(payload, db)


@router.post("/refresh-token", responses=std_errors(401, 422, 500))
def refresh_token(payload: RefreshRequest, db: Session = Depends(get_db)):
    """【双Token改造 新增】刷新令牌换发新 access_token + refresh_token。

    - 校验 Redis 内令牌有效性（签名 + 黑名单 + 存在性，TTL 与 refresh 有效期一致）；
    - 一次性轮换：成功后立即删除旧 refresh_token 并签发新令牌，防止重复复用；
    - 失效/已拉黑的令牌返回 401，由前端清空凭据并跳转登录页。

    入参示例：{"refresh_token": "xxxxxxxx"}
    返回示例：{"code": 0, "message": "ok",
               "data": {"access_token": "新访问令牌(30min)", "refresh_token": "新刷新令牌(7天)"}}
    错误码：401 令牌无效/已拉黑；422 缺少 refresh_token 参数；500 服务端异常。
    """
    return _do_refresh(payload, db)


@router.get("/me", responses=AUTH_ERRORS)
def me(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """返回当前登录用户信息。"""
    return ok(auth_service.get_me(db, user.id))


@router.post("/logout", responses=AUTH_ERRORS)
def logout(
    request: Request,
    payload: LogoutRequest | None = None,
    user: CurrentUser = Depends(get_current_user),
):
    """登出：删除 access 会话；【双Token改造 新增】随请求体传入 refresh_token 时加入 Redis 黑名单（主动踢下线）。

    - 请求体可选：{"refresh_token": "xxxxxxxx"}（推荐传入，实现刷新令牌同步拉黑）；
    - 返回示例：{"code": 0, "message": "ok", "data": {}}
    - 错误码：401 未登录 / access_token 无效；500 服务端异常。
    """
    token = security.extract_token(request.headers.get("Authorization"))
    refresh_token = payload.refresh_token if payload else None
    auth_service.logout(token, refresh_token)
    return ok({})


@router.post("/change-password", responses=std_errors(400, 401, 500))
def change_password(
    payload: ChangePasswordRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """修改当前用户密码。"""
    auth_service.change_password(db, user.id, payload.old_password, payload.new_password)
    return ok({})
