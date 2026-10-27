"""checkpoint 服务（硬性约束 4）：RedisSaver 作为 checkpointer，TTL=72h。

策略（``CHECKPOINTER_BACKEND``）：
- ``redis``：强制 RedisSaver，失败直接抛错（生产排障时用）；
- ``auto``（默认）：先试 AsyncRedisSaver；Redis 不可用或缺少 RedisJSON/RediSearch 模块时，
  自动降级 ``InMemorySaver`` 并打印告警（本地开发 / CI 无需起 Redis Stack）；
- ``memory``：纯内存（单测）。

TTL：``LANGGRAPH_CHECKPOINT_TTL_SECONDS``（默认 259200 = 72h），
RedisSaver 的 ttl 配置以**分钟**为单位，这里统一换算，避免两套单位混用。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from app.core.config import settings
from app.core.logging import get_logger
from app.core.w2_config import w2
from app.utils.ids import new_trace_id

logger = get_logger(__name__)

_saver: Any = None
_lock = asyncio.Lock()


def _memory_saver() -> Any:
    try:  # langgraph >= 0.3
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    except Exception:  # pragma: no cover - 旧版本回退
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()


def ttl_config() -> dict[str, Any]:
    """RedisSaver TTL 配置（分钟）。"""
    minutes = max(1, int(settings.LANGGRAPH_CHECKPOINT_TTL_SECONDS // 60))
    return {"default_ttl": minutes, "refresh_on_read": True}


async def _build_redis_saver() -> Any:
    from redis.asyncio import Redis

    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    # 必须在 saver 真正保存快照之前打补丁：0.0.8 的 aput 用父快照 id 当 key，
    # 会让快照互相覆盖、interrupt 挂起信息找不到归属（详见 redis_saver_patch 模块注释）。
    from app.services.redis_saver_patch import install_aput_fix

    install_aput_fix()

    client = Redis.from_url(settings.redis_url, decode_responses=False)
    try:
        await client.ping()
        saver = AsyncRedisSaver(redis_client=client, ttl=ttl_config())
        await saver.asetup()
        logger.info(
            "checkpointer_redis_ready",
            extra={"ttl_minutes": ttl_config()["default_ttl"], "ttl_hours": settings.LANGGRAPH_CHECKPOINT_TTL_SECONDS // 3600},
        )
        return saver
    except Exception:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
        raise


async def get_checkpointer() -> Any:
    """进程内单例；并发首次调用只构建一次。"""
    global _saver
    if _saver is not None:
        return _saver
    async with _lock:
        if _saver is not None:
            return _saver
        backend = w2.CHECKPOINTER_BACKEND
        if backend == "memory":
            _saver = _memory_saver()
            logger.info("checkpointer_memory", extra={"reason": "explicit_config"})
            return _saver

        try:
            _saver = await _build_redis_saver()
        except Exception as exc:  # noqa: BLE001
            if backend == "redis":
                raise
            logger.warning("checkpointer_redis_unavailable_fallback_memory", extra={"error": str(exc)[:200]})
            _saver = _memory_saver()
        return _saver


async def reset_checkpointer() -> None:
    """测试 / 热重启用：关闭并清空单例。"""
    global _saver
    if _saver is None:
        return
    closer = getattr(_saver, "aclose", None)
    if closer is not None:
        try:
            await closer()
        except Exception:  # noqa: BLE001
            pass
    _saver = None


class CheckpointService:
    """对已编译 graph 的 save / restore 封装（断点恢复 FR-012 的基础能力）。"""

    def __init__(self, graph: Any = None) -> None:
        self.graph = graph

    @staticmethod
    def config(task_id: int | str, *, trace_id: str | None = None) -> dict[str, Any]:
        """LangGraph 运行配置：thread_id = task_id（一个任务一条快照线）。"""
        return {
            "configurable": {"thread_id": str(task_id)},
            "metadata": {"task_id": str(task_id), "trace_id": trace_id or new_trace_id()},
        }

    def _require_graph(self) -> Any:
        if self.graph is None:
            raise RuntimeError("CheckpointService 未绑定已编译的 graph")
        return self.graph

    async def save(self, task_id: int | str, values: dict[str, Any], *, as_node: str | None = None) -> None:
        """手动写入快照（一般由 LangGraph 自动完成，此处供人工修正 / 测试）。"""
        graph = self._require_graph()
        await graph.aupdate_state(self.config(task_id), values, as_node=as_node)

    async def restore(self, task_id: int | str) -> dict[str, Any]:
        """读取最近快照的 state values；无快照返回空 dict。"""
        graph = self._require_graph()
        snapshot = await graph.aget_state(self.config(task_id))
        return dict(snapshot.values or {})

    async def snapshot(self, task_id: int | str) -> Any:
        graph = self._require_graph()
        return await graph.aget_state(self.config(task_id))

    async def next_nodes(self, task_id: int | str) -> tuple[str, ...]:
        graph = self._require_graph()
        snapshot = await graph.aget_state(self.config(task_id))
        return tuple(snapshot.next or ())


def make_resumer(graph: Any) -> Callable[[int | str, dict[str, Any]], Any]:
    """构造恢复函数（W3 的 POST /tasks/{id}/resume 直接复用）。"""
    from langgraph.types import Command

    async def resume(task_id: int | str, decision: dict[str, Any]) -> dict[str, Any]:
        config = CheckpointService.config(task_id)
        result = await graph.ainvoke(Command(resume=decision), config=config)
        return dict(result)

    return resume


__all__ = [
    "get_checkpointer",
    "reset_checkpointer",
    "ttl_config",
    "CheckpointService",
    "make_resumer",
]
