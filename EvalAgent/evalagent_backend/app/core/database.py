"""数据库引擎与会话。

命名约定：所有表位于 eval_agent_db，且与项目一 kb_agent 完全隔离
（项目二的评测数据不依赖、不污染项目一）。
"""

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,
    pool_pre_ping=True,          # 断连自愈（MySQL wait_timeout 8h 坑）
    pool_recycle=3600,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """全局 ORM 基类（SQLAlchemy 2.0 风格）。"""


def get_db() -> Session:
    """FastAPI 依赖：请求级会话，异常自动回滚。"""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_connection() -> tuple[bool, str]:
    """探活：返回 (是否可用, 描述)。不抛异常，供 /health 使用。"""
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar_one()
            db_name = conn.execute(text("SELECT DATABASE()")).scalar_one()
        return True, f"MySQL {version} / schema={db_name}"
    except Exception as exc:  # noqa: BLE001 - 健康检查不允许抛
        return False, f"{type(exc).__name__}: {exc}"


def create_all_tables() -> list[str]:
    """建表（幂等）。返回本次存在的表名清单，供启动日志打印。"""
    import app.models  # noqa: F401 - 触发模型注册到 Base.metadata

    Base.metadata.create_all(bind=engine)
    return sorted(Base.metadata.tables.keys())
