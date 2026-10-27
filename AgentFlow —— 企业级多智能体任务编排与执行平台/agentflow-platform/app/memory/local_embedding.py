"""记忆语义向量本地实现：HTTP embedding + 进程内余弦检索。

背景（2026-10-27 实测结论）：``HttpRagVectorStore`` 依赖项目一的
``/api/v1/kb/upsert`` + ``/kb/search``，两接口在项目一**不存在**（404），
导致 real 模式下记忆从未真正向量化、召回长期静默降级为关键词打分。

本模块提供不依赖项目一的「真语义」路径：

- ``HttpEmbedder``：OpenAI 兼容 ``/embeddings`` 客户端（默认百炼
  ``text-embedding-v3``，1024 维；key 可复用平台已有的 dashscope key）。
  **必须** ``trust_env=False`` 直连（本机代理会把 api 域名也劫走，见评测链路同款事故）。
- ``LocalEmbeddingVectorStore``：实现 ``MemoryVectorStore`` 协议，
  content → 向量缓存（进程级，按 model+内容哈希复用），检索=查询向量与
  候选集余弦排序。embedding 失败抛 ``MemoryStoreError`` → 上层自动降级
  本地关键词打分（与 http-rag 故障路径语义一致，不会丢数据）。

分数口径注意：该模型无关联文本的余弦基线约 0.3~0.4（不是从 0 起），
阈值须用 ``MEMORY_RECALL_MIN_SCORE_VECTOR``（默认 0.45），不能沿用关键词的 0.15。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Sequence

import httpx

from app.memory.layered_memory import MemoryStoreError

__all__ = ["EmbeddingError", "HttpEmbedder", "LocalEmbeddingVectorStore", "cosine_similarity"]


class EmbeddingError(RuntimeError):
    """embedding 调用失败（网络 / 鉴权 / 响应不合规）。"""


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """纯 python 余弦（记忆规模 ≤200 条，无 numpy 依赖必要）。零向量保守给 0。"""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class HttpEmbedder:
    """OpenAI 兼容 /embeddings 客户端，按 batch 切分输入。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        batch_size: int,
        timeout: float,
        dims: int,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.batch_size = max(1, int(batch_size))
        self.timeout = float(timeout)
        self.dims = int(dims)

    def _batches(self, texts: list[str]) -> list[list[str]]:
        return [texts[i : i + self.batch_size] for i in range(0, len(texts), self.batch_size)]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化；返回顺序与输入一致。任一批失败整体抛错。"""
        cleaned = [(t or "").strip() for t in texts]
        vectors: list[list[float] | None] = [None] * len(cleaned)
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            offset = 0
            for batch in self._batches(cleaned):
                vectors[offset : offset + len(batch)] = await self._embed_batch(client, batch)
                offset += len(batch)
        if any(v is None for v in vectors):
            raise EmbeddingError("embedding 响应不完整")
        return [v for v in vectors if v is not None]  # type: ignore[misc]

    async def _embed_batch(self, client: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
        try:
            response = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": batch, "encoding_format": "float"},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingError(f"embedding 调用失败: {exc}") from exc
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(batch):
            raise EmbeddingError(f"embedding 响应条数不符（期望 {len(batch)}）")
        pairs = sorted(data, key=lambda item: int(item.get("index", 0)))
        out: list[list[float]] = []
        for item in pairs:
            vec = item.get("embedding")
            if not isinstance(vec, list) or len(vec) != self.dims:
                raise EmbeddingError(f"embedding 维度不符（期望 {self.dims}）")
            out.append([float(x) for x in vec])
        return out


#: 进程级向量缓存：key = model + content sha256。跨请求/跨服务实例复用，
#: 记忆规模 ≤200 条，内存开销 ≈ 200×1024×8B ≈ 1.6MB，可忽略。
_VECTOR_CACHE: dict[str, list[float]] = {}


def _cache_key(model: str, content: str) -> str:
    return f"{model}:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


class LocalEmbeddingVectorStore:
    """本地语义向量库：upsert 即向量化入库，search = 查询向量余弦排序。

    进程内 ``_docs`` 与入参候选集取并集参与打分（与 LocalKeywordVectorStore 同策略：
    进程刚重启缓存为空时，仅靠 MySQL 候选集也能完成检索）。
    """

    name = "local-embedding"

    def __init__(self, embedder: HttpEmbedder) -> None:
        self._embedder = embedder
        self._docs: dict[str, str] = {}

    def stats(self) -> dict[str, Any]:
        return {"store": self.name, "indexed": len(self._docs)}

    async def upsert(self, *, memory_id: int, content: str, metadata: dict[str, Any]) -> str:
        doc_id = str(metadata.get("doc_id") or f"local-emb-{memory_id}")
        text = (content or "").strip()
        if not text:
            raise MemoryStoreError(f"memory {memory_id} 内容为空，无法向量化")
        try:
            key = _cache_key(self._embedder.model, text)
            if key not in _VECTOR_CACHE:
                (vectors,) = await self._embedder.embed([text])
                _VECTOR_CACHE[key] = vectors
        except EmbeddingError as exc:
            raise MemoryStoreError(str(exc)) from exc
        self._docs[doc_id] = text
        return doc_id

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        candidates: Sequence[tuple[str, str]] | None = None,
    ) -> list[tuple[str, float]]:
        try:
            return await self._search_impl(query, top_k=top_k, candidates=candidates)
        except EmbeddingError as exc:
            raise MemoryStoreError(str(exc)) from exc

    async def _search_impl(
        self,
        query: str,
        *,
        top_k: int,
        candidates: Sequence[tuple[str, str]] | None = None,
    ) -> list[tuple[str, float]]:
        pool: dict[str, str] = dict(self._docs)
        for doc_id, content in candidates or ():
            pool.setdefault(doc_id, content)
        if not pool or not (query or "").strip():
            return []

        # 1) 补齐缓存：候选集里还没向量化过的内容批量补
        missing: dict[str, str] = {}
        for doc_id, content in pool.items():
            if _cache_key(self._embedder.model, content) not in _VECTOR_CACHE:
                missing[doc_id] = content
        if missing:
            texts = list(missing.values())
            vectors = await self._embedder.embed(texts)
            for content, vec in zip(texts, vectors):
                _VECTOR_CACHE[_cache_key(self._embedder.model, content)] = vec

        # 2) 查询向量化 + 余弦排序
        (query_vec,) = await self._embedder.embed([query.strip()])
        scored = [
            (doc_id, cosine_similarity(query_vec, _VECTOR_CACHE[_cache_key(self._embedder.model, content)]))
            for doc_id, content in pool.items()
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]
