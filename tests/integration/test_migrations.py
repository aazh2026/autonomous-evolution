"""TASK-P1-003 —— PostgreSQL 接入与迁移工具链。

验证（T1）：
① ``alembic upgrade head`` 后 ``downgrade base`` 再 ``upgrade head`` 成功（迁移可逆）；
② 空库从零迁移耗时 < 30s。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.conftest import ALL_TABLES, REPO_ROOT, _run_alembic

pytestmark = pytest.mark.integration


async def _list_tables(database_url: str) -> list[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version' "
                    "ORDER BY tablename"
                )
            )
            return sorted(row[0] for row in result)
    finally:
        await engine.dispose()


class TestMigrationReversibility:
    """① 迁移可逆。"""

    def test_upgrade_downgrade_upgrade_succeeds(self, scratch_database: str) -> None:
        _run_alembic("upgrade", "head", database_url=scratch_database)
        tables_after_upgrade = asyncio.run(_list_tables(scratch_database))
        assert set(ALL_TABLES).issubset(set(tables_after_upgrade))

        _run_alembic("downgrade", "base", database_url=scratch_database)
        assert asyncio.run(_list_tables(scratch_database)) == []

        _run_alembic("upgrade", "head", database_url=scratch_database)
        assert sorted(ALL_TABLES) == asyncio.run(_list_tables(scratch_database))

    def test_downgrade_leaves_no_business_tables(self, scratch_database: str) -> None:
        _run_alembic("upgrade", "head", database_url=scratch_database)
        _run_alembic("downgrade", "base", database_url=scratch_database)
        remaining = asyncio.run(_list_tables(scratch_database))
        assert remaining == [], f"回滚后仍残留表：{remaining}"

    def test_upgrade_is_idempotent(self, scratch_database: str) -> None:
        """重复执行 upgrade head 不应报错，也不应重复建表。"""
        _run_alembic("upgrade", "head", database_url=scratch_database)
        _run_alembic("upgrade", "head", database_url=scratch_database)
        assert sorted(ALL_TABLES) == asyncio.run(_list_tables(scratch_database))


class TestMigrationSpeed:
    """② 空库从零迁移耗时 < 30s。"""

    def test_empty_database_migration_is_under_30_seconds(self, scratch_database: str) -> None:
        elapsed = _run_alembic("upgrade", "head", database_url=scratch_database)
        assert elapsed < 30, f"空库迁移耗时 {elapsed:.1f}s，超过 30s 上限"


class TestMigratedDatabase:
    """迁移后的库结构。"""

    async def _alembic_version(self, engine: AsyncEngine) -> str:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            return str(result.scalar_one())

    async def _tables(self, engine: AsyncEngine) -> list[str]:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version' "
                    "ORDER BY tablename"
                )
            )
            return [row[0] for row in result]

    async def _triggers(self, engine: AsyncEngine) -> list[str]:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal AND tgname LIKE 'trg_%_append_only_%' "
                    "ORDER BY tgname"
                )
            )
            return [row[0] for row in result]

    async def test_alembic_version_tracks_head(self, engine: AsyncEngine) -> None:
        assert await self._alembic_version(engine) == "0002"

    async def test_twelve_entity_tables_exist(self, engine: AsyncEngine) -> None:
        tables = await self._tables(engine)
        assert len(tables) == 12, f"应有 12 张核心表，实际 {len(tables)}：{tables}"

    async def test_append_only_triggers_exist(self, engine: AsyncEngine) -> None:
        triggers = await self._triggers(engine)
        assert len(triggers) == 4, f"应有 4 个 append-only 触发器，实际 {triggers}"


def test_connection_string_is_not_hardcoded() -> None:
    """连接串不来自代码 —— 版本库里不能有凭证。"""
    content = (REPO_ROOT / "alembic.ini").read_text(encoding="utf-8")
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("sqlalchemy.url"):
            assert stripped == "sqlalchemy.url ="

    # 真实连接串由 env.py 从运行配置注入 —— 那里才有凭证。
    env_source = (REPO_ROOT / "migrations/env.py").read_text(encoding="utf-8")
    assert "get_settings" in env_source
    assert "database_url" in env_source
