"""知识库工具连通性自检（内置工具 kb-search 的项目一数据源）。

用途：把「项目一知识库 HTTP 接口到底通不通」变成一次显式探测，而不是等到某个任务
跑到 kb-search 子任务才发现降级。运维/联调时调用 ``GET /kb/status?probe=true`` 即可看到：

- 当前生效的检索契约（project1_kb_query / legacy_kb_search / mall_only）与 Mock 开关；
- 项目一后端地址、目标 kb_id、鉴权方式（静态令牌 / 服务账号 / 未配置）；
- probe=true 时真实发起一次检索，返回耗时、命中条数与片段标题（不落库、不写轨迹）。

权限：ADMIN / APPROVER（与日志检索一致，属运维配套）。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.core.config import settings
from app.core.deps import require_role
from app.core.w2_config import w2
from app.schemas.auth import CurrentUser
from app.schemas.common import ApiResponse
from app.schemas.enums import UserRole
from app.schemas.kb import KbProbeChunk, KbStatusData
from app.tools.kb_adapter import build_kb_provider, get_breaker

router = APIRouter(prefix="/kb", tags=["kb"])


def _auth_mode() -> str:
    if settings.EXTERNAL_RAG_TOKEN:
        return "static_token"
    if settings.EXTERNAL_RAG_USERNAME and settings.EXTERNAL_RAG_PASSWORD:
        return "service_account"
    return "not_configured"


@router.get("/status", summary="知识库工具连通性自检（可选真实探测）", response_model=ApiResponse[KbStatusData])
async def kb_status(
    probe: bool = Query(default=False, description="true 时真实发起一次检索验证链路"),
    q: str = Query(default="库存断货风险", max_length=200, description="探测用查询语句"),
    top_k: int = Query(default=3, ge=1, le=5, description="探测返回条数"),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER)),
) -> ApiResponse[KbStatusData]:
    breaker = get_breaker().snapshot().as_dict()
    data = KbStatusData(
        mock=settings.MOCK_EXTERNAL_RAG,
        contract=(settings.EXTERNAL_RAG_CONTRACT or "project1_kb_query").lower(),
        base_url=settings.EXTERNAL_RAG_BASE_URL,
        kb_id=int(settings.EXTERNAL_RAG_KB_ID),
        auth_mode=_auth_mode(),
        tenant=w2.KB_TENANT,
        timeout_seconds=int(settings.EXTERNAL_RAG_TIMEOUT_SECONDS),
        breaker=breaker,
    )

    if not probe:
        return ApiResponse.ok(data)

    started = time.perf_counter()
    provider = build_kb_provider()
    result = await provider.search(q[: w2.KB_MAX_QUERY_LEN], top_k, w2.KB_TENANT)
    elapsed = int((time.perf_counter() - started) * 1000)
    data.probe = {
        "query": q,
        "ok": bool(result.chunks) and not result.degraded,
        "degraded": result.degraded,
        "error_code": result.error_code,
        "chunks": len(result.chunks),
        "provider_latency_ms": result.latency_ms,
        "total_latency_ms": elapsed,
        "breaker": get_breaker().snapshot().as_dict(),
    }
    data.samples = [
        KbProbeChunk(doc_id=c.doc_id, title=c.title, score=c.score, source=c.source, excerpt=c.content[:120])
        for c in result.chunks
    ]
    return ApiResponse.ok(data)


__all__ = ["router"]
