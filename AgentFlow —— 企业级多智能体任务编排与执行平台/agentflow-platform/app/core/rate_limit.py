"""全局限流（v1.1 任务 2）：固定窗口计数 + 用户 / 任务双维度 + 三类配额。

分三层的职责划分（这样做是为了不动已验收链路的任何一行）：

1. ``RateLimiter``：纯计数服务（Redis ``INCR + EXPIRE``；无 Redis 时进程内降级），
   可单测、可被其它服务直接复用（比如未来在 LLMClient 内部做"调用级"限流）；
2. ``RateLimitMiddleware``：**纯 ASGI 中间件**（不是 ``BaseHTTPMiddleware``）。
   这一点很关键——``BaseHTTPMiddleware`` 会包一层 body 迭代器，历史上会破坏
   SSE 的 ``is_disconnected`` 与背压行为；纯 ASGI 只做「进站判断 + 出站加头」，
   对已成型的 SSE 流式逻辑零影响（用户明确要求该链路不得改动）；
3. ``classify_request``：把请求映射到配额类型，规则集中在一处，便于 review。

错误口径：超限统一返回 HTTP 429 + ``{"code": "RATE_LIMITED", ...}``（与全局异常处理器
同一信封），并带 ``Retry-After`` 与 ``X-RateLimit-*`` 头；日志埋点 ``rate_limit_exceeded``
记录 kind / scope / 主体 / 路径，便于排查"是谁把配额打满的"。
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.context import get_trace_id
from app.core.logging import get_logger
from app.core.rate_config import rate_limit as rl

logger = get_logger(__name__)

#: ``/api/v1/tasks/123/...`` 里抽出任务 ID（用于任务维度限流）
_TASK_RE = re.compile(r"/tasks/(\d+)(?:/|$)")

#: 会驱动 LLM 的写接口（单次成本最高，配额最小）
_LLM_PATHS: tuple[tuple[str, str], ...] = (
    ("POST", "/tasks"),
    ("POST", "/tasks/stream"),
    ("POST", "/approvals/batch"),
)
#: 断点续跑（POST /tasks/{id}/resume）也属于 LLM 类，用正则单独判
_RESUME_RE = re.compile(r"/tasks/\d+/resume$")

#: 知识库相关（外部项目一，配额中等）
_KB_PREFIXES: tuple[str, ...] = ("/kb/", "/kb")


@dataclass(slots=True)
class RateDecision:
    """一次配额判定结果。"""

    kind: str
    scope: str
    subject: str
    limit: int
    used: int
    window: int
    reset_in: int
    allowed: bool

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def as_headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_in),
            "X-RateLimit-Policy": f"{self.kind}:{self.scope}",
        }


@dataclass
class RateLimiter:
    """固定窗口计数器 + LLM 类令牌桶（加固任务 3）。

    ``redis`` 为 None 时退化为进程内字典（单副本够用，多副本会各算各的，
    因此生产/多副本必须给 Redis；降级只保证"不 500"，不保证全局精确）。

    令牌桶只对 ``rl.bucket_params()`` 命中的 kind（当前仅 ``llm``）生效：
    容量 = 最大突发量（默认配额一半），补给速率 = 配额/窗口秒数——
    平滑限速的同时允许轻微突发，比固定窗口更抗短时脉冲。
    ``clock`` 可注入（单测 refill 用），生产用 ``time.time``。
    """

    redis: Any = None
    window: int = field(default_factory=lambda: rl.RATE_LIMIT_WINDOW_SECONDS)
    limits: dict[str, int] = field(default_factory=dict)
    clock: Any = time.time
    _memory: dict[str, tuple[int, int]] = field(default_factory=dict)  # key -> (bucket, count)
    _bucket_mem: dict[str, tuple[float, float]] = field(default_factory=dict)  # key -> (tokens, ts)

    def limit_of(self, kind: str) -> int:
        return int(self.limits.get(kind) or rl.limit_for(kind))

    def _key(self, kind: str, subject: str, bucket: int) -> str:
        return f"{rl.RATE_LIMIT_KEY_PREFIX}:{kind}:{subject}:{bucket}"

    async def hit(
        self,
        kind: str,
        *,
        user_id: int | str | None = None,
        task_id: int | str | None = None,
        ip: str | None = None,
        cost: int = 1,
    ) -> RateDecision:
        """记一次配额消耗，返回判定结果（不抛异常：限流故障不应该变成 500）。"""
        subject, scope = self._subject(kind, user_id=user_id, task_id=task_id, ip=ip)

        # 加固任务 3：命中令牌桶的 kind（llm）走平滑限速，其余保持固定窗口
        bucket_enabled, capacity, refill = rl.bucket_params(kind)
        if bucket_enabled:
            return await self._bucket_hit(kind, subject, scope, capacity, refill, cost=cost)

        limit = self.limit_of(kind)
        now = int(self.clock())
        bucket = now // self.window
        reset_in = self.window - (now % self.window)
        key = self._key(kind, subject, bucket)

        used = await self._incr(key, cost)
        allowed = used <= limit
        decision = RateDecision(
            kind=kind,
            scope=scope,
            subject=subject,
            limit=limit,
            used=used,
            window=self.window,
            reset_in=reset_in,
            allowed=allowed,
        )
        if not allowed:
            logger.warning(
                "rate_limit_exceeded",
                extra={
                    "kind": kind,
                    "scope": scope,
                    "subject": subject,
                    "used": used,
                    "limit": limit,
                    "window": self.window,
                    "retry_after": reset_in,
                },
            )
        return decision

    # ---- 令牌桶（加固任务 3） ----------------------------------------------
    def _bucket_key(self, kind: str, subject: str) -> str:
        return f"{rl.RATE_LIMIT_KEY_PREFIX}:tb:{kind}:{subject}"

    async def _bucket_hit(
        self, kind: str, subject: str, scope: str, capacity: int, refill: float, *, cost: int = 1
    ) -> RateDecision:
        now = float(self.clock())
        tokens_left, allowed = await self._bucket_take(kind, subject, capacity, refill, now, cost=cost)
        if allowed:
            reset_in = 0
        elif refill > 0:
            reset_in = max(1, math.ceil((cost - tokens_left) / refill))
        else:
            reset_in = self.window
        decision = RateDecision(
            kind=kind,
            scope=scope,
            subject=subject,
            limit=capacity,
            used=int(round(capacity - tokens_left)),
            window=self.window,
            reset_in=min(reset_in, self.window),
            allowed=allowed,
        )
        if not allowed:
            logger.warning(
                "rate_limit_exceeded",
                extra={
                    "kind": kind,
                    "scope": scope,
                    "subject": subject,
                    "algorithm": "token_bucket",
                    "capacity": capacity,
                    "refill_per_sec": round(refill, 4),
                    "retry_after": decision.reset_in,
                },
            )
        return decision

    async def _bucket_take(
        self, kind: str, subject: str, capacity: int, refill: float, now: float, *, cost: int = 1
    ) -> tuple[float, bool]:
        """取一个令牌，返回 ``(取后余量, 是否放行)``。Redis 优先，故障降级进程内。"""
        key = self._bucket_key(kind, subject)
        if self.redis is not None:
            try:
                return await self._redis_bucket_take(key, capacity, refill, now, cost=cost)
            except Exception as exc:  # noqa: BLE001 - Redis 故障降级为本地计数
                logger.warning("rate_limit_redis_failed", extra={"error": str(exc)[:200]})
        return self._memory_bucket_take(key, capacity, refill, now, cost=cost)

    @staticmethod
    def _parse_bucket_state(raw: Any, capacity: int, now: float) -> tuple[float, float]:
        """解析桶状态（``tok``/``ts``），首次或损坏视为满桶；时钟回拨时钳制。"""
        try:
            tokens = float(raw.get("tok"))  # type: ignore[union-attr]
            last = float(raw.get("ts"))  # type: ignore[union-attr]
        except (TypeError, ValueError, AttributeError):
            return float(capacity), now
        if last > now:  # 时钟回拨保护：只按 0 补给处理，不产生负数
            last = now
        return tokens, last

    async def _redis_bucket_take(
        self, key: str, capacity: int, refill: float, now: float, *, cost: int = 1
    ) -> tuple[float, bool]:
        # WATCH/MULTI 乐观事务（与 redis_lock 同款，fakeredis 兼容）：
        # 读桶状态 → 按流逝时间补令牌 → 判定并扣减 → 写回。并发改写则重试。
        ttl = max(120, int(capacity / refill * 2)) if refill > 0 else max(120, self.window * 2)
        for _ in range(3):
            async with self.redis.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                current = await pipe.hgetall(key)
                tokens, last = self._parse_bucket_state(current, capacity, now)
                tokens = min(float(capacity), tokens + max(0.0, now - last) * refill)
                allowed = tokens >= cost
                if allowed:
                    tokens -= cost
                pipe.multi()
                pipe.hset(key, mapping={"tok": f"{tokens:.6f}", "ts": f"{now:.6f}"})
                pipe.expire(key, ttl)
                try:
                    await pipe.execute()
                except Exception as exc:  # noqa: BLE001 - 并发冲突视为 CAS 失败重试
                    if type(exc).__name__ != "WatchError":
                        raise
                    continue
                return tokens, allowed
        # CAS 三连败（极端并发）：退化为无 watch 直写，误差只影响补给精度，方向保守
        current = await self.redis.hgetall(key) or {}
        tokens, last = self._parse_bucket_state(current, capacity, now)
        tokens = min(float(capacity), tokens + max(0.0, now - last) * refill)
        allowed = tokens >= cost
        if allowed:
            tokens -= cost
        await self.redis.hset(key, mapping={"tok": f"{tokens:.6f}", "ts": f"{now:.6f}"})
        await self.redis.expire(key, ttl)
        return tokens, allowed

    def _memory_bucket_take(
        self, key: str, capacity: int, refill: float, now: float, *, cost: int = 1
    ) -> tuple[float, bool]:
        tokens, last = self._bucket_mem.get(key, (float(capacity), now))
        tokens = min(float(capacity), tokens + max(0.0, now - last) * refill)
        allowed = tokens >= cost
        if allowed:
            tokens -= cost
        if len(self._bucket_mem) > 4096:
            self._bucket_mem.clear()
        self._bucket_mem[key] = (tokens, now)
        return tokens, allowed

    @staticmethod
    def _subject(
        kind: str,
        *,
        user_id: int | str | None,
        task_id: int | str | None,
        ip: str | None,
    ) -> tuple[str, str]:
        """主体口径：任务维度优先用任务 ID，其余用用户 ID，最后退 IP。"""
        if kind == "task" and task_id is not None:
            return f"task:{task_id}", "task"
        if user_id is not None:
            return f"u:{user_id}", "user"
        if ip:
            return f"ip:{ip}", "ip"
        return "anon", "anon"

    async def _incr(self, key: str, cost: int) -> int:
        if self.redis is not None:
            try:
                pipe = self.redis.pipeline()
                pipe.incrby(key, cost)
                pipe.expire(key, self.window + 1)
                result = await pipe.execute()
                return int(result[0])
            except Exception as exc:  # noqa: BLE001 - Redis 故障降级为本地计数
                logger.warning("rate_limit_redis_failed", extra={"error": str(exc)[:200]})
        bucket = int(key.rsplit(":", 1)[-1])
        current_bucket, count = self._memory.get(key, (bucket, 0))
        if current_bucket != bucket:
            count = 0
        count += cost
        # 只保留当前窗口，避免内存无界增长
        if len(self._memory) > 4096:
            self._memory.clear()
        self._memory[key] = (bucket, count)
        return count

    async def usage(self, kind: str, *, user_id: int | str | None = None, task_id: int | str | None = None) -> dict[str, Any]:
        """只读查询当前窗口用量（运维自检 / 前端展示剩余次数用）。"""
        subject, scope = self._subject(kind, user_id=user_id, task_id=task_id, ip=None)
        now = int(self.clock())
        bucket_enabled, capacity, refill = rl.bucket_params(kind)
        if bucket_enabled:
            # 桶口径：used = 容量 - 当前余量（含按流逝时间补足后的余量）
            now_f = float(self.clock())
            key = self._bucket_key(kind, subject)
            tokens = float(capacity)
            if self.redis is not None:
                try:
                    raw = await self.redis.hgetall(key)
                    raw_tokens, last = self._parse_bucket_state(raw, capacity, now_f)
                    tokens = min(float(capacity), raw_tokens + max(0.0, now_f - last) * refill)
                except Exception:  # noqa: BLE001 - 读失败按满桶处理，不阻塞自检
                    pass
            else:
                raw_tokens, last = self._bucket_mem.get(key, (float(capacity), now_f))
                tokens = min(float(capacity), raw_tokens + max(0.0, now_f - last) * refill)
            used = int(round(capacity - tokens))
            return {
                "kind": kind,
                "scope": scope,
                "subject": subject,
                "algorithm": "token_bucket",
                "limit": capacity,
                "used": used,
                "remaining": max(0, capacity - used),
                "reset_in": 0 if used < capacity else (max(1, math.ceil(1 / refill)) if refill > 0 else self.window),
            }
        limit = self.limit_of(kind)
        bucket = now // self.window
        key = self._key(kind, subject, bucket)
        used = 0
        if self.redis is not None:
            try:
                raw = await self.redis.get(key)
                used = int(raw or 0)
            except Exception:  # noqa: BLE001 - 读失败按 0 处理，不阻塞自检
                used = self._memory.get(key, (bucket, 0))[1]
        else:
            used = self._memory.get(key, (bucket, 0))[1]
        return {
            "kind": kind,
            "scope": scope,
            "subject": subject,
            "limit": self.limit_of(kind),
            "used": used,
            "remaining": max(0, self.limit_of(kind) - used),
            "reset_in": self.window - (now % self.window),
        }


# ---------------------------------------------------------------------------
# 请求分类
# ---------------------------------------------------------------------------
def classify_request(method: str, path: str) -> list[str]:
    """返回该请求要消耗的配额类型（按顺序判定，首个为"主配额"）。"""
    normalized = path.rstrip("/") or "/"
    kinds: list[str] = []

    # 去掉 /api/v1 前缀后统一比较，避免版本前缀变化导致规则失效
    suffix = normalized
    for prefix in ("/api/v1", "/api"):
        if suffix.startswith(prefix):
            suffix = suffix[len(prefix) :] or "/"
            break

    upper = method.upper()
    for verb, target in _LLM_PATHS:
        if upper == verb and suffix == target:
            kinds.append("llm")
            break
    if upper == "POST" and _RESUME_RE.search(suffix):
        kinds.append("llm")
    if any(suffix == p.rstrip("/") or suffix.startswith(p) for p in _KB_PREFIXES):
        kinds.append("kb")

    # api 作为兜底总闸始终计一次：即便 LLM/KB 配额没打满，也不能无限刷普通接口
    kinds.append("api")
    # 去重保序：llm/kb 是主配额（用于回写响应头），api 兜底
    ordered: list[str] = []
    for kind in kinds:
        if kind not in ordered:
            ordered.append(kind)
    return ordered


def is_exempt(path: str) -> bool:
    if not path.startswith("/api") and not path.startswith("/mcp"):
        # 非 API 路径（静态、文档、健康页）不进限流
        return True
    return any(path == p or path.startswith(p) for p in rl.exempt_prefixes())


def _client_ip(scope: dict[str, Any]) -> str | None:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else None


def _resolve_user(scope: dict[str, Any]) -> int | str | None:
    """从令牌里解出 user_id（只验签，不查库——限流是前置防线，不能引入 DB 依赖）。"""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
    token = ""
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        query = scope.get("query_string", b"").decode("latin-1")
        for pair in query.split("&"):
            if pair.startswith("access_token="):
                token = pair[len("access_token=") :]
                break
    if not token:
        return None
    try:
        from app.core.security import TokenType, decode_token

        claims = decode_token(token, TokenType.ACCESS)
        return int(claims.user_id)
    except Exception:  # noqa: BLE001 - 无效令牌交给后续鉴权层返回 401，这里只降级为 IP 口径
        return None


def _resolve_task_id(path: str) -> int | None:
    match = _TASK_RE.search(path)
    return int(match.group(1)) if match else None


def _enabled(app: Any) -> bool:
    override = getattr(getattr(app, "state", None), "rate_limit_enabled", None)
    if override is not None:
        return bool(override)
    from app.core.config import settings

    # 测试环境默认关闭：476 项回归里有大量连续请求，限流会让用例互相污染
    return bool(rl.RATE_LIMIT_ENABLED) and not settings.is_test


def _limiter(app: Any) -> RateLimiter:
    existing = getattr(getattr(app, "state", None), "rate_limit_limiter", None)
    if existing is not None:
        return existing
    redis = None
    try:
        from app.core.redis_client import get_shared_redis

        redis = get_shared_redis()
    except Exception:  # noqa: BLE001
        redis = None
    limiter = RateLimiter(redis=redis)
    try:
        app.state.rate_limit_limiter = limiter
    except Exception:  # noqa: BLE001 - 极端情况下 state 不可写，退回临时实例
        pass
    return limiter


def _daily_quota(app: Any) -> Any | None:
    """取（或懒建）日配额服务；惰性导入避免与 daily_quota 模块循环依赖。"""
    existing = getattr(getattr(app, "state", None), "daily_quota", None)
    if existing is not None:
        return existing
    try:
        from app.core.daily_quota import DailyQuotaService

        redis = None
        try:
            from app.core.redis_client import get_shared_redis

            redis = get_shared_redis()
        except Exception:  # noqa: BLE001 - 无 Redis 时 DailyQuotaService 自带内存降级
            redis = None
        service = DailyQuotaService(redis=redis)
    except Exception:  # noqa: BLE001 - 日配额装配失败不能拖垮限流主链路
        return None
    try:
        app.state.daily_quota = service
    except Exception:  # noqa: BLE001
        pass
    return service


class RateLimitMiddleware:
    """纯 ASGI 限流中间件：进站判配额、出站回写额度头，不包装 body。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "GET")
        app = scope.get("app") or self.app
        if not _enabled(app) or is_exempt(path):
            await self.app(scope, receive, send)
            return

        limiter = _limiter(app)
        user_id = _resolve_user(scope)
        ip = _client_ip(scope) if rl.RATE_LIMIT_PER_IP_ENABLED else None
        task_id = _resolve_task_id(path)
        kinds = classify_request(method, path)

        decisions: list[RateDecision] = []
        blocked: RateDecision | None = None
        try:
            # 加固任务 3：LLM 类请求先过"日配额"（当日请求数 + 当日 Token 预算），
            # 再进短时频率（令牌桶/固定窗口）——累计成本防线必须先于频率防线，
            # 否则配额已耗尽的用户仍可继续消耗短时窗口内的调用。
            # 仅对能识别身份的请求生效：匿名请求无法归属成本，交给 IP/窗口口径。
            if "llm" in kinds and user_id is not None:
                quota = _daily_quota(app)
                if quota is not None:
                    daily = await quota.hit_requests(user_id)
                    decisions.append(daily)
                    if not daily.allowed:
                        blocked = daily
                    if blocked is None:
                        budget = await quota.check_budget(user_id)
                        decisions.append(budget)
                        if not budget.allowed:
                            blocked = budget
            if blocked is None:
                for kind in kinds:
                    decision = await limiter.hit(kind, user_id=user_id, task_id=task_id, ip=ip)
                    decisions.append(decision)
                    if not decision.allowed:
                        blocked = decision
                        break
            if blocked is None and task_id is not None and "task" not in kinds:
                task_decision = await limiter.hit("task", user_id=user_id, task_id=task_id, ip=ip)
                decisions.append(task_decision)
                if not task_decision.allowed:
                    blocked = task_decision
        except Exception:  # noqa: BLE001 - 限流自身故障必须 fail-open，不能把业务打成 500
            logger.exception("rate_limit_middleware_failed", extra={"path": path, "method": method})
            await self.app(scope, receive, send)
            return

        if blocked is not None:
            await _send_429(scope, receive, send, blocked)
            return

        expose = rl.RATE_LIMIT_EXPOSE_HEADERS
        main = decisions[0] if decisions else None

        async def send_with_headers(message: dict[str, Any]) -> None:
            if expose and message.get("type") == "http.response.start" and main is not None:
                headers = list(message.get("headers") or [])
                for key, value in main.as_headers().items():
                    headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _send_429(scope: dict[str, Any], receive: Any, send: Any, decision: RateDecision) -> None:
    from starlette.responses import JSONResponse

    # 日配额场景给"人话"：用户不需要理解窗口概念，需要知道是"今天用完了"
    if decision.kind == "daily_tasks":
        message = (
            f"今日任务类请求次数已达上限（{decision.used}/{decision.limit}），"
            f"请明天再试（约 {decision.reset_in // 3600} 小时后重置）"
        )
    elif decision.kind == "daily_tokens":
        message = (
            f"今日 Token 额度已耗尽（已用 {decision.used}/{decision.limit}），"
            f"请明天再试或联系管理员调整配额（约 {decision.reset_in // 3600} 小时后重置）"
        )
    else:
        message = (
            f"请求过于频繁（{decision.kind} 配额 {decision.limit}/{decision.window}s），"
            f"请 {decision.reset_in}s 后重试"
        )
    body = {
        "code": "RATE_LIMITED",
        "message": message,
        "data": {
            "detail": {
                "kind": decision.kind,
                "scope": decision.scope,
                "limit": decision.limit,
                "used": decision.used,
                "window_seconds": decision.window,
                "retry_after": decision.reset_in,
            }
        },
        "trace_id": get_trace_id() or None,
    }
    headers = {**decision.as_headers(), "Retry-After": str(decision.reset_in)}
    logger.warning(
        "rate_limit_rejected",
        extra={
            "path": scope.get("path"),
            "method": scope.get("method"),
            "kind": decision.kind,
            "subject": decision.subject,
            "trace_id": body["trace_id"],
        },
    )
    await JSONResponse(status_code=429, content=body, headers=headers)(scope, receive, send)


__all__ = [
    "RateDecision",
    "RateLimiter",
    "RateLimitMiddleware",
    "classify_request",
    "is_exempt",
]
