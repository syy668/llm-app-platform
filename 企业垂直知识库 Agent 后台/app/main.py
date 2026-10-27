"""FastAPI 应用入口：注册路由、全局异常处理、CORS、生命周期。"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.v1 import api_router
from .config import settings
from .core.database import create_tables, engine
from .core.exception_handler import register_exception_handlers
from .core.logging import setup_logging
from .core.middleware import RequestLoggingMiddleware
from .core.schema_check import run_schema_check

# 提前初始化日志，保证异常处理器等模块可用
setup_logging()
logger = logging.getLogger(__name__)


def _check_redis_backend() -> None:
    """启动期 Redis 后端自检（【批次3 新增】）。

    三种情况分别给出明确日志，避免"以为连着真实 Redis、其实跑的是内存版"这类静默问题：
    - fake：会话不跨进程/重启即失效 + AI 限流停用 —— 只在离线演示/CI 可接受；
    - real 且连通：会话共享与限流生效；
    - real 但连不通：登录与限流都不可用（不阻塞启动，由 /health 持续反映）。
    """
    try:
        from .core.redis import is_fake_redis, ping_redis

        if is_fake_redis():
            logger.warning(
                "Redis 后端 = fakeredis 内存实现（FAKE_REDIS=true）："
                "会话不跨进程共享、重启即失效，AI 问答限流已停用；"
                "联调/生产请设置 FAKE_REDIS=false 并确保 REDIS_URL 可连接"
            )
        elif ping_redis():
            logger.info("Redis 后端 = 真实 Redis：会话跨进程共享，AI 问答限流已生效")
        else:
            logger.warning(
                "Redis 真实后端连接失败：登录态与 AI 限流不可用，请检查 REDIS_URL 与 Redis 服务状态"
            )
    except Exception as exc:  # noqa: BLE001 - 自检失败不阻塞启动
        logger.warning("Redis 后端自检跳过: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auto_create_tables:
        try:
            create_tables()
            logger.info("自动建表完成")
        except Exception as exc:
            # 数据库未就绪 / 未建库时不阻塞启动，由 /health 反映真实状态
            logger.warning("自动建表失败（请确认已执行 scripts/init_db.sql 且数据库可用）: %s", exc)

    # 【v1.7 新增】Schema 自检：create_all 只建缺失的表、不会给存量表补列，
    # 所以 ORM 新增字段后旧库会一直"缺列"，直到某个接口读写该列才 500。
    # 这里在启动期比对一次，缺列直接给出可执行的 ALTER 并中止启动（strict）。
    # 注意：不吞异常 —— 就是要让它阻断启动，否则等于没检查。
    run_schema_check(engine)

    # 【B2-1 新增】僵死任务最小保护：进程重启后，历史 running 状态的评测任务已无
    # 执行线程，启动时一次性扫描置为 failed（阈值 EVAL_STALE_RUN_SECONDS，默认 600s）。
    # cleanup_stale_running_runs 内部吞异常，此处仅延迟导入以避免在模块加载期引入 ragas 重依赖。
    # 【v1.13】clear_pending=True：残留 pending 也一并清理 —— 新进程不会接管旧线程，
    # 这些僵尸会永久占住「同时运行评测任务数」的名额，导致用户新建任务全部 429。
    try:
        from .services.eval_service import backfill_terminal_metrics, cleanup_stale_running_runs

        cleaned = cleanup_stale_running_runs(clear_pending=True)
        if cleaned:
            logger.warning("启动清理僵死评测任务 %d 个", cleaned)
        # 【v1.13.1】修复历史脏数据：v1.13.1 之前中断的任务只落了得分没落汇总指标，
        # 列表里四项全「—」但点开有明细。启动时补一次，幂等。
        repaired = backfill_terminal_metrics()
        if repaired:
            logger.warning("启动回填历史评测指标 %d 个", repaired)
    except Exception as exc:  # noqa: BLE001 - 与自动建表同策略：不阻塞启动
        logger.warning("僵死评测任务清理跳过: %s", exc)
    _check_redis_backend()
    yield
    # 应用关闭时释放数据库连接池
    engine.dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# 全局异常处理器（统一返回体 {code, message, data}）
register_exception_handlers(app)

# CORS（开发环境可用 *，生产请在 .env 中配置具体来源）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# HTTP 请求日志中间件：方法/路径/客户端IP/状态码/耗时 输出到 stdout
app.add_middleware(RequestLoggingMiddleware)


@app.middleware("http")
async def ensure_utf8_json(request, call_next):
    """为 JSON 响应补充 charset=utf-8。

    部分旧客户端（如 Windows PowerShell 5.1）在响应头未标注字符集时，
    会按本地编码(GBK/Latin-1)误读 UTF-8 中文，导致乱码。
    """
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json") and "charset" not in content_type:
        response.headers["content-type"] = "application/json; charset=utf-8"
    return response


# 注册业务路由
app.include_router(api_router)


@app.get("/")
def root():
    return {"code": 0, "message": settings.app_name, "data": None}
