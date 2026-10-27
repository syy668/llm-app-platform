"""v1.5 新增功能测试：重排序阈值生效、消息反馈校验、全局统计权限。

原则：纯逻辑/纯校验离线验证，不触碰真实业务数据。
"""
from types import SimpleNamespace

import app.rag.retriever as retriever
from conftest import assert_error


# ---------------- 重排序阈值（rerank_threshold） ----------------

def _fake_chunk(cid: int, content: str):
    return SimpleNamespace(
        id=cid, chroma_id=f"1:{cid}", content=content, chunk_metadata={},
        doc_id=1, chunk_index=cid,
    )


def test_cosine_basic():
    assert abs(retriever._cosine([1, 0], [1, 0]) - 1.0) < 1e-9
    assert abs(retriever._cosine([1, 0], [0, 1])) < 1e-9
    assert retriever._cosine([0, 0], [1, 0]) == 0.0


def _patch_retrieval(monkeypatch, chunk_vectors, query_vec=(1.0, 0.0)):
    """构造 2 个候选块：向量检索命中两个，BM25 不命中。

    embed_texts 桩按输入长度区分：1 条 = 查询向量；多条 = 候选块向量（与候选一一对应）。
    """
    chunks = [_fake_chunk(1, "相关内容"), _fake_chunk(2, "无关内容")]
    monkeypatch.setattr(retriever, "_get_bm25", lambda db, kb_id: (chunks, SimpleNamespace(search=lambda q, k: [])))
    monkeypatch.setattr(retriever.vector_store, "query", lambda kb_id, vec, k: [("1:1", 0.1), ("1:2", 0.2)])

    def fake_embed(texts):
        return [query_vec] if len(texts) == 1 else chunk_vectors

    monkeypatch.setattr(retriever.embeddings, "embed_texts", fake_embed)


def test_rerank_threshold_filters_irrelevant(monkeypatch):
    """阈值 0.6：相似度 1.0 的块保留，0.0 的块被过滤。"""
    kb = SimpleNamespace(id=1, rerank_threshold=0.6)
    _patch_retrieval(monkeypatch, chunk_vectors=[[1.0, 0.0], [0.0, 1.0]])
    hits = retriever.hybrid_retrieve(db=None, kb=kb, question="q", top_k=2)
    assert [h["chunk_id"] for h in hits] == [1]  # 无关块被阈值过滤


def test_rerank_orders_by_similarity(monkeypatch):
    """启用重排序后按相似度降序（RRF 排名被覆盖）。"""
    kb = SimpleNamespace(id=1, rerank_threshold=0.1)
    # chunk1 相似度 0.6、chunk2 相似度 1.0（均高于阈值）→ 重排后 chunk2 在前
    _patch_retrieval(monkeypatch, chunk_vectors=[[0.6, 0.8], [1.0, 0.0]])
    hits = retriever.hybrid_retrieve(db=None, kb=kb, question="q", top_k=2)
    assert [h["chunk_id"] for h in hits] == [2, 1]


def test_rerank_off_by_default(monkeypatch):
    """未配置阈值（0）时走纯 RRF，不触发向量重排（行为与旧版一致）。"""
    kb = SimpleNamespace(id=1, rerank_threshold=0.0)
    _patch_retrieval(monkeypatch, chunk_vectors=[[0.0, 1.0], [1.0, 0.0]])
    hits = retriever.hybrid_retrieve(db=None, kb=kb, question="q", top_k=2)
    assert [h["chunk_id"] for h in hits] == [1, 2]  # RRF：向量排名第 1 的 chunk1 在前


# ---------------- 消息反馈 / 全局统计：校验与权限 ----------------

def test_feedback_invalid_rating_422(user_client):
    """rating 仅允许 1/-1，其余 422（FastAPI 校验，先于业务逻辑）。"""
    assert_error(user_client.post("/api/v1/chat/feedback", json={"message_id": 1, "rating": 0}), status=422, code=422)


def test_feedback_requires_login(client):
    assert_error(client.post("/api/v1/chat/feedback", json={"message_id": 1, "rating": 1}), status=401, code=401)


def test_stats_overview_admin_only(user_client):
    """/stats/overview 仅管理员，普通用户 403。"""
    assert_error(user_client.get("/api/v1/stats/overview"), status=403, code=403)


def test_reports_require_login(client):
    assert_error(client.get("/api/v1/reports"), status=401, code=401)
