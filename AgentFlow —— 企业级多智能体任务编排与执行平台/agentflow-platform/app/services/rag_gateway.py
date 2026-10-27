"""项目一 RAG 检索网关（PRD FR-007 / A-01）。

与 kb-search MCP Server 的关系：Server 负责协议与降级语义，本网关是**服务层门面**，
供后续（W4 记忆检索、直接服务调用）复用，保证"接口变更只改适配层"（PRD R-7）。

Mock 开关 ``MOCK_EXTERNAL_RAG``：true → 返回内置 W41 周报等预置片段；false → HTTP 调用项目一。
"""

from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.mcp import KbSearchOutput
from app.tools.mcp_servers.kb_search import HttpKbProvider, KbProvider, MockKbProvider

logger = get_logger(__name__)


def is_mock() -> bool:
    return settings.MOCK_EXTERNAL_RAG


class RagGateway:
    """检索门面：统一返回 ``KbSearchOutput``，失败降级不抛异常。"""

    def __init__(self, provider: KbProvider | None = None) -> None:
        if provider is not None:
            self.provider = provider
        else:
            self.provider = MockKbProvider() if is_mock() else HttpKbProvider()

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        tenant: str | None = None,
    ) -> KbSearchOutput:
        top = max(1, min(top_k or w2.KB_DEFAULT_TOP_K, w2.KB_MAX_TOP_K))
        return await self.provider.search(query[: w2.KB_MAX_QUERY_LEN], top, tenant or w2.KB_TENANT)


def get_rag_gateway() -> RagGateway:
    return RagGateway()


__all__ = ["is_mock", "RagGateway", "get_rag_gateway"]
