"""健康检查：liveness / readiness / 运行态摘要（含 Mock 开关与预留能力状态）。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import __version__
from app.core.config import settings
from app.core.exceptions import ErrorCode
from app.core.redis_client import ping_redis
from app.core.runtime_mode import runtime_mode
from app.db.checkpointer import checkpointer_status
from app.db.session import check_db

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="存活探针（不依赖任何外部组件）")
async def healthz() -> dict:
    return {"status": "ok", "service": settings.APP_NAME, "version": __version__}


@router.get("/readyz", summary="就绪探针（MySQL + Redis，含运行模式标识）")
async def readyz() -> JSONResponse:
    db_ok = await check_db()
    redis_ok = await ping_redis()
    ready = db_ok and redis_ok
    # 探针执行后单例必然已物化，此时判定运行模式才与实际绑定一致
    # （dev_serve 演示进程为 demo，真实 MySQL/Redis 进程为 production）。
    # 前端据 `mode == "demo"` 常驻"演示模式"角标，避免数据源静默切换。
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "mode": runtime_mode(),
            "checks": {"mysql": db_ok, "redis": redis_ok},
            "code": ErrorCode.OK.value if ready else ErrorCode.SERVICE_UNAVAILABLE.value,
        },
    )


@router.get("/api/v1/health", summary="运行态摘要（Mock 开关 / checkpointer 预留状态）")
async def health_summary() -> dict:
    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "version": __version__,
        "env": settings.APP_ENV,
        "mock": {
            "external_rag": settings.MOCK_EXTERNAL_RAG,
            "external_eval": settings.MOCK_EXTERNAL_EVAL,
        },
        "auth": {
            "access_expire_minutes": settings.ACCESS_TOKEN_EXPIRE_MINUTES,
            "refresh_expire_days": settings.REFRESH_TOKEN_EXPIRE_DAYS,
            "blacklist_enabled": True,
        },
        "langgraph": checkpointer_status(),
    }
