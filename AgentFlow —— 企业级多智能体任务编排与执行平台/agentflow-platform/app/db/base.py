"""SQLAlchemy 2.0 声明式基类、命名约定、公共列。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, MetaData, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.utils.time import utcnow

# 统一约束命名，保证 alembic autogenerate 产出稳定可回滚的迁移脚本
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全局声明式基类（PRD 第六章全部表继承它）。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


def pk_column() -> Mapped[int]:
    """主键：MySQL BIGINT UNSIGNED AUTO_INCREMENT；SQLite 下退化为 INTEGER 以支持自增（测试用）。"""
    return mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )


class TimestampMixin:
    """所有表统一 created_at / updated_at（PRD 第六章通用约定）。

    时区口径（**务必遵守**）：两列都由应用层 `utcnow()` 赋 **UTC naive** 值，与
    `tasks.finished_at` / `task_executions.started_at` 等 Python 写入的时间保持一致。

    历史坑：早期只给了 `server_default=CURRENT_TIMESTAMP`，而 MySQL 服务器时区是本地（UTC+8），
    导致 `created_at`（本地墙钟）与 `finished_at`（UTC）混在同一张表里，`finished - created`
    直接算出负数（实测 -28787s）。因此这里显式挂 Python 端 default；`server_default` 仅作
    DDL 兜底（配合 `session.py` 把会话时区固定为 +00:00，裸 SQL 插入也不会再写本地时间）。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="创建时间（UTC naive）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="更新时间（UTC naive）",
    )
