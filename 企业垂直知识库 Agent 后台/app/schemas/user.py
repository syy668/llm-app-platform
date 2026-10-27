"""认证与用户相关的请求/响应模型。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class LogoutRequest(BaseModel):
    """【双Token改造 新增】登出请求体：refresh_token 可选（传入即加入黑名单踢下线）。"""

    refresh_token: str | None = Field(None, min_length=1)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=64)


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=64)
    display_name: str | None = Field(None, max_length=64)
    email: str | None = Field(None, max_length=128)
    # 仅允许 admin / user 两种角色，从源头杜绝越权写入其他角色
    role: Literal["admin", "user"] = "user"


class UserUpdate(BaseModel):
    display_name: str | None = Field(None, max_length=64)
    email: str | None = Field(None, max_length=128)
    role: Literal["admin", "user"] | None = None
    status: Literal["active", "disabled"] | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=64)


class UserOut(BaseModel):
    """对外返回的用户对象（不含密码哈希）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str | None
    email: str | None
    role: str
    status: str
    created_at: datetime


def user_to_dict(user) -> dict:
    """将 ORM 用户对象转换为可序列化的字典（datetime -> ISO 字符串）。"""
    return UserOut.model_validate(user).model_dump(mode="json")
