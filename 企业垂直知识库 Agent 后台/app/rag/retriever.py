"""向量 + BM25 混合检索，使用 RRF（倒数排名融合）合并结果。

可选重排序：知识库配置 rerank_threshold > 0 时，对 RRF 融合的候选块
按「查询向量与块向量的余弦相似度」重排并过滤低于阈值的结果；
未配置阈值时保持纯 RRF 排序，行为与旧版完全一致。
"""
import logging
import math

from sqlalchemy.orm import Session

from ..models import DocumentChunk, KnowledgeBase
from . import bm25, embeddings, vector_store

logger = logging.getLogger(__name__)

# BM25 索引缓存：kb_id -> (corpus_chunks, BM25Index)
_bm25_cache: dict[int, tuple[list[DocumentChunk], bm25.BM25Index]] = {}

_RRF_K = 60

# 【新增】请求级检索模式枚举：
#   - "bm25_vector_hybrid"（默认）：向量 + BM25 双路召回，RRF 融合（与旧版行为一致）
#   - "vector_only"：纯向量检索，跳过 BM25 召回与 RRF 融合
SEARCH_MODE_HYBRID = "bm25_vector_hybrid"
SEARCH_MODE_VECTOR_ONLY = "vector_only"


def invalidate_bm25(kb_id: int) -> None:
    """文档增删后使对应知识库的 BM25 缓存失效。"""
    _bm25_cache.pop(kb_id, None)


def _load_chunks(db: Session, kb_id: int) -> list[DocumentChunk]:
    return (
        db.query(DocumentChunk)
        .filter(DocumentChunk.kb_id == kb_id)
        .order_by(DocumentChunk.doc_id, DocumentChunk.chunk_index)
        .all()
    )


def _get_bm25(db: Session, kb_id: int) -> tuple[list[DocumentChunk], bm25.BM25Index]:
    cached = _bm25_cache.get(kb_id)
    if cached:
        return cached
    chunks = _load_chunks(db, kb_id)
    index = bm25.BM25Index([c.content for c in chunks])
    _bm25_cache[kb_id] = (chunks, index)
    return chunks, index


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（零向量返回 0）。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def hybrid_retrieve(
    db: Session, kb: KnowledgeBase, question: str, top_k: int,
    search_mode: str | None = None,  # 【新增】请求级检索模式，None = 默认混合检索
) -> list[dict]:
    """混合检索：返回带引用来源的命中列表。

    search_mode【新增】：请求级临时参数，优先级高于知识库固定行为。
      - None / "bm25_vector_hybrid"：向量 + BM25 双路召回 + RRF 融合（原有逻辑，向后兼容）；
      - "vector_only"：纯向量检索，跳过 BM25 召回与 RRF 融合。

    kb.rerank_threshold > 0 时启用向量相似度重排序（两种模式均生效）：
    取融合后前 top_k*3 个候选，计算与查询的余弦相似度，
    过滤低于阈值者并按相似度重新排序（阈值语义：只保留与问题语义相关的块）。
    """
    chunks, index = _get_bm25(db, kb.id)
    if not chunks:
        return []

    # 1) 向量检索
    # 【v1.14】外部 embedding 不可用（欠费 402 / 密钥失效 401 / 超时）时降级纯 BM25：
    # BM25 索引是本地内存缓存、不依赖任何外部服务，降级后检索仍可工作，
    # 充值或换 key 之后自动恢复双路召回，无需改配置、无需重建索引。
    # 纯向量模式（vector_only）是调用方显式指定的检索方式，没有 BM25 可退，仍照常抛出。
    query_vec: list[float] | None = None
    embed_failed = False
    try:
        query_vec = embeddings.embed_texts([question])[0]
    except Exception as e:  # noqa: BLE001 - 外部服务异常类型不可控，统一降级
        if search_mode == SEARCH_MODE_VECTOR_ONLY:
            raise
        embed_failed = True
        logger.warning(
            "embedding_unavailable -> 降级纯 BM25 kb_id=%s reason=%s: %s",
            kb.id, type(e).__name__, str(e)[:160],
        )
    vector_hits = vector_store.query(kb.id, query_vec, top_k) if query_vec is not None else []

    chroma_to_chunk = {c.chroma_id: c for c in chunks if c.chroma_id}

    # 【新增】2) 按请求级 search_mode 决定是否叠加 BM25 召回
    if search_mode == SEARCH_MODE_VECTOR_ONLY:
        # 纯向量模式：向量库按距离升序返回（相关度降序），
        # 换算为有界相似度分 1/(1+dist)，与 rerank 分数同为 [0,1] 区间，便于统一阈值过滤
        ranked: list[tuple[int, float]] = [
            (chunk.id, 1.0 / (1.0 + max(dist, 0.0)))
            for chroma_id, dist in vector_hits
            if (chunk := chroma_to_chunk.get(chroma_id)) is not None
        ]
    else:
        # 混合模式（默认）：BM25 检索 + RRF 融合（排名越高权重越大）
        bm25_hits = index.search(question, top_k)  # [(corpus_index, score)]
        fused: dict[int, float] = {}
        for rank, (chroma_id, _dist) in enumerate(vector_hits, start=1):
            chunk = chroma_to_chunk.get(chroma_id)
            if chunk:
                fused[chunk.id] = fused.get(chunk.id, 0.0) + 1.0 / (_RRF_K + rank)
        for rank, (idx, _score) in enumerate(bm25_hits, start=1):
            if idx < len(chunks):
                chunk = chunks[idx]
                fused[chunk.id] = fused.get(chunk.id, 0.0) + 1.0 / (_RRF_K + rank)
        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)

    # 3) 可选：向量相似度重排序 + 阈值过滤（rerank_threshold 未配置时不生效，两种模式均适用）
    threshold = float(kb.rerank_threshold or 0)
    rerank_scores: dict[int, float] | None = None
    if threshold > 0 and embed_failed:
        # 重排序要现算查询与候选块的余弦，embedding 不可用时无法执行。
        # 这里选择「跳过重排序、保留融合排序」而不是「返回空」：阈值语义是
        # 「只保留语义相关的块」，降级时无从判定，宁可不过滤也不让用户一无所获。
        logger.warning("rerank_skipped reason=embedding_unavailable kb_id=%s", kb.id)
    elif threshold > 0:
        candidates = ranked[: top_k * 3]
        if candidates:
            by_id = {c.id: c for c in chunks}
            texts = [by_id[cid].content for cid, _ in candidates if cid in by_id]
            vecs = embeddings.embed_texts(texts) if texts else []
            sim_by_cid = {
                cid: _cosine(query_vec, vec)
                for (cid, _), vec in zip(candidates, vecs)
            }
            rerank_scores = sim_by_cid
            ranked = [
                (cid, sim)
                for cid, sim in sorted(sim_by_cid.items(), key=lambda x: x[1], reverse=True)
                if sim >= threshold
            ][:top_k]
        else:
            ranked = []

    ranked = ranked[:top_k]

    by_id = {c.id: c for c in chunks}
    results = []
    for chunk_id, score in ranked:
        c = by_id.get(chunk_id)
        if not c:
            continue
        meta = c.chunk_metadata or {}
        results.append({
            "chunk_id": c.id,
            "document_id": c.doc_id,
            "document_title": meta.get("title", f"文档{c.doc_id}"),
            "chunk_index": c.chunk_index,
            "content": c.content,
            # 启用重排序时输出相似度分，否则输出 RRF 分
            "score": round(score, 6),
        })
    return results
