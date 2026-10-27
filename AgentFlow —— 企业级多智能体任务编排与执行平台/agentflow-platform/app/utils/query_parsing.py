"""W6 查询参数解析（多值枚举 / 时间区间 / 排序）。

约定（W6接口设计文档 §0/§1）：
- 多值参数用英文逗号分隔，非法值 → 422 VALIDATION_ERROR；
- 时间区间参数闭区间 [start, end]，ISO8601；
- 排序只允许白名单值。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import TypeVar

from app.core.exceptions import AppException, ErrorCode

E = TypeVar("E", bound=Enum)


def parse_multi_enum(raw: str | None, enum_cls: type[E], *, field: str) -> list[E] | None:
    """'PLANNING,COMPLETED' -> [TaskStatus.PLANNING, TaskStatus.COMPLETED]；空串/缺省 -> None（不过滤）。"""
    if raw is None or not raw.strip():
        return None
    values: list[E] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            values.append(enum_cls(token))
        except ValueError as exc:
            allowed = ",".join(item.value for item in enum_cls)
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"参数 {field} 非法值: {token}（允许：{allowed}）",
            ) from exc
    return values or None


def parse_datetime(raw: str | None, *, field: str) -> datetime | None:
    """ISO8601 解析；非法 → 422。兼容结尾 'Z'（UTC）。"""
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"参数 {field} 不是合法的 ISO8601 时间: {raw}",
        ) from exc
    # 带 tzinfo 时转为 **UTC** naive 再比较（落库口径统一 UTC naive，见 app/utils/time.py）。
    # 注意不能转成本地时区：列里存的是 UTC，转本地会让时间窗整体偏移（UTC+8 环境偏 8 小时）。
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_order(raw: str | None, allowed: tuple[str, ...], default: str) -> str:
    value = (raw or default).strip() or default
    if value not in allowed:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"参数 order 非法值: {value}（允许：{','.join(allowed)}）",
        )
    return value


__all__ = ["parse_multi_enum", "parse_datetime", "parse_order"]
