"""应用入口。

端口 8200：与项目一（8000 后端 / 5173 前端）、项目三（8000/5173 各自实例）、
项目二前端（5174）全部错开，避免「跑错服务」这类经典事故。
"""

import logging
import logging.config
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __app_name__, __version__
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import check_connection, create_all_tables
from app.core.response import envelope
from app.services.rag_client import rag_client
from app.services.runner import recover_stale_runs

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
logging.getLogger("sqlalchemy.engine").setLevel(
    logging.INFO if settings.SQL_ECHO else logging.WARNING
)
logger = logging.getLogger("evalagent")

BOOT_TS = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 78)
    logger.info("%s v%s 启动中 ...", __app_name__, __version__)
    logger.info("监听地址: http://%s:%s", settings.APP_HOST, settings.APP_PORT)
    logger.info("数据库: %s", settings.DATABASE_URL.split("@")[-1])

    healthy, desc = check_connection()
    if not healthy:
        # 不直接退出：健康检查接口要能把根因报出来，比进程起不来更好排查
        logger.error("数据库不可用 -> %s", desc)
    else:
        logger.info("数据库连接正常: %s", desc)
        if settings.AUTO_CREATE_TABLES:
            tables = create_all_tables()
            logger.info("建表完成（幂等），当前 ORM 表: %s", ", ".join(tables))
        else:
            logger.info("AUTO_CREATE_TABLES=False，跳过建表")

        # 进程被杀会留下永久 running 的任务，占住并发闸且前端永远转圈
        try:
            stale = recover_stale_runs()
            if stale:
                logger.warning("已回收僵死评测任务 %s 条", stale)
        except Exception as exc:  # noqa: BLE001 - 回收失败不能挡住启动
            logger.warning("僵死任务回收失败：%s", exc)

    logger.info("被测系统(项目一): %s", settings.UPSTREAM_RAG_BASE_URL)
    # 裁判配置以库里为准（界面改过就覆盖 .env），启动日志打印实际生效值便于排障
    try:
        from app.services.runtime_config import build_judge_config

        _judge_cfg = build_judge_config()
        logger.info(
            "裁判模型(生效): %s @ %s（embedding=%s，温度=0 锁定）",
            _judge_cfg.model or "<未配置>",
            _judge_cfg.api_base or "<默认>",
            _judge_cfg.embedding_model,
        )
    except Exception as exc:  # noqa: BLE001 - 读配置失败不能挡住启动
        logger.warning("读取裁判配置失败：%s", exc)
    logger.info("文档: http://%s:%s/docs", settings.APP_HOST, settings.APP_PORT)
    logger.info("=" * 78)
    yield
    await rag_client.aclose()
    logger.info("%s 已停止", __app_name__)


app = FastAPI(
    title=__app_name__,
    version=__version__,
    description="EvalAgent 独立后端：评测集管理 + RAGAS 异步评测（算力隔离于项目一）",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 主挂载：/api/v1/eval/*（前端 axios baseURL 与项目三都走这里）
app.include_router(api_router, prefix=settings.API_PREFIX)
# 兼容挂载：/eval/*（需求书里写的裸路径，直接 curl 也能用；不进 OpenAPI）
app.include_router(api_router, prefix=settings.COMPAT_PREFIX, include_in_schema=False)


# ------------------------- 统一异常信封 -------------------------
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return envelope(exc.status_code, detail)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    return envelope(422, f"参数校验失败: {loc} {first.get('msg', '')}".strip())


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception):
    logger.exception("未捕获异常: %s", exc)
    return envelope(500, f"服务内部错误: {type(exc).__name__}: {exc}")


# ------------------------- 健康检查 -------------------------
@app.get("/", tags=["健康检查"], summary="服务信息")
def root():
    db_ok, db_desc = check_connection()
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "name": __app_name__,
            "version": __version__,
            "uptime_seconds": round(time.time() - BOOT_TS, 1),
            "database": {"healthy": db_ok, "detail": db_desc},
            "docs": "/docs",
        },
    }


@app.get("/healthz", tags=["健康检查"], summary="就绪探针")
def healthz():
    db_ok, db_desc = check_connection()
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={"code": 0 if db_ok else 503, "message": "ok" if db_ok else "database unavailable",
                 "data": {"version": __version__, "database": db_desc}},
    )


@app.get(f"{settings.API_PREFIX}/health", tags=["健康检查"], summary="API 健康检查")
def api_health():
    db_ok, db_desc = check_connection()
    return {"code": 0, "message": "ok",
            "data": {"version": __version__, "database_healthy": db_ok, "database": db_desc}}
