"""AI 问答接口限流依赖（Redis 令牌桶）。

- 按用户 ID 限流，限制每个用户每分钟 N 次 AI 类请求（conversations/chat、
  conversations/chat/stream、kbs/{id}/query —— 三者都会真实消耗 RAG 检索与 LLM 额度）；
- 阈值取系统配置页的 `ai_rate_limit_per_minute`（热更新），未覆盖时回退 .env
  的 `AI_RATE_LIMIT_PER_MINUTE`（默认 30）；
- 超限抛 429 Too Many Requests（统一响应体 `{code:429, message:"请求过于频繁，请稍后重试"}`）；
- `FAKE_REDIS=true` 时跳过：fakeredis 不支持 Lua EVAL，且内存模式本就只用于
  离线演示 / CI，不应阻断用例；
- Redis 异常时 fail-open（放行 + WARNING 日志）：限流是防护手段，不能因缓存故障
  把整个问答业务打挂。

【批次3 改造】
1. `_is_fake_redis` 改为复用 `core.redis.is_fake_redis`，消除两处重复判定口径；
2. 跳过限流时打一次 WARNING（每个进程一次），避免"以为开着限流其实没开"的静默风险；
3. 新增 `rate_limit_status()` 供 /health 暴露限流真实状态。
"""
import logging
import time

from fastapi import Depends

from ..core import runtime_config
from ..core.exceptions import BizError
from ..core.redis import get_redis, is_fake_redis
from .deps import CurrentUser, get_current_user

logger = logging.getLogger(__name__)

# 令牌桶 Lua 脚本（Redis 原子执行，支持突发容量 + 匀速补充）
_TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1])
local last_refill = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

local elapsed = now - last_refill
if elapsed > 0 then
    tokens = math.min(capacity, tokens + elapsed * refill_rate)
    last_refill = now
end

local allowed = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
end

redis.call('HSET', key, 'tokens', tokens, 'last_refill', last_refill)
redis.call('EXPIRE', key, math.ceil(capacity / refill_rate) + 60)

return {allowed, tokens}
"""

# 兼容旧引用（tests/test_rate_limit.py 直接导入该名字）：语义等价于 core.redis.is_fake_redis
_is_fake_redis = is_fake_redis

# 跳过限流的告警只打一次，避免每条问答都刷日志
_fake_skip_warned = False


def _rate_limit_per_minute() -> int:
    """每用户每分钟 AI 请求上限。

    优先取系统配置页保存的覆盖值（热更新）；
    未覆盖时回退 .env: AI_RATE_LIMIT_PER_MINUTE（默认 30），与旧逻辑一致。
    """
    return int(runtime_config.get_value("ai_rate_limit_per_minute"))


def rate_limit_status() -> str:
    """限流真实状态：enabled / disabled(fake redis) / degraded(redis 不可用)。

    供 /health 展示——fake 模式下"看起来一切正常但限流没生效"是最容易误判的运维盲区。
    """
    if is_fake_redis():
        return "disabled"
    from ..core.redis import ping_redis

    return "enabled" if ping_redis() else "degraded"


def _allow_request(user_id: int) -> bool:
    """令牌桶判定是否放行；Redis 异常时失败放开（不阻断业务）。"""
    capacity = _rate_limit_per_minute()
    refill_rate = capacity / 60.0
    key = f"rate_limit:ai:{user_id}"
    try:
        r = get_redis()
        result = r.eval(_TOKEN_BUCKET_SCRIPT, 1, key, capacity, refill_rate, time.time(), 1)
        return int(result[0]) == 1
    except Exception as exc:  # noqa: BLE001 - 限流故障不应阻断业务
        logger.warning("限流 Redis 操作失败，放行请求: %s", exc)
        return True


def rate_limit_ai(user: CurrentUser = Depends(get_current_user)) -> None:
    """AI 问答限流依赖：超限抛 429；FAKE_REDIS 下跳过。"""
    global _fake_skip_warned
    if is_fake_redis():
        if not _fake_skip_warned:
            _fake_skip_warned = True
            logger.warning("FAKE_REDIS=true：AI 问答限流已停用（仅在离线演示/CI 场景可接受）")
        return
    if not _allow_request(user.id):
        logger.info("AI 限流命中 user_id=%s limit=%s/min", user.id, _rate_limit_per_minute())
        raise BizError(429, "请求过于频繁，请稍后重试", 429)
