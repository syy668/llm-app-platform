"""FR-014 分层记忆（W5-Part1 任务 2）—— **外层增强模块**。

## 三层架构

| 层 | 存储 | 生命周期 | 内容形态 | 写入时机 |
|---|---|---|---|---|
| 短期 short | Redis (List) | TTL 24h | 当前任务会话消息**原文**，不摘要 | 任务执行中逐条 append |
| 中期 mid | MySQL ``memories``(scope='mid') | 30 天滚动 | 复用 W2 ``ContextService.compress`` 的摘要 | 任务结束后 |
| 长期 long | MySQL + 项目一 RAG 向量库 | 永久（可手工删） | Reviewer ≥ 3 分的关键业务结论 | 任务结束后且评分达标 |

## 为什么是「外层」而不是「改内核」

1. ``plan_context`` / ``build_planner_messages`` **一行未改**：Planner 早就预留了
   ``TaskState.memory_hits`` 与提示词里的「长期记忆（参考，可裁剪）」独立片段，
   本模块只负责往这个既有插槽里填值；
2. 注入发生在**图入口**（``MemoryAwareGraph.ainvoke/astream`` 包装），不是节点内部，
   因此挂起/恢复、SSE 流式、审批断点等 W3 行为完全不受影响；
3. 开关 ``ENABLE_LAYERED_MEMORY=false``（默认）时：不连 Redis、不查 MySQL、不改 state，
   **零副作用**——这也是 W1~W4 的 150 条基线用例能保持原样通过的前提。

## 关键取舍

- **中期摘要**复用 W2 压缩算法（``ContextService.compress``），不另起一套摘要提示词，
  避免同一份对话出现两套口径的「摘要」；
- **长期召回**始终以 MySQL 行做候选集（保证 ``user_id`` 隔离与删除一致性），
  向量库只负责打分。这样即使项目一 RAG 挂了，本地关键词兜底也能给出可用排序；
- **兜底排序**用「2-gram + 数字 + 领域词」的覆盖率加权，不引入额外依赖，
  ``MOCK_EXTERNAL_RAG=true`` 时完全离线可测。
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings as base_settings
from app.core.logging import get_logger
from app.core.w5_config import layered_memory_enabled, w5
from app.schemas.enums import MemoryScope
from app.schemas.orm_models import Memory
from app.services.context_service import ContextService

logger = get_logger(__name__)

DEFAULT_USER_ID = 0  # 系统级记忆（无归属用户时）


# ---------------------------------------------------------------------------
# 1) 文本相似度（本地兜底排序，零依赖）
# ---------------------------------------------------------------------------
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-_.]*")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")
_STOPWORDS = frozenset(
    "的了是在和与及以对为我你他这那个有不也就都而但并其之或被从到上下中元天么什怎样".split()
)


def tokenize(text: str) -> set[str]:
    """中英混合切词：中文 2-gram + 英文/数字词 + 数字（含百分号）。"""
    lowered = (text or "").lower()
    tokens: set[str] = set()
    for block in _CJK_RE.findall(lowered):
        if len(block) == 1:
            tokens.add(block)
        for index in range(len(block) - 1):
            tokens.add(block[index : index + 2])
    tokens.update(_WORD_RE.findall(lowered))
    tokens.update(_NUM_RE.findall(text or ""))
    return {token for token in tokens if token and token not in _STOPWORDS}


def keyword_score(query: str, text: str) -> float:
    """相似度 = 0.6×查询覆盖率 + 0.4×Jaccard，数字命中额外加权（经营分析里数字最关键）。"""
    query_tokens = tokenize(query)
    text_tokens = tokenize(text)
    if not query_tokens or not text_tokens:
        return 0.0
    inter = len(query_tokens & text_tokens)
    coverage = inter / len(query_tokens)
    jaccard = inter / len(query_tokens | text_tokens)
    numbers_q = {t for t in query_tokens if t[0].isdigit()}
    numbers_hit = len(numbers_q & text_tokens) / len(numbers_q) if numbers_q else 0.0
    score = 0.6 * coverage + 0.4 * jaccard + 0.1 * numbers_hit
    return round(min(1.0, score), 4)


def rank_candidates(
    query: str, candidates: Sequence[tuple[str, str]], *, top_k: int = 3, min_score: float = 0.0
) -> list[tuple[str, float]]:
    """对 ``(doc_id, content)`` 候选集做本地排序。"""
    scored = [(doc_id, keyword_score(query, content)) for doc_id, content in candidates]
    scored = [item for item in scored if item[1] >= min_score]
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:top_k]


# ---------------------------------------------------------------------------
# 2) 向量库抽象（项目一 RAG ⇄ 本地关键词）
# ---------------------------------------------------------------------------
class MemoryStoreError(RuntimeError):
    """向量库不可用（HTTP 故障 / 鉴权失败 / 响应非法）——由上层降级到本地排序。"""


class MemoryVectorStore(Protocol):
    """最小向量库契约：只做 upsert 与 search，权限与生命周期由 MySQL 侧负责。"""

    name: str

    async def upsert(self, *, memory_id: int, content: str, metadata: dict[str, Any]) -> str: ...

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        candidates: Sequence[tuple[str, str]] | None = None,
    ) -> list[tuple[str, float]]: ...


class LocalKeywordVectorStore:
    """本地关键词检索兜底（``MOCK_EXTERNAL_RAG=true`` 的默认实现）。

    进程内维护 doc_id -> content 字典，检索时对「索引 + 本次传入候选集」取并集打分，
    因此即使进程刚重启（索引为空）也能仅靠 MySQL 候选集给出结果。
    """

    name = "local-keyword"

    def __init__(self) -> None:
        self._docs: dict[str, str] = {}

    async def upsert(self, *, memory_id: int, content: str, metadata: dict[str, Any]) -> str:
        doc_id = str(metadata.get("doc_id") or f"local-mem-{memory_id}")
        self._docs[doc_id] = content
        return doc_id

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        candidates: Sequence[tuple[str, str]] | None = None,
    ) -> list[tuple[str, float]]:
        pool: dict[str, str] = dict(self._docs)
        for doc_id, content in candidates or ():
            pool.setdefault(doc_id, content)
        return rank_candidates(query, list(pool.items()), top_k=top_k)

    # 便于自检
    def stats(self) -> dict[str, Any]:
        return {"store": self.name, "indexed": len(self._docs)}


class HttpRagVectorStore:
    """项目一 RAG 向量化适配（非 mock 路径）：``POST /api/v1/kb/upsert`` + ``/kb/search``。"""

    name = "project1-rag"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        upsert_path: str | None = None,
        search_path: str | None = None,
        tenant: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or base_settings.EXTERNAL_RAG_BASE_URL).rstrip("/")
        self.token = base_settings.EXTERNAL_RAG_TOKEN if token is None else token
        self.upsert_path = upsert_path or w5.MEMORY_RAG_UPSERT_PATH
        self.search_path = search_path or w5.MEMORY_RAG_SEARCH_PATH
        self.tenant = tenant or w5.MEMORY_RAG_TENANT
        self.timeout = float(timeout or base_settings.EXTERNAL_RAG_TIMEOUT_SECONDS)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def upsert(self, *, memory_id: int, content: str, metadata: dict[str, Any]) -> str:
        doc_id = str(metadata.get("doc_id") or f"mem-{memory_id}-{uuid.uuid4().hex[:8]}")
        payload = {
            "tenant_id": self.tenant,
            "doc_id": doc_id,
            "content": content,
            "metadata": {**metadata, "memory_id": memory_id, "source": "agentflow.memories"},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}{self.upsert_path}", json=payload, headers=self._headers()
                )
            if response.status_code in (401, 403):
                raise MemoryStoreError(f"RAG 鉴权失败（{response.status_code}）")
            response.raise_for_status()
        except (httpx.HTTPError, MemoryStoreError) as exc:
            raise MemoryStoreError(f"RAG 向量化入库失败: {exc}") from exc
        try:
            body = response.json()
        except ValueError:
            return doc_id
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict) and data.get("doc_id"):
            return str(data["doc_id"])
        return doc_id

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        candidates: Sequence[tuple[str, str]] | None = None,  # 服务端向量检索无需候选集
    ) -> list[tuple[str, float]]:
        payload = {"tenant_id": self.tenant, "query": query, "top_k": top_k}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}{self.search_path}", json=payload, headers=self._headers()
                )
            if response.status_code in (401, 403):
                raise MemoryStoreError(f"RAG 鉴权失败（{response.status_code}）")
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError, MemoryStoreError) as exc:
            raise MemoryStoreError(f"RAG 向量检索失败: {exc}") from exc

        chunks = (body.get("data") or {}).get("chunks") if isinstance(body, dict) else None
        chunks = chunks or (body.get("chunks") if isinstance(body, dict) else None) or []
        pairs: list[tuple[str, float]] = []
        for item in chunks[:top_k]:
            if not isinstance(item, dict):
                continue
            doc_id = str(item.get("doc_id") or item.get("id") or "")
            if not doc_id:
                continue
            pairs.append((doc_id, float(item.get("score") or 0.0)))
        return pairs


def build_embedder() -> "HttpEmbedder | None":
    """按 w5 配置构建 embedding 客户端；未配 key 返回 None（调用方自行回退）。

    懒加载 local_embedding：该模块反向依赖本模块的 MemoryStoreError。
    """
    from app.memory.local_embedding import HttpEmbedder

    if not w5.MEMORY_EMBEDDING_API_KEY:
        return None
    return HttpEmbedder(
        base_url=w5.MEMORY_EMBEDDING_BASE_URL,
        api_key=w5.MEMORY_EMBEDDING_API_KEY,
        model=w5.MEMORY_EMBEDDING_MODEL,
        batch_size=w5.MEMORY_EMBEDDING_BATCH_SIZE,
        timeout=w5.MEMORY_EMBEDDING_TIMEOUT_SECONDS,
        dims=w5.MEMORY_EMBEDDING_DIMS,
    )


def build_memory_store(*, mock: bool | None = None) -> MemoryVectorStore:
    """按开关选择向量库实现。

    - ``MOCK_EXTERNAL_RAG=true`` → 本地关键词（离线零依赖，行为不变）；
    - 非 mock → 按 ``w5.MEMORY_VECTOR_BACKEND``：
      - ``local-embedding``（默认）：本地 embedding + 余弦（**真语义**）；
        未配 ``MEMORY_EMBEDDING_API_KEY`` 时回退 http-rag（保持旧行为并告警）；
      - ``keyword``：本地关键词；
      - ``http-rag``：项目一 ``/kb/upsert`` + ``/kb/search``（当前项目一未提供该接口）。
    """
    use_mock = base_settings.MOCK_EXTERNAL_RAG if mock is None else bool(mock)
    if use_mock:
        return LocalKeywordVectorStore()

    backend = w5.MEMORY_VECTOR_BACKEND
    if backend == "keyword":
        return LocalKeywordVectorStore()
    if backend == "local-embedding":
        embedder = build_embedder()
        if embedder is not None:
            from app.memory.local_embedding import LocalEmbeddingVectorStore

            return LocalEmbeddingVectorStore(embedder)
        logger.warning(
            "memory_vector_backend_fallback",
            extra={"backend": backend, "reason": "MEMORY_EMBEDDING_API_KEY 为空，回退 http-rag"},
        )
    return HttpRagVectorStore()


# ---------------------------------------------------------------------------
# 3) 读模型
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class MemoryHit:
    memory_id: int
    content: str
    score: float
    scope: str = MemoryScope.LONG.value
    importance: int = 3

    def as_prompt(self, *, max_chars: int = 400) -> str:
        text = " ".join(self.content.split())
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
        return f"[记忆#{self.memory_id}·重要度{self.importance}] {text}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "score": round(self.score, 4),
            "scope": self.scope,
            "importance": self.importance,
        }


@dataclass(slots=True)
class MemoryRecord:
    id: int
    scope: str
    user_id: int
    task_id: int | None
    content: str
    importance: int = 3
    hit_count: int = 0
    chroma_doc_id: str | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None
    score: float = 0.0

    @classmethod
    def from_row(cls, row: Memory) -> "MemoryRecord":
        scope = row.scope.value if isinstance(row.scope, MemoryScope) else str(row.scope)
        return cls(
            id=int(row.id),
            scope=scope,
            user_id=int(row.user_id),
            task_id=int(row.task_id) if row.task_id is not None else None,
            content=row.content,
            importance=int(row.importance or 3),
            hit_count=int(row.hit_count or 0),
            chroma_doc_id=row.chroma_doc_id,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "user_id": self.user_id,
            "task_id": self.task_id,
            "content": self.content,
            "importance": self.importance,
            "hit_count": self.hit_count,
            "chroma_doc_id": self.chroma_doc_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "score": round(self.score, 4),
        }


# ---------------------------------------------------------------------------
# 4) 分层记忆服务
# ---------------------------------------------------------------------------
class LayeredMemoryService:
    """三层记忆门面。所有方法在开关关闭时**直接返回空结果**，不做任何 IO。"""

    #: 最近一次 recall() 的检索通道自省（recall 未执行时为 None）。
    #: 结构：{"store": str, "scorer": "vector" | "local-keyword", "degraded": bool}
    last_retrieval: dict[str, Any] | None = None

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        session: AsyncSession | None = None,
        redis: Redis | None = None,
        context: ContextService | None = None,
        store: MemoryVectorStore | None = None,
        fallback_store: MemoryVectorStore | None = None,
        enabled: bool | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise ValueError("LayeredMemoryService 需要 session 或 session_factory 之一")
        self._session = session
        self._session_factory = session_factory
        self._redis = redis
        self._context = context or ContextService()
        self._store = store or build_memory_store()
        self._fallback_store = fallback_store or LocalKeywordVectorStore()
        self._enabled_override = enabled

    # ---------------- 开关 ----------------
    @property
    def enabled(self) -> bool:
        return layered_memory_enabled() if self._enabled_override is None else bool(self._enabled_override)

    @property
    def store(self) -> MemoryVectorStore:
        return self._store

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "store": getattr(self._store, "name", type(self._store).__name__),
            "fallback": getattr(self._fallback_store, "name", type(self._fallback_store).__name__),
            "short_ttl_seconds": w5.MEMORY_SHORT_TTL_SECONDS,
            "mid_ttl_days": w5.MEMORY_MID_TTL_DAYS,
            "long_min_review_score": w5.MEMORY_LONG_MIN_REVIEW_SCORE,
        }

    # ---------------- 会话 ----------------
    @asynccontextmanager
    async def _session_scope(self) -> AsyncIterator[AsyncSession]:
        if self._session is not None:
            yield self._session
            await self._session.commit()
            return
        assert self._session_factory is not None
        async with self._session_factory() as session:
            yield session
            await session.commit()

    def _redis_client(self) -> Redis | None:
        if self._redis is not None:
            return self._redis
        from app.core.redis_client import get_shared_redis

        return get_shared_redis()

    @staticmethod
    def short_key(task_id: int) -> str:
        return base_settings.key("memory", "short", str(int(task_id)))

    # =========================================================
    # 4.1 短期记忆：Redis List，TTL 24h，原文不摘要
    # =========================================================
    async def append_short(
        self,
        task_id: int,
        message: dict[str, Any],
        *,
        user_id: int | None = None,
        ttl: int | None = None,
    ) -> int:
        """追加一条会话消息；返回当前长度（0 表示未启用 / 无 Redis）。"""
        if not self.enabled:
            return 0
        client = self._redis_client()
        if client is None:
            return 0
        payload = dict(message)
        payload.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        if user_id is not None:
            payload.setdefault("user_id", int(user_id))
        key = self.short_key(task_id)
        max_messages = w5.MEMORY_SHORT_MAX_MESSAGES
        effective_ttl = int(ttl or w5.MEMORY_SHORT_TTL_SECONDS)
        try:
            async with client.pipeline(transaction=False) as pipe:
                pipe.rpush(key, json.dumps(payload, ensure_ascii=False, default=str))
                pipe.ltrim(key, -max_messages, -1)
                pipe.expire(key, effective_ttl)
                results = await pipe.execute()
        except Exception as exc:  # noqa: BLE001 - 短期层是旁路，故障不能拖垮主流程
            logger.warning("memory_short_append_failed", extra={"task_id": task_id, "error": str(exc)[:200]})
            return 0
        return int(results[0] or 0)

    async def load_short(self, task_id: int) -> list[dict[str, Any]]:
        """任务内完整可用的会话消息（按写入顺序）。"""
        if not self.enabled:
            return []
        client = self._redis_client()
        if client is None:
            return []
        try:
            raw_items = await client.lrange(self.short_key(task_id), 0, -1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_short_load_failed", extra={"task_id": task_id, "error": str(exc)[:200]})
            return []
        messages: list[dict[str, Any]] = []
        for raw in raw_items or []:
            try:
                messages.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        return messages

    async def clear_short(self, task_id: int) -> bool:
        if not self.enabled:
            return False
        client = self._redis_client()
        if client is None:
            return False
        try:
            return bool(await client.delete(self.short_key(task_id)))
        except Exception:  # noqa: BLE001
            return False

    async def short_ttl(self, task_id: int) -> int:
        """剩余 TTL（秒）；-2 表示 key 不存在，-1 表示无过期时间。"""
        client = self._redis_client()
        if client is None:
            return -2
        return int(await client.ttl(self.short_key(task_id)))

    # =========================================================
    # 4.2 中期记忆：复用 W2 压缩摘要 → memories(scope='mid')，30 天
    # =========================================================
    def summarize(self, messages: Sequence[dict[str, Any]], *, goal: str = "") -> str:
        """复用 ``ContextService.compress``（W2 上下文工程的同一套算法，不另立口径）。"""
        items = [dict(item) for item in messages]
        if not items:
            return f"[任务摘要] {goal.strip()[:300]}" if goal else ""
        result = self._context.compress(items, budget=w5.MEMORY_MID_SUMMARY_BUDGET)
        digest = (result.digest or "").strip()
        if digest:
            return digest[:4000]
        # 极端情况（历史全被裁掉）：退化为最后一条 assistant 正文
        for item in reversed(items):
            if str(item.get("role")) == "assistant" and item.get("content"):
                return str(item["content"])[:4000]
        return f"[任务摘要] {goal.strip()[:300]}"

    async def write_mid(
        self,
        *,
        task_id: int | None,
        user_id: int | None,
        goal: str = "",
        messages: Sequence[dict[str, Any]] | None = None,
        summary: str | None = None,
        importance: int = 2,
    ) -> MemoryRecord | None:
        if not self.enabled:
            return None
        content = (summary or self.summarize(messages or [], goal=goal)).strip()
        if not content:
            return None
        row = Memory(
            scope=MemoryScope.MID,
            user_id=int(user_id or DEFAULT_USER_ID),
            task_id=int(task_id) if task_id is not None else None,
            content=content[:4000],
            importance=max(1, min(5, int(importance))),
            hit_count=0,
            chroma_doc_id=None,
            expires_at=datetime.now() + timedelta(days=w5.MEMORY_MID_TTL_DAYS),
        )
        async with self._session_scope() as session:
            session.add(row)
            await session.flush()
            record = MemoryRecord.from_row(row)
        logger.info("memory_mid_written", extra={"task_id": task_id, "memory_id": record.id, "chars": len(content)})
        return record

    # =========================================================
    # 4.3 长期记忆：Reviewer ≥ 3 分 → 向量化归档
    # =========================================================
    async def archive_long(
        self,
        *,
        task_id: int | None,
        user_id: int | None,
        content: str,
        review_score: float,
        importance: int | None = None,
    ) -> MemoryRecord | None:
        """评分不达标直接跳过（这是 FR-014「关键业务结论」的唯一准入条件）。"""
        if not self.enabled:
            return None
        if float(review_score) < w5.MEMORY_LONG_MIN_REVIEW_SCORE:
            logger.info(
                "memory_long_skipped",
                extra={"task_id": task_id, "review_score": float(review_score), "threshold": w5.MEMORY_LONG_MIN_REVIEW_SCORE},
            )
            return None
        text = " ".join((content or "").split())
        if not text:
            return None

        level = importance if importance is not None else max(3, min(5, int(round(float(review_score)))))
        row = Memory(
            scope=MemoryScope.LONG,
            user_id=int(user_id or DEFAULT_USER_ID),
            task_id=int(task_id) if task_id is not None else None,
            content=text[:4000],
            importance=max(3, min(5, int(level))),
            hit_count=0,
            chroma_doc_id=None,
            expires_at=None,  # 长期不过期，只能显式删除
        )
        async with self._session_scope() as session:
            session.add(row)
            await session.flush()  # 先拿自增 id 作为向量库 doc 命名依据
            doc_id, degraded = await self._vectorize(row)
            row.chroma_doc_id = doc_id
            await session.flush()
            record = MemoryRecord.from_row(row)
        logger.info(
            "memory_long_archived",
            extra={
                "task_id": task_id,
                "memory_id": record.id,
                "review_score": float(review_score),
                "doc_id": doc_id,
                "degraded": degraded,
            },
        )
        return record

    async def _vectorize(self, row: Memory) -> tuple[str, bool]:
        metadata = {
            "doc_id": f"agentflow-mem-{row.id}",
            "memory_id": int(row.id),
            "task_id": int(row.task_id) if row.task_id is not None else None,
            "user_id": int(row.user_id),
            "scope": MemoryScope.LONG.value,
            "importance": int(row.importance or 3),
            "title": f"AgentFlow 任务结论 #{row.task_id}" if row.task_id else "AgentFlow 记忆",
        }
        try:
            doc_id = await self._store.upsert(memory_id=int(row.id), content=row.content, metadata=metadata)
            return doc_id, False
        except Exception as exc:  # noqa: BLE001 - 向量库故障必须降级，不能丢业务结论
            logger.warning(
                "memory_vector_upsert_degraded",
                extra={"memory_id": int(row.id), "store": getattr(self._store, "name", "?"), "error": str(exc)[:200]},
            )
            if self._fallback_store is self._store:
                return str(metadata["doc_id"]), True
            doc_id = await self._fallback_store.upsert(
                memory_id=int(row.id), content=row.content, metadata=metadata
            )
            return doc_id, True

    # =========================================================
    # 4.4 长期记忆召回（相似度）
    # =========================================================
    async def _load_long_rows(self, user_id: int | None, *, limit: int | None = None) -> list[Memory]:
        stmt = select(Memory).where(Memory.scope == MemoryScope.LONG)
        if user_id is not None:
            stmt = stmt.where(Memory.user_id == int(user_id))
        stmt = stmt.order_by(Memory.importance.desc(), Memory.id.desc()).limit(limit or w5.MEMORY_RECALL_CANDIDATES)
        async with self._session_scope() as session:
            return list((await session.execute(stmt)).scalars().all())

    async def recall(
        self,
        goal: str,
        *,
        user_id: int | None = None,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> list[MemoryHit]:
        """检索与 goal 相似的长期记忆；向量库故障自动降级为本地关键词排序。"""
        if not self.enabled:
            return []
        query = (goal or "").strip()
        if not query:
            return []

        top = max(1, min(top_k or w5.MEMORY_LONG_TOP_K, w5.MEMORY_LONG_TOP_K))
        if min_score is not None:
            threshold = float(min_score)
        else:
            # 阈值按打分通道分流：向量余弦的「无关联基线」约 0.3~0.4（不是 0 起），
            # 沿用关键词的 0.15 会全命中；两套口径分开校准。
            is_vector_store = getattr(self._store, "name", "") == "local-embedding"
            base = w5.MEMORY_RECALL_MIN_SCORE_VECTOR if is_vector_store else w5.MEMORY_RECALL_MIN_SCORE
            threshold = float(base)
        rows = await self._load_long_rows(user_id)
        if not rows:
            return []

        by_doc: dict[str, Memory] = {}
        candidates: list[tuple[str, str]] = []
        for row in rows:
            doc_id = row.chroma_doc_id or f"agentflow-mem-{row.id}"
            by_doc[doc_id] = row
            by_doc.setdefault(f"agentflow-mem-{row.id}", row)
            candidates.append((doc_id, row.content))

        scored: list[tuple[str, float]] = []
        degraded = False
        try:
            scored = await self._store.search(query, top_k=max(top * 3, top), candidates=candidates)
        except Exception as exc:  # noqa: BLE001 - 向量库故障 → 本地兜底
            degraded = True
            logger.warning("memory_recall_degraded", extra={"store": getattr(self._store, "name", "?"), "error": str(exc)[:200]})

        if (not scored) and w5.MEMORY_LOCAL_RECALL_ENABLED:
            scored = rank_candidates(query, candidates, top_k=max(top * 3, top))
            degraded = degraded or bool(scored)

        # 检索通道自省：向调用方（路由/前端）如实暴露「本次分数是谁打的」。
        # 两种关键词路径：mock 模式（store 本身就是 local-keyword）与
        # project1-rag 通道故障降级（项目一无 /kb/search，长期 404）。
        # 两者 score 都是关键词重合度而非语义相似度，UI 必须能区分。
        store_name = getattr(self._store, "name", "?")
        self.last_retrieval = {
            "store": store_name,
            "scorer": "vector" if (store_name != LocalKeywordVectorStore.name and not degraded) else "local-keyword",
            "degraded": bool(degraded),
        }

        hits: list[MemoryHit] = []
        seen: set[int] = set()
        for doc_id, score in scored:
            row = by_doc.get(doc_id)
            if row is None or row.id in seen:
                continue
            if score < threshold:
                continue
            seen.add(int(row.id))
            hits.append(
                MemoryHit(
                    memory_id=int(row.id),
                    content=row.content,
                    score=float(score),
                    scope=MemoryScope.LONG.value,
                    importance=int(row.importance or 3),
                )
            )
            if len(hits) >= top:
                break

        if hits and degraded:
            logger.info("memory_recall_degraded_hit", extra={"hits": len(hits)})
        if hits:
            await self._bump_hits([hit.memory_id for hit in hits])
        return hits

    async def _bump_hits(self, memory_ids: Sequence[int]) -> None:
        """命中计数（best-effort，失败不影响召回结果）。"""
        if not memory_ids:
            return
        try:
            async with self._session_scope() as session:
                for memory_id in memory_ids:
                    row = await session.get(Memory, int(memory_id))
                    if row is not None:
                        row.hit_count = int(row.hit_count or 0) + 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("memory_hit_bump_failed", extra={"error": str(exc)[:120]})

    async def build_injection(
        self,
        goal: str,
        *,
        user_id: int | None = None,
        top_k: int | None = None,
    ) -> list[str]:
        """生成注入片段（≤3 条、单条截断），直接对应 ``PlannerInput.memory_hits``。"""
        hits = await self.recall(goal, user_id=user_id, top_k=top_k)
        max_chars = w5.MEMORY_INJECT_MAX_CHARS
        return [hit.as_prompt(max_chars=max_chars) for hit in hits]

    async def augment_state(self, state: dict[str, Any], *, user_id: int | None = None) -> dict[str, Any]:
        """在图入口把召回结果填进既有插槽 ``memory_hits``（**不改动入参 dict**）。"""
        if not self.enabled:
            return state
        goal = str(state.get("goal") or "")
        owner = user_id if user_id is not None else state.get("creator_id")
        try:
            hits = await self.build_injection(goal, user_id=int(owner) if owner is not None else None)
        except Exception as exc:  # noqa: BLE001 - 记忆是增强，绝不阻断任务
            logger.warning("memory_augment_failed", extra={"error": str(exc)[:200]})
            return state
        if not hits:
            return state
        merged = dict(state)
        merged["memory_hits"] = hits
        return merged

    # =========================================================
    # 4.5 任务收尾：mid + long 一次完成
    # =========================================================
    async def finalize_task(
        self,
        *,
        task_id: int | None,
        user_id: int | None,
        goal: str = "",
        messages: Sequence[dict[str, Any]] | None = None,
        summary: str | None = None,
        review_score: float = 0.0,
        long_content: str | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "mid_id": None, "long_id": None}
        mid = await self.write_mid(task_id=task_id, user_id=user_id, goal=goal, messages=messages, summary=summary)
        source = long_content or (mid.content if mid is not None else (summary or ""))
        long = await self.archive_long(
            task_id=task_id, user_id=user_id, content=source, review_score=review_score
        )
        return {
            "enabled": True,
            "mid_id": mid.id if mid is not None else None,
            "long_id": long.id if long is not None else None,
            "review_score": float(review_score),
        }

    # =========================================================
    # 4.6 管理 API 支撑
    # =========================================================
    async def list_memories(
        self,
        *,
        user_id: int | None = None,
        scope: MemoryScope | str | None = None,
        task_id: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[MemoryRecord], int]:
        conditions = []
        if user_id is not None:
            conditions.append(Memory.user_id == int(user_id))
        if scope is not None:
            conditions.append(Memory.scope == (scope if isinstance(scope, MemoryScope) else MemoryScope(str(scope))))
        if task_id is not None:
            conditions.append(Memory.task_id == int(task_id))

        offset = max(0, (max(1, page) - 1) * max(1, size))
        stmt = (
            select(Memory)
            .where(*conditions)
            .order_by(Memory.id.desc())
            .offset(offset)
            .limit(max(1, size))
        )
        count_stmt = select(func.count(Memory.id)).where(*conditions)
        async with self._session_scope() as session:
            rows = list((await session.execute(stmt)).scalars().all())
            total = int((await session.execute(count_stmt)).scalar_one() or 0)
        return [MemoryRecord.from_row(row) for row in rows], total

    async def get_memory(self, memory_id: int, *, user_id: int | None = None) -> MemoryRecord | None:
        async with self._session_scope() as session:
            row = await session.get(Memory, int(memory_id))
            if row is None:
                return None
            if user_id is not None and int(row.user_id) != int(user_id):
                return None
            return MemoryRecord.from_row(row)

    async def create_memory(
        self,
        *,
        user_id: int,
        scope: MemoryScope | str = MemoryScope.LONG,
        content: str,
        task_id: int | None = None,
        importance: int = 3,
    ) -> MemoryRecord:
        """手工新增（管理 API）：long 会同步向量化，mid 自动加 30 天 TTL。"""
        if not self.enabled:
            raise MemoryDisabledError()
        scope_enum = scope if isinstance(scope, MemoryScope) else MemoryScope(str(scope))
        if scope_enum == MemoryScope.SHORT:
            raise ValueError("短期记忆由任务会话自动维护，不支持手工新增")

        text = " ".join((content or "").split())
        if not text:
            raise ValueError("content 不能为空")

        row = Memory(
            scope=scope_enum,
            user_id=int(user_id),
            task_id=int(task_id) if task_id is not None else None,
            content=text[:4000],
            importance=max(1, min(5, int(importance))),
            hit_count=0,
            chroma_doc_id=None,
            expires_at=(
                datetime.now() + timedelta(days=w5.MEMORY_MID_TTL_DAYS)
                if scope_enum == MemoryScope.MID
                else None
            ),
        )
        async with self._session_scope() as session:
            session.add(row)
            await session.flush()
            if scope_enum == MemoryScope.LONG:
                doc_id, _ = await self._vectorize(row)
                row.chroma_doc_id = doc_id
                await session.flush()
            record = MemoryRecord.from_row(row)
        return record

    async def delete_memory(self, memory_id: int, *, user_id: int | None = None) -> bool:
        if not self.enabled:
            raise MemoryDisabledError()
        async with self._session_scope() as session:
            row = await session.get(Memory, int(memory_id))
            if row is None:
                return False
            if user_id is not None and int(row.user_id) != int(user_id):
                return False
            await session.execute(delete(Memory).where(Memory.id == int(memory_id)))
        return True

    async def purge_expired(self) -> int:
        """清理过期中期记忆（TTL 30 天滚动；可由 Part2 的定时任务调用）。"""
        if not self.enabled:
            return 0
        async with self._session_scope() as session:
            result = await session.execute(
                delete(Memory).where(Memory.scope == MemoryScope.MID, Memory.expires_at < datetime.now())
            )
            return int(result.rowcount or 0)


class MemoryDisabledError(RuntimeError):
    """分层记忆开关关闭时，写操作明确拒绝（避免"静默成功"造成误解）。"""

    def __init__(self) -> None:
        super().__init__("ENABLE_LAYERED_MEMORY=false，分层记忆未启用")


# ---------------------------------------------------------------------------
# 5) 图入口包装（注入 + 收尾，不改节点）
# ---------------------------------------------------------------------------
def review_score_of(state: dict[str, Any]) -> float:
    """从终态里取 Reviewer 总分（``review.total`` 为主，兼容 score/overall 命名）。"""
    review = state.get("review") or {}
    if not isinstance(review, dict):
        return 0.0
    for key in ("total", "score", "overall"):
        value = review.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    scores = review.get("scores")
    if isinstance(scores, dict):
        numbers = [float(v) for v in scores.values() if isinstance(v, (int, float))]
        if numbers:
            return round(sum(numbers) / len(numbers), 4)
    return 0.0


class MemoryAwareGraph:
    """透明图包装器：入口注入长期记忆，出口归档中期/长期记忆。

    ``__getattr__`` 把 ``aget_state`` / ``aupdate_state`` / ``get_state`` /
    ``pending_interrupts`` 等全部透传给内部图，因此 W3 的挂起判断、断点续跑、
    轨迹回放代码**无需任何改动**即可继续工作。
    """

    def __init__(
        self,
        graph: Any,
        memory: LayeredMemoryService,
        *,
        user_id: int | None = None,
        finalize: bool = True,
    ) -> None:
        self._graph = graph
        self.memory = memory
        self.user_id = user_id
        self.finalize_enabled = finalize

    # ---------------- 透传 ----------------
    def __getattr__(self, item: str) -> Any:
        return getattr(self._graph, item)

    @property
    def inner(self) -> Any:
        return self._graph

    # ---------------- 注入 ----------------
    async def prepare(self, input: Any) -> Any:
        if not self.memory.enabled or not isinstance(input, dict):
            return input
        return await self.memory.augment_state(input, user_id=self.user_id)

    # ---------------- 收尾 ----------------
    async def finalize(self, state: Any) -> None:
        if not (self.finalize_enabled and self.memory.enabled) or not isinstance(state, dict):
            return
        if str(state.get("status")) != "COMPLETED":
            return
        summary = state.get("context_digest") or ""
        artifacts = state.get("artifacts") or []
        if not summary and artifacts:
            summary = json.dumps(artifacts[:2], ensure_ascii=False, default=str)
        try:
            await self.memory.finalize_task(
                task_id=state.get("task_id"),
                user_id=self.user_id if self.user_id is not None else state.get("creator_id"),
                goal=str(state.get("goal") or ""),
                messages=list(state.get("messages") or []),
                summary=summary or None,
                review_score=review_score_of(state),
            )
        except Exception as exc:  # noqa: BLE001 - 记忆归档失败不回滚任务
            logger.warning("memory_finalize_failed", extra={"task_id": state.get("task_id"), "error": str(exc)[:200]})

    # ---------------- 执行入口 ----------------
    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        prepared = await self.prepare(input)
        state = await self._graph.ainvoke(prepared, config, **kwargs)
        await self.finalize(state)
        return state

    async def astream(self, input: Any, config: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        prepared = await self.prepare(input)
        last: Any = None
        async for chunk in self._graph.astream(prepared, config, **kwargs):
            last = chunk
            yield chunk
        # 流式模式下终态以最后一个 chunk 为准（values 模式即完整 state）
        if isinstance(last, dict):
            await self.finalize(last)


def install_layered_memory(
    graph: Any,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    redis: Redis | None = None,
    store: MemoryVectorStore | None = None,
    context: ContextService | None = None,
    enabled: bool | None = None,
    user_id: int | None = None,
) -> MemoryAwareGraph:
    """给任意已编译图套上分层记忆（一行接入，不碰 ``build_graph``）。"""
    memory = LayeredMemoryService(
        session_factory=session_factory,
        redis=redis,
        store=store,
        context=context,
        enabled=enabled,
    )
    return MemoryAwareGraph(graph, memory, user_id=user_id)


__all__ = [
    "DEFAULT_USER_ID",
    "MemoryDisabledError",
    "MemoryHit",
    "MemoryRecord",
    "MemoryStoreError",
    "MemoryVectorStore",
    "LocalKeywordVectorStore",
    "HttpRagVectorStore",
    "LayeredMemoryService",
    "MemoryAwareGraph",
    "build_memory_store",
    "install_layered_memory",
    "keyword_score",
    "rank_candidates",
    "review_score_of",
    "tokenize",
]
