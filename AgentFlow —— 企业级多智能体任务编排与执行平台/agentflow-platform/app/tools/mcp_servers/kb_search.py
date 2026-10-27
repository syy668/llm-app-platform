"""MCP Server：kb-search（PRD FR-007）。

两条路径由 ``MOCK_EXTERNAL_RAG`` 开关分流：
- ``true``（默认）：返回内置 Mock chunks，其中 **W41 周报「风险与异常」段**内含
  "数码配件爆款 SKU-10086 库存周转天数升至 21 天，存在断货风险"，
  用于支撑 demo 的"断货 → 转化率下跌"归因链条（PRD 11.3 埋因）；
- ``false``：HTTP 调用项目一 ``POST /api/v1/kb/search``，超时 8s、失败重试 2 次（1s/2s）。

降级策略（FR-007）：重试穷尽后返回 ``degraded=true, chunks=[]``，**不抛异常**，
由 Executor 标记"引用缺失"继续执行，绝不阻塞主流程。
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.mcp import KbChunk, KbSearchInput, KbSearchOutput, McpErrorCode
from app.tools.mcp_stdio import FunctionTool, MCPServerApp, ToolError, run_server

logger = get_logger(__name__)

SERVER_NAME = "kb-search"
TOOL_NAME = "kb-search"


# ---------------------------------------------------------------------------
# Mock 语料（20 份周报中的关键样本，命中优先级按关键词覆盖率）
# ---------------------------------------------------------------------------
MOCK_CHUNKS: list[dict[str, Any]] = [
    {
        "doc_id": "kb-w41-001",
        "title": "W41 电商经营周报｜风险与异常",
        "content": (
            "数码配件爆款 SKU-10086 库存周转天数由 9 天升至 21 天，存在断货风险，已同步供应链；"
            "该 SKU 贡献数码配件品类 31% 自然流量，若断货将导致流量向低转化长尾 SKU 分散。"
        ),
        "score": 0.93,
        "source": "kb:weekly_report:W41",
        "keywords": ["库存", "断货", "sku-10086", "数码配件", "风险", "周转", "供应链", "周报"],
    },
    {
        "doc_id": "kb-w41-002",
        "title": "W41 电商经营周报｜经营概览",
        "content": (
            "W41（10-05~10-11）全店 GMV 3,842 万元，环比 +1.2%；数码配件品类 GMV 占比 27%，"
            "客单价 268 元，转化率 3.1%，为近 8 周峰值。"
        ),
        "score": 0.81,
        "source": "kb:weekly_report:W41",
        "keywords": ["gmv", "经营概览", "数码配件", "转化率", "周报", "客单价"],
    },
    {
        "doc_id": "kb-w42-001",
        "title": "W42 电商经营周报｜品类明细",
        "content": (
            "W42 数码配件品类 GMV 环比下滑 18%，转化率降至 1.7%，主因爆款 SKU 断货后流量结构恶化；"
            "其余 7 个品类环比波动均在 ±3% 以内。"
        ),
        "score": 0.88,
        "source": "kb:weekly_report:W42",
        "keywords": ["gmv", "下滑", "转化率", "数码配件", "品类", "周报", "环比"],
    },
    {
        "doc_id": "kb-w40-003",
        "title": "W40 电商经营周报｜投放复盘",
        "content": (
            "数码配件品类 W41 起日预算由 12,000 元下调至 7,200 元（-40%），付费流量预计减少约 24%，"
            "建议同步评估自然流量承接能力。"
        ),
        "score": 0.76,
        "source": "kb:weekly_report:W40",
        "keywords": ["投放", "预算", "付费流量", "数码配件", "复盘", "roas"],
    },
]


class KbProvider(ABC):
    @abstractmethod
    async def search(self, query: str, top_k: int, tenant: str) -> KbSearchOutput: ...


class MockKbProvider(KbProvider):
    """预置语料检索：按关键词覆盖率排序，保证 demo 可复现。"""

    def __init__(self, chunks: list[dict[str, Any]] | None = None) -> None:
        self.chunks = chunks or MOCK_CHUNKS

    async def search(self, query: str, top_k: int, tenant: str) -> KbSearchOutput:
        started = time.perf_counter()
        lowered = query.lower()
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in self.chunks:
            hits = sum(1 for kw in chunk["keywords"] if kw.lower() in lowered)
            if hits == 0:
                continue
            score = min(0.99, chunk["score"] + 0.02 * hits)
            scored.append((score, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        selected = scored[:top_k] or [(c["score"], c) for c in self.chunks[:top_k]]
        chunks = [
            KbChunk(
                doc_id=c["doc_id"],
                title=c["title"],
                content=c["content"][:300],
                score=round(score, 4),
                source=c["source"],
            )
            for score, c in selected
        ]
        return KbSearchOutput(
            chunks=chunks,
            latency_ms=int((time.perf_counter() - started) * 1000),
            degraded=False,
        )


class HttpKbProvider(KbProvider):
    """项目一 RAG 适配（A-01）：POST /api/v1/kb/search。"""

    def __init__(self, *, base_url: str | None = None, token: str | None = None, retry_times: int | None = None) -> None:
        self.base_url = (base_url or settings.EXTERNAL_RAG_BASE_URL).rstrip("/")
        self.token = token if token is not None else settings.EXTERNAL_RAG_TOKEN
        self.retry_times = w2.KB_RETRY_TIMES if retry_times is None else retry_times

    async def search(self, query: str, top_k: int, tenant: str) -> KbSearchOutput:
        started = time.perf_counter()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {"tenant_id": tenant, "query": query, "top_k": top_k}
        last_error = McpErrorCode.KB_UNAVAILABLE

        for attempt in range(self.retry_times + 1):
            try:
                async with httpx.AsyncClient(timeout=settings.EXTERNAL_RAG_TIMEOUT_SECONDS) as client:
                    response = await client.post(f"{self.base_url}/api/v1/kb/search", json=payload, headers=headers)
                if response.status_code in (401, 403):
                    last_error = McpErrorCode.KB_AUTH_FAILED
                    break
                response.raise_for_status()
                body = response.json()
                raw_chunks = (body.get("data") or {}).get("chunks") or body.get("chunks") or []
                chunks = [
                    KbChunk(
                        doc_id=str(item.get("doc_id") or item.get("id") or ""),
                        title=str(item.get("title") or ""),
                        content=str(item.get("content") or "")[:300],
                        score=float(item.get("score") or 0.0),
                        source=str(item.get("source") or "project1"),
                    )
                    for item in raw_chunks[:top_k]
                ]
                return KbSearchOutput(
                    chunks=chunks, latency_ms=int((time.perf_counter() - started) * 1000), degraded=False
                )
            except (httpx.TimeoutException, asyncio.TimeoutError):
                last_error = McpErrorCode.KB_TIMEOUT
            except httpx.HTTPError as exc:
                last_error = McpErrorCode.KB_UNAVAILABLE
                logger.warning("kb_search_http_error", extra={"attempt": attempt + 1, "error": str(exc)[:200]})
            if attempt < self.retry_times:
                await asyncio.sleep(1.0 * (attempt + 1))  # 退避 1s / 2s

        logger.warning("kb_search_degraded", extra={"error_code": last_error.value, "query": query[:60]})
        return KbSearchOutput(
            chunks=[],
            latency_ms=int((time.perf_counter() - started) * 1000),
            degraded=True,
            error_code=last_error.value,
        )


class KbSearchTool:
    name = TOOL_NAME
    description = (
        "检索企业知识库（历史电商经营周报），返回最相关的 1~5 条片段与得分。"
        "用于为归因结论补充历史证据（如库存风险、投放调整）。"
    )

    def __init__(self, provider: KbProvider | None = None) -> None:
        if provider is not None:
            self.provider = provider
            return
        # W4 适配层（仅替换 provider 选择，MCP 协议与工具签名不变）：
        # MOCK_EXTERNAL_RAG=true → 原样 MockKbProvider；false → HTTP + 熔断 + mall 只读库兜底。
        # 延迟 import 避免 kb_adapter ↔ kb_search 循环依赖。
        from app.tools.kb_adapter import build_kb_provider

        self.provider = build_kb_provider()

    async def __call__(self, args: KbSearchInput) -> dict[str, Any]:
        if len(args.query) > w2.KB_MAX_QUERY_LEN:
            raise ToolError(McpErrorCode.KB_UNAVAILABLE, f"query 超长（>{w2.KB_MAX_QUERY_LEN} 字）")
        top_k = max(1, min(args.top_k, w2.KB_MAX_TOP_K))
        result = await self.provider.search(args.query, top_k, args.tenant or w2.KB_TENANT)
        return result.model_dump()


def build_app(provider: KbProvider | None = None) -> MCPServerApp:
    return MCPServerApp(
        SERVER_NAME,
        [
            FunctionTool(
                name=TOOL_NAME,
                description=KbSearchTool.description,
                input_model=KbSearchInput,
                handler=KbSearchTool(provider),
            )
        ],
    )


def main() -> None:  # pragma: no cover - 子进程入口
    run_server(build_app())


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "SERVER_NAME",
    "TOOL_NAME",
    "MOCK_CHUNKS",
    "KbProvider",
    "MockKbProvider",
    "HttpKbProvider",
    "KbSearchTool",
    "build_app",
    "main",
]
