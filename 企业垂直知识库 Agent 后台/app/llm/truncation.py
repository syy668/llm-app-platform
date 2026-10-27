"""输出截断感知：判断一次大模型返回是否被 max_tokens 硬切断。

## 为什么需要单独一层

`max_tokens` 只是「请求上限」，不是「结果保证」。一旦命中，各厂商的表现是：

- OpenAI 兼容协议（DeepSeek / 硅基流动 / 百炼 / 魔搭）：`finish_reason="length"`，
  正文在半句/半个表格处戛然而止，HTTP 依然 200；
- Anthropic：`stop_reason="max_tokens"`，同样是 200 且正文完整到不了结尾。

也就是说——**截断不会抛异常**。业务侧如果不主动看 finish_reason，
用户看到的就是一条「正常结束但话没说完」的回答，刷新后也一模一样，
事后连排查线索都没有（日志里只有一次成功的 LLM 调用）。

本模块把「协议差异」收成两个函数 + 一段统一提示文案，
供三处使用，避免各自硬编码字符串：
- `agent/graph.py::answer_node` —— 决定是否在回答末尾追加提示；
- `core/tracing.py` —— 落 `trace_logs.is_truncated`，供大盘统计截断率；
- `services/stream_service.py` —— SSE done 事件回传截断标记。

## 兼容矩阵（实测口径）

| 来源                  | 字段                        | 截断值        |
|-----------------------|-----------------------------|---------------|
| ChatOpenAI 兼容       | response_metadata.finish_reason | length    |
| ChatAnthropic         | response_metadata.stop_reason   | max_tokens|
| 部分网关/自建代理     | generation_info.finish_reason   | length    |

字段名大小写与来源都不稳定，故统一 lower() + 多键探测。
"""
from typing import Any

#: 归一化后的截断标记（全部小写）
_TRUNCATED_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})

#: 回答末尾统一追加的提示（与前端 SSE 的 truncation_notice 同源，只此一份）
TRUNCATION_NOTICE = "\n\n[注意：回答已达到最大长度限制，可能未完全展示。]"

#: 探测顺序：OpenAI 兼容 → Anthropic → 部分网关
_REASON_KEYS = ("finish_reason", "stop_reason", "finishReason")


def _reason_of(metadata: Any) -> str | None:
    """从 response_metadata / generation_info 里取出归一化的结束原因。"""
    if not isinstance(metadata, dict):
        return None
    for key in _REASON_KEYS:
        value = metadata.get(key)
        if value:
            return str(value).strip().lower()
    return None


def is_truncated_message(message: Any) -> bool:
    """判断单条返回消息（AIMessage / AIMessageChunk）是否被长度上限截断。"""
    if message is None:
        return False
    return _reason_of(getattr(message, "response_metadata", None)) in _TRUNCATED_REASONS


def is_truncated_result(llm_result: Any) -> bool:
    """判断 LLMResult / ChatResult 里**任意一条** generation 是否被截断。

    Trace 回调拿到的是 LLMResult（`generations: list[list[Generation]]`），
    与业务侧拿到的 AIMessage 不是同一层，故单独提供入口。
    batch 场景下只要有一条截断就判 True：宁可多提示一次，也不要漏报。
    """
    for group in getattr(llm_result, "generations", None) or []:
        for generation in group or []:
            if _reason_of(getattr(generation, "generation_info", None)) in _TRUNCATED_REASONS:
                return True
            if is_truncated_message(getattr(generation, "message", None)):
                return True
    return False


def append_notice(answer: str) -> str:
    """在回答末尾追加截断提示（已追加过则不重复）。"""
    text = answer or ""
    if TRUNCATION_NOTICE.strip() and TRUNCATION_NOTICE in text:
        return text
    return text + TRUNCATION_NOTICE
