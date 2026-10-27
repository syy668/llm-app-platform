"""任务粒度 Redis 分布式锁——审批 resume 等 checkpointer 写路径互斥（任务 1）。

为什么需要：resume 的竞态窗口是「两个请求都读到 AWAITING_APPROVAL、都通过
``ensure_resumable``、各自驱动一次图执行」——checkpointer（AsyncRedisSaver）
直写 Redis、绕过 ORM，数据库 version 乐观锁管不到它，必须在服务层入口用
Redis 锁把同一 task_id 的写路径串行化。

实现选型（不引入 aioredlock / 不用 redis-py 的 ``Redis.lock()``）：
- RedLock 是为多主 Redis 集群设计的，本项目单实例 Redis 用不上，徒增依赖；
- ``Redis.lock()`` 的释放走 Lua（``evalsha``），fakeredis 测试环境不支持，
  且会把测试基础设施和业务实现绑死。
因此用普通命令手工实现，语义等价、两端（真实 Redis / fakeredis）通吃：
- **加锁**：``SET key token NX EX ttl`` —— 单命令原子；
- **释放**：``WATCH key`` + 事务内比对 token 后 ``DEL`` —— 乐观事务保证
  「只删自己的锁」，不会误删已过期被他人重新持有的锁；
- **防死锁**：token 含随机串 + TTL 到期自动释放，持锁进程崩溃后锁最多存活
  ``ttl`` 秒；抢锁侧 ``wait`` 超时立即失败返回业务异常，不无限排队。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from uuid import uuid4

from app.core.logging import get_logger
from app.core.w3_errors import TASK_LOCKED, conflict

logger = get_logger(__name__)

# ---------------- 常量 ----------------
TASK_LOCK_PREFIX = "lock:task:"
# 锁 TTL：覆盖一次完整图驱动的合理上界；进程崩溃后最多 5 分钟自愈。
# 注：若单次图驱动超过 TTL，锁会先行过期——此时第二道防线是任务状态已被
# 置 RUNNING，后续 resume 会被 ensure_resumable 的 TASK_NOT_PAUSED 拦截。
TASK_LOCK_TTL_SECONDS = 300
# 抢锁等待窗口：双击/并发审批不该排队，等不到就立即 409。
TASK_LOCK_WAIT_SECONDS = 0.2
# 抢锁自旋间隔
_TASK_LOCK_POLL_SECONDS = 0.05


async def _release(redis: Any, key: str, token: str) -> None:
    """安全释放：WATCH + 事务内比对 token，只删自己持有的锁。"""
    try:
        async with redis.pipeline(transaction=True) as pipe:
            await pipe.watch(key)
            current = await pipe.get(key)
            if current == token:
                pipe.multi()
                pipe.delete(key)
                await pipe.execute()
            else:
                # 锁已过期并被他人持有（或已自然释放），不动它
                await pipe.unwatch()
    except Exception as exc:  # noqa: BLE001 - 释放失败只留痕，不掩盖主流程结果
        logger.warning(
            "task_lock_release_failed",
            extra={"lock_key": key, "error": str(exc)[:200]},
        )


@asynccontextmanager
async def task_lock(
    redis: Any,
    task_id: int | str,
    *,
    ttl: int = TASK_LOCK_TTL_SECONDS,
    wait: float = TASK_LOCK_WAIT_SECONDS,
    message: str = "操作太频繁，请稍后再试",
) -> AsyncIterator[None]:
    """按 task_id 加 Redis 分布式锁；抢不到抛 409 ``TASK_LOCKED``。

    用法::

        async with task_lock(redis, task_id):
            await drive_graph(task_id)

    参数：
        redis: asyncio Redis 客户端（生产为真实 Redis，测试为 fakeredis）；
        task_id: 锁粒度=单任务；
        ttl: 锁自动过期秒数（防死锁兜底）；
        wait: 抢锁最长等待秒数，超时抛冲突异常；
        message: 冲突文案（不同入口可给出更具体的提示，如取消场景）。
    """
    key = f"{TASK_LOCK_PREFIX}{int(task_id)}"
    token = uuid4().hex
    if redis is None:
        # fail-open：锁是「并发互斥」优化，不是业务正确性的前提。
        # Redis 客户端未就绪（如精简部署 / 刚启动未惰性初始化）时，让审批等人工操作照常进行，
        # 只留痕告警——绝不能让「锁拿不到」退化成「审批点了没反应且返回 200」。
        logger.warning(
            "task_lock_skipped_no_redis",
            extra={"task_id": int(task_id), "lock_key": key, "impact": "单实例下无并发互斥保护"},
        )
        yield
        return
    deadline = time.monotonic() + max(wait, 0.0)
    acquired = False
    while True:
        acquired = bool(await redis.set(key, token, nx=True, ex=ttl))
        if acquired or time.monotonic() >= deadline:
            break
        await asyncio.sleep(_TASK_LOCK_POLL_SECONDS)

    if not acquired:
        raise conflict(
            TASK_LOCKED,
            message,
            task_id=int(task_id),
            lock_key=key,
            lock_ttl=ttl,
        )
    try:
        yield
    finally:
        await _release(redis, key, token)


__all__ = [
    "TASK_LOCK_PREFIX",
    "TASK_LOCK_TTL_SECONDS",
    "TASK_LOCK_WAIT_SECONDS",
    "task_lock",
]
