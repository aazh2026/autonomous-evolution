"""TASK-P2-016 —— 架构决策的数据库存储：与文件存储说的是同一件事。

**这一层验的是"两套存储不各说各话"。** 架构决策要存两处：Git 里的
Markdown（给人读）与 PostgreSQL 里的行（给系统读）。两者的语义必须一致
—— 否则"Git 里写的决策"与"库里那条决策"会各说各话，而**各说各话的
事实比没有事实更坏**。

所以这里验三件事：

1. **数据库这一半能走通** —— 写进去、读出来、按 id 取到；
2. **取代关系是外键** —— 指一条不存在的决策，数据库会挡下来
   （不是应用层"先查一下"，那种检查在并发下有窗口期）；
3. **两套存储语义一致** —— 同一组决策分别写进文件与数据库，
   "哪条还生效"、"取代链怎么串"这两件事的答案必须一样。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.architecture.decisions import (
    STATUS_ACCEPTED,
    ArchitectureDecisionError,
    DecisionStore,
)
from core.entities import ArchitectureDecision, Project
from infrastructure.database.repositories import ArchitectureDecisionRepository
from knowledge.architecture.decisions import FileDecisionStore, decisions_as_set

pytestmark = pytest.mark.integration

PROJECT_ID = "p-knowledge"


async def _seed_project(session: AsyncSession) -> None:
    """建一条项目 —— ``architecture_decision.project_id`` 是指向它的外键。"""
    session.add(
        Project(
            id=PROJECT_ID,
            name="知识库演示项目",
            goal={"objective": [{"id": "OBJ-1", "description": "把架构认知落下来"}]},
            policy={"merge_to_main": "approval_required"},
            budget={"currency": "USD"},
            repository_url="https://example.invalid/repo.git",
            tech_stack=["python"],
        )
    )
    await session.flush()


class TestDatabaseStore:
    async def test_a_decision_survives_a_round_trip(self, session: AsyncSession) -> None:
        await _seed_project(session)
        repository = ArchitectureDecisionRepository(session)
        written = await repository.record(
            project_id=PROJECT_ID,
            context="每轮演进都要重读整个仓库",
            decision="索引落盘，刷新时只重读改过的文件",
            tradeoff="索引与仓库可能不同步",
        )
        await session.commit()

        found = await repository.recall(PROJECT_ID)
        assert len(found) == 1
        assert found[0].id == written.id
        assert found[0].context == "每轮演进都要重读整个仓库"
        assert found[0].decision == "索引落盘，刷新时只重读改过的文件"
        assert found[0].tradeoff == "索引与仓库可能不同步"
        assert found[0].status == STATUS_ACCEPTED
        assert found[0].supersedes_id is None

    async def test_get_by_id(self, session: AsyncSession) -> None:
        await _seed_project(session)
        repository = ArchitectureDecisionRepository(session)
        written = await repository.record(project_id=PROJECT_ID, context="背景", decision="决策")
        await session.commit()

        found = await repository.get(written.id)
        assert found is not None
        assert found.decision == "决策"

    async def test_missing_decision_returns_none(self, session: AsyncSession) -> None:
        await _seed_project(session)
        repository = ArchitectureDecisionRepository(session)
        assert await repository.get("00000000-0000-0000-0000-000000000000") is None

    async def test_a_bad_id_raises(self, session: AsyncSession) -> None:
        """不是 UUID 就不像 id —— 报错，不猜。"""
        await _seed_project(session)
        repository = ArchitectureDecisionRepository(session)
        with pytest.raises(ArchitectureDecisionError):
            await repository.get("不是-UUID")

    async def test_an_invalid_status_is_rejected(self, session: AsyncSession) -> None:
        await _seed_project(session)
        repository = ArchitectureDecisionRepository(session)
        with pytest.raises(ArchitectureDecisionError):
            await repository.record(
                project_id=PROJECT_ID, context="背景", decision="决策", status="随便写的"
            )

    async def test_the_supersede_link_is_stored(self, session: AsyncSession) -> None:
        """**决策可以被推翻，而不是被删掉。**"""
        await _seed_project(session)
        repository = ArchitectureDecisionRepository(session)
        first = await repository.record(project_id=PROJECT_ID, context="背景", decision="旧决策")
        second = await repository.record(
            project_id=PROJECT_ID,
            context="背景",
            decision="新决策",
            supersedes_id=first.id,
        )
        await session.commit()

        found = await repository.get(second.id)
        assert found is not None
        assert found.supersedes_id == first.id

    async def test_superseding_a_missing_decision_is_rejected(self, session: AsyncSession) -> None:
        """**外键是最后一道防线。** 应用层"先查一下"在并发下有窗口期。"""
        await _seed_project(session)
        repository = ArchitectureDecisionRepository(session)
        with pytest.raises(IntegrityError):
            await repository.record(
                project_id=PROJECT_ID,
                context="背景",
                decision="决策",
                supersedes_id="00000000-0000-0000-0000-000000000000",
            )
        await session.rollback()

    async def test_the_row_lands_in_the_architecture_decision_table(
        self, session: AsyncSession
    ) -> None:
        """**十二个核心实体里的那一个 —— 真的插进去了。**"""
        await _seed_project(session)
        repository = ArchitectureDecisionRepository(session)
        await repository.record(project_id=PROJECT_ID, context="背景", decision="决策")
        await session.commit()

        rows = await session.scalars(
            select(ArchitectureDecision).where(ArchitectureDecision.project_id == PROJECT_ID)
        )
        assert len(rows.all()) == 1

    async def test_recall_only_returns_own_project(self, session: AsyncSession) -> None:
        await _seed_project(session)
        repository = ArchitectureDecisionRepository(session)
        await repository.record(project_id=PROJECT_ID, context="背景", decision="一")
        await session.commit()
        assert await repository.recall("别的项目") == ()


class TestBothStoresAgree:
    async def test_both_stores_implement_the_same_port(self, session: AsyncSession) -> None:
        """**同一个端口，两种后端。**"""
        repository = ArchitectureDecisionRepository(session)
        assert isinstance(repository, DecisionStore)

    async def test_effective_decisions_and_chains_agree(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """**"Git 里写的"与"库里那条"必须说同一件事。**

        同一组决策分别写进文件存储与数据库存储，然后比两边的答案：
        哪条还生效、取代链怎么串。两边不一致就说明其中一套说错了 ——
        而"各说各话的事实比没有事实更坏"。
        """
        await _seed_project(session)
        database = ArchitectureDecisionRepository(session)
        files = FileDecisionStore(tmp_path / "adr")

        # 第一条：被第二条取代；第三条取代第二条。
        first_db = await database.record(
            project_id=PROJECT_ID, context="背景", decision="最初的决策"
        )
        second_db = await database.record(
            project_id=PROJECT_ID,
            context="背景",
            decision="中间的决策",
            supersedes_id=first_db.id,
        )
        third_db = await database.record(
            project_id=PROJECT_ID,
            context="背景",
            decision="最新的决策",
            supersedes_id=second_db.id,
        )
        await session.commit()

        first_file = await files.record(
            project_id=PROJECT_ID, context="背景", decision="最初的决策"
        )
        second_file = await files.record(
            project_id=PROJECT_ID,
            context="背景",
            decision="中间的决策",
            supersedes_id=first_file.id,
        )
        third_file = await files.record(
            project_id=PROJECT_ID,
            context="背景",
            decision="最新的决策",
            supersedes_id=second_file.id,
        )

        from_db = decisions_as_set(await database.recall(PROJECT_ID))
        from_file = decisions_as_set(await files.recall(PROJECT_ID))

        # 生效的决策：都只剩最新那一条。
        assert len(from_db.effective()) == 1
        assert len(from_file.effective()) == 1
        assert from_db.effective()[0].decision == from_file.effective()[0].decision

        # 取代链：两条链的决策内容顺序必须一样。
        assert [item.decision for item in from_db.chain(third_db.id)] == [
            item.decision for item in from_file.chain(third_file.id)
        ]
        assert len(from_db.chain(third_db.id)) == len(from_file.chain(third_file.id)) == 3

    async def test_both_stores_carry_the_same_fields(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """**字段一个都不能少。** 少一个字段，两套存储就会在那一项上分叉。"""
        await _seed_project(session)
        database = ArchitectureDecisionRepository(session)
        files = FileDecisionStore(tmp_path / "adr")

        await database.record(
            project_id=PROJECT_ID,
            context="背景",
            decision="决策",
            tradeoff="代价",
            status=STATUS_ACCEPTED,
        )
        await session.commit()
        await files.record(
            project_id=PROJECT_ID,
            context="背景",
            decision="决策",
            tradeoff="代价",
            status=STATUS_ACCEPTED,
        )

        from_db = (await database.recall(PROJECT_ID))[0]
        from_file = (await files.recall(PROJECT_ID))[0]
        assert from_db.project_id == from_file.project_id
        assert from_db.context == from_file.context
        assert from_db.decision == from_file.decision
        assert from_db.tradeoff == from_file.tradeoff
        assert from_db.status == from_file.status
        assert from_db.supersedes_id == from_file.supersedes_id
