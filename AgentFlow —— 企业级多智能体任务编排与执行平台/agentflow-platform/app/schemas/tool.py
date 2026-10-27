"""工具注册表 Schema（``/tools``）。

字段刻意保持"给人和前端看"的形态：入参用 JSON Schema 子集、出参用「字段名 -> 说明」映射，
而不是把 Pydantic 模型原样吐出来——前端表格能直接渲染，也不需要跟随后端类型变更。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolConfigHint(BaseModel):
    """工具依赖的配置项（敏感值已脱敏）。"""

    key: str
    value: str = ""
    purpose: str = ""
    sensitive: bool = False


class ToolInfo(BaseModel):
    name: str = Field(description="工具名，与 MCP tools/list 返回一致")
    display_name: str
    server: str
    category: str = Field(description="retrieval / database / visualization / notification")
    description: str
    capability: str = Field(description="能力说明：它到底做什么、依赖哪些外部服务")
    risk_level: str = Field(description="low / medium / high（high 会触发人工审批）")
    external: bool = Field(description="是否依赖外部服务（如项目一知识库）")
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    config_hints: list[ToolConfigHint] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    enabled: bool = True
    #: 加固任务 2：熔断器状态（连续失败 5 次 → 冷却 60s 内快速拒绝），只读展示
    circuit_open: bool = False
    updated_at: str | None = None
    updated_by: str | None = None


class ToolListData(BaseModel):
    items: list[ToolInfo] = Field(default_factory=list)
    total: int = 0
    enabled: int = 0
    disabled: int = 0
    generated_at: str = ""
    audit: list[dict[str, Any]] = Field(default_factory=list, description="最近变更记录（最多 20 条）")
    #: 缺陷 D-02（P2 观察项）的**显式提示位**，不改变任何运行时行为：
    #: 工具被全部禁用时 ``filter_enabled_tools`` 会 fail-open 退回全量工具（安全兜底，有意设计），
    #: 这里只是把该状态如实回传，让运维与前端界面不再误判为"已全部禁用"。
    fail_open: bool = Field(
        default=False,
        description="true = 当前全部工具均被禁用，运行时已自动回退为全量工具（并非真的无工具可用）",
    )


class ToolToggleRequest(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = Field(description="true=启用，false=禁用")
    reason: str = Field(default="", max_length=200, description="变更原因（写审计）")


__all__ = ["ToolConfigHint", "ToolInfo", "ToolListData", "ToolToggleRequest"]
