"""知识库工具自检相关 schema（``GET /kb/status``）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class KbProbeChunk(BaseModel):
    """探测命中的片段摘要（原文只保留前 120 字，便于确认命中的是真实周报）。"""

    doc_id: str
    title: str = ""
    score: float = 0.0
    source: str = ""
    excerpt: str = ""


class KbStatusData(BaseModel):
    """知识库工具当前配置与（可选）真实探测结果。"""

    mock: bool = Field(description="MOCK_EXTERNAL_RAG 开关：true 时 kb-search 返回内置语料")
    contract: str = Field(description="生效的检索契约")
    base_url: str = Field(description="项目一后端根地址")
    kb_id: int = Field(description="目标知识库 ID")
    auth_mode: str = Field(description="static_token / service_account / not_configured")
    tenant: str = ""
    timeout_seconds: int = 8
    breaker: dict[str, Any] = Field(default_factory=dict, description="熔断器快照")
    probe: dict[str, Any] | None = Field(default=None, description="probe=true 时的探测结果")
    samples: list[KbProbeChunk] = Field(default_factory=list)


__all__ = ["KbProbeChunk", "KbStatusData"]
