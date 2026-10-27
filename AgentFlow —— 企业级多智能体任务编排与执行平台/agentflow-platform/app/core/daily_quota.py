"""用户级日配额（加固任务 3）：每日请求数 + 每日 Token 预算 + Token 记账。

职责边界（与 ``rate_limit.py`` 的固定窗口/令牌桶互补）：

- 固定窗口/令牌桶管的是**短时频率**（秒级/分钟级），防的是刷接口；
- 日配额管的是**累计成本**（天级），防的是"合法频率下烧钱"——
  每个请求都不超速，但一天 10 万次照样能把 Token 预算烧穿。

三个能力：

1. ``hit_requests``：进站自增当日请求计数（``INCR + 当日剩余秒数 EXPIRE``），
   超过 ``RATE_LIMIT_DAILY_TASKS_PER_USER`` 返回 ``allowed=False``；
2. ``check_budget``：进站**只读预检**当日 Token 消耗，已达预算则拒绝新任务。
   之所以是预检而不是预扣：任务开始前无法预知本次消耗多少 Token，
   只能在任务完成时回记账（``record_tokens``），进站时按"已消耗 ≥ 预算"拦截；
3. ``record_tokens``：任务终态落库后回记当日消耗（``INCRBY``）。

口径约定：

- 日界用 **UTC 日期**（与全库时间字段一致，见 2026-10-22 时间口径决定）；
- Redis 故障时降级为进程内字典（与 ``RateLimiter`` 同策略：不 500、单副本可用）；
- 记账是旁路动作，任何失败只留 warning 日志，绝不影响任务落库主流程。

 key 设计（``rl`` 前缀与限流共用，便于统一清理）：
- ``rl:daily:req:{user}:{yyyymmdd}``  当日请求计数
- ``rl:daily:tok:{user}:{yyyymmdd}``  当日 Token 消耗累计
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.context import get_trace_id
from app.core.logging import get_logger
from app.core.rate_config import rate_limit as rl
from app.core.rate_limit import RateDecision

logger = get_logger(__name__)

#: 日配额 TTL 上限兜底（正常等于"到下一个 UTC 零点"的秒数）
_DAY_KEY_MAX_TTL = 60 * 60 * 26

_REQUEST_KIND = "daily_tasks"
_TOKEN_KIND = "daily_tokens"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _seconds_to_utc_midnight(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


class DailyQuotaService:
    """日配额计数器（Redis 优先，无 Redis / Redis 故障退进程内字典）。"""

    def __init__(self, redis: Any = None) -> None:
        self.redis = redis
        # key -> (yyyymmdd, value)；内存降级只保当日，跨日自动作废
        self._memory: dict[str, tuple[str, int]] = {}

    # ---- key 构造 ---------------------------------------------------------
    @staticmethod
    def _req_key(user_id: int | str, day: str) -> str:
        return f"{rl.RATE_LIMIT_KEY_PREFIX}:daily:req:{user_id}:{day}"

    @staticmethod
    def _tok_key(user_id: int | str, day: str) -> str:
        return f"{rl.RATE_LIMIT_KEY_PREFIX}:daily:tok:{user_id}:{day}"

    # ---- 1. 当日请求计数 ---------------------------------------------------
    async def hit_requests(self, user_id: int | str | None) -> RateDecision:
        """自增当日请求计数并判定（不抛异常：配额故障不得变成 500）。"""
        limit = int(rl.RATE_LIMIT_DAILY_TASKS_PER_USER)
        now = _utc_now()
        day = now.strftime("%Y%m%d")
        reset_in = _seconds_to_utc_midnight(now)
        subject = f"u:{user_id if user_id is not None else 'anon'}"
        used = await self._bump(self._req_key(user_id, day), day, 1, reset_in)
        decision = RateDecision(
            kind=_REQUEST_KIND,
            scope="user",
            subject=subject,
            limit=limit,
            used=used,
            window=_seconds_to_utc_midnight(now),
            reset_in=reset_in,
            allowed=(limit <= 0) or (used <= limit),
        )
        if not decision.allowed:
            logger.warning(
                "daily_quota_exceeded",
                extra={"kind": _REQUEST_KIND, "subject": subject, "used": used, "limit": limit},
            )
        return decision

    # ---- 2. Token 预算预检 -------------------------------------------------
    async def check_budget(self, user_id: int | str | None) -> RateDecision:
        """只读预检当日 Token 消耗；已达/超预算则拒绝（不消耗任何计数）。"""
        limit = int(rl.RATE_LIMIT_DAILY_TOKEN_BUDGET_PER_USER)
        now = _utc_now()
        day = now.strftime("%Y%m%d")
        reset_in = _seconds_to_utc_midnight(now)
        subject = f"u:{user_id if user_id is not None else 'anon'}"
        used = await self._peek(self._tok_key(user_id, day), day)
        return RateDecision(
            kind=_TOKEN_KIND,
            scope="user",
            subject=subject,
            limit=limit,
            used=used,
            window=reset_in,
            reset_in=reset_in,
            allowed=(limit <= 0) or (used < limit),
        )

    # ---- 3. Token 记账 ------------------------------------------------------
    async def record_tokens(self, user_id: int | str | None, tokens: int) -> int:
        """任务终态后回记当日消耗，返回累计值（记账失败静默降级为本地计数）。"""
        if user_id is None or tokens <= 0:
            return 0
        now = _utc_now()
        day = now.strftime("%Y%m%d")
        return await self._bump(self._tok_key(user_id, day), day, int(tokens), _seconds_to_utc_midnight(now))

    # ---- 运维自检 -----------------------------------------------------------
    async def usage(self, user_id: int | str) -> dict[str, Any]:
        now = _utc_now()
        day = now.strftime("%Y%m%d")
        reset_in = _seconds_to_utc_midnight(now)
        req_limit = int(rl.RATE_LIMIT_DAILY_TASKS_PER_USER)
        tok_limit = int(rl.RATE_LIMIT_DAILY_TOKEN_BUDGET_PER_USER)
        req_used = await self._peek(self._req_key(user_id, day), day)
        tok_used = await self._peek(self._tok_key(user_id, day), day)
        return {
            "subject": f"u:{user_id}",
            "reset_in": reset_in,
            "requests": {"limit": req_limit, "used": req_used, "remaining": max(0, req_limit - req_used)},
            "tokens": {"limit": tok_limit, "used": tok_used, "remaining": max(0, tok_limit - tok_used)},
        }

    # ---- 内部：原子自增 / 只读（Redis 优先，降级内存） ----------------------
    async def _bump(self, key: str, day: str, delta: int, ttl: int) -> int:
        if self.redis is not None:
            try:
                pipe = self.redis.pipeline()
                pipe.incrby(key, delta)
                pipe.expire(key, min(max(ttl, 60), _DAY_KEY_MAX_TTL))
                result = await pipe.execute()
                return int(result[0])
            except Exception as exc:  # noqa: BLE001 - Redis 故障降级为本地计数
                logger.warning("daily_quota_redis_failed", extra={"error": str(exc)[:200]})
        stored_day, value = self._memory.get(key, (day, 0))
        value = value + delta if stored_day == day else delta
        if len(self._memory) > 4096:
            self._memory.clear()
        self._memory[key] = (day, value)
        return value

    async def _peek(self, key: str, day: str) -> int:
        if self.redis is not None:
            try:
                return int(await self.redis.get(key) or 0)
            except Exception:  # noqa: BLE001 - 读失败按 0 处理，不阻塞放行判断
                pass
        stored_day, value = self._memory.get(key, (day, 0))
        return value if stored_day == day else 0


# ---------------------------------------------------------------------------
# 旁路记账入口：任务终态落库后调用（fail-open，绝不影响主流程）
# ---------------------------------------------------------------------------
def _shared_redis() -> Any:
    try:
        from app.core.redis_client import get_shared_redis

        return get_shared_redis()
    except Exception:  # noqa: BLE001
        return None


async def record_task_tokens(creator_id: int | str | None, tokens: int) -> None:
    """把任务实际消耗的 Token 记到创建者的当日预算账本上。

    旁路语义：任何异常只留日志；调用方（任务落库）不感知成败。
    """
    if not creator_id or tokens <= 0:
        return
    try:
        service = DailyQuotaService(redis=_shared_redis())
        await service.record_tokens(creator_id, tokens)
    except Exception as exc:  # noqa: BLE001 - 记账失败绝不能影响任务落库
        logger.warning("daily_token_record_failed", extra={"error": str(exc)[:200], "trace_id": get_trace_id()})


__all__ = ["DailyQuotaService", "record_task_tokens"]
