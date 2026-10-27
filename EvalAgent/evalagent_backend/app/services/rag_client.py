"""项目一（被测系统）RAG 问答客户端。

职责边界：**只负责拿到 answer 和 contexts**，不算分、不落库。
算分在 judge.py，编排在 runner.py —— 三层分开是为了让「被测系统」与
「裁判」物理隔离：换被测系统不影响打分口径，换裁判不影响取数。

鉴权：项目一 /kbs/{id}/query 走 get_accessible_kb 依赖，必须带 Bearer。
支持两种模式（与项目三 EXTERNAL_RAG_* 同口径）：
  1. 静态 token（UPSTREAM_RAG_TOKEN）
  2. 账号密码自动登录 + 缓存（token 30 分钟有效，提前 60s 刷新）
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger("evalagent.upstream")


class UpstreamError(RuntimeError):
    """被测系统不可用 / 返回业务错误。"""


@dataclass
class RagAnswer:
    """单次问答的结构化结果。"""

    question: str
    answer: str
    contexts: list[str] = field(default_factory=list)
    latency_ms: int = 0
    raw_sources: list[dict[str, Any]] = field(default_factory=list)


class RagClient:
    def __init__(self) -> None:
        self._base = settings.UPSTREAM_RAG_BASE_URL.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._token: str = settings.UPSTREAM_RAG_TOKEN or ""
        self._token_expire_at: float = 0.0
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(settings.UPSTREAM_RAG_CONCURRENCY)

    # ---------------- 连接与令牌 ----------------
    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    settings.UPSTREAM_RAG_TIMEOUT,
                    connect=settings.UPSTREAM_RAG_CONNECT_TIMEOUT,
                ),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
                # 被测系统在本机 127.0.0.1:8010 —— 绝不能交给系统代理
                # （httpx 默认 trust_env=True，会把 localhost 请求也塞给代理，实测会被打断）
                trust_env=settings.HTTP_TRUST_ENV,
            )
        return self._client

    async def _login(self) -> str:
        if not settings.UPSTREAM_RAG_USERNAME:
            raise UpstreamError(
                "项目一鉴权未配置：UPSTREAM_RAG_TOKEN 与账号密码同时为空"
            )
        client = self._ensure_client()
        try:
            resp = await client.post(
                f"{self._base}/api/v1/auth/login",
                json={
                    "username": settings.UPSTREAM_RAG_USERNAME,
                    "password": settings.UPSTREAM_RAG_PASSWORD,
                },
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"登录项目一失败（网络层）：{exc}") from exc

        body = self._parse(resp)
        token = (body.get("data") or {}).get("access_token")
        if not token:
            raise UpstreamError(f"项目一登录未返回 access_token：{body}")
        logger.info("项目一登录成功，token 已缓存 %ss", settings.UPSTREAM_RAG_TOKEN_TTL)
        return token

    async def _get_token(self) -> str:
        # 静态 token 直接用，不做登录也不做过期管理（由调用方保证有效）
        if settings.UPSTREAM_RAG_TOKEN:
            return settings.UPSTREAM_RAG_TOKEN
        if self._token and time.time() < self._token_expire_at:
            return self._token
        async with self._lock:  # 并发首调只登录一次
            if self._token and time.time() < self._token_expire_at:
                return self._token
            self._token = await self._login()
            self._token_expire_at = time.time() + settings.UPSTREAM_RAG_TOKEN_TTL
            return self._token

    @staticmethod
    def _parse(resp: httpx.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError as exc:
            raise UpstreamError(
                f"项目一返回非 JSON（HTTP {resp.status_code}）：{resp.text[:120]}"
            ) from exc
        # 项目一成功码是 0，错误码是字符串（如 AUTH_INVALID_CREDENTIALS / NOT_FOUND）
        code = body.get("code", 0)
        if code not in (0, "0"):
            raise UpstreamError(
                f"项目一业务错误 [{code}] {body.get('message') or ''}".strip()
            )
        return body

    # ---------------- 问答 ----------------
    async def query(self, kb_id: int, question: str, top_k: int | None = None) -> RagAnswer:
        """调项目一问答接口，返回结构化答案。

        一次 401 自动重登重试；其余错误直接抛 UpstreamError（由 runner 记到样本上）。
        """
        payload: dict[str, Any] = {"question": question}
        if top_k:
            payload["top_k"] = int(top_k)

        async with self._sem:  # 上游并发闸，避免把项目一打满
            started = time.perf_counter()
            for attempt in (1, 2):
                token = await self._get_token()
                client = self._ensure_client()
                try:
                    resp = await client.post(
                        f"{self._base}/api/v1/kbs/{kb_id}/query",
                        json=payload,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                except httpx.HTTPError as exc:
                    raise UpstreamError(f"调用项目一问答失败（网络层）：{exc}") from exc

                if resp.status_code == 401 and attempt == 1:
                    # token 过期：清缓存重登一次
                    self._token, self._token_expire_at = "", 0.0
                    logger.warning("项目一返回 401，重新登录后重试")
                    continue
                if resp.status_code >= 500:
                    raise UpstreamError(f"项目一 {resp.status_code}：{resp.text[:160]}")

                body = self._parse(resp)
                data = body.get("data") or {}
                sources = data.get("sources") or []
                contexts = [
                    str(s.get("content", "")).strip()
                    for s in sources
                    if str(s.get("content", "")).strip()
                ]
                return RagAnswer(
                    question=question,
                    answer=str(data.get("answer") or ""),
                    contexts=contexts,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    raw_sources=sources,
                )
            raise UpstreamError("项目一问答鉴权失败（重登后仍 401）")

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


rag_client = RagClient()
