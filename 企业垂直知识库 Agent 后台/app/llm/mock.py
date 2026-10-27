"""Mock LLM：不连任何大模型 / 不需要密钥，按规则产出可预期结果。

用途（这是本文件存在的唯一理由）：
1. 离线演示——断网、没有 API Key 也能把「上传文档 → 检索 → 问答 → Trace」整条链路走通；
2. CI 测试——断言稳定的字符串，不受上游模型波动、限流、锁区影响。

设计约束：
- 必须实现 LangChain `BaseChatModel` 接口，这样 Agent 图（classify / answer / 对比拆词）
  与 /kbs/{id}/query 可以共用同一套调用姿势（invoke / stream / with_structured_output），
  业务代码无需为 mock 写 if 分支；
- 产出由**输入 prompt 的形态**决定，而不是随机数，确保同一问题永远同一答案；
- 支持 `_stream`，使 SSE 逐字输出在 mock 下依然可用（真流式，不是一次性吐出再切片）。

规则总表（与 docs 交付说明保持一致）：
| prompt 形态                        | 输出                                        |
|------------------------------------|---------------------------------------------|
| 含【资料】+【回答】（知识库问答）  | 回显前 3 条资料；无资料则「资料中未找到相关信息。」 |
| 含【工具检索/查询结果】（Agent 回答） | 基于工具结果给出摘要式回答                  |
| 含 query_a（对比拆词）             | 规则切分出两个检索词，返回 JSON             |
| 结构化输出 schema 含 route 字段    | 关键词规则判定五路之一                      |
| 其他                               | 固定格式的「已收到问题」提示                |
"""
import json
import re
from typing import Any, Iterator, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableLambda

# ---------------- 关键词规则（顺序即优先级，先命中先返回） ----------------

# 报表/统计类：命中即 report
_REPORT_RE = re.compile(r"报表|统计|汇总|周报|月报|报一份|出一份")
# 对比类：命中即 parallel_compare
_COMPARE_RE = re.compile(r"对比|比较|差异|区别|不同|vs|VS")
# 业务库明细类：设备/工单/告警等结构化数据
_DB_RE = re.compile(r"设备|工单|告警|台账|清单|列表|多少台|有多少|数量|明细")
# 知识库类：制度/流程/手册/操作方法
_RAG_RE = re.compile(r"知识库|资料|文档|制度|流程|规定|标准|手册|指引|怎么办|如何|怎么")

# 对比问题的规则切分（与 agent/graph.py 的 _fallback_split 口径一致）
_LEADING_VERB_RE = re.compile(r"^(请|帮我|麻烦)?(对比|比较)(一下)?")
_SPLIT_RE = re.compile(r"[、，,。]|和|与|跟|及|[Vv][Ss]")

# prompt 里的定位锚点
_Q_MARK = "用户问题："
_RAG_CTX_START = "【资料】"
_RAG_CTX_END = "【问题】"
_TOOL_MARK = "【工具检索/查询结果】"
_TOOL_END_MARK = "请基于以上结果"

_NO_CONTEXT_TEXT = "资料中未找到相关信息。"
_NO_TOOL_TEXT = "知识库中未找到相关信息（模拟模式）。"


def _messages_to_text(messages: Any) -> str:
    """把 BaseMessage 列表 / 字符串统一拼成纯文本，便于规则匹配。"""
    if isinstance(messages, str):
        return messages
    if isinstance(messages, BaseMessage):
        return str(messages.content or "")
    if isinstance(messages, Sequence):
        parts = []
        for m in messages:
            content = getattr(m, "content", None)
            if content is None and isinstance(m, str):
                content = m
            if content:
                parts.append(str(content))
        return "\n".join(parts)
    return str(messages or "")


def _extract_question(text: str) -> str:
    """从 prompt 中截出「用户真正问的那句话」。

    必须截取而不是全文匹配：路由 prompt 自带「report：需要汇总、统计或生成报表」
    这类说明文字，全文匹配会让每道题都判成 report。
    """
    idx = text.rfind(_Q_MARK)
    if idx != -1:
        return text[idx + len(_Q_MARK):].strip()
    return text.strip()


def _pick_question(text: str, human_messages: Sequence[str]) -> str:
    """确定「要复述给用户看的问题」。

    优先取最后一条 human 消息（Agent 回答节点会把工具结果作为末条 human 追加，
    真正的提问是它前一条），都没有时再退化到 prompt 里的「用户问题：」锚点。
    """
    for content in reversed(list(human_messages or [])):
        text_item = str(content or "").strip()
        if text_item and not text_item.startswith(_TOOL_MARK):
            return text_item
    return _extract_question(text)


def _guess_route(question: str) -> str:
    """关键词规则判定五路之一；无法判定时回退 direct。"""
    if _COMPARE_RE.search(question):
        return "parallel_compare"
    if _REPORT_RE.search(question):
        return "report"
    if _DB_RE.search(question):
        return "db_query"
    if _RAG_RE.search(question):
        return "rag"
    return "direct"


def _split_pair(question: str) -> tuple[str, str]:
    """把对比问题拆成两个检索词；拆不开则两边都用原问题。"""
    text = _LEADING_VERB_RE.sub("", question).strip(" ？?：:")
    parts = [p.strip(" 的？?。.、") for p in _SPLIT_RE.split(text) if p.strip()]
    if len(parts) >= 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return question, question


def _mock_rag_answer(text: str) -> str:
    """知识库问答（/kbs/{id}/query）：回显前 3 条资料。

    与改造前 rag/llm.py 的 mock 分支**逐字节一致**，避免已有演示脚本/文档口径漂移。
    """
    start = text.find(_RAG_CTX_START)
    end = text.find(_RAG_CTX_END)
    ctx_block = text[start + len(_RAG_CTX_START):end] if (start != -1 and end > start) else ""
    items = [
        m.group(1).strip()
        for m in re.finditer(r"\[资料 \d+\] (.*?)(?=\n\n\[资料 \d+\]|\Z)", ctx_block, re.S)
    ]
    items = [i for i in items if i]
    if not items:
        return _NO_CONTEXT_TEXT
    return "根据知识库资料，检索到以下相关内容：\n\n" + "\n\n".join(items[:3])


def _mock_agent_answer(text: str, human_messages: Sequence[str] = ()) -> str:
    """Agent answer 节点：把工具结果整理成一段确定的回答。

    提问取自真正的 human 消息，避免把 system prompt 当成问题复述出去。
    """
    question = _pick_question(text, human_messages)
    start = text.find(_TOOL_MARK)
    end = text.find(_TOOL_END_MARK)
    tool_output = ""
    if start != -1:
        tool_output = text[start + len(_TOOL_MARK):end if end > start else len(text)].strip()
    if not tool_output:
        return f"【模拟回答】{_NO_TOOL_TEXT}\n\n（当前 LLM_PROVIDER=mock，未调用真实大模型；问题：{question or '空'}）"
    snippet = tool_output if len(tool_output) <= 600 else tool_output[:600] + "…"
    return (
        f"【模拟回答】{question or '你的问题'}\n\n"
        f"根据检索到的内容，整理如下：\n\n{snippet}\n\n"
        "（以上为 mock 模式下的规则化回答，仅用于离线演示与自动化测试）"
    )


class MockChatModel(BaseChatModel):
    """符合 LangChain ChatModel 协议的规则化假模型。"""

    # 便于在 Trace / 日志里区分「这是假模型」
    model: str = "mock-rules-v1"

    @property
    def _llm_type(self) -> str:
        return "mock"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model, "provider": "mock"}

    # ---------------- 核心：根据 prompt 形态产出文本 ----------------

    def _compose(self, text: str, human_messages: Sequence[str] = ()) -> str:
        """按优先级匹配 prompt 形态，返回确定性文本。"""
        if _RAG_CTX_START in text and _RAG_CTX_END in text:
            return _mock_rag_answer(text)
        if _TOOL_MARK in text:
            return _mock_agent_answer(text, human_messages)
        # 拆词 / 路由类 prompt 自带「用户问题：」锚点，以锚点为准（比整条 prompt 可靠）；
        # 普通对话则取真正的 human 消息，避免把 system prompt 复述出去。
        marker_question = _extract_question(text) if _Q_MARK in text else ""
        question = marker_question or _pick_question(text, human_messages)
        if "query_a" in text:
            qa, qb = _split_pair(question)
            return json.dumps({"query_a": qa, "query_b": qb}, ensure_ascii=False)
        if "route" in text:
            return json.dumps({"route": _guess_route(question)}, ensure_ascii=False)
        return (
            f"【模拟回答】已收到问题：{question or '（空）'}。"
            "当前 LLM_PROVIDER=mock，未调用真实大模型。"
        )

    @staticmethod
    def _human_texts(messages: Any) -> list[str]:
        """取出所有 human 消息的文本（用于定位真正的用户提问）。"""
        if not isinstance(messages, Sequence) or isinstance(messages, str):
            return []
        return [
            str(m.content or "")
            for m in messages
            if isinstance(m, HumanMessage) and str(m.content or "").strip()
        ]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        answer = self._compose(_messages_to_text(messages), self._human_texts(messages))
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=answer))],
            llm_output={"provider": "mock", "model": self.model},
        )

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """按固定长度切片输出，让 SSE 在 mock 下也有真实的逐字效果。"""
        answer = self._compose(_messages_to_text(messages), self._human_texts(messages))
        step = 24
        for i in range(0, len(answer), step):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=answer[i:i + step]))
            if run_manager:
                run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk

    # ---------------- 结构化输出：供 router.classify_route 使用 ----------------

    def with_structured_output(
        self,
        schema: Any,
        *,
        method: str = "function_calling",
        include_raw: bool = False,
        **kwargs: Any,
    ):
        """用规则替代 tool_calling 结构化输出，返回与真实模型同形的 schema 实例。"""

        def _invoke(input: Any, config: Any = None, **kw: Any) -> Any:
            text = _messages_to_text(input)
            question = _extract_question(text)
            parsed = self._structured(schema, question)
            if include_raw:
                return {
                    "raw": AIMessage(content=json.dumps(parsed, ensure_ascii=False, default=str)),
                    "parsed": parsed,
                    "parsing_error": None,
                }
            return parsed

        return RunnableLambda(_invoke)

    def _structured(self, schema: Any, question: str) -> Any:
        """按 schema 字段构造结果；无法识别时抛错，由调用方走既有兜底逻辑。"""
        fields = getattr(schema, "model_fields", {}) or {}
        if "route" in fields:
            return schema(route=_guess_route(question))
        if {"query_a", "query_b"} <= set(fields):
            qa, qb = _split_pair(question)
            return schema(query_a=qa, query_b=qb)
        raise ValueError(f"MockChatModel 不支持的结构化输出 schema: {list(fields)}")
