"""Alembic 运行环境（异步版）。

设计依据：docs/tasks.md TASK-P1-003。

三件事：

1. **连接串从配置注入**，不写在 ``alembic.ini`` 里 —— 那里有凭证。
2. **目标元数据来自 ``core.entities.Base``**，保证迁移与模型同源。
3. **异步引擎**。迁移工具不该逼着整个项目退回同步 SQLAlchemy。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from core.entities import Base
from infrastructure.config.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """迁移用的连接串。

    优先取 ``alembic.ini`` 里的 ``sqlalchemy.url``（留空则跳过），
    否则用运行配置里的 ``database_url``。
    """
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured
    return get_settings().database_dsn


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在同步连接上执行迁移。"""
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式：连库执行。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        url=get_url(),
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
