"""上下文工程 Schema：Token 记账 / 压缩配置 / 工具结果瘦身配置（PRD FR-010、第 9 章）。

这些模型只描述「度量与策略」，不包含任何 LLM 调用；压缩执行在
``services/context_service.py``，节点只消费其返回值并写入 ``token_ledger``。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NodeName = Literal["planner", "executor", "reviewer", "summarizer", "replan", "notifier"]


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class TokenEstimate(ContextModel):
    """单次提示构造的分组件预估（PRD 9.1 构成拆分）。"""

    components: dict[str, int] = Field(default_factory=dict, description="组件名 -> 预估 Token")
    total: int = 0
    budget: int = 0
    ratio: float = 0.0
    over_threshold: bool = False

    @classmethod
    def of(cls, components: dict[str, int], budget: int, trigger_ratio: float) -> "TokenEstimate":
        total = sum(components.values())
        ratio = round(total / budget, 4) if budget > 0 else 0.0
        return cls(
            components=components,
            total=total,
            budget=budget,
            ratio=ratio,
            over_threshold=ratio > trigger_ratio,
        )


class CompressionConfig(ContextModel):
    """压缩策略（PRD 9.2）：触发阈值 / 保留轮数 / 最大层数。"""

    enabled: bool = True
    trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    message_rounds_threshold: int = 12
    keep_recent_rounds: int = 3
    max_layers: int = 2
    # 模板摘要必须保留的关键数值字段（白名单，防信息塌缩，PRD R-5）
    numeric_field_whitelist: list[str] = Field(
        default_factory=lambda: ["gmv", "amount", "conv_rate", "budget", "cost", "roas", "qty", "环比", "同比"]
    )


class SlimmingConfig(ContextModel):
    """工具结果瘦身策略（PRD 9.3）。"""

    # sql-query：>50 行时保留前 20 行 + 统计行
    sql_threshold_rows: int = 50
    sql_preview_rows: int = 20
    sql_float_digits: int = 2
    # kb-search：top-3、每条 300 字
    kb_top_k: int = 3
    kb_content_chars: int = 300
    # 各工具进上下文的字段白名单
    chart_fields: list[str] = Field(default_factory=lambda: ["chart_id", "image_url", "title"])
    notify_fields: list[str] = Field(default_factory=lambda: ["message_id", "status"])
    # 单个结果进上下文的硬上限（字符近似 ≈ Token×2）
    max_digest_chars: int = 4000
    max_kb_chunks: int = 3


class SlimResult(ContextModel):
    """瘦身结果：``digest`` 进上下文，``raw`` 仅落库 / trace。"""

    tool: str
    digest: str
    payload: dict[str, Any] = Field(default_factory=dict)
    before_tokens: int = 0
    after_tokens: int = 0
    truncated: bool = False
    dropped_fields: list[str] = Field(default_factory=list)

    @property
    def saved_tokens(self) -> int:
        return max(self.before_tokens - self.after_tokens, 0)


class ContextBuildResult(ContextModel):
    """一次上下文构造的结果（写入 state.context_meta 与 token_ledger）。"""

    node: str
    role: str = ""
    content: str = ""
    budget: int = 0
    estimate: TokenEstimate = Field(default_factory=TokenEstimate)
    compressed: bool = False
    compress_layers: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    kept_rounds: int = 0
    history_digest: str = Field(default="", description="压缩后的历史摘要（供节点拼进提示）")


class CompressionResult(ContextModel):
    digest: str = ""
    compressed: bool = False
    layers: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    kept_rounds: int = 0
    preserved_numbers: list[str] = Field(default_factory=list)


__all__ = [
    "NodeName",
    "TokenEstimate",
    "CompressionConfig",
    "SlimmingConfig",
    "SlimResult",
    "ContextBuildResult",
    "CompressionResult",
]
