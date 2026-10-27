"""任务 9：极限成本防御单测。

覆盖两件事：

1. 单任务 Token 硬熔断（``LLMClient`` 门面层）：
   - usage INCRBY 累计 + TTL；
   - 第 N 次调用前预检命中上限 → 抛 ``TokenBudgetExceededError``，且不再向 LLM 发请求；
   - 未绑定 task_id 不记账不拦截；Redis 故障 fail-open；
   - 主模型失败切备用后，备用消耗同样记账；预算熔断不触发备用重试。
2. LLM 调用硬超时 + max_tokens 上限：
   - ``asyncio.wait_for`` 总超时转 ``LLMError(LLM_TIMEOUT)``（主/备两条路径）；
   - OpenAICompatibleClient 实际 payload 强制携带 ``max_tokens=w2.LLM_MAX_TOKENS``。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

from app.agents import llm as llm_mod
from app.agents.llm import (
    BaseLLMClient,
    LLMClient,
    LLMError,
    LLMResponse,
    OpenAICompatibleClient,
    TokenBudgetExceededError,
)
from app.core.w2_config import w2
from app.services.stream_context import StreamContext, bind, unbind


# ---------------- 测试基建 ----------------
class StubLLM(BaseLLMClient):
    """可控桩：可注入 usage、延迟、异常，并记录调用次数。"""

    model = "stub-llm"

    def __init__(
        self,
        *,
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
        delay: float = 0.0,
        error: LLMError | None = None,
    ) -> None:
        self.calls = 0
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.delay = delay
        self.error = error

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
    ) -> LLMResponse:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return LLMResponse(
            content="ok",
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


@pytest.fixture()
def fake_redis(monkeypatch):
    """把 llm 门面里惰性取用的共享 Redis 换成 fakeredis。"""
    client = FakeRedis(decode_responses=True)
    monkeypatch.setattr("app.core.redis_client.get_shared_redis", lambda: client)
    return client


@pytest.fixture()
def bound_task():
    """模拟 _drive 的 bind/unbind 生命周期，把 task_id 注入 ContextVar。"""
    tokens_stack: list[tuple[StreamContext, Any]] = []

    def _bind(task_id: int):
        ctx = StreamContext(task_id=task_id, trace_id=f"trace-{task_id}", bus=None, trace=None)
        tokens = bind(ctx)
        tokens_stack.append((ctx, tokens))
        return tokens

    yield _bind
    for _, tokens in reversed(tokens_stack):
        unbind(tokens)


# ---------------- 改进 1：任务级 Token 硬熔断 ----------------
async def test_usage_accumulates_in_redis_with_ttl(fake_redis, bound_task):
    bound_task(7)
    client = LLMClient(primary=StubLLM(prompt_tokens=100, completion_tokens=50))
    await client.complete([{"role": "user", "content": "hi"}])
    await client.complete([{"role": "user", "content": "hi"}])

    key = "task:token_usage:7"
    assert int(await fake_redis.get(key)) == 2 * 150
    ttl = await fake_redis.ttl(key)
    assert 0 < ttl <= w2.TASK_TOKEN_USAGE_TTL_SECONDS


async def test_budget_blocks_at_nth_call_without_more_llm_requests(fake_redis, bound_task):
    """连续调用累计超标：第 3 次调用被拦截，桩客户端只被真正调用了 2 次。"""
    bound_task(42)
    stub = StubLLM(prompt_tokens=100, completion_tokens=50)  # 每次消耗 150
    client = LLMClient(primary=stub)
    client.max_tokens_per_task = 200

    await client.complete([{"role": "user", "content": "c1"}])  # used: 0 -> 150
    await client.complete([{"role": "user", "content": "c2"}])  # used: 150 -> 300
    assert stub.calls == 2

    with pytest.raises(TokenBudgetExceededError) as exc_info:
        await client.complete([{"role": "user", "content": "c3"}])
    assert exc_info.value.code == "TOKEN_BUDGET_EXCEEDED"
    assert exc_info.value.used_tokens >= 200
    assert exc_info.value.limit == 200
    # 熔断后不再向 LLM 发起真实请求
    assert stub.calls == 2


async def test_budget_precheck_blocks_before_any_request(fake_redis, bound_task):
    """预检在请求之前：Redis 里已超限时，桩客户端一次都不应被调用。"""
    bound_task(43)
    await fake_redis.set("task:token_usage:43", 500)
    stub = StubLLM()
    client = LLMClient(primary=stub)
    client.max_tokens_per_task = 200

    with pytest.raises(TokenBudgetExceededError):
        await client.complete([{"role": "user", "content": "c1"}])
    assert stub.calls == 0


async def test_no_task_binding_skips_guard(fake_redis):
    """未绑定 task_id（离线/评测等场景）不记账、不拦截。"""
    stub = StubLLM()
    client = LLMClient(primary=stub)
    client.max_tokens_per_task = 1  # 极小阈值，若误拦截会立刻失败

    await client.complete([{"role": "user", "content": "a"}])
    await client.complete([{"role": "user", "content": "b"}])
    assert stub.calls == 2
    assert await fake_redis.keys("task:token_usage:*") == []


async def test_fail_open_when_redis_down(monkeypatch, bound_task):
    """Redis 故障时 fail-open：只留日志，绝不阻断业务调用。"""

    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.core.redis_client.get_shared_redis", _boom)
    bound_task(99)
    stub = StubLLM()
    client = LLMClient(primary=stub)
    client.max_tokens_per_task = 1

    response = await client.complete([{"role": "user", "content": "hi"}])
    assert response.content == "ok"
    assert stub.calls == 1


async def test_fallback_usage_also_recorded(fake_redis, bound_task):
    """主模型失败切备用：备用返回的 usage 同样 INCRBY 累计。"""
    bound_task(8)
    primary = StubLLM(error=LLMError("primary boom", code="LLM_HTTP_ERROR", retryable=True))
    fallback = StubLLM(prompt_tokens=10, completion_tokens=5)
    client = LLMClient(primary=primary, fallback=fallback, mock=False)

    response = await client.complete([{"role": "user", "content": "hi"}])
    assert response.content == "ok"
    assert primary.calls == 1 and fallback.calls == 1
    assert int(await fake_redis.get("task:token_usage:8")) == 15


async def test_budget_exception_does_not_trigger_fallback(fake_redis, bound_task):
    """预算熔断异常（AppException 家族）不得触发主备降级。"""
    bound_task(9)
    await fake_redis.set("task:token_usage:9", 999)
    primary = StubLLM()
    fallback = StubLLM()
    client = LLMClient(primary=primary, fallback=fallback, mock=False)
    client.max_tokens_per_task = 200

    with pytest.raises(TokenBudgetExceededError):
        await client.complete([{"role": "user", "content": "hi"}])
    assert primary.calls == 0 and fallback.calls == 0


# ---------------- 改进 2：硬超时 + max_tokens 上限 ----------------
async def test_hard_timeout_primary(fake_redis, bound_task):
    """LLM 无限挂起：wait_for 硬超时转 LLMError(LLM_TIMEOUT)，且远早于挂起时长返回。"""
    bound_task(10)
    stub = StubLLM(delay=0.5)
    client = LLMClient(primary=stub)
    client.hard_timeout = 0.05

    started = time.perf_counter()
    with pytest.raises(LLMError) as exc_info:
        await client.complete([{"role": "user", "content": "hi"}])
    elapsed = time.perf_counter() - started
    assert exc_info.value.code == "LLM_TIMEOUT"
    assert elapsed < 0.45  # 0.05s 超时触发，远小于 0.5s 挂起时长


async def test_hard_timeout_fallback_path(fake_redis, bound_task):
    """主模型立即失败 + 备用模型挂起：备用路径的超时同样生效。"""
    bound_task(11)
    primary = StubLLM(error=LLMError("primary boom", retryable=True))
    fallback = StubLLM(delay=0.5)
    client = LLMClient(primary=primary, fallback=fallback, mock=False)
    client.hard_timeout = 0.05

    with pytest.raises(LLMError) as exc_info:
        await client.complete([{"role": "user", "content": "hi"}])
    assert exc_info.value.code == "LLM_TIMEOUT"
    assert primary.calls == 1 and fallback.calls == 1


class _FakeResponse:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {
            "model": "stub-model",
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


class _FakeAsyncClient:
    """捕获 OpenAICompatibleClient 实际发出的 payload。"""

    captured: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None: ...

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def post(self, url: str, json: dict[str, Any] | None = None, headers: Any = None) -> _FakeResponse:
        _FakeAsyncClient.captured = dict(json or {})
        return _FakeResponse()


async def test_openai_client_forces_max_tokens_cap(monkeypatch):
    """初始化/请求层强制 max_tokens：调用方不传时必须落到 w2.LLM_MAX_TOKENS（2048）。"""
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeAsyncClient)
    client = OpenAICompatibleClient(base_url="http://stub", api_key="k", model="stub-model")

    response = await client.complete([{"role": "user", "content": "hi"}])
    assert response.content == "{}"
    # payload 必须携带配置值（.env 可覆盖）；代码默认值收紧为 2048
    assert _FakeAsyncClient.captured["max_tokens"] == w2.LLM_MAX_TOKENS
    from app.core.w2_config import W2Settings

    assert W2Settings.model_fields["LLM_MAX_TOKENS"].default == 2048


async def test_openai_client_respects_explicit_max_tokens(monkeypatch):
    """调用方显式传入的 max_tokens 不被覆盖。"""
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeAsyncClient)
    client = OpenAICompatibleClient(base_url="http://stub", api_key="k", model="stub-model")

    await client.complete([{"role": "user", "content": "hi"}], max_tokens=128)
    assert _FakeAsyncClient.captured["max_tokens"] == 128
