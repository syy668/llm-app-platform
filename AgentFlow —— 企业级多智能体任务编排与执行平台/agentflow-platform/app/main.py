"""FastAPI 入口：中间件、异常处理、路由注册、OpenAPI 文档。

W1 只做工程骨架：鉴权 + 健康检查可用；Planner / Executor / Reviewer、
4 个 MCP Server、LangGraph 业务节点均不在本阶段（见 README-W1.md 边界说明）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.core.redis_client import close_redis, get_shared_redis
from app.core.runtime_mode import detect_demo_mode
from app.core.w2_config import w2
from app.core.w3_config import w3
from app.db.checkpointer import checkpointer_status
from app.db.session import dispose_engine
from app.routers import health
from app.routers.api_router import api_router

logger = get_logger(__name__)

OPENAPI_TAGS = [
    {"name": "auth", "description": "登录 / 刷新 / 注销（双 Token + Redis 黑名单）"},
    {"name": "health", "description": "健康检查与运行态摘要"},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger.info(
        "app_starting",
        extra={
            "env": settings.APP_ENV,
            "version": __version__,
            "mock_rag": settings.MOCK_EXTERNAL_RAG,
            "mock_eval": settings.MOCK_EXTERNAL_EVAL,
            "checkpointer": checkpointer_status(),
        },
    )
    # 数据源自曝：dev_serve.py 会把单例换成 SQLite / fakeredis，此前是静默切换，
    # 曾导致"任务怎么没了"的排查成本。启动时必须让看日志的人一眼分清连的是哪套库。
    if detect_demo_mode():
        logger.warning("=" * 72)
        logger.warning(
            "⚠️  EMBEDDED DEMO MODE ACTIVATED: Using SQLite & fakeredis"
            "（演示模式：数据为临时数据，与真实 MySQL/Redis 环境完全隔离，勿用于验收真实数据）"
        )
        logger.warning("=" * 72)
    # 任务 9 配套：Token 成本控制配置自检——防御性上限一旦被改大，机制即失效，
    # 所以启动时必须对着日志喊一遍，让运维第一时间看见
    token_config_ok = True
    if w2.LLM_MAX_TOKENS > 4096:
        token_config_ok = False
        logger.warning(
            f"[安全警告] 检测到 LLM_MAX_TOKENS 配置过高 ({w2.LLM_MAX_TOKENS})，"
            "可能存在 Output 爆炸与 Token 成本失控风险！建议不高于 2048。"
        )
    if w2.MAX_TOKENS_PER_TASK > 100000:
        token_config_ok = False
        logger.warning(
            f"[安全警告] 检测到 MAX_TOKENS_PER_TASK 配置过高 ({w2.MAX_TOKENS_PER_TASK})，"
            "可能存在重试风暴下 Token 成本失控风险！建议不高于 50000。"
        )
    if token_config_ok:
        logger.info("[启动自检] Token 成本控制配置正常。")

    # 启动期只做连通性探测，失败不阻断进程（便于本地先起服务后补依赖）
    try:
        await get_shared_redis().ping()
        logger.info("redis_connected")
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis_unavailable_on_startup", extra={"error": str(exc)})

    # 审批超时收口定时器（PRD L98/L245/L382 的验收点，此前完全缺失）。
    # 必须在这里起后台协程，不能靠"用户打开收件箱时惰性收口"：审批 TTL 24h 短于
    # 断点 TTL 72h，中间 48h 是唯一还能"从最近 checkpoint 重发"的窗口，一旦拖过
    # 72h 任务就永久救不回来（历史 task#10 即如此）。
    from app.services.interrupt_service import start_approval_sweeper, stop_approval_sweeper

    sweeper = await start_approval_sweeper()
    if sweeper is not None:
        logger.info("approval_sweeper_scheduled", extra={"interval_s": w3.APPROVAL_SWEEP_INTERVAL_SECONDS})

    yield

    await stop_approval_sweeper()
    # 任务 13：先 flush 轨迹缓冲（不丢尾部数据），再关 Redis / 引擎
    from app.services.trace_service import shutdown_trace_writer

    await shutdown_trace_writer()
    await close_redis()
    await dispose_engine()
    logger.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=__version__,
        description=(
            "AgentFlow —— 企业级多智能体任务编排与执行平台。\n\n"
            "已交付：多智能体 LangGraph 编排（Planner / Executor / Reviewer / human_gate 人工审批）、"
            "断点续跑（AsyncRedisSaver）、三层记忆、SSE 流式执行、双 Token 鉴权与 RBAC、"
            "MCP 工具注册表（kb-search / sql-query / chart-render / notify）、"
            "只读 SQL 安全守卫、全局限流、TraceID 链路追踪与人工干预（改计划 / 注入事实 / 重跑子任务）。"
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )

    # 中间件：trace 在内层、CORS 在外层（异常响应也能带上 CORS 头）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-ID"],
    )
    # 全局限流（v1.1）：纯 ASGI 中间件，不包装 body，对 SSE 流式链路零影响。
    # 先挂限流、再挂 TraceID（后加=更外层），使 429 响应也能带上 trace_id / X-Trace-ID。
    from app.core.rate_limit import RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware)

    from app.core.trace import TraceIDMiddleware

    app.add_middleware(TraceIDMiddleware)

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(api_router)

    # 图表等产物静态目录：chart-render 落盘到 `storage/charts/`，业务侧只给出
    # `/static/charts/xxx.png` 这样的相对 URL。dev 用 dev_serve.py 挂载，但直接用
    # uvicorn 起主程序时也需要挂载，否则前端经 vite 代理请求会 404。
    # 仅当目录存在时挂载，且避免与已有路由重复挂载冲突。
    _static_dir = Path(__file__).resolve().parent.parent / "storage"
    _static_mounted = any(
        getattr(r, "path", "") == "/static" for r in app.routes
    )
    if _static_dir.is_dir() and not _static_mounted:
        app.mount(
            "/static",
            StaticFiles(directory=str(_static_dir)),
            name="static-storage",
        )
        logger.info("static_mounted", extra={"directory": str(_static_dir)})

    @app.get("/", tags=["health"], summary="服务信息")
    async def root() -> dict:
        return {
            "service": settings.APP_NAME,
            "version": __version__,
            "env": settings.APP_ENV,
            "docs": "/docs",
            "api_prefix": settings.API_V1_PREFIX,
        }

    return app


app = create_app()
