"""P2 收尾 · 任务 2 单元测试：长期记忆去重 / 时间衰减 / TTL 回收。

覆盖任务书要求的三类用例：
1. **重复记忆合并**——同文合并为一行、正文取更长、importance 取高、命中计数 +1；
2. **时间衰减打分**——``w = exp(-λ·Δt)`` 的半衰期、下限、importance 微调与召回重排；
3. **过期记忆自动过滤**——召回期过滤 + ``purge_expired`` 按层清理。

外加两条硬性边界：
- **开关关闭时记忆模块完全无执行逻辑**（不替换方法、不查库、不调用相似度计算）；
- 对外接口不变（``LifecycleMemoryService`` 是 ``LayeredMemoryService`` 的子类，
  返回类型/签名逐一对齐）。

全部离线：SQLite 内存库 + 本地关键词向量库，不依赖 MySQL / Redis / 真实 embedding。
"""

from __future__ import annotations

import inspect
import math
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.w5_config import w5
from app.memory import memory_lifecycle as ml
from app.memory.layered_memory import LayeredMemoryService, LocalKeywordVectorStore, MemoryHit
from app.memory.memory_lifecycle import (
    LifecycleMemoryService,
    age_days,
    archive_long_with_lifecycle,
    build_lifecycle_memory,
    cosine_similarity,
    create_memory_with_lifecycle,
    decay_weight,
    effective_score,
    embed_text,
    find_duplicate,
    install_memory_lifecycle,
    lifecycle_enabled,
    purge_expired_details,
    purge_with_ttl,
    recall_with_decay,
    reset,
    semantic_similarity,
    set_embedder,
    status,
    uninstall_memory_lifecycle,
)
from app.schemas.enums import MemoryScope
from app.schemas.orm_models import Memory
from app.services.context_service import ContextService

GOAL = "分析上周数码配件 GMV 下滑原因"
CONTENT_DIGITAL = "数码配件 W42 GMV 环比下滑 18%，转化率由 3.1% 降至 1.7%；" "根因是爆款 SKU-10086 断货导致自然流量结构恶化。"
CONTENT_BEAUTY = "美妆品类 W42 投放预算上调后 ROI 1.8，低于大盘 2.4，建议回收 20% 预算。"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_lifecycle_state() -> None:
    """每个用例结束都卸载包装、清空计数、清掉注入的 embedder（防止污染其它模块）。"""
    yield
    reset()


@pytest.fixture
def memory_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w5, "ENABLE_LAYERED_MEMORY", True)


@pytest.fixture
def lifecycle_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ml.memory_lifecycle, "ENABLE_MEMORY_LIFECYCLE", True)
    monkeypatch.setattr(w5, "ENABLE_LAYERED_MEMORY", True)


def make_service(session_factory: async_sessionmaker[AsyncSession], *, enabled: bool = True) -> LayeredMemoryService:
    return LayeredMemoryService(
        session_factory=session_factory,
        context=ContextService(),
        enabled=enabled,
        store=LocalKeywordVectorStore(),
        fallback_store=LocalKeywordVectorStore(),
    )


async def add_long(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: int,
    content: str,
    *,
    days_old: float = 0.0,
    importance: int = 4,
    expires_in_days: float | None = None,
) -> int:
    """直接落一行 long 记忆（created_at 可回溯，用于构造「久远记忆」）。"""
    async with session_factory() as session:
        row = Memory(
            scope=MemoryScope.LONG,
            user_id=user_id,
            task_id=None,
            content=content,
            importance=importance,
            hit_count=0,
            chroma_doc_id=None,
            created_at=datetime.now() - timedelta(days=days_old),
            expires_at=(None if expires_in_days is None else datetime.now() + timedelta(days=expires_in_days)),
        )
        session.add(row)
        await session.flush()
        memory_id = int(row.id)
        await session.commit()
    return memory_id


async def count_rows(session_factory: async_sessionmaker[AsyncSession], scope: MemoryScope | None = None) -> int:
    async with session_factory() as session:
        stmt = select(func.count(Memory.id))
        if scope is not None:
            stmt = stmt.where(Memory.scope == scope)
        return int((await session.execute(stmt)).scalar_one() or 0)


async def get_row(session_factory: async_sessionmaker[AsyncSession], memory_id: int) -> Memory | None:
    async with session_factory() as session:
        return await session.get(Memory, int(memory_id))


# ---------------------------------------------------------------------------
# 1) 时间衰减：w = exp(-λ·Δt)
# ---------------------------------------------------------------------------
def test_decay_weight_identity_at_zero_age() -> None:
    assert decay_weight(0) == 1.0
    assert decay_weight(0, lam=0.05) == 1.0


def test_decay_weight_matches_half_life_formula() -> None:
    half_life = math.log(2) / 0.01
    assert decay_weight(half_life, lam=0.01, min_weight=1e-9) == pytest.approx(0.5, abs=1e-4)
    # 单调递减
    samples = [decay_weight(day, lam=0.01, min_weight=1e-9) for day in (0, 10, 50, 200)]
    assert samples == sorted(samples, reverse=True)


def test_decay_weight_respects_floor() -> None:
    assert decay_weight(10_000, lam=0.01, min_weight=0.05) == 0.05
    assert decay_weight(10_000, lam=0.01, min_weight=0.2) == 0.2


def test_decay_weight_lambda_zero_never_decays() -> None:
    assert decay_weight(3650, lam=0.0) == 1.0


def test_decay_weight_handles_negative_and_none_age() -> None:
    assert decay_weight(-5) == 1.0  # 未来时间按 0 处理
    assert decay_weight(None) == 1.0  # type: ignore[arg-type]


def test_age_days_from_datetime() -> None:
    now = datetime(2026, 10, 21, 12, 0, 0)
    assert age_days(None, now=now) == 0.0
    assert age_days(now - timedelta(days=10), now=now) == pytest.approx(10.0, abs=1e-6)
    assert age_days(now + timedelta(days=3), now=now) == 0.0  # 时钟漂移不产生负年龄


def test_effective_score_applies_decay_and_importance_boost() -> None:
    fresh = effective_score(0.8, age_in_days=0, importance=3)
    aged = effective_score(0.8, age_in_days=100, importance=3)
    important = effective_score(0.8, age_in_days=100, importance=5)
    assert fresh == pytest.approx(0.8, abs=1e-4)
    assert aged < fresh
    assert important > aged  # 同年龄下重要度更高 → 分数略高
    assert important <= 1.0


def test_effective_score_clamped_to_one() -> None:
    assert effective_score(1.0, age_in_days=0, importance=5, boost=1.0) == 1.0
    assert effective_score(0.0, age_in_days=100) == 0.0


def test_half_life_property_from_settings() -> None:
    assert ml.memory_lifecycle.half_life_days == pytest.approx(69.31, abs=0.02)
    assert ml.memory_lifecycle.long_ttl_delta == timedelta(days=180)


# ---------------------------------------------------------------------------
# 2) 语义相似度与重复判定
# ---------------------------------------------------------------------------
def test_embed_text_is_deterministic_and_normalized() -> None:
    first = embed_text(CONTENT_DIGITAL)
    second = embed_text(CONTENT_DIGITAL)
    assert first == second
    assert len(first) == ml.memory_lifecycle.MEMORY_EMBED_DIM
    norm = math.sqrt(sum(value * value for value in first))
    assert norm == pytest.approx(1.0, abs=1e-9)
    assert embed_text("") == [0.0] * len(first)


def test_cosine_similarity_edge_cases() -> None:
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)  # 维度不同按短的截断


def test_semantic_similarity_ranks_related_above_unrelated() -> None:
    identical = semantic_similarity(CONTENT_DIGITAL, CONTENT_DIGITAL)
    related = semantic_similarity(CONTENT_DIGITAL, CONTENT_DIGITAL.replace("18%", "19%"))
    unrelated = semantic_similarity(CONTENT_DIGITAL, CONTENT_BEAUTY)
    assert identical == pytest.approx(1.0, abs=1e-6)
    assert identical > related > unrelated
    assert semantic_similarity("", CONTENT_DIGITAL) == 0.0


def test_find_duplicate_returns_best_match_above_threshold() -> None:
    existing = [(1, CONTENT_DIGITAL), (2, CONTENT_BEAUTY)]
    exact = find_duplicate(CONTENT_DIGITAL, existing)
    assert exact is not None and exact.memory_id == 1
    assert exact.similarity == pytest.approx(1.0, abs=1e-6)

    loose = find_duplicate(CONTENT_DIGITAL.replace("18%", "19%"), existing, threshold=0.6)
    assert loose is None or loose.memory_id == 1

    assert find_duplicate(CONTENT_BEAUTY, [(1, CONTENT_DIGITAL)], threshold=0.99) is None
    assert find_duplicate(CONTENT_DIGITAL, []) is None


def test_embedder_can_be_injected() -> None:
    set_embedder(lambda text: [1.0, 0.0] if "数码" in text else [0.0, 1.0])
    assert semantic_similarity("数码配件", "数码配件断货") == pytest.approx(1.0)
    assert semantic_similarity("数码配件", "美妆品类") == 0.0
    set_embedder(None)
    assert ml.get_embedder() is None


# ---------------------------------------------------------------------------
# 3) 开关关闭 → 零执行逻辑
# ---------------------------------------------------------------------------
def test_install_returns_false_and_patches_nothing_when_disabled() -> None:
    before = (LayeredMemoryService.archive_long, LayeredMemoryService.recall, LayeredMemoryService.purge_expired)
    assert install_memory_lifecycle() is False
    after = (LayeredMemoryService.archive_long, LayeredMemoryService.recall, LayeredMemoryService.purge_expired)
    assert after == before
    assert status()["installed"] is False
    assert status()["original_methods_intact"] is True


async def test_switch_off_never_touches_lifecycle_logic(session_factory, memory_on, monkeypatch: pytest.MonkeyPatch, seeded_users) -> None:
    """开关关闭时：相似度计算、TTL、过期过滤一律不执行（用「一调用就炸」的探针验证）。"""
    uid = seeded_users["admin_user"]

    def _boom(*_args, **_kwargs):
        raise AssertionError("开关关闭时不应调用相似度计算")

    monkeypatch.setattr(ml, "find_duplicate", _boom)
    monkeypatch.setattr(ml, "semantic_similarity", _boom)

    service = make_service(session_factory)

    def _no_ttl(*_args, **_kwargs):
        raise AssertionError("开关关闭时不应写 TTL")

    monkeypatch.setattr(ml, "_apply_ttl", _no_ttl)

    record = await service.archive_long(task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.2)
    assert record is not None
    assert record.expires_at is None  # 原实现语义：长期记忆不过期
    assert await count_rows(session_factory, MemoryScope.LONG) == 1


def test_lifecycle_enabled_override() -> None:
    assert lifecycle_enabled(True) is True
    assert lifecycle_enabled(False) is False
    assert lifecycle_enabled() is bool(ml.memory_lifecycle.ENABLE_MEMORY_LIFECYCLE)


# ---------------------------------------------------------------------------
# 4) 重复记忆合并
# ---------------------------------------------------------------------------
async def test_duplicate_archive_merges_into_existing_row(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)

    first = await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.2)
    assert first is not None
    second = await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.6)
    assert second is not None
    assert second.id == first.id  # 合并而不是新增
    assert await count_rows(session_factory, MemoryScope.LONG) == 1

    row = await get_row(session_factory, first.id)
    assert row is not None
    assert row.hit_count == 1  # 命中 +1
    assert row.importance >= 4
    assert row.expires_at is not None  # 合并同时刷新 TTL
    assert ml.stats()["merged"] == 1


async def test_distinct_content_creates_new_rows(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)

    await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.2)
    await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=CONTENT_BEAUTY, review_score=4.5)
    assert await count_rows(session_factory, MemoryScope.LONG) == 2
    assert ml.stats()["merged"] == 0


async def test_merge_keeps_longer_content(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    detail = CONTENT_DIGITAL + " 建议优先补货并恢复日预算至 12000 元。"

    first = await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.0)
    merged = await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=detail, review_score=4.4)
    assert merged is not None and first is not None and merged.id == first.id
    row = await get_row(session_factory, first.id)
    assert row is not None and row.content == detail  # 正文取更长的那条

    shorter = await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.0)
    assert shorter is not None and shorter.id == first.id
    row = await get_row(session_factory, first.id)
    assert row is not None and row.content == detail  # 更短的重复不覆盖


async def test_dedup_respects_user_isolation(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    admin = seeded_users["admin_user"]
    viewer = seeded_users["viewer_user"]
    service = make_service(session_factory)

    await archive_long_with_lifecycle(service, task_id=None, user_id=admin, content=CONTENT_DIGITAL, review_score=4.2)
    await archive_long_with_lifecycle(service, task_id=None, user_id=viewer, content=CONTENT_DIGITAL, review_score=4.2)
    # 不同用户 → 各自一条（记忆不能跨用户合并）
    assert await count_rows(session_factory, MemoryScope.LONG) == 2


async def test_dedup_disabled_creates_duplicates(session_factory, memory_on, lifecycle_on, seeded_users, monkeypatch) -> None:
    monkeypatch.setattr(ml.memory_lifecycle, "MEMORY_DEDUP_ENABLED", False)
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    for _ in range(2):
        await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.2)
    assert await count_rows(session_factory, MemoryScope.LONG) == 2


async def test_childscores_below_threshold_does_not_merge(session_factory, memory_on, lifecycle_on, seeded_users, monkeypatch) -> None:
    monkeypatch.setattr(ml.memory_lifecycle, "MEMORY_DEDUP_THRESHOLD", 0.999)
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.2)
    await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=CONTENT_DIGITAL.replace("18%", "19%"), review_score=4.2)
    assert await count_rows(session_factory, MemoryScope.LONG) == 2


async def test_archive_below_review_score_never_writes(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    assert await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=2.0) is None
    assert await count_rows(session_factory, MemoryScope.LONG) == 0


# ---------------------------------------------------------------------------
# 5) TTL
# ---------------------------------------------------------------------------
async def test_archive_long_sets_default_ttl(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    record = await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.2)
    assert record is not None and record.expires_at is not None
    remaining = record.expires_at - datetime.now()
    assert timedelta(days=179) < remaining <= timedelta(days=180)
    assert ml.stats()["ttl_applied"] == 1


async def test_ttl_zero_means_permanent(session_factory, memory_on, lifecycle_on, seeded_users, monkeypatch) -> None:
    monkeypatch.setattr(ml.memory_lifecycle, "MEMORY_LONG_TTL_DAYS", 0)
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    record = await archive_long_with_lifecycle(service, task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.2)
    assert record is not None and record.expires_at is None
    assert ml.memory_lifecycle.long_ttl_delta is None


async def test_purge_removes_expired_mid_and_long(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)

    await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=200, expires_in_days=-1)
    await add_long(session_factory, uid, CONTENT_BEAUTY, days_old=1, expires_in_days=30)
    async with session_factory() as session:
        session.add(
            Memory(
                scope=MemoryScope.MID,
                user_id=uid,
                task_id=None,
                content="过期摘要",
                importance=2,
                hit_count=0,
                created_at=datetime.now() - timedelta(days=40),
                expires_at=datetime.now() - timedelta(days=10),
            )
        )
        await session.commit()
    assert await count_rows(session_factory) == 3

    assert await purge_with_ttl(service) == 2  # 一条过期 long + 一条过期 mid
    assert await count_rows(session_factory) == 1
    details = await purge_expired_details(service)
    assert details == {"mid": 0, "long": 0}  # 已无过期项


async def test_purge_details_breakdown(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=300, expires_in_days=-2)
    async with session_factory() as session:
        session.add(
            Memory(
                scope=MemoryScope.MID,
                user_id=uid,
                task_id=None,
                content="过期摘要",
                importance=2,
                hit_count=0,
                created_at=datetime.now() - timedelta(days=40),
                expires_at=datetime.now() - timedelta(days=5),
            )
        )
        await session.commit()
    details = await purge_expired_details(service)
    assert details == {"mid": 1, "long": 1}


async def test_purge_returns_zero_when_memory_disabled(session_factory) -> None:
    service = make_service(session_factory, enabled=False)
    assert await purge_with_ttl(service) == 0
    assert await purge_expired_details(service) == {"mid": 0, "long": 0}


async def test_purge_falls_back_to_original_when_cleanup_disabled(
    session_factory, memory_on, lifecycle_on, seeded_users, monkeypatch
) -> None:
    monkeypatch.setattr(ml.memory_lifecycle, "MEMORY_TTL_CLEANUP_ENABLED", False)
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=300, expires_in_days=-2)
    # 原实现只清 mid，long 保持不动
    assert await purge_with_ttl(service) == 0
    assert await count_rows(session_factory, MemoryScope.LONG) == 1


# ---------------------------------------------------------------------------
# 6) 召回：衰减重排 + 过期过滤
# ---------------------------------------------------------------------------
async def test_recall_decay_reorders_identical_content_by_age(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    # 正文完全一致 → 原始分相同，唯一差别是年龄
    old_id = await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=100)
    new_id = await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=1)

    hits = await recall_with_decay(service, GOAL, user_id=uid, top_k=3)
    assert [hit.memory_id for hit in hits] == [new_id, old_id]
    assert hits[0].score > hits[1].score
    assert hits[1].score == pytest.approx(hits[0].score * math.exp(-0.01 * 99), rel=0.02)
    assert ml.stats()["decayed"] == 2


async def test_recall_filters_expired_entries(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=2, expires_in_days=-1)  # 已过期
    fresh_id = await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=2)

    hits = await recall_with_decay(service, GOAL, user_id=uid)
    assert [hit.memory_id for hit in hits] == [fresh_id]
    assert ml.stats()["expired_filtered"] == 1


async def test_recall_without_decay_keeps_raw_scores(session_factory, memory_on, lifecycle_on, seeded_users, monkeypatch) -> None:
    monkeypatch.setattr(ml.memory_lifecycle, "MEMORY_DECAY_ENABLED", False)
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=100)
    await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=1)

    hits = await recall_with_decay(service, GOAL, user_id=uid)
    assert len(hits) == 2
    assert hits[0].score == hits[1].score  # 不衰减 → 同文同分
    assert ml.stats()["decayed"] == 0


async def test_recall_decay_disabled_still_filters_expired(session_factory, memory_on, lifecycle_on, seeded_users, monkeypatch) -> None:
    monkeypatch.setattr(ml.memory_lifecycle, "MEMORY_DECAY_ENABLED", False)
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=2, expires_in_days=-1)
    hits = await recall_with_decay(service, GOAL, user_id=uid)
    assert hits == []


async def test_recall_refilter_drops_very_old_low_score(session_factory, memory_on, lifecycle_on, seeded_users, monkeypatch) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    stale_id = await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=3000)
    await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=1)

    kept = await recall_with_decay(service, GOAL, user_id=uid)
    assert stale_id in [hit.memory_id for hit in kept]  # 默认只降权，不过滤

    monkeypatch.setattr(ml.memory_lifecycle, "MEMORY_DECAY_REFILTER", True)
    monkeypatch.setattr(w5, "MEMORY_RECALL_MIN_SCORE", 0.6)
    filtered = await recall_with_decay(service, GOAL, user_id=uid)
    assert stale_id not in [hit.memory_id for hit in filtered]


async def test_recall_empty_when_no_memories(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    service = make_service(session_factory)
    assert await recall_with_decay(service, GOAL, user_id=seeded_users["admin_user"]) == []
    assert ml.stats()["recall_calls"] == 1


async def test_recall_returns_memory_hit_instances(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    await add_long(session_factory, uid, CONTENT_DIGITAL, days_old=3)
    hits = await recall_with_decay(service, GOAL, user_id=uid)
    assert hits and isinstance(hits[0], MemoryHit)
    assert 0.0 <= hits[0].score <= 1.0
    assert hits[0].as_prompt().startswith("[记忆#")


# ---------------------------------------------------------------------------
# 7) 运行期包装：既有代码零改动生效
# ---------------------------------------------------------------------------
async def test_install_patches_and_plain_service_gains_dedup(session_factory, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)  # 注意：普通 LayeredMemoryService，不是子类
    assert install_memory_lifecycle() is True
    assert status()["installed"] is True
    assert status()["original_methods_intact"] is False
    assert set(status()["patched_methods"]) == {"archive_long", "recall", "purge_expired", "create_memory"}

    first = await service.archive_long(task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.2)
    second = await service.archive_long(task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.6)
    assert first is not None and second is not None
    assert first.id == second.id
    assert await count_rows(session_factory, MemoryScope.LONG) == 1

    uninstall_memory_lifecycle()
    third = await service.archive_long(task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.6)
    assert third is not None and third.id != first.id  # 卸载后回到原实现
    assert await count_rows(session_factory, MemoryScope.LONG) == 2


async def test_patch_honours_runtime_switch_toggle(session_factory, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    install_memory_lifecycle()

    ml.memory_lifecycle.ENABLE_MEMORY_LIFECYCLE = False  # 运行期关闭（模拟热切换）
    try:
        record = await service.archive_long(task_id=None, user_id=uid, content=CONTENT_DIGITAL, review_score=4.2)
        assert record is not None and record.expires_at is None  # 无 TTL = 原实现
        assert ml.stats()["archive_calls"] == 0  # 生命周期实现根本没被调用
    finally:
        ml.memory_lifecycle.ENABLE_MEMORY_LIFECYCLE = True


def test_install_is_idempotent(lifecycle_on) -> None:
    assert install_memory_lifecycle() is True
    assert install_memory_lifecycle() is True
    uninstall_memory_lifecycle()
    uninstall_memory_lifecycle()  # 幂等
    assert status()["installed"] is False


# ---------------------------------------------------------------------------
# 8) 手工新增（管理 API 路径）
# ---------------------------------------------------------------------------
async def test_create_memory_dedupes_long_scope(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    first = await create_memory_with_lifecycle(service, user_id=uid, content=CONTENT_DIGITAL, importance=4)
    second = await create_memory_with_lifecycle(service, user_id=uid, content=CONTENT_DIGITAL, importance=5)
    assert first.id == second.id
    assert await count_rows(session_factory, MemoryScope.LONG) == 1
    row = await get_row(session_factory, first.id)
    assert row is not None and row.importance == 5 and row.expires_at is not None


async def test_create_memory_mid_scope_untouched(session_factory, memory_on, lifecycle_on, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    service = make_service(session_factory)
    await create_memory_with_lifecycle(service, user_id=uid, scope=MemoryScope.MID, content=CONTENT_DIGITAL)
    await create_memory_with_lifecycle(service, user_id=uid, scope=MemoryScope.MID, content=CONTENT_DIGITAL)
    assert await count_rows(session_factory, MemoryScope.MID) == 2  # 中期是「每任务摘要」，不去重


# ---------------------------------------------------------------------------
# 9) 对外接口不变 & 工厂
# ---------------------------------------------------------------------------
def test_lifecycle_service_is_subclass_with_same_public_interface() -> None:
    assert issubclass(LifecycleMemoryService, LayeredMemoryService)

    def public_api(cls: type) -> set[str]:
        return {name for name in dir(cls) if not name.startswith("_")}

    assert public_api(LifecycleMemoryService) >= public_api(LayeredMemoryService)
    for name in ("archive_long", "recall", "purge_expired", "create_memory", "augment_state", "finalize_task"):
        child = inspect.signature(getattr(LifecycleMemoryService, name))
        parent = inspect.signature(getattr(LayeredMemoryService, name))
        assert list(child.parameters) == list(parent.parameters)
    assert inspect.signature(LifecycleMemoryService.recall).return_annotation is not None


def test_build_lifecycle_memory_factory(session_factory, lifecycle_on) -> None:
    assert isinstance(build_lifecycle_memory(session_factory=session_factory), LifecycleMemoryService)
    ml.memory_lifecycle.ENABLE_MEMORY_LIFECYCLE = False
    assert type(build_lifecycle_memory(session_factory=session_factory)) is LayeredMemoryService


def test_status_exposes_policy_and_stats(lifecycle_on) -> None:
    info = status()
    assert info["enabled"] is True
    assert info["policy"]["dedup"]["threshold"] == ml.memory_lifecycle.MEMORY_DEDUP_THRESHOLD
    assert info["policy"]["ttl"]["long_ttl_days"] == 180
    assert set(info["stats"]) == {
        "archive_calls",
        "merged",
        "ttl_applied",
        "recall_calls",
        "decayed",
        "expired_filtered",
        "purged",
    }
    assert info["policy"]["decay"]["half_life_days"] == pytest.approx(69.31, abs=0.02)


def test_settings_describe_contains_all_three_capabilities() -> None:
    info = ml.memory_lifecycle.describe()
    assert {"dedup", "decay", "ttl"} <= set(info)
    assert info["ttl"]["cleanup_enabled"] is True
