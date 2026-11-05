"""异步数据库引擎与会话。

设计依据：docs/design.md §3.6（PostgreSQL 为项目状态库）、docs/tasks.md
TASK-P1-003。

为什么是异步：演进循环里最慢的从来不是 CPU，而是"等一次模型调用"和
"等一次测试跑完"。同步引擎会让整个进程在这些等待上闲置。

两条纪律：

1. **连接串只从配置来**，绝不写死在代码里。连接串里有凭证。
2. **事务边界由调用方决定**。会话工厂不做隐式提交 —— 隐式提交会让
   "状态写入与事件发布同事务"（TASK-P1-025）变成不可能。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from infrastructure.config.settings import Settings, get_settings


def build_engine(settings: Settings | None = None, *, url: str | None = None) -> AsyncEngine:
    """构造一个异步引擎。

    ``url`` 优先于配置里的 ``database_url``。测试用它指向临时库；
    生产与本地开发用配置里的值。
    """
    cfg = settings or get_settings()
    target = url or cfg.database_dsn
    return create_async_engine(
        target,
        pool_size=cfg.database_pool_size,
        max_overflow=cfg.database_max_overflow,
        echo=cfg.database_echo,
        # 应用层显式管理事务边界（见模块文档）。开了它，每次查询都自动提交 ——
        # 那会让"回滚后状态与事件一起消失"这种断言永远无法成立。
        expire_on_commit=False,
        future=True,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """构造会话工厂。"""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """事务边界。

    正常退出提交，异常退出回滚。**不做隐式提交** —— 跨多个仓储的写操作
    必须能被整体回滚，否则"状态与事件同事务"无从谈起。
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@cache
def get_engine() -> AsyncEngine:
    """进程级引擎单例。

    引擎持有连接池，应当整个进程共用一个 —— 每个请求建一个引擎等于
    每个请求建一个连接池。
    """
    return build_engine()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """进程级会话工厂。"""
    return build_session_factory(get_engine())


async def dispose_engine(engine: AsyncEngine | None = None) -> None:
    """关闭引擎并释放连接池。

    进程退出前必须调用 —— 否则连接池里的连接会一直挂到进程被强杀。
    """
    target = engine if engine is not None else get_engine()
    await target.dispose()
    if engine is None:
        get_engine.cache_clear()


def describe(engine: AsyncEngine) -> dict[str, Any]:
    """引擎的可读描述（脱敏）。用于启动日志与诊断。"""
    url = engine.url
    return {
        "driver": url.get_backend_name(),
        "host": url.host,
        "port": url.port,
        "database": url.database,
        "pool": type(engine.pool).__name__,
    }


__all__ = [
    "build_engine",
    "build_session_factory",
    "describe",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
    "session_scope",
]
