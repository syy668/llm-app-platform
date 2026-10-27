"""RAG 检索工具：对指定知识库执行向量+BM25混合检索，返回拼接好的上下文文本。

工具自建数据库会话（不共享请求会话），便于放到独立线程中执行并施加超时。

【v1.9】新增 `rag_retrieve_with_sources`：同时返回「给模型看的带标号文本」与
「给前端看的结构化来源」（文档名 / 块号 / 相似度 / 原文），用于回答下方的引用卡片。
`rag_retrieve` 保留原签名返回纯文本，历史调用方零改动。
"""
import logging

from ...core.database import SessionLocal
from ...models import KnowledgeBase
from ...rag import retriever
from .retry import with_retry

logger = logging.getLogger(__name__)

_RAG_TOP_K = 5

# 无命中时的固定文案：不让模型拿到空上下文自由发挥（防幻觉第一道闸）
NO_HIT_TEXT = "知识库中未检索到与问题相关的内容。"


#: 块号对外展示 = 存储序号 + 1。
#: chunk_index 在库里是 0-based（`range(len(chunks))`），直接拼进上下文会让模型
#: 输出「第 0 块」，用户看到必然误判成 bug。这里统一换算成 1-based，
#: 让「模型抄的号 / 用户看到的号 / 前端匹配用的号」三者一致。
def display_chunk_no(hit: dict) -> int:
    """存储序号（0-based）-> 展示序号（1-based）。"""
    idx = hit.get("chunk_index")
    if idx is None:
        return 0
    return int(idx) + 1


def format_context(hits: list[dict]) -> str:
    """把检索命中拼成带 `[来源i]` 标号的上下文文本（标号供答案内引用与溯源）。"""
    parts = []
    for i, h in enumerate(hits, start=1):
        parts.append(
            f"[来源{i}] 文档《{h['document_title']}》 第{display_chunk_no(h)}块：\n{h['content']}"
        )
    return "\n\n".join(parts)


def to_sources(hits: list[dict]) -> list[dict]:
    """命中 -> 前端引用卡片所需的结构化来源（去掉与 UI 无关的内部字段）。

    `chunk_index` 保留存储原值（0-based）以备排查，`chunk_no` 是给用户看的 1-based 号。
    前端一律以 `chunk_no` 展示与匹配。
    """
    return [
        {
            "index": i,
            "chunk_id": h.get("chunk_id"),
            "document_id": h.get("document_id"),
            "document_title": h.get("document_title"),
            "chunk_index": h.get("chunk_index"),
            "chunk_no": display_chunk_no(h),
            "score": h.get("score"),
            "content": h.get("content"),
        }
        for i, h in enumerate(hits, start=1)
    ]


@with_retry
def _retrieve(question: str, kb_id: int, top_k: int | None = None,
              search_mode: str | None = None) -> tuple[str, list[dict]]:
    """实际执行检索（唯一带重试的实现，两个公开入口都只套一层 retry）。"""
    db = SessionLocal()
    try:
        kb = db.get(KnowledgeBase, kb_id)
        if not kb:
            return "未找到对应的知识库，无法检索。", []

        hits = retriever.hybrid_retrieve(
            db, kb, question, top_k or kb.top_k or _RAG_TOP_K,
            search_mode=search_mode,
        )
        if not hits:
            return NO_HIT_TEXT, []

        return format_context(hits), to_sources(hits)
    except Exception as exc:  # 检索异常不阻断 Agent，降级为提示文本
        logger.exception("RAG 检索失败 kb_id=%s", kb_id)
        return f"RAG 检索失败：{exc}", []
    finally:
        db.close()


def rag_retrieve_with_sources(
    question: str, kb_id: int, top_k: int | None = None,
    search_mode: str | None = None,
) -> tuple[str, list[dict]]:
    """检索知识库，返回 (上下文文本, 结构化来源列表)。

    检索条数优先级：随请求传入的 top_k > 知识库自身配置（kb.top_k） > 默认 5。
    检索模式：vector_only 纯向量 / bm25_vector_hybrid 混合；未传走默认混合检索。
    异常不阻断 Agent：降级为空来源 + 提示文本（与旧实现行为一致）。
    """
    return _retrieve(question, kb_id, top_k, search_mode)


def rag_retrieve(
    question: str, kb_id: int, top_k: int | None = None,
    search_mode: str | None = None,
) -> str:
    """检索知识库，返回带来源标识的上下文文本（沿用原签名，历史调用方零改动）。"""
    text, _sources = _retrieve(question, kb_id, top_k, search_mode)
    return text
