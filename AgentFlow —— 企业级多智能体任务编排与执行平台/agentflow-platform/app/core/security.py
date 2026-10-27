"""密码哈希与 JWT 双 Token 签发/校验。

说明：
- 本项目按 PRD A-03 的**接口语义**内部复现双 Token 轮换 + Redis 黑名单逻辑，
  不引入、不复制项目二 auth-kit 的任何源码；对外函数签名与语义保持一致，
  后续若替换为 auth-kit 官方 wheel，只需替换本模块实现。
- 轮换策略：refresh 一次性消费（GETDEL），旧 refresh 即刻失效；access 无状态，
  注销时以 jti 进黑名单（TTL = 剩余有效期）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

import bcrypt
import jwt
from pydantic import BaseModel, ConfigDict

from app.core.config import settings
from app.core.exceptions import AppException, ErrorCode


class TokenType(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True)
class IssuedToken:
    token: str
    jti: str
    expires_at: datetime


class TokenClaims(BaseModel):
    """解码后的载荷（字段名保持 JWT 标准短名）。"""

    model_config = ConfigDict(extra="ignore")

    sub: str
    username: str
    role: str
    typ: TokenType
    jti: str
    iat: int
    exp: int
    iss: str | None = None

    @property
    def user_id(self) -> int:
        return int(self.sub)

    @property
    def expires_at(self) -> datetime:
        return datetime.fromtimestamp(self.exp, tz=timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# 密码
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """bcrypt 哈希，输出 60 字符（对齐 users.password_hash CHAR(60)）。"""
    if not password:
        raise ValueError("password 不能为空")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# 用户不存在时也执行一次校验，抹平响应时间差（防用户名枚举）
_DUMMY_HASH = bcrypt.hashpw(b"agentflow-dummy-password", bcrypt.gensalt(rounds=4)).decode("utf-8")


def dummy_verify(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
def _expire_delta(token_type: TokenType) -> timedelta:
    if token_type is TokenType.ACCESS:
        return timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)


def create_token(
    *,
    user_id: int,
    username: str,
    role: str,
    token_type: TokenType,
    expires_delta: timedelta | None = None,
    jti: str | None = None,
) -> IssuedToken:
    now = datetime.now(timezone.utc)
    delta = expires_delta if expires_delta is not None else _expire_delta(token_type)
    expire_at = now + delta
    token_jti = jti or uuid.uuid4().hex
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "typ": token_type.value,
        "jti": token_jti,
        "iat": int(now.timestamp()),
        "exp": int(expire_at.timestamp()),
        "iss": settings.JWT_ISSUER,
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return IssuedToken(
        token=token,
        jti=token_jti,
        expires_at=expire_at.replace(tzinfo=None),
    )


def decode_token(token: str, expected_type: TokenType) -> TokenClaims:
    """解码并校验：签名 / 过期 / 签发者 / 类型。失败一律抛 AppException。"""
    if not token:
        raise AppException(ErrorCode.AUTH_TOKEN_MISSING, "缺少访问令牌")
    try:
        raw = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "jti", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AppException(ErrorCode.AUTH_TOKEN_EXPIRED, "令牌已过期") from exc
    except jwt.InvalidIssuerError as exc:
        raise AppException(ErrorCode.AUTH_TOKEN_INVALID, "令牌签发者不匹配") from exc
    except jwt.InvalidTokenError as exc:
        raise AppException(ErrorCode.AUTH_TOKEN_INVALID, "令牌非法") from exc

    try:
        claims = TokenClaims.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - 结构异常统一收敛
        raise AppException(ErrorCode.AUTH_TOKEN_INVALID, "令牌结构非法") from exc

    if claims.typ is not expected_type:
        raise AppException(
            ErrorCode.AUTH_TOKEN_TYPE_INVALID,
            f"需要 {expected_type.value} token，收到 {claims.typ.value}",
        )
    return claims
