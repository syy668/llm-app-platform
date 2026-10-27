"""数据库连接与会话管理（SQLAlchemy）。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from ..config import settings

# 所有 ORM 模型的基类
Base = declarative_base()

# 创建引擎：
#   pool_pre_ping 保证连接断开后自动重连；pool_recycle 防止 MySQL 长连接超时。
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    """FastAPI 依赖：为每个请求提供独立数据库会话，请求结束后自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables() -> None:
    """按已导入的 ORM 模型自动建表（幂等；需先确保目标数据库已存在）。"""
    from .. import models  # noqa: F401  导入模型以注册到 Base.metadata

    Base.metadata.create_all(bind=engine, checkfirst=True)
