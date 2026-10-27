"""记忆本地语义向量（local-embedding 后端）单元测试。

背景：项目一未提供 /kb/upsert|/kb/search（404），real 模式下记忆召回长期静默
降级为关键词打分。local_embedding.py 提供「本地 embedding + 余弦」的真语义路径。
本文件用确定性 Fake embedder 覆盖核心逻辑，不发真实网络请求。
"""

from __future__ import annotations

import math
from typing import Sequence

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.w5_config import w5
from app.memory.layered_memory import LayeredMemoryService, MemoryStoreError, build_memory_store
from app.memory.local_embedding import (
    EmbeddingError,
    HttpEmbedder,
    LocalEmbeddingVectorStore,
    cosine_similarity,
)
from app.services.context_service import ContextService


class FakeEmbedder:
    """确定性 embedder：向量 = [关键词命中标记...]，可人工构造相似度关系。"""

    def __init__(self, *, fail: bool = False, dims: int = 4) -> None:
        self.fail = fail
        self.dims = dims
        self.model = "fake-embed"
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise EmbeddingError("fake embedder down")
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        markers = ["gmv", "退款", "客服", "天气"]
        vec = [1.0 if m in text.lower() else 0.0 for m in markers]
        # 保证非零（零向量余弦恒 0，会掩盖排序逻辑）
        if all(x == 0.0 for x in vec):
            vec = [0.1, 0.1, 0.1, 0.1]
        return vec


# ---------------------------------------------------------------------------
# 1) cosine_similarity
# ---------------------------------------------------------------------------
def test_cosine_similarity_basics() -> None:
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([1, 2], [2, 4]) == pytest.approx(1.0)
    assert cosine_similarity([], [1]) == 0.0  # 维度不符
    assert cosine_similarity([0, 0], [1, 1]) == 0.0  # 零向量保守 0


# ---------------------------------------------------------------------------
# 2) HttpEmbedder 批切分
# ---------------------------------------------------------------------------
def test_embedder_batch_split() -> None:
    embedder = HttpEmbedder(
        base_url="https://example.invalid/v1", api_key="k", model="m", batch_size=6, timeout=5, dims=1024
    )
    batches = embedder._batches([str(i) for i in range(14)])
    assert [len(b) for b in batches] == [6, 6, 2]


# ---------------------------------------------------------------------------
# 3) LocalEmbeddingVectorStore：排序 / 候选集兜底 / 失败包装
# ---------------------------------------------------------------------------
async def test_embedding_store_ranks_semantically() -> None:
    store = LocalEmbeddingVectorStore(FakeEmbedder())
    await store.upsert(memory_id=1, content="各渠道GMV与退款汇总", metadata={"doc_id": "d1"})
    await store.upsert(memory_id=2, content="客服排班与值班记录", metadata={"doc_id": "d2"})

    pairs = await store.search("大促GMV分析", top_k=2, candidates=[])
    assert pairs[0][0] == "d1"  # 语义命中排在前面
    # 查询向量 [1,0,0,0]（仅 gmv 命中），d1 向量 [1,1,0,0]（gmv+退款）→ cos = 1/√2
    assert pairs[0][1] == pytest.approx(1 / math.sqrt(2))
    assert pairs[1][1] < pairs[0][1]


async def test_embedding_store_uses_candidates_when_index_empty() -> None:
    store = LocalEmbeddingVectorStore(FakeEmbedder())
    pairs = await store.search(
        "客服排班", top_k=2, candidates=[("d1", "各渠道GMV与退款汇总"), ("d2", "客服排班与值班记录")]
    )
    assert pairs[0][0] == "d2"


async def test_embedding_store_wraps_failure_as_memory_store_error() -> None:
    store = LocalEmbeddingVectorStore(FakeEmbedder(fail=True))
    with pytest.raises(MemoryStoreError):
        await store.upsert(memory_id=3, content="x", metadata={})
    with pytest.raises(MemoryStoreError):
        await store.search("q", top_k=1, candidates=[("d", "c")])


# ---------------------------------------------------------------------------
# 4) build_memory_store：后端选择
# ---------------------------------------------------------------------------
def test_build_store_mock_always_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w5, "MEMORY_EMBEDDING_API_KEY", "sk-test")
    # mock=True 恒为本地关键词，与 embedding key 无关
    store = build_memory_store(mock=True)
    assert getattr(store, "name", "") == "local-keyword"


def test_build_store_real_defaults_to_local_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w5, "MEMORY_EMBEDDING_API_KEY", "sk-test")
    store = build_memory_store(mock=False)
    assert getattr(store, "name", "") == "local-embedding"


def test_build_store_real_without_key_falls_back_to_http_rag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w5, "MEMORY_EMBEDDING_API_KEY", "")
    store = build_memory_store(mock=False)
    assert getattr(store, "name", "") == "project1-rag"


def test_build_store_explicit_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w5, "MEMORY_VECTOR_BACKEND", "keyword")
    monkeypatch.setattr(w5, "MEMORY_EMBEDDING_API_KEY", "sk-test")
    assert getattr(build_memory_store(mock=False), "name", "") == "local-keyword"


def test_build_store_rejects_unknown_backend() -> None:
    from pydantic import ValidationError

    from app.core.w5_config import W5Settings

    with pytest.raises(ValidationError):
        W5Settings(MEMORY_VECTOR_BACKEND="bogus")


# ---------------------------------------------------------------------------
# 5) recall 阈值分流：向量通道用 MEMORY_RECALL_MIN_SCORE_VECTOR
# ---------------------------------------------------------------------------
class StubVectorStore:
    """name=local-embedding 的桩：返回预设余弦分，验证阈值分流。

    doc_id 用 ``agentflow-mem-{id}``（archive_long 默认伪 id），与 by_doc 映射对齐。
    """

    name = "local-embedding"

    async def upsert(self, *, memory_id: int, content: str, metadata: dict) -> str:  # noqa: ANN001
        return f"agentflow-mem-{memory_id}"

    async def search(self, query: str, *, top_k: int, candidates=None) -> list[tuple[str, float]]:  # noqa: ANN001
        return [("agentflow-mem-1", 0.52), ("agentflow-mem-2", 0.30)]  # 0.30 在关键词阈值(0.15)下会命中，向量阈值(0.45)下应被滤掉


async def test_recall_vector_threshold_filters_baseline_cosine(
    session_factory: async_sessionmaker,
    fake_redis,
    seeded_users: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(w5, "ENABLE_LAYERED_MEMORY", True)
    monkeypatch.setattr(w5, "MEMORY_RECALL_MIN_SCORE_VECTOR", 0.45)
    service = LayeredMemoryService(
        session_factory=session_factory,
        redis=fake_redis,
        context=ContextService(),
        store=StubVectorStore(),
    )
    uid = seeded_users["admin_user"]
    await service.archive_long(task_id=None, user_id=uid, content="记忆一", review_score=4.0)
    await service.archive_long(task_id=None, user_id=uid, content="记忆二", review_score=4.0)

    hits = await service.recall("任意目标", user_id=uid)
    assert [h.memory_id for h in hits] == [1]  # 只剩 0.52 那条
    assert service.last_retrieval == {"store": "local-embedding", "scorer": "vector", "degraded": False}
