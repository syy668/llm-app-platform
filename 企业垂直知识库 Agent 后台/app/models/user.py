"""用户表 ORM 模型。"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base

# 角色与状态取值（与数据库 ENUM 保持一致）
ROLE_ADMIN = "admin"
ROLE_USER = "user"
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, comment="登录名")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, comment="bcrypt 密码哈希")
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="显示名")
    email: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="邮箱")
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=ROLE_USER, comment="角色 admin/user")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_ACTIVE, comment="账号状态")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
