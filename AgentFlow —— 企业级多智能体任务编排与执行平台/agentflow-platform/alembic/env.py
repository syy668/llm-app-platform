"""Alembic 迁移环境（async engine + Settings 注入 DSN）。

常用命令（在项目根目录、已激活虚拟环境下）：
    alembic revision --autogenerate -m "init schema"   # 生成迁移
    alembic upgrade head                                # 应用迁移
    alembic downgrade -1                                # 回滚一步
    alembic current / history                           # 查看状态
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.db.base import Base

# 必须导入 7 张表的 ORM 定义，否则 autogenerate 检测不到表
from app.schemas import orm_models  # noqa: F401  (User/Task/Subtask/TaskExecution/Approval/Memory/EvalRun)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 由 Settings 注入，避免明文 DSN 入库；% 需转义以兼容 ConfigParser 插值
config.set_main_option("sqlalchemy.url", settings.alembic_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只输出 SQL（用于 DBA 评审）。"""
    context.configure(
        url=settings.alembic_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
