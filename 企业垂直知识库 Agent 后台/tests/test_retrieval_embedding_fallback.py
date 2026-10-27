"""外部 Embedding 不可用时的检索降级（v1.14）。

## 背景

线上真实故障：硅基流动账户欠费，`/v1/embeddings` 返回
`402 Payment Required {'code': 30001, 'message': 'account balance is insufficient'}`。
`hybrid_retrieve` 第一行就要向量化，异常一路冒到 `rag_node`，用户只看到
「AI 服务调用失败，请稍后重试」——但其实 **BM25 索引就在本地内存里，完全可以继续检索**。

## 本文件锁死的行为

1. embedding 异常 → 降级纯 BM25，**结果仍返回**（不是空、不是抛错）；
2. 降级时不再调用向量库（没有查询向量，不该拿 None 去问 Chroma）；
3. `vector_only` 是调用方显式指定的检索方式，没有 BM25 可退 → 仍照常抛错；
4. `rerank_threshold > 0` 时重排序要现算余弦，降级无法执行 → 跳过过滤而不是返回空；
5. 降级必须留下 WARNING 日志，便于从日志侧统计发生率。
"""
import logging

import pytest

from app.core.database import Base, SessionLocal, engine
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_base import KnowledgeBase
from app.rag import retriever
from app.rag.bm25 import BM25Index

KB_ID = 889
C1_CONTENT = "机器学习算法入门教程 线性回归 决策树 聚类 主流模型介绍"
C2_CONTENT = "深度学习与神经网络基础 反向传播 损失函数"
C3_CONTENT = "员工差旅报销制度流程说明 发票要求 审批时限"

EMBED_ERROR = "Error code: 402 - {'code': 30001, 'message': 'account balance is insufficient'}"


def _emb_raises(_texts):
    raise RuntimeError(EMBED_ERROR)


def _emb_ok(texts):
    return [[0.1, 0.2, 0.3] for _ in texts]


@pytest.fixture()
def env(monkeypatch):
    """内存库 + 1 知识库 3 块；向量库调用计数；embedding 行为由用例自己换。"""
    Base.metadata.create_all(bind=engine, checkfirst=True)
    db = SessionLocal()
    try:
        db.add(KnowledgeBase(
            id=KB_ID, name="embedding 降级测试库", owner_id=1, embedding_model="mock",
            chunk_size=500, chunk_overlap=50, top_k=5, rerank_threshold=0.0,
            status="ready", doc_count=0,
        ))
        db.flush()
        for cid, content, chroma_id in (
            (11, C1_CONTENT, "chunk-c1"),
            (12, C2_CONTENT, "chunk-c2"),
            (13, C3_CONTENT, "chunk-c3"),
        ):
            db.add(DocumentChunk(
                id=cid, doc_id=1, kb_id=KB_ID, chunk_index=cid - 11,
                content=content, token_count=0, chroma_id=chroma_id,
            ))
        db.commit()

        stats = {"vector_calls": 0, "bm25_calls": 0}

        def spy_query(kb_id, vec, top_k):
            stats["vector_calls"] += 1
            return [("chunk-c3", 0.2)]

        orig_search = BM25Index.search

        def spy_search(self, query, top_k):
            stats["bm25_calls"] += 1
            return orig_search(self, query, top_k)

        monkeypatch.setattr(retriever.vector_store, "query", spy_query)
        monkeypatch.setattr(BM25Index, "search", spy_search)
        retriever.invalidate_bm25(KB_ID)
        yield db, stats
    finally:
        retriever.invalidate_bm25(KB_ID)
        db.rollback()
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def kb(env):
    db, _ = env
    return db.get(KnowledgeBase, KB_ID)


def test_embedding_failure_degrades_to_bm25(env, kb, monkeypatch, caplog):
    """embedding 挂掉 → 仍用 BM25 返回结果，且不碰向量库。"""
    db, stats = env
    monkeypatch.setattr(retriever.embeddings, "embed_texts", _emb_raises)

    with caplog.at_level(logging.WARNING, logger=retriever.__name__):
        hits = retriever.hybrid_retrieve(db, kb, "机器学习 算法 入门", 5)

    ids = [h["chunk_id"] for h in hits]
    assert 11 in ids, "BM25 强匹配的 c1 必须被召回"
    assert stats["vector_calls"] == 0, "没有查询向量时不得调用向量库"
    assert stats["bm25_calls"] >= 1
    assert any("embedding_unavailable" in r.getMessage() for r in caplog.records)


def test_vector_only_still_raises_on_embedding_failure(env, kb, monkeypatch):
    """纯向量是显式诉求，没有 BM25 可退，不能悄悄换检索方式。"""
    db, _ = env
    monkeypatch.setattr(retriever.embeddings, "embed_texts", _emb_raises)

    with pytest.raises(RuntimeError, match="402"):
        retriever.hybrid_retrieve(db, kb, "机器学习", 5, search_mode="vector_only")


def test_rerank_skipped_not_empty_when_embedding_down(env, kb, monkeypatch, caplog):
    """阈值重排序依赖余弦，降级时跳过过滤而不是把结果清空。"""
    db, _ = env
    kb.rerank_threshold = 0.5
    db.commit()
    monkeypatch.setattr(retriever.embeddings, "embed_texts", _emb_raises)

    with caplog.at_level(logging.WARNING, logger=retriever.__name__):
        hits = retriever.hybrid_retrieve(db, kb, "机器学习 算法 入门", 5)

    assert hits, "降级时不得返回空结果"
    assert any("rerank_skipped" in r.getMessage() for r in caplog.records)


def test_normal_embedding_keeps_dual_recall(env, kb, monkeypatch):
    """embedding 正常时行为不变：向量路与 BM25 路都要走。"""
    db, stats = env
    monkeypatch.setattr(retriever.embeddings, "embed_texts", _emb_ok)

    hits = retriever.hybrid_retrieve(db, kb, "机器学习 算法 入门", 5)

    ids = [h["chunk_id"] for h in hits]
    assert 13 in ids and 11 in ids
    assert stats["vector_calls"] == 1
    assert stats["bm25_calls"] >= 1
