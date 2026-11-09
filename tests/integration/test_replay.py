"""TASK-P1-065 —— 确定性演练 runner 的**落库**验证。

设计依据：docs/tasks.md TASK-P1-065（T1 ① 同一脚本回放两次，产生**完全相同**
的状态迁移序列与 **DB 记录**）、R-108。

**这一条要打到数据库上，因为"回放两次一样"最容易被自己骗过去。** 内存里
比留痕，只能证明编排层的结论一样；**落库的记录一样，才说明"这一轮真的发生了
同样的事"** —— 行挂对了没、引用指向对不对、计数数出来对不对。

**比的是规范化之后的行。** 行 id 是 uuid、时间戳是墙钟 —— 它们天生不可重复，
比它们等于比运气。所以规范化做两件事：

1. **uuid 换成序号。** 按"第一次出现的顺序"编号 —— 同一个 uuid 在所有行里
   换成同一个序号。**这样比的是引用的结构，不是引用的值**：任务还挂在同一
   条决策上、决策还挂在同一个候选上，序号就一样；挂错了，序号就对不上。
2. **时间戳丢掉。** 它们是墙钟，回放两次必然不同 —— 而它们**不该**是
   回放的一部分（打桩件用固定的 ``REPLAY_TIME``）。

**为什么中间要清库。** 第二次回放必须从**空库**开始 —— 否则第二次读到的
是第一轮与第二轮的混合，"两次一样"就变成了"库里有一份混合数据"。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities import Project
from infrastructure.database.evolution_journal import EvolutionJournal
from infrastructure.database.execution_journal import DatabaseExecutionJournal
from tests.integration.conftest import ALL_TABLES, APPEND_ONLY_TABLES
from tests.system.replay_runner import ReplayRunner, ReplayScript

pytestmark = pytest.mark.integration

#: 演练脚本用的项目 id —— 脚本里写死的。
_PROJECT_ID = "proj-replay"

#: 脚本目录。
_SCRIPT_DIR = Path(__file__).resolve().parents[1] / "system" / "scripts"

#: 要比的表。**顺序固定** —— 比的是行序列，顺序变了就是另一回事。
_TABLES = (
    "evolution_cycle",
    "evolution_candidate",
    "evolution_decision",
    "task",
    "task_dependency",
    "project_snapshot",
    "agent_session",
    "execution_event",
)

#: 每张表按什么排序。**排序键必须是确定性的** —— 按 uuid 排没有意义（uuid
#: 每次都不一样），按业务列排才对。
_ORDER_BY = {
    "evolution_cycle": "cycle_id",
    "evolution_candidate": "title",
    "evolution_decision": "decision, rationale",
    "task": "title",
    "task_dependency": "dependency_type",
    "project_snapshot": "version",
    "agent_session": "session_id",
    "execution_event": "event_type, sequence",
}


def _script_path(name: str) -> str:
    """脚本的路径。"""
    return str(_SCRIPT_DIR / name)


async def _seed_project(session: AsyncSession) -> None:
    """建一个项目 —— 候选、周期、快照、任务都挂在它下面。"""
    session.add(
        Project(
            id=_PROJECT_ID,
            name="演练项目",
            goal={"objective": [{"id": "OBJ-1", "description": "把演练跑通"}]},
            policy={"merge_to_main": "approval_required"},
            budget={"currency": "USD"},
            repository_url="https://example.invalid/replay.git",
        )
    )
    await session.flush()


async def _truncate(session: AsyncSession) -> None:
    """清空全部业务表 —— 与 ``tests/integration/conftest.py`` 的做法一致。

    **先关掉 append-only 守卫的 TRUNCATE 触发器。** 守卫存在的目的是挡住
    误删，不是挡住测试重建环境。
    """
    for table in APPEND_ONLY_TABLES:
        await session.execute(
            text(f"ALTER TABLE {table} DISABLE TRIGGER trg_{table}_append_only_truncate")
        )
    await session.execute(text(f"TRUNCATE TABLE {', '.join(ALL_TABLES)} CASCADE"))
    for table in APPEND_ONLY_TABLES:
        await session.execute(
            text(f"ALTER TABLE {table} ENABLE TRIGGER trg_{table}_append_only_truncate")
        )


#: uuid 的形状。**JSON 里的 uuid 也要换成序号** —— 授权载荷里带着任务行 id，
#: 事件载荷里带着幂等键与任务 id；它们是**引用**，比引用得比成同一个序号。
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _token(value: str, tokens: dict[str, str]) -> str:
    """给一个 uuid 字符串一个序号。"""
    return tokens.setdefault(value, f"#{len(tokens)}")


def _canonical(value: Any, tokens: dict[str, str]) -> Any:
    """把一个值规范化。

    **uuid 换成序号**（按第一次出现的顺序），**时间戳丢掉**。
    """
    if isinstance(value, uuid.UUID):
        value = str(value)
    if isinstance(value, str):
        # 字符串里嵌着的 uuid 也换掉 —— 幂等键就是"uuid:次数"。
        return _UUID_PATTERN.sub(lambda match: _token(match.group(0), tokens), value)
    if isinstance(value, datetime):
        return "<timestamp>"
    if isinstance(value, dict):
        return {key: _canonical(item, tokens) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item, tokens) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


async def _snapshot_rows(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    """读出这一轮写下的行 —— 规范化之后的那一份。"""
    result: dict[str, list[dict[str, Any]]] = {}
    # 序号表**跨表共享**：同一个 uuid 在任何一张表里都换成同一个序号。
    # 分表编号的话，"任务挂到了另一条决策上"就看不出来了。
    tokens: dict[str, str] = {}
    for table in _TABLES:
        rows = (
            await session.execute(
                text(f"SELECT * FROM {table} WHERE project_id = :pid ORDER BY {_ORDER_BY[table]}"),
                {"pid": _PROJECT_ID},
            )
        ).mappings()
        canonical = [
            {key: _canonical(value, tokens) for key, value in dict(row).items()} for row in rows
        ]
        # 再按规范化后的内容排一次 —— **排序键相同的行也要有确定次序**，
        # 否则两轮的行序可能不同，差异就出在"其实一样"的地方。
        canonical.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))
        result[table] = canonical
    return result


async def _replay_into(session: AsyncSession, script_name: str) -> None:
    """把一份脚本回放进真的库。"""
    script = ReplayScript.from_yaml(_script_path(script_name))
    runner = ReplayRunner(
        journal=EvolutionJournal(session),
        session_journal=DatabaseExecutionJournal(session),
    )
    await runner.replay(script)
    await session.flush()


class TestReplayWritesTheSameRows:
    """T1 ① —— 同一脚本回放两次，库里留下的行完全一样。"""

    @pytest.mark.parametrize(
        "script_name",
        ["happy_path.yaml", "verification_failure.yaml", "critic_veto.yaml"],
        ids=["happy_path", "verification_failure", "critic_veto"],
    )
    async def test_the_same_script_writes_the_same_rows(
        self, session: AsyncSession, script_name: str
    ) -> None:
        await _seed_project(session)

        await _replay_into(session, script_name)
        first = await _snapshot_rows(session)

        # 第二次从**空库**开始 —— 否则读到的是两轮混在一起的东西。
        await _truncate(session)
        await _seed_project(session)

        await _replay_into(session, script_name)
        second = await _snapshot_rows(session)

        assert second == first

    async def test_the_replay_really_writes_rows(self, session: AsyncSession) -> None:
        """库里确实多了那些行 —— 比"两次一样"更基本的一条。

        **两次都是空的也"一样"。** 所以先证明第一轮不是空的。
        """
        await _seed_project(session)
        await _replay_into(session, "happy_path.yaml")

        rows = await _snapshot_rows(session)

        assert len(rows["evolution_cycle"]) == 1
        assert len(rows["evolution_candidate"]) == 1
        assert len(rows["evolution_decision"]) == 1
        assert len(rows["task"]) == 1
        assert len(rows["project_snapshot"]) == 1
        assert rows["agent_session"]
        assert rows["execution_event"]

    async def test_the_task_hangs_off_the_decision(self, session: AsyncSession) -> None:
        """任务挂在决策上，决策挂在候选上 —— 挂错了不会报错。

        **规范化之后的序号能验出这件事。** 任务行里的 ``decision_id`` 与决策
        行里的 ``id`` 必须是同一个序号 —— 不一样就说明挂错了。
        """
        await _seed_project(session)
        await _replay_into(session, "happy_path.yaml")

        rows = await _snapshot_rows(session)
        decision_ids = {row["id"] for row in rows["evolution_decision"]}
        candidate_ids = {row["id"] for row in rows["evolution_candidate"]}

        assert len(decision_ids) == 1
        for task in rows["task"]:
            assert task["decision_id"] in decision_ids
        for decision in rows["evolution_decision"]:
            assert decision["candidate_id"] in candidate_ids

    async def test_the_execution_events_hang_off_the_session(self, session: AsyncSession) -> None:
        """执行事件挂在它所属的那次执行的会话上。

        **"属于哪一次执行"是事后唯一能回答"当时发生了什么"的东西。** 事件
        挂错会话（或者没有会话），轨迹就断成两截。

        **第一条事件没有会话 —— 那是对的。** 会话是"起一次执行"那一步才开的，
        而 ``task_ready`` 发生在它之前。要求它也有会话，等于要求日志比它
        记录的事还早。
        """
        await _seed_project(session)
        await _replay_into(session, "happy_path.yaml")

        rows = await _snapshot_rows(session)
        session_ids = {row["id"] for row in rows["agent_session"]}
        task_ids = {row["id"] for row in rows["task"]}

        assert session_ids
        assert task_ids
        with_session = [event for event in rows["execution_event"] if event["session_id"]]
        assert with_session
        for event in with_session:
            assert event["session_id"] in session_ids
        for event in rows["execution_event"]:
            assert event["task_id"] in task_ids

    async def test_the_veto_leaves_a_decision_but_no_task(self, session: AsyncSession) -> None:
        """Critic 否决：决策留了，任务没有。

        **"没有任务"不能靠"任务表是空的"来断言** —— 那也可能是整轮根本没跑
        到那一步。所以同时断言决策在、周期在：跑到了，只是没派生任务。
        """
        await _seed_project(session)
        await _replay_into(session, "critic_veto.yaml")

        rows = await _snapshot_rows(session)

        assert len(rows["evolution_cycle"]) == 1
        assert len(rows["evolution_decision"]) == 1
        assert rows["task"] == []
        assert rows["agent_session"] == []
