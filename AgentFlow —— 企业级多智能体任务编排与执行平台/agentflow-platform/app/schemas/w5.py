"""W5-Part1 入参 / 出参 Schema（分层记忆管理 API）。

刻意与 W1~W4 的 schema 文件分开：新增字段只在本文件演化，避免碰到基线文件的 Pydantic 模型。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class W5Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class MemoryCreateRequest(W5Model):
    """手工新增记忆（短期层由任务会话自动维护，不允许手写）。"""

    scope: Literal["mid", "long"] = Field(default="long", description="mid=中期（30 天）/ long=长期（永久）")
    content: str = Field(min_length=1, max_length=4000, description="记忆正文")
    task_id: int | None = Field(default=None, ge=1, description="关联任务（可空）")
    importance: int = Field(default=3, ge=1, le=5, description="1~5，long 层强制 >=3")


class MemoryRecallRequest(W5Model):
    """相似度召回（演示 / 调试用）：goal → 命中的长期记忆片段。"""

    goal: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=3, ge=1, le=3, description="PlannerInput.memory_hits 上限为 3")


__all__ = ["W5Model", "MemoryCreateRequest", "MemoryRecallRequest"]
