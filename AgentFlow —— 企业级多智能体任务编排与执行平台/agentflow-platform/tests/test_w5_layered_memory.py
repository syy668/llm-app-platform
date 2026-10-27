"""W5-Part1 任务 2 单元测试：FR-014 分层记忆。

**新增用例**，不改动任何 W2/W3 既有测试。

覆盖点（对应任务书「新增 pytest 用例：记忆写入、相似度召回、开关关闭时无记忆注入；
mock 模式下兜底逻辑验证」）：
1. 短期（Redis / TTL 24h / 原文不摘要）、中期（复用 W2 压缩摘要 / 30 天）、长期（评分 ≥3 才归档）；
2. 相似度召回排序、注入片段截断与条数上限；
3. 开关关闭 → 不写、不查、不注入（零副作用）；
4. mock 模式本地关键词兜底；向量库故障降级；
5. 图入口包装：注入 ``memory_hits`` + 终态归档，且 ``aget_state`` 等属性透传；
6. 记忆管理 API（鉴权 / 分页 / 召回 / 新增 / 删除 / 未启用 503）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.prompts import build_planner_messages
from app.core.w5_config import w5
from app.memory.layered_memory import (
    HttpRagVectorStore,
    LayeredMemoryService,
    LocalKeywordVectorStore,
    MemoryAwareGraph,
    MemoryStoreError,
    build_memory_store,
    keyword_score,
    rank_candidates,
    review_score_of,
)
from app.schemas.agent_io import PlannerInput
from app.schemas.enums import MemoryScope
from app.schemas.graph_state import TimeRange
from app.schemas.orm_models import Memory
from app.services.context_service import ContextService

PASSWORD = "Passw0rd!"
LOGIN_URL = "/api/v1/auth/login"

GOAL_DIGITAL = "分析上周数码配件 GMV 下滑原因"
GOAL_BEAUTY = "评估美妆品类投放预算是否需要继续上调"

LONG_DIGITAL = (
    "数码配件品类 W42 GMV 环比下滑 18%，转化率由 3.1% 降至 1.7%；"
    "根因是爆款 SKU-10086 断货导致自然流量结构恶化，建议优先补货并恢复日预算至 12000 元。"
)
LONG_BEAUTY = "美妆品类 W42 投放预算上调后 ROI 1.8，低于大盘 2.4，建议回收 20% 预算到数码配件。"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture
def memory_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w5, "ENABLE_LAYERED_MEMORY", True)


def make_service(
    session_factory: async_sessionmaker[AsyncSession],
    redis,
    *,
    enabled: bool = True,
    store=None,
    fallback_store=None,
) -> LayeredMemoryService:
    return LayeredMemoryService(
        session_factory=session_factory,
        redis=redis,
        context=ContextService(),
        enabled=enabled,
        store=store,
        fallback_store=fallback_store,
    )


async def _count_memories(session_factory: async_sessionmaker[AsyncSession], scope: MemoryScope | None = None) -> int:
    async with session_factory() as session:
        stmt = select(func.count(Memory.id))
        if scope is not None:
            stmt = stmt.where(Memory.scope == scope)
        return int((await session.execute(stmt)).scalar_one() or 0)


@pytest.fixture
def service(session_factory, fake_redis) -> LayeredMemoryService:
    return make_service(session_factory, fake_redis)


# ---------------------------------------------------------------------------
# 1) 短期记忆：Redis 原文，TTL 24h
# ---------------------------------------------------------------------------
async def test_short_term_memory_roundtrip_and_ttl(service: LayeredMemoryService, fake_redis) -> None:
    await service.append_short(101, {"role": "user", "content": GOAL_DIGITAL}, user_id=1)
    length = await service.append_short(101, {"role": "assistant", "content": "规划完成：4 个子任务"}, user_id=1)
    assert length == 2

    messages = await service.load_short(101)
    assert [m["content"] for m in messages] == [GOAL_DIGITAL, "规划完成：4 个子任务"]
    assert all("ts" in m for m in messages)  # 时间戳用于轨迹回放

    # 原文不摘要：长文本逐字保留
    long_text = "证据" * 500
    await service.append_short(101, {"role": "tool", "content": long_text})
    loaded = await service.load_short(101)
    assert loaded[-1]["content"] == long_text

    ttl = await service.short_ttl(101)
    assert 86000 < ttl <= w5.MEMORY_SHORT_TTL_SECONDS
    assert service.short_key(101) == "agentflow:memory:short:101"

    assert await service.clear_short(101) is True
    assert await service.load_short(101) == []


async def test_short_term_switch_off_touches_nothing(session_factory, fake_redis) -> None:
    service = make_service(session_factory, fake_redis, enabled=False)
    assert await service.append_short(202, {"role": "user", "content": "x"}) == 0
    assert await service.load_short(202) == []
    assert await fake_redis.keys("*") == []  # 连 key 都没建


async def test_short_term_trims_to_max_messages(session_factory, fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(w5, "MEMORY_SHORT_MAX_MESSAGES", 3)
    service = make_service(session_factory, fake_redis)
    for index in range(5):
        await service.append_short(303, {"role": "user", "content": f"m{index}"})
    messages = await service.load_short(303)
    assert [m["content"] for m in messages] == ["m2", "m3", "m4"]


# ---------------------------------------------------------------------------
# 2) 中期记忆：复用 W2 压缩摘要 + 30 天 TTL
# ---------------------------------------------------------------------------
def _long_history(rounds: int = 10) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for index in range(rounds):
        history.append({"role": "user", "content": f"第{index}轮：请查询数码配件 GMV 环比变化，关注转化率"})
        history.append(
            {
                "role": "assistant",
                "content": (
                    f"第{index}轮结论：数码配件 GMV 环比 -18%，转化率 3.1%→1.7%，"
                    "SKU-10086 断货，库存周转 9 天升至 21 天，预算 12000 元降至 7200 元。"
                ),
            }
        )
    return history


def test_summarize_reuses_context_compression(service: LayeredMemoryService) -> None:
    history = _long_history(10)
    digest = service.summarize(history, goal=GOAL_DIGITAL)
    raw_len = sum(len(item["content"]) for item in history)
    assert digest
    assert len(digest) < raw_len  # 确实压缩了
    assert "数码配件" in digest or "GMV" in digest  # 关键实体保留


async def test_mid_memory_written_with_ttl(service: LayeredMemoryService, session_factory, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    record = await service.write_mid(
        task_id=None, user_id=uid, goal=GOAL_DIGITAL, messages=_long_history(6)
    )
    assert record is not None and record.scope == MemoryScope.MID.value
    assert record.expires_at is not None
    remaining = record.expires_at - datetime.now()
    assert timedelta(days=29) < remaining <= timedelta(days=30)

    async with session_factory() as session:
        row = await session.get(Memory, record.id)
        assert row is not None
        assert row.scope == MemoryScope.MID
        assert row.user_id == uid
        assert row.chroma_doc_id is None  # 中期不入向量库
        assert "数码配件" in row.content


# ---------------------------------------------------------------------------
# 3) 长期记忆：Reviewer >= 3 分才归档
# ---------------------------------------------------------------------------
async def test_long_memory_requires_review_score(
    service: LayeredMemoryService, session_factory, seeded_users
) -> None:
    uid = seeded_users["admin_user"]

    skipped = await service.archive_long(
        task_id=None, user_id=uid, content=LONG_DIGITAL, review_score=2.9
    )
    assert skipped is None
    assert await _count_memories(session_factory, MemoryScope.LONG) == 0

    record = await service.archive_long(
        task_id=None, user_id=uid, content=LONG_DIGITAL, review_score=4.2
    )
    assert record is not None
    assert record.scope == MemoryScope.LONG.value
    assert record.importance == 4  # 4.2 → round 4
    assert record.chroma_doc_id == f"agentflow-mem-{record.id}"
    assert record.expires_at is None  # 长期不过期
    assert await _count_memories(session_factory, MemoryScope.LONG) == 1


async def test_long_memory_importance_floor_is_three(
    service: LayeredMemoryService, seeded_users
) -> None:
    uid = seeded_users["admin_user"]
    record = await service.archive_long(task_id=None, user_id=uid, content=LONG_BEAUTY, review_score=3.0)
    assert record is not None and record.importance == 3


# ---------------------------------------------------------------------------
# 4) 相似度召回
# ---------------------------------------------------------------------------
def test_keyword_score_prefers_topical_overlap() -> None:
    assert keyword_score(GOAL_DIGITAL, LONG_DIGITAL) > keyword_score(GOAL_DIGITAL, LONG_BEAUTY)
    assert keyword_score(GOAL_DIGITAL, LONG_DIGITAL) > 0.15
    assert keyword_score("", LONG_DIGITAL) == 0.0


def test_rank_candidates_sorted_and_limited() -> None:
    ranked = rank_candidates(
        GOAL_DIGITAL,
        [("a", LONG_BEAUTY), ("b", LONG_DIGITAL)],
        top_k=1,
    )
    assert [doc_id for doc_id, _ in ranked] == ["b"]


async def test_recall_returns_most_similar_first(
    service: LayeredMemoryService, seeded_users
) -> None:
    uid = seeded_users["admin_user"]
    await service.archive_long(task_id=None, user_id=uid, content=LONG_BEAUTY, review_score=3.6)
    await service.archive_long(task_id=None, user_id=uid, content=LONG_DIGITAL, review_score=4.6)

    hits = await service.recall(GOAL_DIGITAL, user_id=uid)
    assert hits
    assert "SKU-10086" in hits[0].content
    assert hits[0].score > 0.15
    assert len(hits) <= w5.MEMORY_LONG_TOP_K


async def test_recall_isolated_by_user(service: LayeredMemoryService, seeded_users) -> None:
    owner, other = seeded_users["admin_user"], seeded_users["viewer_user"]
    await service.archive_long(task_id=None, user_id=owner, content=LONG_DIGITAL, review_score=4.5)
    assert await service.recall(GOAL_DIGITAL, user_id=other) == []


async def test_recall_switch_off_returns_empty(session_factory, fake_redis, seeded_users) -> None:
    enabled = make_service(session_factory, fake_redis, enabled=True)
    uid = seeded_users["admin_user"]
    await enabled.archive_long(task_id=None, user_id=uid, content=LONG_DIGITAL, review_score=4.5)

    disabled = make_service(session_factory, fake_redis, enabled=False)
    assert await disabled.recall(GOAL_DIGITAL, user_id=uid) == []
    assert await disabled.build_injection(GOAL_DIGITAL, user_id=uid) == []


# ---------------------------------------------------------------------------
# 5) mock 模式 / 向量库故障兜底
# ---------------------------------------------------------------------------
def test_build_store_follows_mock_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(build_memory_store(mock=True), LocalKeywordVectorStore)
    # 非 mock + 未配 embedding key → 回退 http-rag（项目一路径，当前 404 会降级）
    monkeypatch.setattr(w5, "MEMORY_EMBEDDING_API_KEY", "")
    assert isinstance(build_memory_store(mock=False), HttpRagVectorStore)
    # 非 mock + 配了 key + 默认后端 local-embedding → 真语义路径
    monkeypatch.setattr(w5, "MEMORY_EMBEDDING_API_KEY", "sk-test")
    from app.memory.local_embedding import LocalEmbeddingVectorStore

    assert isinstance(build_memory_store(mock=False), LocalEmbeddingVectorStore)


async def test_local_store_search_ranks_without_index() -> None:
    """进程刚重启（索引为空）时，仅靠候选集也能给出排序。"""
    store = LocalKeywordVectorStore()
    pairs = await store.search(
        GOAL_DIGITAL, top_k=1, candidates=[("x", LONG_BEAUTY), ("y", LONG_DIGITAL)]
    )
    assert pairs and pairs[0][0] == "y"

    doc_id = await store.upsert(memory_id=9, content=LONG_DIGITAL, metadata={"doc_id": "agentflow-mem-9"})
    assert doc_id == "agentflow-mem-9"
    assert store.stats()["indexed"] == 1


class BoomStore:
    """模拟项目一 RAG 全挂：入库与检索都抛错。"""

    name = "boom"

    async def upsert(self, *, memory_id: int, content: str, metadata: dict) -> str:
        raise MemoryStoreError("rag down")

    async def search(self, query: str, *, top_k: int, candidates=None):  # noqa: ANN001
        raise MemoryStoreError("rag down")


async def test_recall_degrades_to_local_when_vector_store_fails(
    session_factory, fake_redis, seeded_users
) -> None:
    fallback = LocalKeywordVectorStore()
    service = make_service(session_factory, fake_redis, store=BoomStore(), fallback_store=fallback)
    uid = seeded_users["admin_user"]

    record = await service.archive_long(task_id=None, user_id=uid, content=LONG_DIGITAL, review_score=4.4)
    assert record is not None and record.chroma_doc_id  # 入库降级成功，doc_id 来自本地兜底

    hits = await service.recall(GOAL_DIGITAL, user_id=uid)
    assert hits and "SKU-10086" in hits[0].content  # 检索也降级成功
    # 检索通道自省：向量通道故障 → 必须如实标注 degraded + local-keyword
    assert service.last_retrieval is not None
    assert service.last_retrieval["store"] == "boom"
    assert service.last_retrieval["scorer"] == "local-keyword"
    assert service.last_retrieval["degraded"] is True


# ---------------------------------------------------------------------------
# 6) 注入：条数上限 / 截断 / 不改入参
# ---------------------------------------------------------------------------
async def test_injection_limited_and_truncated(service: LayeredMemoryService, seeded_users, monkeypatch) -> None:
    monkeypatch.setattr(w5, "MEMORY_INJECT_MAX_CHARS", 60)
    uid = seeded_users["admin_user"]
    for index in range(5):
        await service.archive_long(
            task_id=None,
            user_id=uid,
            content=f"{GOAL_DIGITAL} 第{index}次结论：" + LONG_DIGITAL,
            review_score=4.5,
        )

    injected = await service.build_injection(GOAL_DIGITAL, user_id=uid)
    assert 0 < len(injected) <= 3
    for item in injected:
        assert item.startswith("[记忆#")
        assert "…" in item  # 超长被截断
        assert len(item) <= 60 + 20


async def test_augment_state_does_not_mutate_input(service: LayeredMemoryService, seeded_users) -> None:
    uid = seeded_users["admin_user"]
    await service.archive_long(task_id=None, user_id=uid, content=LONG_DIGITAL, review_score=4.5)

    state = {"goal": GOAL_DIGITAL, "creator_id": uid, "memory_hits": []}
    merged = await service.augment_state(state)
    assert merged is not state
    assert state["memory_hits"] == []  # 原 dict 未被改写
    assert merged["memory_hits"] and "SKU-10086" in merged["memory_hits"][0]


async def test_augment_state_switch_off_returns_same_object(session_factory, fake_redis) -> None:
    service = make_service(session_factory, fake_redis, enabled=False)
    state = {"goal": GOAL_DIGITAL, "creator_id": 1, "memory_hits": []}
    assert await service.augment_state(state) is state


# ---------------------------------------------------------------------------
# 7) 图入口包装
# ---------------------------------------------------------------------------
class FakeGraph:
    """记录入参的最小图替身（不跑 LangGraph，只验证包装层行为）。"""

    def __init__(self, result: dict) -> None:
        self.result = result
        self.seen: dict | None = None

    async def ainvoke(self, input: dict, config=None, **kwargs):  # noqa: ANN001
        self.seen = input
        return self.result

    async def aget_state(self, config):  # noqa: ANN001
        return "snapshot"

    async def aupdate_state(self, config, values):  # noqa: ANN001
        return {"updated": values}

    def pending_interrupts(self, config):  # noqa: ANN001
        return ["interrupt-1"]


async def test_memory_aware_graph_injects_and_finalizes(
    service: LayeredMemoryService, session_factory, seeded_users
) -> None:
    uid = seeded_users["admin_user"]
    await service.archive_long(task_id=None, user_id=uid, content=LONG_DIGITAL, review_score=4.5)

    final_state = {
        "task_id": 7001,
        "creator_id": uid,
        "goal": GOAL_DIGITAL,
        "status": "COMPLETED",
        "review": {"total": 4.3},
        "context_digest": "结论：数码配件 GMV 环比 -18%，主因 SKU-10086 断货。",
        "messages": [{"role": "assistant", "content": "分析完成"}],
        "memory_hits": [],
    }
    graph = FakeGraph(final_state)
    wrapped = MemoryAwareGraph(graph, service, user_id=uid)

    result = await wrapped.ainvoke({"goal": GOAL_DIGITAL, "creator_id": uid, "memory_hits": []})
    assert result is final_state
    assert graph.seen is not None and graph.seen["memory_hits"]  # 注入成功
    assert "SKU-10086" in graph.seen["memory_hits"][0]

    # 透传：W3 挂起判断 / 状态读取仍走内部图
    assert await wrapped.aget_state({}) == "snapshot"
    assert wrapped.pending_interrupts({}) == ["interrupt-1"]
    assert await wrapped.aupdate_state({}, {"x": 1}) == {"updated": {"x": 1}}

    # 终态归档：mid + long 各一条
    assert await _count_memories(session_factory, MemoryScope.MID) == 1
    assert await _count_memories(session_factory, MemoryScope.LONG) == 2  # 前置 1 条 + 归档 1 条


async def test_memory_aware_graph_skips_when_disabled(
    session_factory, fake_redis, seeded_users
) -> None:
    service = make_service(session_factory, fake_redis, enabled=False)
    final_state = {
        "task_id": 7002,
        "creator_id": seeded_users["admin_user"],
        "goal": GOAL_DIGITAL,
        "status": "COMPLETED",
        "review": {"total": 4.9},
    }
    graph = FakeGraph(final_state)
    wrapped = MemoryAwareGraph(graph, service, user_id=seeded_users["admin_user"])

    await wrapped.ainvoke({"goal": GOAL_DIGITAL, "memory_hits": []})
    assert graph.seen == {"goal": GOAL_DIGITAL, "memory_hits": []}  # 原样透传
    assert await _count_memories(session_factory) == 0


async def test_memory_aware_graph_ignores_non_completed(service: LayeredMemoryService, session_factory) -> None:
    graph = FakeGraph({"task_id": 7003, "status": "FAILED", "review": {"total": 4.9}})
    wrapped = MemoryAwareGraph(graph, service)
    await wrapped.ainvoke({"goal": GOAL_DIGITAL, "memory_hits": []})
    assert await _count_memories(session_factory) == 0


def test_review_score_of_variants() -> None:
    assert review_score_of({"review": {"total": 4.2}}) == 4.2
    assert review_score_of({"review": {"score": 3.6}}) == 3.6
    assert review_score_of({"review": {"scores": {"accuracy": 4.0, "depth": 3.0}}}) == 3.5
    assert review_score_of({}) == 0.0
    assert review_score_of({"review": None}) == 0.0


def test_planner_prompt_renders_memory_block() -> None:
    """注入点就在既有提示词模板里：memory_hits 作为独立片段渲染（W2 模板未改）。"""
    payload = PlannerInput(
        goal=GOAL_DIGITAL,
        time_range=TimeRange(start=date(2026, 10, 13), end=date(2026, 10, 19)),
        memory_hits=["[记忆#12·重要度4] 数码配件 SKU-10086 断货，转化率 3.1%→1.7%"],
    )
    messages = build_planner_messages(payload)
    user_text = messages[1]["content"]
    assert "长期记忆（参考，可裁剪）" in user_text
    assert "SKU-10086" in user_text


async def test_real_graph_receives_memory_hits(
    w2_graph, session_factory, fake_redis, seeded_users
) -> None:
    """真图（7 节点）验证：开关开启时记忆确实进入 state 并抬高 Planner 输入 token。"""
    uid = seeded_users["admin_user"]
    service = make_service(session_factory, fake_redis)
    await service.archive_long(task_id=None, user_id=uid, content=LONG_DIGITAL, review_score=4.5)

    def initial_state() -> dict:
        return {
            "task_id": 8801,
            "trace_id": "trace-w5-memory",
            "creator_id": uid,
            "goal": GOAL_DIGITAL,
            "time_range": {"start": "2026-10-12", "end": "2026-10-18"},
            "messages": [],
            "token_ledger": {},
            "subtask_results": {},
            "artifacts": [],
            "status": "PENDING",
        }

    baseline = await w2_graph.ainvoke(initial_state(), {"configurable": {"thread_id": "w5-base"}})
    enriched = await MemoryAwareGraph(w2_graph, service, user_id=uid, finalize=False).ainvoke(
        initial_state(), {"configurable": {"thread_id": "w5-memory"}}
    )

    assert not baseline.get("memory_hits")  # 对照组无注入
    assert enriched["memory_hits"] and "SKU-10086" in enriched["memory_hits"][0]
    assert enriched["token_ledger"]["planner"]["input"] > baseline["token_ledger"]["planner"]["input"]


# ---------------------------------------------------------------------------
# 8) 管理 API
# ---------------------------------------------------------------------------
async def _login(client: AsyncClient, username: str = "admin_user") -> str:
    resp = await client.post(LOGIN_URL, json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_memory_api_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/memories")
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_MISSING"


async def test_memory_api_meta_reports_layers(client: AsyncClient, memory_on) -> None:
    token = await _login(client)
    resp = await client.get("/api/v1/memories/meta", headers=_headers(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["enabled"] is True
    assert data["layers"]["short"]["ttl_seconds"] == w5.MEMORY_SHORT_TTL_SECONDS
    assert data["layers"]["mid"]["ttl_days"] == w5.MEMORY_MID_TTL_DAYS
    assert data["layers"]["long"]["min_review_score"] == w5.MEMORY_LONG_MIN_REVIEW_SCORE


async def test_memory_api_crud_and_recall(client: AsyncClient, memory_on) -> None:
    token = await _login(client)
    headers = _headers(token)

    created = await client.post(
        "/api/v1/memories",
        json={"scope": "long", "content": LONG_DIGITAL, "importance": 4},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    memory_id = created.json()["data"]["id"]
    assert created.json()["data"]["chroma_doc_id"]

    listed = await client.get("/api/v1/memories", params={"scope": "long"}, headers=headers)
    assert listed.status_code == 200
    body = listed.json()["data"]
    assert body["enabled"] is True
    assert body["meta"]["total"] == 1
    assert body["items"][0]["id"] == memory_id

    recall = await client.post(
        "/api/v1/memories/recall", json={"goal": GOAL_DIGITAL, "top_k": 3}, headers=headers
    )
    assert recall.status_code == 200
    hits = recall.json()["data"]["hits"]
    assert hits and hits[0]["memory_id"] == memory_id
    assert recall.json()["data"]["injection"][0].startswith("[记忆#")
    # 检索通道自省必须随响应返回（mock 模式：本地关键词、非降级）
    retrieval = recall.json()["data"]["retrieval"]
    assert retrieval["store"] == "local-keyword"
    assert retrieval["scorer"] == "local-keyword"
    assert retrieval["degraded"] is False

    detail = await client.get(f"/api/v1/memories/{memory_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["hit_count"] >= 1  # 召回累加命中数

    deleted = await client.delete(f"/api/v1/memories/{memory_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True

    missing = await client.get(f"/api/v1/memories/{memory_id}", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"


async def test_memory_api_viewer_cannot_write(client: AsyncClient, memory_on) -> None:
    token = await _login(client, "viewer_user")
    resp = await client.post(
        "/api/v1/memories", json={"scope": "long", "content": "x"}, headers=_headers(token)
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_memory_api_create_rejected_when_disabled(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(w5, "ENABLE_LAYERED_MEMORY", False)
    token = await _login(client)
    resp = await client.post(
        "/api/v1/memories", json={"scope": "long", "content": "x"}, headers=_headers(token)
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "SERVICE_UNAVAILABLE"

    listed = await client.get("/api/v1/memories", headers=_headers(token))
    assert listed.status_code == 200
    assert listed.json()["data"] == {
        "items": [],
        "meta": {"page": 1, "size": 20, "total": 0},
        "enabled": False,
    }


async def test_memory_api_rejects_short_scope(client: AsyncClient, memory_on) -> None:
    token = await _login(client)
    resp = await client.post(
        "/api/v1/memories", json={"scope": "short", "content": "x"}, headers=_headers(token)
    )
    assert resp.status_code == 422


async def test_service_describe_and_disabled_write(session_factory, fake_redis) -> None:
    from app.memory.layered_memory import MemoryDisabledError

    service = make_service(session_factory, fake_redis, enabled=False)
    assert service.describe()["enabled"] is False
    with pytest.raises(MemoryDisabledError):
        await service.create_memory(user_id=1, content="x")
    with pytest.raises(MemoryDisabledError):
        await service.delete_memory(1)
