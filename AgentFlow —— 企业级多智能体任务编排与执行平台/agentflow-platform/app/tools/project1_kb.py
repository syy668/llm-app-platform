"""项目一知识库真实检索适配（内置工具 kb-search 的 HTTP 数据源）。

背景：PRD 的假设 A-01 认为项目一暴露 ``POST /api/v1/kb/search``，但项目一实际
**没有**该端点（实测 404 ``Cannot POST /api/v1/kb/search``）。项目一真正可用的
检索入口是：

    POST /api/v1/kbs/{kb_id}/query
    body: {"question": "自然语言问题", "top_k": 1~50}
    resp: {"code": 0, "message": "ok",
           "data": {"answer": "LLM 生成的回答",
                    "sources": [{"chunk_id": 1, "document_id": 3,
                                 "document_title": "W41 电商经营周报",
                                 "chunk_index": 12, "content": "...", "score": 0.0163}]}}

该端点内部执行「BM25 + 向量 RRF 融合 + 可选 Rerank」后返回 ``sources``，
本适配器只取 ``sources`` 作为证据片段（``answer`` 丢弃——AgentFlow 有自己的
Planner/Summarizer，不需要项目一再生成一遍答案）。

鉴权（按 PRD 要求使用服务账号）：
- 优先 ``EXTERNAL_RAG_TOKEN`` 静态令牌；
- 未配置时用 ``EXTERNAL_RAG_USERNAME`` / ``EXTERNAL_RAG_PASSWORD`` 调
  ``POST /api/v1/auth/login`` 换 ``access_token``，进程内缓存
  ``EXTERNAL_RAG_TOKEN_TTL_SECONDS`` 秒；收到 401 时立刻失效缓存并重登一次。

分值口径：项目一返回的 ``score`` 是 RRF 融合分（量纲远小于 1，如 0.0163），
**原样保留**，不做任何放大，避免下游把"编造的相关度"当成真实指标。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.mcp import KbChunk, KbSearchOutput, McpErrorCode
from app.tools.mcp_servers.kb_search import KbProvider

logger = get_logger(__name__)

#: 单条片段落库前的截断长度（KbChunk.content 上限 1200，留出装配余量）
_CONTENT_LIMIT = 600


class ProjectOneAuthError(RuntimeError):
    """项目一鉴权失败（账号密码错误 / 令牌过期且重登失败）。"""


class ProjectOneKbProvider(KbProvider):
    """项目一 ``POST /api/v1/kbs/{kb_id}/query`` 适配器。"""

    contract = "POST /api/v1/kbs/{kb_id}/query"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        kb_id: int | None = None,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float | None = None,
        token_ttl: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.EXTERNAL_RAG_BASE_URL).rstrip("/")
        self.kb_id = int(kb_id if kb_id is not None else settings.EXTERNAL_RAG_KB_ID)
        self._static_token = token if token is not None else settings.EXTERNAL_RAG_TOKEN
        self.username = username if username is not None else settings.EXTERNAL_RAG_USERNAME
        self.password = password if password is not None else settings.EXTERNAL_RAG_PASSWORD
        self.timeout = float(timeout or settings.EXTERNAL_RAG_TIMEOUT_SECONDS)
        self.token_ttl = int(token_ttl if token_ttl is not None else settings.EXTERNAL_RAG_TOKEN_TTL_SECONDS)
        self._token: str | None = None
        self._token_deadline: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        """是否具备调用条件（有静态令牌，或有账号密码）。"""
        return bool(self._static_token or (self.username and self.password))

    async def _login(self) -> str:
        if not (self.username and self.password):
            raise ProjectOneAuthError("未配置 EXTERNAL_RAG_TOKEN，也未配置服务账号账号密码")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/v1/auth/login",
                json={"username": self.username, "password": self.password},
            )
        if response.status_code in (401, 403):
            raise ProjectOneAuthError(f"项目一登录失败：{response.text[:200]}")
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        token = str((data or {}).get("access_token") or "")
        if not token:
            raise ProjectOneAuthError("项目一登录响应缺少 access_token")
        logger.info("project1_kb_login_ok", extra={"base_url": self.base_url, "kb_id": self.kb_id})
        return token

    async def _ensure_token(self, *, force: bool = False) -> str | None:
        """返回可用令牌；无任何凭据时返回 None（调用方按匿名请求处理）。"""
        if self._static_token:
            return self._static_token
        if not (self.username and self.password):
            return None
        if not force and self._token and time.monotonic() < self._token_deadline:
            return self._token
        async with self._lock:
            # 双检：并发进来的协程只让第一个真正登录
            if not force and self._token and time.monotonic() < self._token_deadline:
                return self._token
            token = await self._login()
            self._token = token
            self._token_deadline = time.monotonic() + max(60, self.token_ttl)
            return token

    async def _headers(self, *, force_token: bool = False) -> dict[str, str]:
        token = await self._ensure_token(force=force_token)
        return {"Authorization": f"Bearer {token}"} if token else {}

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    async def search(self, query: str, top_k: int, tenant: str) -> KbSearchOutput:
        started = time.perf_counter()
        if not self.configured:
            logger.warning("project1_kb_not_configured", extra={"base_url": self.base_url})
            return self._degraded(started, McpErrorCode.KB_AUTH_FAILED)

        payload = {"question": query[:2000], "top_k": max(1, min(int(top_k), 50))}
        try:
            body, status = await self._post_query(payload)
            if status in (401, 403):
                # 令牌过期：强制重登一次再试（静态令牌重登无意义，直接判失败）
                if not self._static_token and self.username and self.password:
                    logger.info("project1_kb_token_refreshed", extra={"status": status})
                    self._token = None
                    body, status = await self._post_query(payload, force_token=True)
                if status in (401, 403):
                    return self._degraded(started, McpErrorCode.KB_AUTH_FAILED)
        except ProjectOneAuthError as exc:
            logger.warning("project1_kb_auth_failed", extra={"error": str(exc)[:200]})
            return self._degraded(started, McpErrorCode.KB_AUTH_FAILED)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            logger.warning("project1_kb_timeout", extra={"timeout": self.timeout})
            return self._degraded(started, McpErrorCode.KB_TIMEOUT)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "project1_kb_http_error",
                extra={"status": exc.response.status_code, "error": exc.response.text[:200]},
            )
            return self._degraded(started, McpErrorCode.KB_UNAVAILABLE)
        except httpx.HTTPError as exc:
            logger.warning("project1_kb_unreachable", extra={"error": str(exc)[:200]})
            return self._degraded(started, McpErrorCode.KB_UNAVAILABLE)

        chunks = self._to_chunks(body, top_k)
        latency = int((time.perf_counter() - started) * 1000)
        if not chunks:
            # 调用成功但没有命中：这是"知识库无相关内容"，不是故障，不标记 degraded，
            # 否则会污染熔断器计数（把正常空结果当失败）。
            logger.info("project1_kb_empty", extra={"query": query[:60], "latency_ms": latency})
            return KbSearchOutput(chunks=[], latency_ms=latency, degraded=False, error_code=None)
        return KbSearchOutput(chunks=chunks, latency_ms=latency, degraded=False, error_code=None)

    async def _post_query(self, payload: dict[str, Any], *, force_token: bool = False) -> tuple[dict[str, Any], int]:
        headers = await self._headers(force_token=force_token)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/v1/kbs/{self.kb_id}/query",
                json=payload,
                headers=headers,
            )
        if response.status_code in (401, 403):
            return {}, response.status_code
        response.raise_for_status()
        body = response.json()
        return (body if isinstance(body, dict) else {}), response.status_code

    def _to_chunks(self, body: dict[str, Any], top_k: int) -> list[KbChunk]:
        data = body.get("data")
        if not isinstance(data, dict):
            data = body
        sources = data.get("sources") or data.get("chunks") or []
        chunks: list[KbChunk] = []
        for item in sources:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            doc_id = str(item.get("chunk_id") or item.get("document_id") or item.get("doc_id") or "")
            document_id = item.get("document_id")
            title = str(item.get("document_title") or item.get("title") or "")
            if title and item.get("chunk_index") is not None:
                title = f"{title} 第{item.get('chunk_index')}块"
            chunks.append(
                KbChunk(
                    doc_id=doc_id or f"p1-kb{self.kb_id}-{len(chunks)}",
                    title=title or f"项目一知识库 #{document_id}",
                    content=content[:_CONTENT_LIMIT],
                    score=float(item.get("score") or 0.0),
                    source=f"project1:kb{self.kb_id}:doc{document_id}",
                )
            )
            if len(chunks) >= max(1, min(top_k, 5)):
                break
        return chunks

    @staticmethod
    def _degraded(started: float, code: McpErrorCode) -> KbSearchOutput:
        return KbSearchOutput(
            chunks=[],
            latency_ms=int((time.perf_counter() - started) * 1000),
            degraded=True,
            error_code=code.value,
        )


__all__ = ["ProjectOneAuthError", "ProjectOneKbProvider"]
