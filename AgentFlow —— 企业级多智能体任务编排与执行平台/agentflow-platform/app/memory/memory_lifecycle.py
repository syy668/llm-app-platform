"""P2 收尾 · 任务 2：长期记忆「语义去重 + 时间衰减 + TTL 回收」。

## 落点选择：包装而不是改文件

``app/memory/layered_memory.py`` 属 W5-Part1 冻结件（207 条基线用例依赖它的逐字节行为）。
本模块**一行都不改它**，而是提供两种等价的生效方式：

| 方式 | 用法 | 适用 |
|---|---|---|
| 显式子类 | ``LifecycleMemoryService(...)`` | 新代码/新路由，类型清晰 |
| 运行期包装 | ``install_memory_lifecycle()`` | 已有代码（``memory_router`` 等）零改动即生效 |

两者共用同一组实现函数（``archive_long_with_lifecycle`` / ``recall_with_decay`` /
``purge_with_ttl``），所以不存在「子类与包装行为不一致」的问题。
``install_memory_lifecycle()`` 在开关关闭时**直接 return False，一个方法都不替换**；
即使替换后运行期把开关改成 false，包装函数也会立刻委派回原实现。

## 三项能力

1. **语义去重**：新结论入库前与该用户最近 ``MEMORY_DEDUP_CANDIDATES`` 条长期记忆比对，
   相似度 ≥ ``MEMORY_DEDUP_THRESHOLD`` 则**合并**（取更长正文、取更高 importance、
   命中数 +1、刷新 TTL），不再新增一行；
2. **时间衰减**：召回分重排为 ``w = exp(-λ·Δt)``，``Δt`` 以天计，λ 默认 0.01
   （半衰期 ≈ 69 天），并设 ``MEMORY_DECAY_MIN_WEIGHT`` 下限，保证久远但高价值的
   结论不会被彻底压到 0；
3. **TTL 回收**：长期记忆默认 180 天过期（``MEMORY_LONG_TTL_DAYS=0`` 表示永久），
   召回时过滤 ``expires_at`` 已过的条目，``purge_expired()`` 一并清理 mid + long。

## 相似度怎么算（为什么不用向量库）

进程内用**特征哈希向量 + 余弦**：``tokenize`` 复用分层记忆既有切词（中文 2-gram +
英文词 + 数字），token 经 ``crc32`` 落到固定维度并 L2 归一化。它确定性、离线、
零依赖，且与召回用的是同一套词表口径（不会出现「去重认为不同、召回认为相同」）。
需要真实语义向量时用 ``set_embedder(fn)`` 注入项目一的 embedding 接口即可，
其余逻辑不变。
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import delete, select

from app.core.logging import get_logger
from app.core.w5_config import w5
from app.memory.layered_memory import LayeredMemoryService, MemoryHit, MemoryRecord, tokenize
from app.schemas.enums import MemoryScope
from app.schemas.orm_models import Memory

logger = get_logger(__name__)

#: 冻结原实现的快照（**必须在任何 install 之前取**），包装函数靠它回调原始逻辑
_ORIGINAL_ARCHIVE_LONG = LayeredMemoryService.archive_long
_ORIGINAL_RECALL = LayeredMemoryService.recall
_ORIGINAL_PURGE_EXPIRED = LayeredMemoryService.purge_expired
_ORIGINAL_CREATE_MEMORY = LayeredMemoryService.create_memory


# ---------------------------------------------------------------------------
# 1) 配置
# ---------------------------------------------------------------------------
class MemoryLifecycleSettings(BaseSettings):
    """生命周期专项开关（独立收口，不改 ``w5_config``）。默认全部关闭。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # 总开关：false → 不比对、不衰减、不加 TTL，包装函数直接委派原实现
    ENABLE_MEMORY_LIFECYCLE: bool = False

    # ---------------- 语义去重 ----------------
    MEMORY_DEDUP_ENABLED: bool = True
    # 0.88 是「同一结论换了个说法」的经验下限；接真实 embedding 后可上调到 0.95
    MEMORY_DEDUP_THRESHOLD: float = Field(default=0.88, ge=0.5, le=1.0)
    MEMORY_DEDUP_CANDIDATES: int = Field(default=50, ge=1, le=500)

    # ---------------- 时间衰减 ----------------
    MEMORY_DECAY_ENABLED: bool = True
    MEMORY_DECAY_LAMBDA: float = Field(default=0.01, ge=0.0, le=1.0)
    MEMORY_DECAY_MIN_WEIGHT: float = Field(default=0.05, gt=0.0, le=1.0)
    MEMORY_DECAY_IMPORTANCE_BOOST: float = Field(default=0.06, ge=0.0, le=0.5)
    # 衰减后的分数是否再按 min_score 二次过滤（默认否：只影响排序，不减少召回条数）
    MEMORY_DECAY_REFILTER: bool = False

    # ---------------- TTL ----------------
    MEMORY_LONG_TTL_DAYS: int = Field(default=180, ge=0, le=3650)
    MEMORY_TTL_CLEANUP_ENABLED: bool = True

    # ---------------- 向量 ----------------
    MEMORY_EMBED_DIM: int = Field(default=128, ge=32, le=1024)

    @property
    def long_ttl_delta(self) -> timedelta | None:
        return timedelta(days=self.MEMORY_LONG_TTL_DAYS) if self.MEMORY_LONG_TTL_DAYS > 0 else None

    @property
    def half_life_days(self) -> float | None:
        lam = self.MEMORY_DECAY_LAMBDA
        return round(math.log(2) / lam, 2) if lam > 0 else None

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.ENABLE_MEMORY_LIFECYCLE),
            "dedup": {
                "enabled": self.MEMORY_DEDUP_ENABLED,
                "threshold": self.MEMORY_DEDUP_THRESHOLD,
                "candidates": self.MEMORY_DEDUP_CANDIDATES,
            },
            "decay": {
                "enabled": self.MEMORY_DECAY_ENABLED,
                "lambda": self.MEMORY_DECAY_LAMBDA,
                "half_life_days": self.half_life_days,
                "min_weight": self.MEMORY_DECAY_MIN_WEIGHT,
                "refilter": self.MEMORY_DECAY_REFILTER,
            },
            "ttl": {
                "long_ttl_days": self.MEMORY_LONG_TTL_DAYS,
                "cleanup_enabled": self.MEMORY_TTL_CLEANUP_ENABLED,
            },
        }


@lru_cache(maxsize=1)
def get_memory_lifecycle_settings() -> MemoryLifecycleSettings:
    return MemoryLifecycleSettings()


memory_lifecycle = get_memory_lifecycle_settings()


def lifecycle_enabled(override: bool | None = None) -> bool:
    """运行期读取（允许测试/调用方覆盖，语义与 ``layered_memory_enabled`` 一致）。"""
    if override is not None:
        return bool(override)
    return bool(memory_lifecycle.ENABLE_MEMORY_LIFECYCLE)


# ---------------------------------------------------------------------------
# 2) 向量与相似度
# ---------------------------------------------------------------------------
_Embedder = Callable[[str], Sequence[float]]
_embedder: _Embedder | None = None


def set_embedder(embedder: _Embedder | None) -> None:
    """注入真实 embedding（项目一接口）；传 ``None`` 恢复内置特征哈希实现。"""
    global _embedder
    _embedder = embedder


def get_embedder() -> _Embedder | None:
    return _embedder


def embed_text(text: str, *, dim: int | None = None) -> list[float]:
    """特征哈希向量：token → crc32 → 桶，最后 L2 归一化（确定性、离线、零依赖）。"""
    if _embedder is not None:
        return [float(value) for value in _embedder(text)]
    size = int(dim or memory_lifecycle.MEMORY_EMBED_DIM)
    vector = [0.0] * size
    tokens = tokenize(text)
    if not tokens:
        return vector
    for token in tokens:
        bucket = zlib.crc32(token.encode("utf-8")) % size
        vector[bucket] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return vector
    return [value / norm for value in vector]


def cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    """余弦相似度；长度不一致时按较短者截断（避免不同 dimension 配置互相炸掉）。"""
    if not vec_a or not vec_b:
        return 0.0
    size = min(len(vec_a), len(vec_b))
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for index in range(size):
        a = float(vec_a[index])
        b = float(vec_b[index])
        dot += a * b
        norm_a += a * a
        norm_b += b * b
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    value = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    return round(max(-1.0, min(1.0, value)), 6)


def semantic_similarity(text_a: str, text_b: str, *, dim: int | None = None) -> float:
    """两段文本的语义相似度 ∈ [0, 1]（余弦负值一律归零，业务上无意义）。"""
    if not (text_a or "").strip() or not (text_b or "").strip():
        return 0.0
    return max(0.0, cosine_similarity(embed_text(text_a, dim=dim), embed_text(text_b, dim=dim)))


@dataclass(slots=True)
class DuplicateMatch:
    """命中重复时返回的既有条目信息（便于调用方日志/断言）。"""

    memory_id: int
    similarity: float
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "similarity": round(self.similarity, 4),
            "content_chars": len(self.content),
        }


def find_duplicate(
    new_text: str,
    existing: Sequence[tuple[int, str]],
    *,
    threshold: float | None = None,
    dim: int | None = None,
) -> DuplicateMatch | None:
    """在 ``(memory_id, content)`` 集合里找与新文本最相似的重复项。"""
    limit = memory_lifecycle.MEMORY_DEDUP_THRESHOLD if threshold is None else float(threshold)
    target = embed_text(new_text, dim=dim)
    best: DuplicateMatch | None = None
    for memory_id, content in existing:
        similarity = cosine_similarity(target, embed_text(content, dim=dim))
        if similarity >= limit and (best is None or similarity > best.similarity):
            best = DuplicateMatch(memory_id=int(memory_id), similarity=similarity, content=content)
            if similarity >= 0.9999:
                break  # 完全一致，无需继续比较
    return best


# ---------------------------------------------------------------------------
# 3) 时间衰减
# ---------------------------------------------------------------------------
def decay_weight(
    age_days: float,
    *,
    lam: float | None = None,
    min_weight: float | None = None,
) -> float:
    """``w = exp(-λ·Δt)``，其中 Δt 以天计；``λ=0`` 表示不衰减。"""
    rate = memory_lifecycle.MEMORY_DECAY_LAMBDA if lam is None else float(lam)
    floor = memory_lifecycle.MEMORY_DECAY_MIN_WEIGHT if min_weight is None else float(min_weight)
    age = max(0.0, float(age_days or 0.0))
    if rate <= 0.0:
        return 1.0
    weight = math.exp(-rate * age)
    return round(max(floor, min(1.0, weight)), 6)


def age_days(created_at: datetime | None, *, now: datetime | None = None) -> float:
    """条目年龄（天）；``created_at`` 缺失按「刚写入」处理（不冤枉新记忆）。"""
    if created_at is None:
        return 0.0
    current = now or datetime.now()
    delta = current - created_at
    return max(0.0, delta.total_seconds() / 86400.0)


def effective_score(
    raw_score: float,
    *,
    age_in_days: float,
    importance: int = 3,
    lam: float | None = None,
    boost: float | None = None,
) -> float:
    """衰减后的召回分：``raw × exp(-λ·Δt) × (1 + boost × (importance - 3))``。

    importance 加权只做**轻微上调**（默认每条 +6%），避免「重要但久远」被时间完全淹没；
    结果统一裁到 ``[0, 1]``，与 ``align_score`` 口径一致。
    """
    rate_boost = memory_lifecycle.MEMORY_DECAY_IMPORTANCE_BOOST if boost is None else float(boost)
    weight = decay_weight(age_in_days, lam=lam)
    factor = 1.0 + rate_boost * (max(1, min(5, int(importance))) - 3)
    value = max(0.0, float(raw_score)) * weight * max(0.0, factor)
    return round(min(1.0, value), 4)


# ---------------------------------------------------------------------------
# 4) 运行统计（自检 / 核对清单用）
# ---------------------------------------------------------------------------
_STATS: dict[str, int] = {
    "archive_calls": 0,
    "merged": 0,
    "ttl_applied": 0,
    "recall_calls": 0,
    "decayed": 0,
    "expired_filtered": 0,
    "purged": 0,
}


def stats() -> dict[str, int]:
    return dict(_STATS)


def reset_stats() -> None:
    for key in _STATS:
        _STATS[key] = 0


# ---------------------------------------------------------------------------
# 5) 数据库侧实现（archive / recall / purge）
# ---------------------------------------------------------------------------
async def _load_rows(service: LayeredMemoryService, memory_ids: Sequence[int]) -> dict[int, Memory]:
    ids = [int(item) for item in memory_ids]
    if not ids:
        return {}
    async with service._session_scope() as session:  # noqa: SLF001 - 同包内复用其会话语义
        rows = (await session.execute(select(Memory).where(Memory.id.in_(ids)))).scalars().all()
    return {int(row.id): row for row in rows}


async def _recent_long_rows(
    service: LayeredMemoryService,
    *,
    user_id: int | None,
    limit: int,
) -> list[Memory]:
    stmt = select(Memory).where(Memory.scope == MemoryScope.LONG)
    if user_id is not None:
        stmt = stmt.where(Memory.user_id == int(user_id))
    stmt = stmt.order_by(Memory.id.desc()).limit(max(1, int(limit)))
    async with service._session_scope() as session:  # noqa: SLF001
        return list((await session.execute(stmt)).scalars().all())


def _is_expired(row: Memory, *, now: datetime | None = None) -> bool:
    if row.expires_at is None:
        return False
    return row.expires_at <= (now or datetime.now())


async def _apply_ttl(service: LayeredMemoryService, memory_id: int, *, days: int | None = None) -> datetime | None:
    delta_days = memory_lifecycle.MEMORY_LONG_TTL_DAYS if days is None else int(days)
    if delta_days <= 0:
        return None
    expiry = datetime.now() + timedelta(days=delta_days)
    async with service._session_scope() as session:  # noqa: SLF001
        row = await session.get(Memory, int(memory_id))
        if row is not None:
            row.expires_at = expiry
    _STATS["ttl_applied"] += 1
    return expiry


async def find_duplicate_row(
    service: LayeredMemoryService,
    *,
    user_id: int | None,
    text: str,
) -> DuplicateMatch | None:
    """在最近 N 条长期记忆里查重（同 user 隔离，避免跨租户误合并）。"""
    if not memory_lifecycle.MEMORY_DEDUP_ENABLED:
        return None
    rows = await _recent_long_rows(service, user_id=user_id, limit=memory_lifecycle.MEMORY_DEDUP_CANDIDATES)
    if not rows:
        return None
    return find_duplicate(text, [(int(row.id), row.content) for row in rows])


async def merge_into(
    service: LayeredMemoryService,
    memory_id: int,
    *,
    text: str,
    review_score: float,
    importance: int | None,
    similarity: float,
) -> MemoryRecord | None:
    """把新结论合并进既有条目：更长正文优先、importance 取高、命中 +1、刷新 TTL。

    返回 ``None`` 表示目标条目已被并发删除——调用方应退回「新增」路径，而不是丢数据。
    """
    replaced = False
    async with service._session_scope() as session:  # noqa: SLF001
        row = await session.get(Memory, int(memory_id))
        if row is None:
            logger.info("memory_merge_target_gone", extra={"memory_id": int(memory_id)})
            return None
        before = " ".join((row.content or "").split())
        merged = text if len(text) > len(before) else before
        if importance is not None:
            row.importance = max(int(row.importance or 3), max(1, min(5, int(importance))))
        else:
            row.importance = max(int(row.importance or 3), max(3, min(5, int(round(float(review_score))))))
        replaced = merged != before
        if replaced:
            row.content = merged[:4000]
        row.hit_count = int(row.hit_count or 0) + 1
        delta = memory_lifecycle.long_ttl_delta
        if delta is not None:
            row.expires_at = datetime.now() + delta
            _STATS["ttl_applied"] += 1
        await session.flush()
        record = MemoryRecord.from_row(row)
        chars = len(row.content or "")

    if replaced and record.chroma_doc_id:
        await _refresh_vector(service, record)
    _STATS["merged"] += 1
    logger.info(
        "memory_long_merged",
        extra={
            "memory_id": record.id,
            "similarity": round(float(similarity), 4),
            "content_replaced": replaced,
            "chars": chars,
            "review_score": float(review_score),
        },
    )
    return record


async def _refresh_vector(service: LayeredMemoryService, record: MemoryRecord) -> None:
    """合并后同步向量库（best-effort：失败只是召回略降级，不影响合并结果）。"""
    metadata = {
        "doc_id": record.chroma_doc_id,
        "memory_id": record.id,
        "task_id": record.task_id,
        "user_id": record.user_id,
        "scope": MemoryScope.LONG.value,
        "importance": record.importance,
        "merged": True,
    }
    try:
        await service._store.upsert(  # noqa: SLF001
            memory_id=record.id, content=record.content, metadata=metadata
        )
    except Exception as exc:  # noqa: BLE001 - 向量库是旁路，合并结果已落 MySQL
        logger.warning("memory_merge_vector_refresh_failed", extra={"memory_id": record.id, "error": str(exc)[:200]})


async def archive_long_with_lifecycle(
    service: LayeredMemoryService,
    *,
    task_id: int | None,
    user_id: int | None,
    content: str,
    review_score: float,
    importance: int | None = None,
) -> MemoryRecord | None:
    """``archive_long`` 的生命周期版本：先查重合并，未命中才走原实现新增。"""
    _STATS["archive_calls"] += 1
    if not service.enabled:
        return await _ORIGINAL_ARCHIVE_LONG(
            service, task_id=task_id, user_id=user_id, content=content, review_score=review_score, importance=importance
        )

    text = " ".join((content or "").split())
    if text and float(review_score) >= w5.MEMORY_LONG_MIN_REVIEW_SCORE:
        match = await find_duplicate_row(service, user_id=user_id, text=text)
        if match is not None:
            merged = await merge_into(
                service,
                match.memory_id,
                text=text,
                review_score=float(review_score),
                importance=importance,
                similarity=match.similarity,
            )
            if merged is not None:
                return merged

    record = await _ORIGINAL_ARCHIVE_LONG(
        service, task_id=task_id, user_id=user_id, content=content, review_score=review_score, importance=importance
    )
    if record is None:
        return None
    expiry = await _apply_ttl(service, record.id)
    if expiry is not None:
        record.expires_at = expiry
    return record


async def recall_with_decay(
    service: LayeredMemoryService,
    goal: str,
    *,
    user_id: int | None = None,
    top_k: int | None = None,
    min_score: float | None = None,
) -> list[MemoryHit]:
    """``recall`` 的生命周期版本：先过滤过期条目，再按时间衰减重排。"""
    _STATS["recall_calls"] += 1
    hits = await _ORIGINAL_RECALL(service, goal, user_id=user_id, top_k=top_k, min_score=min_score)
    if not hits:
        return []

    rows = await _load_rows(service, [hit.memory_id for hit in hits])
    now = datetime.now()
    threshold = w5.MEMORY_RECALL_MIN_SCORE if min_score is None else float(min_score)
    decay_on = bool(memory_lifecycle.MEMORY_DECAY_ENABLED)
    refilter = bool(memory_lifecycle.MEMORY_DECAY_REFILTER)

    kept: list[MemoryHit] = []
    for hit in hits:
        row = rows.get(int(hit.memory_id))
        if row is not None and _is_expired(row, now=now):
            _STATS["expired_filtered"] += 1
            continue
        if decay_on:
            age = age_days(row.created_at if row is not None else None, now=now)
            hit.score = effective_score(hit.score, age_in_days=age, importance=hit.importance)
            _STATS["decayed"] += 1
            if refilter and hit.score < threshold:
                continue
        kept.append(hit)

    cap = max(1, min(top_k or w5.MEMORY_LONG_TOP_K, w5.MEMORY_LONG_TOP_K))
    kept.sort(key=lambda item: (-item.score, item.memory_id))
    result = kept[:cap]
    if decay_on and result:
        logger.debug(
            "memory_recall_decayed",
            extra={"candidates": len(hits), "kept": len(result), "top_score": result[0].score},
        )
    return result


async def purge_with_ttl(service: LayeredMemoryService) -> int:
    """清理过期条目（mid + long）；返回删除总行数，接口与原 ``purge_expired`` 一致。"""
    if not service.enabled:
        return 0
    if not memory_lifecycle.MEMORY_TTL_CLEANUP_ENABLED:
        return await _ORIGINAL_PURGE_EXPIRED(service)
    details = await purge_expired_details(service)
    total = int(details["mid"] + details["long"])
    _STATS["purged"] += total
    return total


async def purge_expired_details(service: LayeredMemoryService) -> dict[str, int]:
    """按层拆分的清理明细（运维/自检用；``purge_expired`` 的返回值仍是 int）。"""
    if not service.enabled:
        return {"mid": 0, "long": 0}
    now = datetime.now()
    counts: dict[str, int] = {}
    async with service._session_scope() as session:  # noqa: SLF001
        for scope in (MemoryScope.MID, MemoryScope.LONG):
            result = await session.execute(
                delete(Memory).where(
                    Memory.scope == scope,
                    Memory.expires_at.is_not(None),
                    Memory.expires_at < now,
                )
            )
            counts[scope.value] = int(result.rowcount or 0)
    if counts.get("mid") or counts.get("long"):
        logger.info("memory_ttl_purged", extra=counts)
    return {"mid": counts.get(MemoryScope.MID.value, 0), "long": counts.get(MemoryScope.LONG.value, 0)}


async def create_memory_with_lifecycle(
    service: LayeredMemoryService,
    *,
    user_id: int,
    scope: MemoryScope | str = MemoryScope.LONG,
    content: str,
    task_id: int | None = None,
    importance: int = 3,
) -> MemoryRecord:
    """``create_memory`` 的生命周期版本：手工新增 long 时同样查重 + 加 TTL。"""
    scope_enum = scope if isinstance(scope, MemoryScope) else MemoryScope(str(scope))
    text = " ".join((content or "").split())
    if scope_enum == MemoryScope.LONG and service.enabled and text:
        match = await find_duplicate_row(service, user_id=user_id, text=text)
        if match is not None:
            merged = await merge_into(
                service,
                match.memory_id,
                text=text,
                review_score=float(importance),
                importance=importance,
                similarity=match.similarity,
            )
            if merged is not None:
                return merged
    record = await _ORIGINAL_CREATE_MEMORY(service, user_id=user_id, scope=scope, content=content, task_id=task_id, importance=importance)
    if scope_enum == MemoryScope.LONG:
        expiry = await _apply_ttl(service, record.id)
        if expiry is not None:
            record.expires_at = expiry
    return record


# ---------------------------------------------------------------------------
# 6) 显式子类（对外接口与父类完全一致）
# ---------------------------------------------------------------------------
class LifecycleMemoryService(LayeredMemoryService):
    """在分层记忆之上叠生命周期策略：``archive_long`` / ``recall`` / ``purge_expired`` /
    ``create_memory`` 四个方法走新实现，其余方法**原样继承**（包括开关语义）。"""

    async def archive_long(
        self,
        *,
        task_id: int | None,
        user_id: int | None,
        content: str,
        review_score: float,
        importance: int | None = None,
    ) -> MemoryRecord | None:
        if not lifecycle_enabled():
            return await _ORIGINAL_ARCHIVE_LONG(
                self, task_id=task_id, user_id=user_id, content=content, review_score=review_score, importance=importance
            )
        return await archive_long_with_lifecycle(
            self,
            task_id=task_id,
            user_id=user_id,
            content=content,
            review_score=review_score,
            importance=importance,
        )

    async def recall(
        self,
        goal: str,
        *,
        user_id: int | None = None,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> list[MemoryHit]:
        if not lifecycle_enabled():
            return await _ORIGINAL_RECALL(self, goal, user_id=user_id, top_k=top_k, min_score=min_score)
        return await recall_with_decay(self, goal, user_id=user_id, top_k=top_k, min_score=min_score)

    async def purge_expired(self) -> int:
        if not lifecycle_enabled():
            return await _ORIGINAL_PURGE_EXPIRED(self)
        return await purge_with_ttl(self)

    async def create_memory(
        self,
        *,
        user_id: int,
        scope: MemoryScope | str = MemoryScope.LONG,
        content: str,
        task_id: int | None = None,
        importance: int = 3,
    ) -> MemoryRecord:
        if not lifecycle_enabled():
            return await _ORIGINAL_CREATE_MEMORY(
                self, user_id=user_id, scope=scope, content=content, task_id=task_id, importance=importance
            )
        return await create_memory_with_lifecycle(
            self, user_id=user_id, scope=scope, content=content, task_id=task_id, importance=importance
        )


# ---------------------------------------------------------------------------
# 7) 运行期包装（既有代码零改动生效）
# ---------------------------------------------------------------------------
_installed = False


async def _archive_long_patch(
    self: LayeredMemoryService,
    *,
    task_id: int | None,
    user_id: int | None,
    content: str,
    review_score: float,
    importance: int | None = None,
) -> MemoryRecord | None:
    if not lifecycle_enabled():
        return await _ORIGINAL_ARCHIVE_LONG(
            self, task_id=task_id, user_id=user_id, content=content, review_score=review_score, importance=importance
        )
    return await archive_long_with_lifecycle(
        self, task_id=task_id, user_id=user_id, content=content, review_score=review_score, importance=importance
    )


async def _recall_patch(
    self: LayeredMemoryService,
    goal: str,
    *,
    user_id: int | None = None,
    top_k: int | None = None,
    min_score: float | None = None,
) -> list[MemoryHit]:
    if not lifecycle_enabled():
        return await _ORIGINAL_RECALL(self, goal, user_id=user_id, top_k=top_k, min_score=min_score)
    return await recall_with_decay(self, goal, user_id=user_id, top_k=top_k, min_score=min_score)


async def _purge_patch(self: LayeredMemoryService) -> int:
    if not lifecycle_enabled():
        return await _ORIGINAL_PURGE_EXPIRED(self)
    return await purge_with_ttl(self)


async def _create_memory_patch(
    self: LayeredMemoryService,
    *,
    user_id: int,
    scope: MemoryScope | str = MemoryScope.LONG,
    content: str,
    task_id: int | None = None,
    importance: int = 3,
) -> MemoryRecord:
    if not lifecycle_enabled():
        return await _ORIGINAL_CREATE_MEMORY(self, user_id=user_id, scope=scope, content=content, task_id=task_id, importance=importance)
    return await create_memory_with_lifecycle(self, user_id=user_id, scope=scope, content=content, task_id=task_id, importance=importance)


def install_memory_lifecycle(*, force: bool | None = None) -> bool:
    """给 ``LayeredMemoryService`` 装上生命周期策略；开关关闭时**不替换任何方法**。"""
    global _installed
    if not lifecycle_enabled(force):
        return False
    if _installed:
        return True
    LayeredMemoryService.archive_long = _archive_long_patch  # type: ignore[method-assign]
    LayeredMemoryService.recall = _recall_patch  # type: ignore[method-assign]
    LayeredMemoryService.purge_expired = _purge_patch  # type: ignore[method-assign]
    LayeredMemoryService.create_memory = _create_memory_patch  # type: ignore[method-assign]
    _installed = True
    logger.info("memory_lifecycle_installed", extra=memory_lifecycle.describe())
    return True


def uninstall_memory_lifecycle() -> None:
    """还原四个方法（测试与热切换用；幂等）。"""
    global _installed
    LayeredMemoryService.archive_long = _ORIGINAL_ARCHIVE_LONG  # type: ignore[method-assign]
    LayeredMemoryService.recall = _ORIGINAL_RECALL  # type: ignore[method-assign]
    LayeredMemoryService.purge_expired = _ORIGINAL_PURGE_EXPIRED  # type: ignore[method-assign]
    LayeredMemoryService.create_memory = _ORIGINAL_CREATE_MEMORY  # type: ignore[method-assign]
    _installed = False


def is_installed() -> bool:
    return _installed


def status() -> dict[str, Any]:
    """自检载体：开关、是否已包装、策略参数、累计计数。"""
    return {
        "enabled": lifecycle_enabled(),
        "installed": _installed,
        "patched_methods": [
            name
            for name in ("archive_long", "recall", "purge_expired", "create_memory")
            if getattr(LayeredMemoryService, name) is not getattr(_OriginalHolder, name)
        ],
        "policy": memory_lifecycle.describe(),
        "stats": stats(),
        "original_methods_intact": (
            LayeredMemoryService.archive_long is _ORIGINAL_ARCHIVE_LONG
            and LayeredMemoryService.recall is _ORIGINAL_RECALL
            and LayeredMemoryService.purge_expired is _ORIGINAL_PURGE_EXPIRED
            and LayeredMemoryService.create_memory is _ORIGINAL_CREATE_MEMORY
        ),
    }


class _OriginalHolder:
    """仅用于 ``status()`` 对比（挂原始函数，避免动态取属性时受 patch 影响）。"""

    archive_long = _ORIGINAL_ARCHIVE_LONG
    recall = _ORIGINAL_RECALL
    purge_expired = _ORIGINAL_PURGE_EXPIRED
    create_memory = _ORIGINAL_CREATE_MEMORY


def reset() -> None:
    """测试辅助：卸载包装 + 清空计数 + 清掉自定义 embedder。"""
    uninstall_memory_lifecycle()
    reset_stats()
    set_embedder(None)


def build_lifecycle_memory(**kwargs: Any) -> LayeredMemoryService:
    """工厂：开启时返回 ``LifecycleMemoryService``，关闭时返回普通实现（行为完全一致）。"""
    cls = LifecycleMemoryService if lifecycle_enabled() else LayeredMemoryService
    return cls(**kwargs)


__all__ = [
    "DuplicateMatch",
    "LifecycleMemoryService",
    "MemoryLifecycleSettings",
    "age_days",
    "archive_long_with_lifecycle",
    "build_lifecycle_memory",
    "cosine_similarity",
    "create_memory_with_lifecycle",
    "decay_weight",
    "effective_score",
    "embed_text",
    "find_duplicate",
    "find_duplicate_row",
    "get_embedder",
    "get_memory_lifecycle_settings",
    "install_memory_lifecycle",
    "is_installed",
    "lifecycle_enabled",
    "memory_lifecycle",
    "merge_into",
    "purge_expired_details",
    "purge_with_ttl",
    "recall_with_decay",
    "reset",
    "reset_stats",
    "semantic_similarity",
    "set_embedder",
    "stats",
    "status",
    "uninstall_memory_lifecycle",
]
