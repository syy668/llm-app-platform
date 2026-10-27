"""分层记忆服务（占位）。

W1 边界：不读写 memories 表、不接 Chroma。
交付计划：W4 交付（FR-014）。
"""

from __future__ import annotations


class MemoryService:
    """W4 交付。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("W1 仅骨架：MemoryService 在 W4 实现（PRD 里程碑 W4）")
