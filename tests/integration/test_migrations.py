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
        assert await self._alembic_version(engine) == "0003"

    async def test_entity_tables_exist(self, engine: AsyncEngine) -> None:
        """十二个核心表 + 两张预算账本表。

        预算那两张不在 design.md §3.6 的清单里，是设计文档的一处缺口
        （docs/tasks.md 第 6 节待裁决项 8）。TASK-P1-012 要求"用真实并发写库
        验证"，而十二个实体里没有任何一张表能承载预算累计额。
        """
        tables = await self._tables(engine)
        assert len(tables) == 14, f"应有 14 张表，实际 {len(tables)}：{tables}"
        assert "budget_account" in tables
        assert "budget_entry" in tables

    async def test_append_only_triggers_exist(self, engine: AsyncEngine) -> None:
        """append-only 守卫覆盖全部证据表与预算表。

        断言**确切的触发器名集合**而不是个数 —— 个数对了但名字错了，
        说明某张表没被守卫却恰好数量对上了，那才是真正危险的情况。
        """
        triggers = await self._triggers(engine)
        assert set(triggers) == {
            "trg_verification_run_append_only_update_delete",
            "trg_verification_run_append_only_truncate",
            "trg_artifact_append_only_update_delete",
            "trg_artifact_append_only_truncate",
            "trg_budget_account_append_only_delete",
            "trg_budget_account_append_only_truncate",
            "trg_budget_entry_append_only_update_delete",
            "trg_budget_entry_append_only_truncate",
        }, f"append-only 触发器集合不符：{triggers}"

    async def test_budget_account_is_monotonic(self, engine: AsyncEngine) -> None:
        """累计额的"只增不减"守卫在位。

        这个触发器不叫 ``append_only`` —— 它拦的不是"改内容"，是"改小"。
        允许把累计额改小就等于允许清零，"总额不超限"立刻失效。
        """
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal AND tgname = 'trg_budget_account_monotonic'"
                )
            )
            assert result.scalar_one() == "trg_budget_account_monotonic"


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
