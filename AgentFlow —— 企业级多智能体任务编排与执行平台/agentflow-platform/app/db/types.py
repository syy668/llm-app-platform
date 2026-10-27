"""数据库列类型助手（MySQL 原生 ENUM，SQLite 下自动降级为带 CHECK 的 VARCHAR）。"""

from __future__ import annotations

import enum

from sqlalchemy import Enum as SAEnum


def db_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """统一枚举列定义。

    - native_enum=True：MySQL 生成原生 ENUM（严格对齐 PRD 字段类型）
    - values_callable：落库值取 Enum.value（如 'admin' / 'PLANNING'），而非成员名
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda e: [str(member.value) for member in e],
        validate_strings=True,
    )
