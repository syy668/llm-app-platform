"""数据库会话（SQLAlchemy 2.0 Async + aiomysql）。

engine / sessionmaker 均**惰性创建**：import 期不加载 DBAPI 驱动，
便于无 MySQL 驱动的环境（如纯 SQLite 单测、文档生成）直接导入模块。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _connect_args() -> dict[str, object]:
    """连接级参数：MySQL 会话时区固定 UTC。

    目的：让 DDL 兜底的 `server_default=CURRENT_TIMESTAMP` / `NOW()` 也产出 UTC，
    与 `TimestampMixin` 的 Python 端 `utcnow()`、`trace_service` 写入的 `started_at`
    保持同一口径（否则本地时区会把 `created_at` 写成 UTC+8，导致耗时算成负数）。
    SQLite（单测）无此概念，直接跳过。
    """
    if settings.database_url.startswith("mysql"):
        return {"init_command": "SET time_zone = '+00:00'"}
    return {}


def get_engine() -> AsyncEngine:
    """进程内单例 Engine（首次使用时创建）。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.MYSQL_ECHO,
            pool_pre_ping=True,
            pool_recycle=settings.MYSQL_POOL_RECYCLE,
            pool_size=settings.MYSQL_POOL_SIZE,
            max_overflow=settings.MYSQL_MAX_OVERFLOW,
            future=True,
            connect_args=_connect_args(),
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个 Session，异常自动回滚。

    注意：本层不做隐式 commit，事务边界由 service 层显式控制。
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def check_db() -> bool:
    """就绪探针：SELECT 1。"""
    try:
        from sqlalchemy import text

        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        logger.warning("mysql_ping_failed")
        return False


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
