"""断点快照服务（占位）：ckpt:{task_id}，TTL 72h。

W1 边界：不写快照、不做恢复；LangGraph RedisSaver 仅在 db/checkpointer.py 做导入预留。
交付计划：W3 交付（FR-012）。
"""

from __future__ import annotations

from app.core.config import settings


def checkpoint_key(task_id: int | str) -> str:
    """快照 key 约定（PRD 5.2）：agentflow:ckpt:{task_id}。"""
    return settings.key("ckpt", str(task_id))


class CheckpointStore:
    """W3 交付。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("W1 仅骨架：CheckpointStore 在 W3 实现（PRD 里程碑 W3）")
