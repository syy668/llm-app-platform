"""操作审计日志表 ORM 模型。"""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="操作人 users.id")
    action: Mapped[str] = mapped_column(String(64), nullable=False, comment="动作")
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="对象类型")
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="对象ID")
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="附加信息")
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
