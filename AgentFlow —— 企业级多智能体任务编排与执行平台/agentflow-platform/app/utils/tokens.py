"""Token 估算工具（PRD 9.1 / 9.2 需要在无 tokenizer 依赖时也能记账）。

估算口径（保守偏高，宁可早触发压缩）：
- CJK 汉字 / 全角标点：1 字 ≈ 1 Token
- ASCII 字母数字：4 字符 ≈ 1 Token
- 其他（空格、换行、半角标点）：2 字符 ≈ 1 Token

说明：生产可替换为 tiktoken / 模型自带 tokenizer，接口保持不变；
这里不引入重量级依赖，保证 CI 与离线环境一致。
"""

from __future__ import annotations

import json
import re
from typing import Any

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
_JSON_DUMPS = dict(ensure_ascii=False, separators=(",", ":"), default=str)


def estimate_tokens(text: str | None) -> int:
    """文本 -> 估算 Token 数。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    rest = text
    if cjk:
        rest = _CJK.sub("", text)
    # 剩余部分：按 ASCII 与非 ASCII 再分
    non_ascii = sum(1 for ch in rest if ord(ch) > 127)
    ascii_chars = len(rest) - non_ascii
    return cjk + (non_ascii + 1) // 2 + (ascii_chars + 3) // 4


def estimate_json_tokens(payload: Any) -> int:
    """结构化数据 -> 估算 Token（序列化后按文本估算）。"""
    if payload is None:
        return 0
    if isinstance(payload, str):
        return estimate_tokens(payload)
    try:
        text = json.dumps(payload, **_JSON_DUMPS)
    except (TypeError, ValueError):
        text = str(payload)
    return estimate_tokens(text)


def truncate_to_tokens(text: str, max_tokens: int, suffix: str = "…") -> str:
    """按估算 Token 截断（用于注入记忆 / 引用片段）。"""
    if max_tokens <= 0 or not text:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    # 逐段收敛，避免一次性 O(n^2)：按字符比例先砍，再线性微调
    keep = max(int(len(text) * max_tokens / max(estimate_tokens(text), 1)), 1)
    candidate = text[:keep]
    while candidate and estimate_tokens(candidate) > max_tokens:
        keep = int(keep * 0.9)
        candidate = text[:keep]
    return candidate + suffix


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """消息列表 -> 估算 Token（每条 +4 结构开销）。"""
    total = 0
    for msg in messages or []:
        total += 4
        total += estimate_tokens(str(msg.get("content", "")))
        for call in msg.get("tool_calls") or []:
            total += estimate_json_tokens(call)
    return total


def message_rounds(messages: list[dict[str, Any]]) -> int:
    """轮数：以 user / assistant 交替计，tool 消息不计入轮次。"""
    return sum(1 for m in messages or [] if m.get("role") in {"user", "assistant"})


__all__ = [
    "estimate_tokens",
    "estimate_json_tokens",
    "truncate_to_tokens",
    "estimate_messages_tokens",
    "message_rounds",
]
