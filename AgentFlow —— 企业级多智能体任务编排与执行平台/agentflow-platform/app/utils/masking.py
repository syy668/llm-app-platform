"""日志 / 轨迹 payload 脱敏（PRD NFR-S3：手机号、邮箱、token 打码）。"""

from __future__ import annotations

import re
from typing import Any

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]{1,3})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# 18 位身份证：6 位地区 + 8 位出生日期 + 3 位顺序码 + 1 位校验码（含 X）。末位 X 大小写都保留
_IDCARD_RE = re.compile(r"(?<!\d)(\d{6})(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")
_TOKEN_KEYS = {"token", "access_token", "refresh_token", "authorization", "password", "api_key", "secret"}
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")


def mask_phone(value: str) -> str:
    return _PHONE_RE.sub(r"\1****\2", value)


def mask_email(value: str) -> str:
    return _EMAIL_RE.sub(r"\1***\2", value)


def mask_idcard(value: str) -> str:
    """身份证打码：保留前 6 位（地区）与末 4 位，中间 8 位（生日+顺序码）打星。"""
    return _IDCARD_RE.sub(lambda m: f"{m.group(1)}********{m.group(0)[-4:]}", value)


def mask_pii(text: str) -> str:
    """PII 脱敏纯函数（合规红线）：bearer 凭证 / 身份证 / 手机号 / 邮箱。

    Trace 落库、日志、工具 payload 的统一入口；顺序无关——手机号正则带数字
    边界环视（``(?<!\\d)...(?!\\d)``），不会被 18 位身份证误切。
    """
    value = _BEARER_RE.sub("Bearer ***", text)
    value = _IDCARD_RE.sub(lambda m: f"{m.group(1)}********{m.group(0)[-4:]}", value)
    return mask_email(mask_phone(value))


def mask_text(value: str) -> str:
    return mask_pii(value)


def mask_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """递归脱敏字典：敏感 key 全量打码，字符串值做正则替换。"""
    masked: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in _TOKEN_KEYS:
            masked[key] = "***"
        elif isinstance(value, dict):
            masked[key] = mask_mapping(value)
        elif isinstance(value, list):
            masked[key] = [mask_mapping(v) if isinstance(v, dict) else (mask_text(v) if isinstance(v, str) else v) for v in value]
        elif isinstance(value, str):
            masked[key] = mask_text(value)
        else:
            masked[key] = value
    return masked
