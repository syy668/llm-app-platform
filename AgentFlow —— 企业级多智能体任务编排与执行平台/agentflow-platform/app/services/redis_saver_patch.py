"""修正 langgraph-checkpoint-redis 0.0.8 的 checkpoint key 错位缺陷。

缺陷现象（RedisSaver 独有，InMemorySaver 无）：
    ``AsyncRedisSaver.aput`` 取 ``config["configurable"]["checkpoint_id"]``（**父快照** id）
    作为新快照的 Redis key、顶层 ``checkpoint_id`` 字段与返回 config，而本次快照的真实 id
    是 ``checkpoint["id"]``。后果是每次保存都覆盖上一条快照，并且：

    1. ``checkpoint_write:<thread>:<ns>:<新 id>:<task>:*`` 这些 pending write（含
       ``channel="__interrupt__"`` 的挂起信息）找不到对应的 checkpoint；
    2. ``aget_state()`` 组装的 ``tasks[].interrupts`` 因此恒为空；
    3. ``task_service`` 用 ``pending_interrupts()`` 判定是否停在 human_gate，探测为空
       → 任务被误判 FAILED，审批抽屉拿不到 preview；
    4. 后端重启后 resume 也会因为断点错位而失败。

修法：调用原实现前，把 config 里的 ``checkpoint_id`` 换成 ``checkpoint["id"]``。
这样 key / 顶层字段 / 返回 config 都指向本次快照，父 id 仍由原实现按自身逻辑写入
（该版本原本就写成自身，属库内另一处无关缺陷，不影响本项目主链路）。

升级 ``langgraph-checkpoint-redis`` 到 >= 0.1 后可删除本模块及 ``install_aput_fix`` 调用。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

_installed = False


def install_aput_fix() -> bool:
    """给 ``AsyncRedisSaver.aput`` 打上 key 错位补丁（进程内幂等）。"""
    global _installed
    if _installed:
        return True
    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver
    except Exception:  # noqa: BLE001 - 未安装 redis 版 checkpointer 时静默跳过
        return False

    if getattr(AsyncRedisSaver.aput, "__agentflow_fixed__", False):
        _installed = True
        return True

    original = AsyncRedisSaver.aput

    async def aput(  # type: ignore[no-untyped-def]
        self: Any,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: Any,
        stream_mode: str = "values",
    ) -> Any:
        checkpoint_id = (checkpoint or {}).get("id")
        if not checkpoint_id:
            return await original(self, config, checkpoint, metadata, new_versions, stream_mode)
        patched = dict(config or {})
        patched["configurable"] = dict(patched.get("configurable") or {})
        # 用本次快照 id 覆盖父 id；thread_ts 一并对齐，避免旧值参与 key 计算
        patched["configurable"]["checkpoint_id"] = checkpoint_id
        patched["configurable"].pop("thread_ts", None)
        return await original(self, patched, checkpoint, metadata, new_versions, stream_mode)

    aput.__agentflow_fixed__ = True  # type: ignore[attr-defined]
    aput.__wrapped__ = original  # type: ignore[attr-defined]
    AsyncRedisSaver.aput = aput  # type: ignore[method-assign]
    _installed = True
    logger.info("redis_saver_aput_patched", extra={"reason": "checkpoint_key_mismatch_0.0.8"})
    return True


__all__ = ["install_aput_fix"]
