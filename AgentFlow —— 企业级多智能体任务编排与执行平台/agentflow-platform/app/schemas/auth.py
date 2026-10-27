"""鉴权相关 Pydantic 模型（请求 / 响应 / 当前用户上下文）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.security import TokenType
from app.schemas.enums import UserRole


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, description="登录名")
    password: str = Field(min_length=1, max_length=128, description="明文密码，仅用于本次校验")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, description="登录时下发的 refresh_token")


class TokenPair(BaseModel):
    access_token: str = Field(description="访问令牌，默认 2h 有效")
    refresh_token: str = Field(description="刷新令牌，默认 7d 有效，一次性消费")
    token_type: str = Field(default="Bearer", description="令牌类型")
    expires_in: int = Field(description="access_token 剩余有效期（秒）")


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: UserRole
    status: int
    last_login_at: datetime | None = None


class CurrentUser(BaseModel):
    """请求级登录态：由 get_current_user 依赖注入。"""

    id: int
    username: str
    role: UserRole
    status: int = 1
    jti: str | None = Field(default=None, description="当前 access token 的 jti（注销拉黑使用）")
    token_type: TokenType = TokenType.ACCESS
    expires_at: datetime | None = Field(default=None, description="当前 access token 过期时间")

    def has_role(self, *roles: UserRole) -> bool:
        return self.role in roles
