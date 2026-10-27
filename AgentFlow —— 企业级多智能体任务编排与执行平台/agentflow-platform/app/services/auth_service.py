"""鉴权业务服务：登录 / 刷新轮换 / 注销拉黑。

对齐项目二 auth-kit 的**行为语义**（登录签发双 Token、refresh 一次性轮换、
注销将 access jti 写入 Redis 黑名单），本项目内部独立实现，未复制任何外部源码。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException, ErrorCode
from app.core.logging import get_logger
from app.core.security import TokenType, create_token, decode_token, dummy_verify, verify_password
from app.schemas.auth import CurrentUser, TokenPair
from app.schemas.enums import UserRole
from app.schemas.orm_models import User
from app.services.token_store import TokenStore
from app.utils.time import utcnow

logger = get_logger(__name__)


class AuthService:
    def __init__(self, db: AsyncSession, store: TokenStore) -> None:
        self._db = db
        self._store = store

    # ---------------- 查询 ----------------
    async def get_user_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username).limit(1)
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def get_user_by_id(self, user_id: int) -> User | None:
        return await self._db.get(User, user_id)

    # ---------------- 登录 ----------------
    async def authenticate(self, username: str, password: str) -> User:
        user = await self.get_user_by_username(username)
        if user is None:
            # 用户不存在也走一次 bcrypt，避免响应时间差暴露账号存在性
            dummy_verify(password)
            logger.info("login_failed", extra={"username": username, "reason": "user_not_found"})
            raise AppException(ErrorCode.AUTH_INVALID_CREDENTIALS, "用户名或密码错误")
        if not verify_password(password, user.password_hash):
            logger.info("login_failed", extra={"username": username, "reason": "bad_password"})
            raise AppException(ErrorCode.AUTH_INVALID_CREDENTIALS, "用户名或密码错误")
        if int(user.status) != 1:
            logger.warning("login_blocked", extra={"user_id": user.id, "reason": "disabled"})
            raise AppException(ErrorCode.AUTH_USER_DISABLED, "账号已被禁用，请联系管理员")
        return user

    async def login(self, username: str, password: str) -> tuple[User, TokenPair]:
        user = await self.authenticate(username, password)
        pair = await self._issue_pair(user)
        user.last_login_at = utcnow()
        await self._db.commit()
        await self._db.refresh(user)
        logger.info("login_success", extra={"user_id": user.id, "role": user.role.value})
        return user, pair

    # ---------------- 刷新（一次性轮换） ----------------
    async def refresh(self, refresh_token: str) -> TokenPair:
        claims = decode_token(refresh_token, TokenType.REFRESH)
        if await self._store.is_blacklisted(claims.jti):
            raise AppException(ErrorCode.AUTH_REFRESH_REVOKED, "刷新令牌已失效，请重新登录")
        if not await self._store.consume_refresh(claims.jti):
            # 已消费 / 过期 / 不存在：可能是令牌重放，直接拒绝
            logger.warning("refresh_replay_detected", extra={"user_id": claims.user_id, "jti": claims.jti})
            raise AppException(ErrorCode.AUTH_REFRESH_REVOKED, "刷新令牌已失效，请重新登录")

        user = await self.get_user_by_id(claims.user_id)
        if user is None:
            raise AppException(ErrorCode.AUTH_TOKEN_INVALID, "令牌对应的用户不存在")
        if int(user.status) != 1:
            raise AppException(ErrorCode.AUTH_USER_DISABLED, "账号已被禁用，请联系管理员")

        pair = await self._issue_pair(user)
        logger.info("token_refreshed", extra={"user_id": user.id})
        return pair

    # ---------------- 注销 ----------------
    async def logout(self, current: CurrentUser) -> None:
        if not current.jti:
            raise AppException(ErrorCode.AUTH_TOKEN_INVALID, "缺少可注销的令牌标识")
        await self._store.blacklist_access(jti=current.jti, expires_at=current.expires_at)
        logger.info("logout_success", extra={"user_id": current.id, "jti": current.jti})

    # ---------------- 内部 ----------------
    async def _issue_pair(self, user: User) -> TokenPair:
        role = user.role.value if isinstance(user.role, UserRole) else str(user.role)
        access = create_token(user_id=user.id, username=user.username, role=role, token_type=TokenType.ACCESS)
        refresh = create_token(user_id=user.id, username=user.username, role=role, token_type=TokenType.REFRESH)
        await self._store.register_refresh(jti=refresh.jti, user_id=user.id, expires_at=refresh.expires_at)
        return TokenPair(
            access_token=access.token,
            refresh_token=refresh.token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )
