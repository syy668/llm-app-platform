"""加固任务 3：用户级日配额 + LLM 令牌桶 的单元测试。

覆盖四条防线：
1. 令牌桶：突发容量内放行、超容量拒绝、随时间补给恢复（注入假时钟）；
2. 日请求数配额：超限 429 语义（allowed=False）；
3. 日 Token 预算：记账累计 + 进站预检拦截；
4. 中间件集成：LLM 类请求日配额先于短时频率生效；Redis 路径双实例共享。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import rate_limit as rl_mod
from app.core.daily_quota import DailyQuotaService, record_task_tokens
from app.core.rate_config import rate_limit as rl
from app.core.rate_limit import RateLimiter, RateLimitMiddleware


# ---------------------------------------------------------------------------
# 1. 令牌桶（llm 类）
# ---------------------------------------------------------------------------
class _FakeClock:
    """可手动推进的时钟（测令牌补给）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_llm_token_bucket_allows_burst_then_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """容量=3：允许 3 连发（轻微突发），第 4 发立即拒绝。"""
    monkeypatch.setattr(rl, "RATE_LIMIT_LLM_BUCKET_ENABLED", True)
    monkeypatch.setattr(rl, "RATE_LIMIT_LLM_BUCKET_CAPACITY", 3)

    limiter = RateLimiter(redis=None, limits={"llm": 6})
    results = [await limiter.hit("llm", user_id=1) for _ in range(4)]

    assert [d.allowed for d in results] == [True, True, True, False]
    assert results[3].used == 3 and results[3].remaining == 0
    assert results[3].reset_in >= 1  # Retry-After 指向下一枚令牌的补给时间
    # 令牌桶只影响 llm：api/kb 仍是固定窗口
    api = await limiter.hit("api", user_id=1)
    assert api.limit == rl.RATE_LIMIT_API_PER_MINUTE


@pytest.mark.asyncio
async def test_llm_token_bucket_refills_over_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """拒绝后随时间补给：补给速率 = 配额/窗口，攒够 1 枚后再次放行。"""
    monkeypatch.setattr(rl, "RATE_LIMIT_LLM_BUCKET_ENABLED", True)
    monkeypatch.setattr(rl, "RATE_LIMIT_LLM_BUCKET_CAPACITY", 2)
    monkeypatch.setattr(rl, "RATE_LIMIT_LLM_PER_MINUTE", 60)  # 1 枚/秒
    monkeypatch.setattr(rl, "RATE_LIMIT_WINDOW_SECONDS", 60)

    clock = _FakeClock()
    limiter = RateLimiter(redis=None, limits={"llm": 60}, clock=clock)

    assert (await limiter.hit("llm", user_id=1)).allowed  # 桶满 2 → 1
    assert (await limiter.hit("llm", user_id=1)).allowed  # 1 → 0
    denied = await limiter.hit("llm", user_id=1)
    assert denied.allowed is False

    clock.advance(0.5)  # 补 0.5 枚，不够 1
    assert (await limiter.hit("llm", user_id=1)).allowed is False
    clock.advance(0.6)  # 累计补给 1.1 枚 → 够 1 发
    assert (await limiter.hit("llm", user_id=1)).allowed is True


@pytest.mark.asyncio
async def test_llm_token_bucket_redis_path(fake_redis: Any) -> None:
    """Redis 路径：双实例共享同一桶（多进程语义），容量耗尽互通可见。"""
    monkey_capacity = 2
    original = rl.RATE_LIMIT_LLM_BUCKET_CAPACITY
    rl.RATE_LIMIT_LLM_BUCKET_CAPACITY = monkey_capacity
    try:
        limiter_a = RateLimiter(redis=fake_redis, limits={"llm": 4})
        limiter_b = RateLimiter(redis=fake_redis, limits={"llm": 4})
        assert (await limiter_a.hit("llm", user_id=9)).allowed
        assert (await limiter_b.hit("llm", user_id=9)).allowed
        third = await limiter_a.hit("llm", user_id=9)
        assert third.allowed is False  # B 的消耗对 A 可见 → 跨实例一致
    finally:
        rl.RATE_LIMIT_LLM_BUCKET_CAPACITY = original


# ---------------------------------------------------------------------------
# 2. 日请求数配额
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_request_quota_blocks_after_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rl, "RATE_LIMIT_DAILY_TASKS_PER_USER", 2)
    service = DailyQuotaService(redis=None)

    first = await service.hit_requests(42)
    second = await service.hit_requests(42)
    third = await service.hit_requests(42)

    assert [d.allowed for d in (first, second, third)] == [True, True, False]
    assert third.kind == "daily_tasks" and third.used == 3
    assert third.reset_in >= 1  # 距 UTC 零点的秒数
    # 用户隔离
    assert (await service.hit_requests(43)).allowed


# ---------------------------------------------------------------------------
# 3. 日 Token 预算（记账 + 预检）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_token_budget_accumulates_and_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rl, "RATE_LIMIT_DAILY_TOKEN_BUDGET_PER_USER", 100)
    service = DailyQuotaService(redis=None)

    assert (await service.check_budget(7)).allowed is True
    assert await service.record_tokens(7, 60) == 60
    assert (await service.check_budget(7)).allowed is True  # 60 < 100，仍放行
    assert await service.record_tokens(7, 50) == 110
    denied = await service.check_budget(7)
    assert denied.allowed is False and denied.kind == "daily_tokens" and denied.used == 110
    # 预检是只读的：拒绝本身不消耗任何计数
    assert (await service.check_budget(7)).used == 110
    # 零/负记账是无操作
    assert await service.record_tokens(7, 0) == 0


@pytest.mark.asyncio
async def test_daily_quota_shared_across_instances(fake_redis: Any) -> None:
    """Redis 路径：记账与预检跨服务实例一致（生产多进程前提）。"""
    writer = DailyQuotaService(redis=fake_redis)
    checker = DailyQuotaService(redis=fake_redis)
    await writer.record_tokens(11, 500)
    assert (await checker.check_budget(11)).used == 500


@pytest.mark.asyncio
async def test_record_task_tokens_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """旁路记账：非法入参 / Redis 装配失败都绝不抛异常。"""

    def _boom() -> None:
        raise RuntimeError("redis 装配炸了")

    monkeypatch.setattr("app.core.daily_quota._shared_redis", _boom)
    await record_task_tokens(1, 100)  # 记账服务内部降级为内存计数，不抛
    await record_task_tokens(None, 100)  # 无归属：无操作
    await record_task_tokens(1, 0)  # 零消耗：无操作


# ---------------------------------------------------------------------------
# 4. 中间件集成：日配额先于短时频率
# ---------------------------------------------------------------------------
def _quota_app(limiter: RateLimiter, quota: DailyQuotaService | None) -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/tasks")
    async def create_task() -> dict:
        return {"code": "OK", "data": {"id": 1}}

    app.add_middleware(RateLimitMiddleware)
    app.state.rate_limit_enabled = True
    app.state.rate_limit_limiter = limiter
    if quota is not None:
        app.state.daily_quota = quota
    return app


@pytest.mark.asyncio
async def test_middleware_daily_request_quota_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rl, "RATE_LIMIT_DAILY_TASKS_PER_USER", 1)
    monkeypatch.setattr(rl_mod, "_resolve_user", lambda scope: 42)  # 模拟已登录
    app = _quota_app(
        RateLimiter(redis=None, limits={"llm": 999, "api": 999}), DailyQuotaService(redis=None)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http:
        first = await http.post("/api/v1/tasks")
        second = await http.post("/api/v1/tasks")

    assert first.status_code == 200
    assert second.status_code == 429
    body = second.json()
    assert body["data"]["detail"]["kind"] == "daily_tasks"
    assert "今日" in body["message"]
    assert second.headers["Retry-After"].isdigit()


@pytest.mark.asyncio
async def test_middleware_token_budget_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rl, "RATE_LIMIT_DAILY_TOKEN_BUDGET_PER_USER", 100)
    monkeypatch.setattr(rl_mod, "_resolve_user", lambda scope: 42)
    quota = DailyQuotaService(redis=None)
    await quota.record_tokens(42, 100)  # 预算已耗尽
    app = _quota_app(
        RateLimiter(redis=None, limits={"llm": 999, "api": 999}), quota
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http:
        resp = await http.post("/api/v1/tasks")

    assert resp.status_code == 429
    body = resp.json()
    assert body["data"]["detail"]["kind"] == "daily_tokens"
    assert "Token" in body["message"]


@pytest.mark.asyncio
async def test_middleware_anonymous_skips_daily_quota() -> None:
    """匿名请求不进日配额（无法归属成本），短时频率照常生效。"""
    app = _quota_app(
        RateLimiter(redis=None, limits={"llm": 999, "api": 999}), DailyQuotaService(redis=None)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http:
        for _ in range(3):
            assert (await http.post("/api/v1/tasks")).status_code == 200
