"""search_mode 请求级透传测试（retriever 层真实逻辑，离线）。

覆盖：
  1. search_mode="vector_only"：纯向量检索，BM25 召回不被触发（index.search 零调用）；
  2. search_mode="bm25_vector_hybrid"：向量 + BM25 双路召回，RRF 融合包含两路结果；
  3. search_mode=None（缺省）：走默认混合检索，行为与旧版完全一致（向后兼容）；
  4. eval_service._build_agent_options 对 search_mode 的白名单透传。

运行方式：离线（不依赖 MySQL / Redis / Chroma / LLM，embeddings 与向量库全部打桩）。
"""
from types import SimpleNamespace

import app.services.eval_service as eval_service
from app.rag import retriever


# ---------------- 测试替身 ----------------

def _make_kb(rerank_threshold=0.0):
    return SimpleNamespace(id=1, rerank_threshold=rerank_threshold)


def _make_chunks():
    """两个 chunk：A 命中向量检索，B 命中 BM25，混合模式应两者都召回。"""
    return [
        SimpleNamespace(
            id=101, chroma_id="c-101", doc_id=1, chunk_index=0,
            content="向量命中的内容", chunk_metadata={"title": "文档A"},
        ),
        SimpleNamespace(
            id=102, chroma_id=None, doc_id=1, chunk_index=1,
            content="关键词命中的内容", chunk_metadata={"title": "文档A"},
        ),
    ]


class SpyBM25Index:
    """包装真实 BM25Index，记录 search 调用次数（验证 vector_only 是否跳过 BM25）。"""

    def __init__(self, corpus):
        from app.rag.bm25 import BM25Index

        self._inner = BM25Index(corpus)
        self.search_calls = 0

    def search(self, query, top_k):
        self.search_calls += 1
        return self._inner.search(query, top_k)


def _patch_rag_stack(monkeypatch, vector_hits, bm25_top_hit=True):
    """打桩 embeddings / vector_store / BM25，返回 spy 便于断言。"""
    chunks = _make_chunks()
    spy = SpyBM25Index([c.content for c in chunks])

    monkeypatch.setattr(retriever, "_get_bm25", lambda db, kb_id: (chunks, spy))
    monkeypatch.setattr(retriever.embeddings, "embed_texts", lambda texts: [[0.1, 0.2, 0.3] for _ in texts])
    monkeypatch.setattr(retriever.vector_store, "query", lambda kb_id, vec, top_k: vector_hits)
    return spy, chunks


# ---------------- 1) vector_only：不走 BM25 ----------------

def test_vector_only_skips_bm25(monkeypatch):
    """search_mode="vector_only"：BM25 index.search 零调用，仅返回向量命中。"""
    # 向量只命中 chunk A（chroma_id="c-101"），BM25 能命中的 B 不应出现
    spy, chunks = _patch_rag_stack(monkeypatch, vector_hits=[("c-101", 0.15)], bm25_top_hit=True)

    kb = _make_kb()
    hits = retriever.hybrid_retrieve(None, kb, "问题", top_k=5, search_mode="vector_only")

    assert spy.search_calls == 0  # 关键断言：纯向量模式完全跳过 BM25
    assert [h["chunk_id"] for h in hits] == [101]
    assert hits[0]["content"] == "向量命中的内容"


# ---------------- 2) hybrid：两路都走 ----------------

def test_hybrid_mode_uses_both_paths(monkeypatch):
    """search_mode="bm25_vector_hybrid"：BM25 被调用，RRF 融合包含向量命中与 BM25 命中。"""
    # 向量命中 A；BM25 命中 corpus 索引 1（chunk B）
    spy, chunks = _patch_rag_stack(monkeypatch, vector_hits=[("c-101", 0.15)])

    kb = _make_kb()
    hits = retriever.hybrid_retrieve(None, kb, "关键词", top_k=5, search_mode="bm25_vector_hybrid")

    assert spy.search_calls == 1  # BM25 召回被触发
    got_ids = {h["chunk_id"] for h in hits}
    assert got_ids == {101, 102}  # 两路结果都在融合结果中


# ---------------- 3) 缺省 None：默认混合（向后兼容） ----------------

def test_default_none_is_hybrid(monkeypatch):
    """search_mode=None：与旧版行为一致，默认走混合检索（BM25 被调用）。"""
    spy, chunks = _patch_rag_stack(monkeypatch, vector_hits=[("c-101", 0.15)])

    kb = _make_kb()
    hits = retriever.hybrid_retrieve(None, kb, "关键词", top_k=5, search_mode=None)

    assert spy.search_calls == 1
    assert {h["chunk_id"] for h in hits} == {101, 102}


# ---------------- 4) eval options 白名单透传 ----------------

def test_build_agent_options_includes_search_mode():
    """_build_agent_options 把 search_mode 一并放入请求级 options；未传时不含该键。"""
    options = eval_service._build_agent_options({"top_k": 8, "search_mode": "vector_only"})
    assert options == {"top_k": 8, "search_mode": "vector_only"}

    options_partial = eval_service._build_agent_options({"top_k": 8})
    assert options_partial == {"top_k": 8}

    assert eval_service._build_agent_options(None) is None
