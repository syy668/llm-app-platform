"""任务终态 Redis 清理（加固任务 4）：任务安全落库后主动回收 Redis 临时产物。

为什么放在服务层旁路（而不是事件/定时器）：清理的**前置条件是 MySQL 持久化成功**
——落库失败的清理等于丢数据。``_finalize``/``_apply_cancel`` 在事务 commit 成功之后
调用本模块，天然满足时序；调用失败（Redis 抖动）只留 warning，不影响任务结果。

清理范围与明确不清理的范围：

- **清理**：checkpointer 状态（``adelete_thread``，thread_id=``str(task_id)``）、
  取消信号 key（``CancelSignal.clear``）。这两类在任务终态后永久失去用途，
  不清理就是"长生不老"key（checkpointer 虽有 72h TTL，但终态任务占着是纯浪费）。
- **不清理**：SSE 事件流（``stream:{task_id}``）——任务终态后客户端可能仍在
  消费收尾事件，立即删除会掐断前端；它自身带 ``STREAM_TTL`` 且每次写入刷新，
  不会泄漏。
- **不清理**：工具注册表/审计（人工维护的持久配置）、限流与日配额计数
  （自带窗口/日界 TTL）。

幂等：对不存在/已删除的 key 重复执行无副作用，可安全重试。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


async def _delete_checkpointer_thread(checkpointer: Any, task_id: int | str) -> bool:
    """删除 checkpointer 的任务快照线，返回是否找到可用的删除 API。

    兼容三类实现：
    - ``AsyncRedisSaver.adelete_thread``（生产 Redis 后端，优先）；
    - ``MemorySaver.delete_thread``（同步 API，测试/降级后端）；
    - 都没有（旧版 saver）→ 返回 False，交给 TTL 兜底，不报错。
    thread_id 口径与 ``CheckpointService.config`` 一致：``str(task_id)``。
    """
    thread_id = str(task_id)
    for name in ("adelete_thread", "delete_thread"):
        deleter = getattr(checkpointer, name, None)
        if callable(deleter):
            outcome = deleter(thread_id)
            if name.startswith("a"):
                await outcome
            return True
    return False


async def cleanup_task_artifacts(
    task_id: int | str,
    *,
    checkpointer: Any = None,
    cancel_signal: Any = None,
) -> dict[str, bool]:
    """终态清理入口（fail-open：任何一步失败只留日志，绝不抛给调用方）。

    返回各步骤执行结果（``{"checkpointer": bool, "cancel_signal": bool}``），
    True = 已执行删除动作（供日志与测试断言），False = 未执行（无 API / 出错）。
    """
    result = {"checkpointer": False, "cancel_signal": False}

    if checkpointer is not None:
        try:
            result["checkpointer"] = await _delete_checkpointer_thread(checkpointer, task_id)
        except Exception as exc:  # noqa: BLE001 - 清理失败不掩盖任务成功
            logger.warning(
                "task_cleanup_checkpointer_failed",
                extra={"task_id": str(task_id), "error": str(exc)[:200]},
            )

    if cancel_signal is not None:
        try:
            await cancel_signal.clear(task_id)
            result["cancel_signal"] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "task_cleanup_cancel_signal_failed",
                extra={"task_id": str(task_id), "error": str(exc)[:200]},
            )

    if any(result.values()):
        logger.info("task_artifacts_cleaned", extra={"task_id": str(task_id), **result})
    return result


__all__ = ["cleanup_task_artifacts"]
