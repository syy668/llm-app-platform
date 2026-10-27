"""结构化输出强制校验 + 修复重试（PRD FR-002 验收 2 / FR-010 验收 3）。

流程（对 Planner / Reviewer / Summarizer 通用）：
1. 调 LLM 取文本 → 提取 JSON（容忍 ```json 围栏与前后废话）；
2. Pydantic 校验；
3. 失败 → 把"原始输出 + 校验错误"回喂 LLM 重新生成，最多 ``STRUCTURED_REPAIR_MAX_RETRY``（2）次；
4. 仍失败 → 若配置了备用模型（SiliconFlow），再试 1 次；
5. 全部失败 → 抛 ``StructuredOutputError``（上游按节点语义决定 FAILED 或降级）。

累计 Token 一并返回，便于节点写入 ``token_ledger``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.agents.llm import LLMClient, LLMError, LLMResponse
from app.core.logging import get_logger
from app.core.w2_config import w2

logger = get_logger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    def __init__(self, message: str, *, attempts: int, raw: str = "", errors: list[str] | None = None) -> None:
        self.code = "PLAN_INVALID"
        self.attempts = attempts
        self.raw = raw
        self.errors = errors or []
        super().__init__(message)


@dataclass(slots=True)
class StructuredResult:
    value: Any
    response: LLMResponse
    attempts: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    repair_errors: list[str] = field(default_factory=list)
    used_fallback: bool = False


def _repair_candidates(fragment: str) -> list[str]:
    """加固任务 6：对畸形 JSON 片段生成保守修复候选（逐个尝试，失败无害）。

    覆盖 LLM 最常见的三类轻量畸形，修好可省一次 LLM 回喂重试：
    1. 缺右括号（截断输出）：按栈补齐未闭合的 ``}``/``]``（含未闭合字符串先闭合）；
    2. 尾逗号（``[1,2,]``）：删除 ``}``/``]`` 前的悬挂逗号；
    3. Python 字面量（``True/False/None``）：转 JSON 的 ``true/false/null``。
    顺序：先原样补括号，再组合修复；任何一步解析成功即返回。
    """
    import re

    candidates: list[str] = []

    def _close_open_braces(text: str) -> str | None:
        stack: list[str] = []
        in_string = False
        escape = False
        for char in text:
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char in "{[":
                stack.append("}" if char == "{" else "]")
            elif char in "}]":
                if not stack or stack[-1] != char:
                    return None  # 括号交错，超出保守修复范围
                stack.pop()
        if in_string:
            text += '"'
        if not stack:
            return None
        return text + "".join(reversed(stack))

    closed = _close_open_braces(fragment)
    if closed is not None:
        candidates.append(closed)
        candidates.append(re.sub(r",\s*([}\]])", r"\1", closed))  # 补括号后再去尾逗号
        candidates.append(
            re.sub(r",\s*([}\]])", r"\1", re.sub(r"\b(True|False|None)\b", lambda m: {"True": "true", "False": "false", "None": "null"}[m.group(1)], closed))
        )
    else:
        candidates.append(re.sub(r",\s*([}\]])", r"\1", fragment))
        candidates.append(
            re.sub(r"\b(True|False|None)\b", lambda m: {"True": "true", "False": "false", "None": "null"}[m.group(1)], fragment)
        )
    return candidates


def extract_json(text: str) -> Any:
    """从 LLM 自由文本中提取第一个完整 JSON 对象/数组。

    四级递进（加固任务 6）：直接解析 → 去围栏 → 平衡括号扫描 → 轻量修复
    （缺右括号/尾逗号/Python 字面量）；全部失败才抛 ValueError 交给上层回喂重试。
    """
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("LLM 返回为空")

    # 1) 直接解析
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2) 去掉 ```json ... ``` 围栏
    if "```" in stripped:
        blocks = stripped.split("```")
        for block in blocks:
            candidate = block.strip()
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") or candidate.startswith("["):
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    for fixed in _repair_candidates(candidate):
                        try:
                            return json.loads(fixed)
                        except json.JSONDecodeError:
                            continue

    # 3) 扫描首个平衡括号片段
    start_index = min(
        (idx for idx in (stripped.find("{"), stripped.find("[")) if idx != -1),
        default=-1,
    )
    if start_index == -1:
        raise ValueError("未找到 JSON 结构")

    opening = stripped[start_index]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for index in range(start_index, len(stripped)):
        char = stripped[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                snippet = stripped[start_index : index + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError:
                    # 平衡但语法错（尾逗号/字面量）→ 轻量修复
                    for fixed in _repair_candidates(snippet):
                        try:
                            return json.loads(fixed)
                        except json.JSONDecodeError:
                            continue
                    raise ValueError(f"JSON 语法错误: {snippet[:80]}") from None

    # 4) 结构不完整（缺右括号/截断）→ 轻量修复补齐
    for fixed in _repair_candidates(stripped[start_index:]):
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            continue
    raise ValueError("JSON 结构不完整")


def _estimate_chars(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


def truncate_context(
    messages: list[dict[str, Any]],
    max_chars: int = 12000,
) -> list[dict[str, Any]]:
    """上下文超载硬熔断（v2.1）：纯函数，不修改入参。

    策略：
    1. 总字符数未超限 → 原样返回（零开销，不影响既有行为）；
    2. 超限 → System Message（约束规则）**必须全量保留**且排在最前；
       其余消息从最新往回保留、最早的先丢弃；
    3. 若最新一条消息本身超预算（典型：[system, 超长 user]），不能整条丢弃
       （会把"问题"丢掉），就地硬截断正文并追加标记；
    4. 触发截断时打 WARNING 日志 ``context_truncated``（丢弃条数 + 原始长度）。
    """
    original_chars = _estimate_chars(messages)
    if original_chars <= max_chars:
        return messages

    system_msgs = [m for m in messages if m.get("role") == "system"]
    others = [m for m in messages if m.get("role") != "system"]
    # 下限 200 只防御"System 本身超限"的病态场景（此时总量必然超限，
    # 优先保 System 是需求约定）；正常情况下 budget 严格 = max_chars - system
    budget = max(max_chars - _estimate_chars(system_msgs), 200)

    kept: list[dict[str, Any]] = []  # 新 → 旧
    dropped = 0
    used = 0
    for msg in reversed(others):
        content = str(msg.get("content") or "")
        cost = len(content)
        if used + cost <= budget:
            kept.append(msg)
            used += cost
        elif not kept:
            # 最新一条就超预算：保住开头（约束/问题在前），硬截断尾部
            truncated = dict(msg)
            truncated["content"] = content[: max(budget - 50, 200)] + "…[上下文超限已截断]"
            kept.append(truncated)
            dropped += 1
            used = budget
        else:
            dropped += 1  # 预算已尽：更早的历史一律丢弃

    result = [*system_msgs, *reversed(kept)]
    logger.warning(
        "context_truncated",
        extra={
            "dropped": dropped,
            "original_chars": original_chars,
            "truncated_chars": _estimate_chars(result),
            "messages_in": len(messages),
            "messages_out": len(result),
        },
    )
    return result


async def complete_structured(
    llm: LLMClient,
    messages: list[dict[str, Any]],
    model_cls: type[TModel],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    max_repair: int | None = None,
    use_fallback_once: bool = True,
) -> StructuredResult:
    """调用 LLM 并强制产出 ``model_cls``；失败自动修复重试。"""
    repair_limit = w2.STRUCTURED_REPAIR_MAX_RETRY if max_repair is None else max_repair
    # v2.1：所有节点发给 LLM 的 messages 统一在公共入口做超载熔断
    conversation = truncate_context(list(messages), max_chars=w2.CONTEXT_MAX_CHARS)
    errors: list[str] = []
    total_in = 0
    total_out = 0
    last_raw = ""
    attempts = 0

    clients = [llm]
    fallback = getattr(llm, "fallback", None)
    if use_fallback_once and fallback is not None:
        clients.append(fallback)  # type: ignore[arg-type]

    for client_index, client in enumerate(clients):
        trial_limit = repair_limit + 1
        for trial in range(trial_limit):
            attempts += 1
            response = await client.complete(
                conversation, temperature=temperature, max_tokens=max_tokens, json_mode=True
            )
            total_in += response.prompt_tokens
            total_out += response.completion_tokens
            last_raw = response.content

            try:
                parsed = extract_json(response.content)
                value = model_cls.model_validate(parsed)
            except (ValueError, json.JSONDecodeError) as exc:
                error_text = f"JSON 解析失败: {exc}"
            except ValidationError as exc:
                error_text = _format_validation_error(exc)
            else:
                return StructuredResult(
                    value=value,
                    response=response,
                    attempts=attempts,
                    input_tokens=total_in,
                    output_tokens=total_out,
                    repair_errors=errors,
                    used_fallback=client_index > 0,
                )

            errors.append(error_text)
            logger.warning(
                "structured_output_invalid",
                extra={
                    "model": model_cls.__name__,
                    "attempt": attempts,
                    "error": error_text[:300],
                    "provider": response.provider,
                },
            )
            if trial == trial_limit - 1:
                break
            conversation = [
                *conversation,
                {"role": "assistant", "content": response.content[:4000]},
                {
                    "role": "user",
                    "content": (
                        "上面的输出不符合要求，必须修复。错误如下：\n"
                        f"{error_text}\n"
                        f"请仅输出符合 `{model_cls.__name__}` 结构要求的 JSON 对象，不要任何解释或 markdown 围栏。"
                    ),
                },
            ]

    # 加固任务 6：修复重试穷尽（含备用模型）——ERROR 级结构化日志留痕，
    # 供 BadCase 排查；异常继续上抛，由各节点按语义兜底（Planner→FAILED / Reviewer→降级放行）
    logger.error(
        "structured_output_exhausted",
        extra={
            "model": model_cls.__name__,
            "attempts": attempts,
            "errors_count": len(errors),
            "raw_preview": last_raw[:200],
        },
    )
    raise StructuredOutputError(
        f"结构化输出连续 {attempts} 次校验失败：{errors[-1] if errors else 'unknown'}",
        attempts=attempts,
        raw=last_raw,
        errors=errors,
    )


def _format_validation_error(exc: ValidationError) -> str:
    items = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(part) for part in err.get("loc", []))
        items.append(f"{loc}: {err.get('msg', '')}")
    return "字段校验失败 -> " + "; ".join(items)


__all__ = [
    "StructuredOutputError",
    "StructuredResult",
    "extract_json",
    "truncate_context",
    "complete_structured",
]
