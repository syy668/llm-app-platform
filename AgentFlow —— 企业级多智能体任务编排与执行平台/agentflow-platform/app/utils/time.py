"""时间工具（统一 UTC 存储、带时区安全）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def utcnow() -> datetime:
    """统一以 UTC naive datetime 落库（MySQL DATETIME 无时区）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_tz() -> datetime:
    return datetime.now(timezone.utc)


def from_timestamp(ts: int | float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def days_ago(days: int) -> date:
    return (utcnow() - timedelta(days=days)).date()
