"""FastAPI 依赖：数据库、Redis、鉴权服务、当前用户、角色校验。

require_role 是 PRD 2.1 / NFR-S2 的统一越权拦截入口，所有写接口必须挂载。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException, ErrorCode
from app.core.logging import get_logger
from app.core.redis_client import get_redis
from app.core.security import TokenType, decode_token
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.schemas.enums import UserRole
from app.schemas.orm_models import User
from app.services.auth_service import AuthService
from app.services.token_store import TokenStore

logger = get_logger(__name__)

# auto_error=False：缺失 Authorization 时由本层抛统一业务异常（而非 403 detail="Not authenticated"）
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="登录后签发的 access_token，格式：Bearer <token>",
)


async def get_token_store(redis: Redis = Depends(get_redis)) -> TokenStore:
    """Token 存储依赖（测试可 override get_redis 指向 fakeredis）。"""
    return TokenStore(redis)


async def get_auth_service(
    db: AsyncSession = Depends(get_db),
    store: TokenStore = Depends(get_token_store),
) -> AuthService:
    return AuthService(db=db, store=store)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
    store: TokenStore = Depends(get_token_store),
) -> CurrentUser:
    """解析并校验 access_token：签名 -> 类型 -> 黑名单 -> 用户有效性。"""
    if credentials is None or not credentials.credentials:
        raise AppException(ErrorCode.AUTH_TOKEN_MISSING, "缺少 Authorization: Bearer <access_token>")

    claims = decode_token(credentials.credentials, TokenType.ACCESS)

    if await store.is_blacklisted(claims.jti):
        raise AppException(ErrorCode.AUTH_TOKEN_REVOKED, "令牌已注销，请重新登录")

    user = await db.get(User, claims.user_id)
    if user is None:
        raise AppException(ErrorCode.AUTH_TOKEN_INVALID, "令牌对应的用户不存在")
    if int(user.status) != 1:
        raise AppException(ErrorCode.AUTH_USER_DISABLED, "账号已被禁用")

    return CurrentUser(
        id=user.id,
        username=user.username,
        role=user.role if isinstance(user.role, UserRole) else UserRole(str(user.role)),
        status=int(user.status),
        jti=claims.jti,
        token_type=claims.typ,
        expires_at=claims.expires_at,
    )


def require_role(*roles: UserRole) -> Callable[..., Awaitable[CurrentUser]]:
    """角色校验依赖工厂。

    用法：
        @router.post("/tasks", dependencies=[Depends(require_role(UserRole.ADMIN))])
        # 或直接取用户：user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER))
    """
    allowed = tuple(roles)

    async def _dependency(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not current.has_role(*allowed):
            logger.warning(
                "rbac_denied",
                extra={
                    "user_id": current.id,
                    "role": current.role.value,
                    "required": [r.value for r in allowed],
                },
            )
            raise AppException(
                ErrorCode.FORBIDDEN,
                "当前角色无权执行该操作",
                detail={"required_roles": [r.value for r in allowed], "actual_role": current.role.value},
            )
        return current

    _dependency.__name__ = "require_role_" + "_".join(r.value for r in allowed)
    return _dependency
