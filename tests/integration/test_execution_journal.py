"""TASK-P1-060 —— 循环 A 的执行轨迹落库。

验证（T1 ④）：每一步都写入 ``AgentSession`` 与 ``ExecutionEvent``。

**为什么这条要打到数据库上。** "留痕"这件事在内存里永远是"留了"—— 打桩
落库口记下来的东西，再怎么说也是我们自己造的。真正能证明"留痕留住了"的，
是**行真的在库里**，而且字段对得上：事件挂在**它所属的那次执行**的会话上、
顺序号连续、载荷里带着这一步经过的任务状态。

**为什么轨迹要能还原状态迁移序列。** 图的结构说明"应该怎么走"，轨迹说明
"实际走了什么"。出事之后能回答问题的只有后者 —— 所以载荷里必须带着这一步
经过的任务状态，而不只是"这一步叫什么"。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities import AgentSession, ExecutionEvent, Project
from core.task.execution_loop import SessionJournal
from core.task.outcome import VerificationVerdict
from core.task.state_machine import TaskState
from infrastructure.database.execution_journal import (
    DatabaseExecutionJournal,
    JournalWriteError,
)
from infrastructure.database.repositories import (
    DECISION_RECORD,
    CandidateRepository,
    DecisionRepository,
    TaskRepository,
)
from orchestration.loop_a import LoopAHandlers, build_loop_a_graph, run_loop_a
from orchestration.repair import RepairCoordinator
from tests.support.execution_loop import StubHandlers, ready_state

pytestmark = pytest.mark.integration

_PROJECT_ID = "p-execution-journal"

#: 一路通过时写的节点。
_HAPPY_PATH_NODES = (
    "task_ready",
    "task_planner",
    "select_coding_agent",
    "create_isolated_workspace",
    "agent_executes",
    "collect_diff_and_artifacts",
    "independent_verification",
    "acceptance_passed",
    "integration_and_merge_gate",
    "update_project_state",
    "execution_complete",
)


async def _seed_task(session: AsyncSession) -> uuid.UUID:
    """造一条"项目 → 候选 → 决策 → 任务"的链，返回任务 id。

    **必须走完整条链。** ``task.decision_id`` 是 NOT NULL 外键 —— 而"任务
    只能由一条 record 决策派生"正是三表分离要保证的事，从这里绕过它，
    这条测试就证明不了任何东西。
    """
    project = Project(
        id=_PROJECT_ID,
        name="执行轨迹项目",
        goal={"objective": []},
        policy={},
        budget={},
        repository_url="https://example.invalid/journal.git",
    )
    session.add(project)
    await session.flush()

    candidate = await CandidateRepository(session).create(
        project_id=_PROJECT_ID,
        category="quality",
        title="把循环 A 的轨迹落库",
        evidence=[{"kind": "defect", "ref": "issue://1"}],
    )
    decision = await DecisionRepository(session).record(
        candidate_id=candidate.id,
        decision=DECISION_RECORD,
        rationale="证据充分",
    )
    task = await TaskRepository(session).create_from_decision(
        decision.id,
        title="把循环 A 的轨迹落库",
        task_type="feature",
    )
    await session.commit()
    return task.id


def _handlers(stubs: StubHandlers, journal: SessionJournal) -> LoopAHandlers:
    """把打桩处理器接到真的落库口上。

    **除留痕之外全是打桩。** 这条测试要证明的是"轨迹写进去了"，不是"整个
    系统跑通了" —— 真执行器要钱、真工作区要磁盘、真验证引擎要跑测试。
    """
    return LoopAHandlers(
        planner=stubs,
        selector=stubs,
        workspace=stubs,
        collector=stubs,
        verifier=stubs,
        diagnoser=stubs,
        merge_gate=stubs,
        project_state=stubs,
        journal=journal,
        executors={stubs.executor.name: stubs.executor},
        repair_coordinator=RepairCoordinator(
            stubs.repair_ledger,
            limits=stubs.repair_limits,
        ),
    )


async def _run_loop(session: AsyncSession, task_id: uuid.UUID, **kwargs: object) -> None:
    stubs = StubHandlers(**kwargs)  # type: ignore[arg-type]
    journal = DatabaseExecutionJournal(session)
    graph = build_loop_a_graph(_handlers(stubs, journal))
    await run_loop_a(
        graph,
        ready_state(task_id=str(task_id), project_id=_PROJECT_ID),
    )
    await session.commit()


async def _events(session: AsyncSession) -> list[ExecutionEvent]:
    result = await session.execute(select(ExecutionEvent).order_by(ExecutionEvent.sequence))
    return list(result.scalars())


async def _session_rows(session: AsyncSession) -> list[AgentSession]:
    result = await session.execute(select(AgentSession).order_by(AgentSession.created_at))
    return list(result.scalars())


class TestTheSessionIsWritten:
    """``AgentSession`` 落库。"""

    async def test_one_session_row_is_written(self, session: AsyncSession) -> None:
        task_id = await _seed_task(session)

        await _run_loop(session, task_id)

        assert len(await _session_rows(session)) == 1

    async def test_the_session_carries_the_authorization_snapshot(
        self, session: AsyncSession
    ) -> None:
        """授权快照跟着会话落库 —— 只在内存里的话，出事之后什么都证明不了。"""
        task_id = await _seed_task(session)

        await _run_loop(session, task_id)

        (row,) = await _session_rows(session)
        assert row.agent_id == "agent-1"
        assert row.executor == "stub-executor"
        assert row.task_id == task_id
        assert row.project_id == _PROJECT_ID
        assert row.authorization["agent_id"] == "agent-1"
        assert row.authorization["workspace_id"] == "ws-1"

    async def test_the_session_keeps_the_executor_side_session_id(
        self, session: AsyncSession
    ) -> None:
        """**两样会话标识都要留住。** 行 id 是数据库的主键，执行器侧的会话
        标识是"去执行器那边查这次执行"用的凭据。"""
        task_id = await _seed_task(session)

        await _run_loop(session, task_id)

        (row,) = await _session_rows(session)
        assert row.session_id == "stub-executor-session-1"


class TestEveryStepIsRecorded:
    """``ExecutionEvent`` 落库。"""

    async def test_every_node_wrote_one_event(self, session: AsyncSession) -> None:
        task_id = await _seed_task(session)

        await _run_loop(session, task_id)

        events = await _events(session)
        assert [event.event_type for event in events] == list(_HAPPY_PATH_NODES)

    async def test_the_events_are_numbered_without_a_gap(self, session: AsyncSession) -> None:
        """顺序号连续 —— 幂等消费与回放都靠它。"""
        task_id = await _seed_task(session)

        await _run_loop(session, task_id)

        events = await _events(session)
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))

    async def test_the_events_point_at_the_task_and_the_project(
        self, session: AsyncSession
    ) -> None:
        task_id = await _seed_task(session)

        await _run_loop(session, task_id)

        for event in await _events(session):
            assert event.task_id == task_id
            assert event.project_id == _PROJECT_ID
            assert event.agent_id == "agent-1"

    async def test_the_events_after_the_execution_belong_to_its_session(
        self, session: AsyncSession
    ) -> None:
        """事件挂在**它所属的那次执行**的会话上。"""
        task_id = await _seed_task(session)

        await _run_loop(session, task_id)

        (session_row,) = await _session_rows(session)
        by_type = {event.event_type: event for event in await _events(session)}
        assert by_type["agent_executes"].session_id == session_row.id
        assert by_type["execution_complete"].session_id == session_row.id

    async def test_the_steps_before_the_execution_have_no_session(
        self, session: AsyncSession
    ) -> None:
        """还没执行的那几步没有会话可挂 —— 空着比挂错好。"""
        task_id = await _seed_task(session)

        await _run_loop(session, task_id)

        by_type = {event.event_type: event for event in await _events(session)}
        assert by_type["task_ready"].session_id is None
        assert by_type["task_planner"].session_id is None

    async def test_the_trace_can_rebuild_the_task_state_sequence(
        self, session: AsyncSession
    ) -> None:
        """**轨迹要能还原状态迁移序列。** 图的结构说明"应该怎么走"，轨迹说明
        "实际走了什么" —— 出事之后能回答问题的只有后者。"""
        task_id = await _seed_task(session)

        await _run_loop(session, task_id)

        states: list[TaskState] = []
        for event in await _events(session):
            raw = event.payload.get("task_states", [])
            assert isinstance(raw, list)
            states.extend(TaskState(str(value)) for value in raw)
        assert states == [
            TaskState.RUNNING,
            TaskState.VERIFYING,
            TaskState.PASSED,
            TaskState.INTEGRATING,
            TaskState.MERGED,
            TaskState.COMPLETED,
        ]


class TestARepairRoundIsAlsoTraced:
    """返修那一轮也要留痕 —— 不然轨迹上只有第一次执行。"""

    async def test_the_repair_round_opens_a_session_of_its_own(self, session: AsyncSession) -> None:
        task_id = await _seed_task(session)

        await _run_loop(
            session,
            task_id,
            verdicts=[VerificationVerdict.FAILED, VerificationVerdict.PASSED],
        )

        rows = await _session_rows(session)
        assert len(rows) == 2
        assert rows[0].id != rows[1].id
        assert rows[0].session_id != rows[1].session_id

    async def test_the_repair_round_events_hang_off_the_new_session(
        self, session: AsyncSession
    ) -> None:
        task_id = await _seed_task(session)

        await _run_loop(
            session,
            task_id,
            verdicts=[VerificationVerdict.FAILED, VerificationVerdict.PASSED],
        )

        rows = await _session_rows(session)
        events = await _events(session)
        executes = [event for event in events if event.event_type == "agent_executes"]
        assert executes[0].session_id == rows[0].id
        assert executes[1].session_id == rows[1].id
        assert events[-1].session_id == rows[1].id


class TestTheWriteFailsClosed:
    """design.md §3.5D：留痕写不进去就要停下来，不能装作没事。"""

    async def test_a_malformed_task_id_is_reported_clearly(self, session: AsyncSession) -> None:
        """外键存的是 UUID —— 传一个不是 UUID 的任务 id，报错要说得清楚是哪
        一样东西不对，而不是丢一个外键违例给读的人看。"""
        journal = DatabaseExecutionJournal(session)
        stubs = StubHandlers()
        graph = build_loop_a_graph(_handlers(stubs, journal))

        with pytest.raises(JournalWriteError) as caught:
            await run_loop_a(graph, ready_state(task_id="not-a-uuid"))

        assert "不是合法的 UUID" in str(caught.value)

    async def test_a_write_failure_stops_the_run(self, session: AsyncSession) -> None:
        """**写不进去就不许往下走。** 留痕写不进去还往下跑，等于这次执行没有
        痕迹 —— 而"没有痕迹"是出事之后才发现的。"""
        journal = DatabaseExecutionJournal(session)
        stubs = StubHandlers()
        graph = build_loop_a_graph(_handlers(stubs, journal))

        with pytest.raises(JournalWriteError):
            await run_loop_a(graph, ready_state(task_id="not-a-uuid"))

        # 会话那一步之前就该停了 —— 事件一条都没写进去。
        assert await _events(session) == []
