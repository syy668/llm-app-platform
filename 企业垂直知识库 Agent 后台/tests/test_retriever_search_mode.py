"""hybrid_retrieve 请求级 search_mode 行为测试（用户 A/B 实验核心保障）。

验证点（对应需求：传 vector_only 时不走 BM25，传 hybrid 时两者都走）：
  1. search_mode="vector_only"：跳过 BM25 召回与 RRF 融合，仅返回向量命中；
  2. search_mode="bm25_vector_hybrid"：向量 + BM25 双路召回，RRF 融合；
  3. search_mode=None（默认）：与混合模式行为一致，向后兼容。

测试策略（离线）：BM25 走真实索引（纯 Python 无外部依赖），
Embedding 与 Chroma 向量库打桩，使「向量只命中 c3、BM25 只命中 c1」，
从而通过结果差集严格区分两种模式。
"""
import pytest

from app.core.database import Base, SessionLocal, engine
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_base import KnowledgeBase
from app.rag import retriever
from app.rag.bm25 import BM25Index

KB_ID = 888
# 三块内容：c1 与查询词强匹配（BM25 路）；c3 由向量库命中（向量路）；c2 两路都不命中
C1_CONTENT = "机器学习算法入门教程 线性回归 决策树 聚类 主流模型介绍"
C2_CONTENT = "深度学习与神经网络基础 反向传播 损失函数"
C3_CONTENT = "员工差旅报销制度流程说明 发票要求 审批时限"


@pytest.fixture()
def retrieve_env(monkeypatch):
    """内存库 + 1 知识库 3 块切片 + 打桩向量路/Embedding + BM25 调用探针。"""
    Base.metadata.create_all(bind=engine, checkfirst=True)

    db = SessionLocal()
    try:
        kb = KnowledgeBase(
            id=KB_ID, name="search_mode 测试库", owner_id=1, embedding_model="mock",
            chunk_size=500, chunk_overlap=50, top_k=5, rerank_threshold=0.0,
            status="ready", doc_count=0,
        )
        db.add(kb)
        db.flush()
        for cid, content, chroma_id in (
            (1, C1_CONTENT, "chunk-c1"),
            (2, C2_CONTENT, "chunk-c2"),
            (3, C3_CONTENT, "chunk-c3"),
        ):
            db.add(DocumentChunk(
                id=cid, doc_id=1, kb_id=KB_ID, chunk_index=cid - 1,
                content=content, token_count=0, chroma_id=chroma_id,
            ))
        db.commit()

        # 探针：记录 BM25Index.search 是否被调用（vector_only 模式下必须为 0 次）
        calls = {"bm25_search": 0}
        orig_search = BM25Index.search

        def spy_search(self, query, top_k):
            calls["bm25_search"] += 1
            return orig_search(self, query, top_k)

        monkeypatch.setattr(BM25Index, "search", spy_search)

        # 打桩 Embedding：返回固定向量（rerank_threshold=0，只用于查询向量化一次）
        monkeypatch.setattr(
            retriever.embeddings, "embed_texts",
            lambda texts: [[0.1, 0.2, 0.3] for _ in texts],
        )
        # 打桩向量库：仅命中 c3（距离 0.2），c1/c2 不在向量结果里
        monkeypatch.setattr(
            retriever.vector_store, "query",
            lambda kb_id, vec, top_k: [("chunk-c3", 0.2)],
        )

        retriever.invalidate_bm25(KB_ID)  # 清缓存，确保本 fixture 的 BM25 索引被重建
        yield db, calls
    finally:
        retriever.invalidate_bm25(KB_ID)
        db.rollback()
        db.close()
        Base.metadata.drop_all(bind=engine)


def _chunk_ids(results):
    return [r["chunk_id"] for r in results]


# ---------- 1) vector_only：纯向量，不走 BM25 ----------

def test_vector_only_skips_bm25(retrieve_env):
    db, calls = retrieve_env
    kb = db.get(KnowledgeBase, KB_ID)

    hits = retriever.hybrid_retrieve(db, kb, "机器学习 算法 入门", 5, search_mode="vector_only")

    assert calls["bm25_search"] == 0, "vector_only 模式不得调用 BM25 召回"
    # 仅向量命中的 c3；BM25 强匹配的 c1 不应出现
    assert _chunk_ids(hits) == [3]
    # 分数 = 1/(1+dist) = 1/1.2
    assert hits[0]["score"] == pytest.approx(1 / 1.2, abs=1e-5)


# ---------- 2) hybrid：双路召回 + RRF 融合 ----------

def test_hybrid_fuses_bm25_and_vector(retrieve_env):
    db, calls = retrieve_env
    kb = db.get(KnowledgeBase, KB_ID)

    hits = retriever.hybrid_retrieve(db, kb, "机器学习 算法 入门", 5, search_mode="bm25_vector_hybrid")

    assert calls["bm25_search"] >= 1, "混合模式必须调用 BM25 召回"
    ids = _chunk_ids(hits)
    # 向量路的 c3 与 BM25 路的 c1 均被召回融合
    assert 3 in ids and 1 in ids


# ---------- 3) 默认 None：与混合模式一致（向后兼容） ----------

def test_default_none_behaves_as_hybrid(retrieve_env):
    db, calls = retrieve_env
    kb = db.get(KnowledgeBase, KB_ID)

    hits = retriever.hybrid_retrieve(db, kb, "机器学习 算法 入门", 5, search_mode=None)

    assert calls["bm25_search"] >= 1, "默认（未传 search_mode）保持原有混合检索行为"
    ids = _chunk_ids(hits)
    assert 3 in ids and 1 in ids
