"""鉴权接口：POST /auth/login、/auth/refresh、/auth/logout、GET /auth/me（PRD 7.1）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.core.deps import get_auth_service, get_current_user
from app.core.exceptions import AppException, ErrorCode
from app.schemas.auth import CurrentUser, LoginRequest, RefreshRequest, TokenPair, UserOut
from app.schemas.common import ApiResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=ApiResponse[TokenPair],
    summary="登录（签发 access + refresh）",
    description="校验用户名密码，返回双 Token。access 默认 2h，refresh 默认 7d 且一次性消费。",
)
async def login(
    payload: LoginRequest,
    svc: AuthService = Depends(get_auth_service),
) -> ApiResponse[TokenPair]:
    _, pair = await svc.login(payload.username, payload.password)
    return ApiResponse.ok(pair, message="登录成功")


@router.post(
    "/refresh",
    response_model=ApiResponse[TokenPair],
    summary="刷新令牌（轮换）",
    description="使用 refresh_token 换取新 Token 对；旧 refresh 立即失效（一次性消费），重复使用返回 AUTH_REFRESH_REVOKED。",
)
async def refresh(
    payload: RefreshRequest,
    svc: AuthService = Depends(get_auth_service),
) -> ApiResponse[TokenPair]:
    pair = await svc.refresh(payload.refresh_token)
    return ApiResponse.ok(pair, message="刷新成功")


@router.post(
    "/logout",
    response_model=ApiResponse[None],
    status_code=status.HTTP_200_OK,
    summary="注销（access token 进黑名单）",
    description="将当前 access_token 的 jti 写入 Redis 黑名单，TTL 为令牌剩余有效期。",
)
async def logout(
    current: CurrentUser = Depends(get_current_user),
    svc: AuthService = Depends(get_auth_service),
) -> ApiResponse[None]:
    await svc.logout(current)
    return ApiResponse.ok(None, message="已注销")


@router.get(
    "/me",
    response_model=ApiResponse[UserOut],
    summary="当前登录用户",
)
async def me(
    current: CurrentUser = Depends(get_current_user),
    svc: AuthService = Depends(get_auth_service),
) -> ApiResponse[UserOut]:
    user = await svc.get_user_by_id(current.id)
    if user is None:  # 理论上 get_current_user 已校验，双保险
        raise AppException(ErrorCode.NOT_FOUND, "用户不存在")
    return ApiResponse.ok(UserOut.model_validate(user))
