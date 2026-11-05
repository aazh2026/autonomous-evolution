"""数据库引擎与会话工厂。

设计依据：docs/tasks.md TASK-P1-003（PostgreSQL 接入）、
TASK-P1-025（状态写入与事件发布同事务）。

**为什么引擎也要有单元测试**：``build_engine`` 只在进程真正要连库时才被调用，
集成测试全部自己在 conftest 里造引擎（为了每个测试能换临时库）。
于是"引擎能不能构造出来"这件事在测试里从来没被验证过 ——
一条无效的构造参数就是靠这条缝溜进来的。
构造一个引擎不需要连库（连接是懒的），所以它是纯单元测试。
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import AsyncAdaptedQueuePool

from infrastructure.config.settings import Environment, Settings
from infrastructure.database.engine import (
    build_engine,
    build_session_factory,
    describe,
    session_scope,
)


def _settings() -> Settings:
    """一份指向本地库的测试配置。

    用 ``localhost`` 而不是容器名：引擎构造是懒连接，不会真的去连，
    所以这个地址不会被解析，也就不需要它真的可达。
    """
    return Settings(
        environment=Environment.TEST,
        database_url=SecretStr("postgresql+asyncpg://ases:ases@localhost:5432/ases_test"),
    )


def test_build_engine_returns_async_engine() -> None:
    """构造引擎不应抛错。

    这条断言的存在理由：``create_async_engine`` 会校验关键字参数，
    传了它不认识的参数当场就抛 ``TypeError``。曾经把会话级的
    ``expire_on_commit`` 传给了引擎，引擎一构造就炸 —— 而这条路径
    当时没有任何测试覆盖。
    """
    engine = build_engine(_settings())
    assert isinstance(engine, AsyncEngine)


def test_build_engine_honours_url_override() -> None:
    """``url`` 参数优先于配置。"""
    engine = build_engine(_settings(), url="postgresql+asyncpg://u:p@other:5432/db")
    assert engine.url.database == "db"
    assert engine.url.host == "other"


def test_build_engine_honours_pool_settings() -> None:
    """连接池参数来自配置。"""
    settings = Settings(
        environment=Environment.TEST,
        database_url=SecretStr("postgresql+asyncpg://ases:ases@localhost:5432/ases_test"),
        database_pool_size=3,
        database_max_overflow=2,
    )
    engine = build_engine(settings)
    assert isinstance(engine.pool, AsyncAdaptedQueuePool)
    assert engine.pool.size() == 3


def test_session_factory_disables_expire_and_autoflush() -> None:
    """会话工厂的两个开关。

    ``expire_on_commit=False``：提交后属性不过期，调用方不必再查一次库。
    ``autoflush=False``：不在查询前隐式刷库 —— 事务边界由调用方定。
    """
    engine = build_engine(_settings())
    factory = build_session_factory(engine)
    assert isinstance(factory, async_sessionmaker)
    kwargs = factory.kw
    assert kwargs["expire_on_commit"] is False
    assert kwargs["autoflush"] is False


def test_session_factory_binds_the_engine() -> None:
    """会话工厂绑定的就是传进来的那个引擎。"""
    engine = build_engine(_settings())
    factory = build_session_factory(engine)
    assert factory.kw["bind"] is engine


def test_describe_is_redacted() -> None:
    """描述里没有口令。

    ``engine.url`` 默认带掩码，但显式确认一次 —— 这个函数会被写进启动日志，
    日志里的东西比返回值更容易被人看见。
    """
    settings = Settings(
        environment=Environment.TEST,
        database_url=SecretStr("postgresql+asyncpg://ases:sup3rs3cret@localhost:5432/ases_test"),
    )
    described = describe(build_engine(settings))
    assert "sup3rs3cret" not in str(described)
    assert described["driver"] == "postgresql"
    assert described["host"] == "localhost"
    assert described["database"] == "ases_test"


@pytest.mark.asyncio
async def test_session_scope_commits_on_success() -> None:
    """正常退出提交。"""
    factory = build_session_factory(build_engine(_settings()))
    async with session_scope(factory) as session:
        assert isinstance(session, AsyncSession)
    # 没有真正连库 —— 会话从未建立连接，提交的是空事务。


@pytest.mark.asyncio
async def test_session_scope_rolls_back_on_error() -> None:
    """异常退出回滚，并把异常继续抛出去。

    "吞掉异常"是最容易埋雷的写法：调用方以为写成功了，
    实际上一条都没落库。
    """
    factory = build_session_factory(build_engine(_settings()))

    with pytest.raises(ValueError, match="boom"):
        async with session_scope(factory):
            raise ValueError("boom")


__all__ = [
    "test_build_engine_honours_pool_settings",
    "test_build_engine_honours_url_override",
    "test_build_engine_returns_async_engine",
    "test_describe_is_redacted",
    "test_session_factory_binds_the_engine",
    "test_session_factory_disables_expire_and_autoflush",
    "test_session_scope_commits_on_success",
    "test_session_scope_rolls_back_on_error",
]
