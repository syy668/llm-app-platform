"""工具级熔断器（加固任务 2）：同一工具连续失败 N 次 → 冷却 M 秒内快速拒绝。

为什么放 Redis 而不是进程内计数：Executor 是进程内单例的一部分，但熔断的意义
在于跨请求/跨任务累积观察故障——进程内字典做不到跨任务共享，也做不到冷却到期
自动恢复（要靠 TTL）。Redis key 自带 EXPIRE，语义正好。

key 设计（都走 ``settings.key`` 统一前缀，与工具注册表 ``tool:registry`` 同族）：
- ``{prefix}:tool:circuit:{tool}``   → 熔断中标记（EX=冷却秒数，到期自动闭合）
- ``{prefix}:tool:circuit_fail:{tool}`` → 连续失败计数（EX 刷新，成功即清零）

fail-open 原则（与 tool_registry 一致）：Redis 抖动/不可用时**不熔断**——熔断器
是保护手段，绝不能因为保护机制自身故障而阻塞业务调用。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.w2_config import w2

logger = get_logger(__name__)

CIRCUIT_OPEN_PREFIX = "tool:circuit"
CIRCUIT_FAIL_PREFIX = "tool:circuit_fail"


class ToolCircuitBreaker:
    """基于 Redis 计数的工具熔断器（连续失败阈值 + 冷却窗口）。"""

    def __init__(
        self,
        redis: Any,
        *,
        failure_threshold: int | None = None,
        cooldown_seconds: int | None = None,
    ) -> None:
        self.redis = redis
        self.threshold = int(failure_threshold or w2.TOOL_CIRCUIT_FAILURE_THRESHOLD)
        self.cooldown = int(cooldown_seconds or w2.TOOL_CIRCUIT_COOLDOWN_SECONDS)

    # ---------------- key ----------------
    def _open_key(self, tool: str) -> str:
        return settings.key(CIRCUIT_OPEN_PREFIX, tool)

    def _fail_key(self, tool: str) -> str:
        return settings.key(CIRCUIT_FAIL_PREFIX, tool)

    # ---------------- 查询 ----------------
    async def is_open(self, tool: str) -> bool:
        """熔断中返回 True（冷却到期后 key 自动过期 → 自动闭合）。"""
        try:
            return bool(await self.redis.get(self._open_key(tool)))
        except Exception as exc:  # noqa: BLE001 - fail-open：保护机制故障不阻塞业务
            logger.warning("circuit_breaker_check_failed", extra={"tool": tool, "error": str(exc)[:200]})
            return False

    # ---------------- 记录 ----------------
    async def record_success(self, tool: str) -> None:
        """成功调用清零连续失败计数。"""
        try:
            await self.redis.delete(self._fail_key(tool))
        except Exception as exc:  # noqa: BLE001
            logger.warning("circuit_breaker_success_record_failed", extra={"tool": tool, "error": str(exc)[:200]})

    async def record_failure(self, tool: str) -> bool:
        """记一次失败；达到阈值则打开熔断（写标记 + 冷却 TTL）。

        返回 True 表示本次失败触发了熔断（供日志/事件使用）。
        """
        try:
            count = int(await self.redis.incr(self._fail_key(tool)) or 0)
            await self.redis.expire(self._fail_key(tool), self.cooldown)
            if count >= self.threshold:
                await self.redis.set(self._open_key(tool), "open", ex=self.cooldown)
                await self.redis.delete(self._fail_key(tool))
                logger.warning(
                    "tool_circuit_opened",
                    extra={"tool": tool, "failures": count, "cooldown_seconds": self.cooldown},
                )
                return True
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("circuit_breaker_failure_record_failed", extra={"tool": tool, "error": str(exc)[:200]})
            return False


__all__ = ["ToolCircuitBreaker", "CIRCUIT_OPEN_PREFIX", "CIRCUIT_FAIL_PREFIX"]
