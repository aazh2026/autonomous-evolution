"""TASK-P1-064 —— 回环与去重在**真实库**上的效果。

设计依据：docs/tasks.md TASK-P1-064（T1 ① 同一事件重复投递只被消费一次；
T1 ② 演进循环触发的执行，其结果回写**不再触发新一轮演进循环**）；I-11。

**这里验的是"库里那几行"，不是"函数返回了对的东西"。**

理由：这一层要解决的正是"**不该有的周期行**"。如果被挡下的事件也留下一个
周期行（哪怕内容是"这一轮我决定不做"），那么"这项目一周跑了多少轮演进"
这个问题会得到一个错的答案 —— 而**库里的行看起来完全正常**。断言必须打在
``evolution_cycle`` 表的行数上：跑了就多一行，没跑就一行不多。

**为什么一次跑两件事。** 一条只有"回写被挡下"的测试，自己看不出"它本来是
该跑出一行的" —— 万一闸门把**所有**事件都挡了，它照样是绿的。所以这里先跑
一轮真的（外部事件 → 一行周期），再拿回写去撞闸门（还是一行）。**两行变一
行才是证据，一行变零行只是巧合。**

**为什么落库口用真的。** 打桩的落库口会记调用次数，而"记了几次调用"与"库
里多了几行"是两回事 —— 真的落库口才会在周期重复、外键对不上、外键缺失时
**报错**。用真的落库口，这条测试顺带证明了"挡下来的时候真的什么都没写"。
"""

from __future__ import annotations

import itertools
from collections.abc import Callable

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.entities import EvolutionCycle, Project
from core.events.dedup import (
    CausationTracker,
    EventDedupWindow,
    InMemoryCausationChainStore,
    InMemoryDedupWindowStore,
)
from core.evolution.evolution_loop import LoopBState, ProjectObservation, TriggerSource
from core.evolution.stall import InMemoryGapProgressStore, StallDetector
from infrastructure.database.evolution_journal import EvolutionJournal
from orchestration.dedup import (
    CausationChainFactory,
    EvolutionTrigger,
    IncomingEvent,
    LoopBCycleRunner,
    LoopBreaker,
)
from orchestration.loop_b import LoopBHandlers, build_loop_b_graph
from tests.support.evolution_loop import StubHandlers

pytestmark = pytest.mark.integration

_PROJECT_ID = "p-dedup"


async def _seed_project(session: AsyncSession) -> None:
    """建一个项目 —— 周期挂在它下面。"""
    project = Project(
        id=_PROJECT_ID,
        name="去重项目",
        goal={"objective": [{"id": "OBJ-1", "description": "把回环挡掉"}]},
        policy={"merge_to_main": "approval_required"},
        budget={"currency": "USD"},
        repository_url="https://example.invalid/dedup.git",
    )
    session.add(project)
    await session.flush()


class _VersionedObserver:
    """一轮一个快照版本号。

    **快照表的 (项目, 版本) 是唯一的** —— 两轮用同一个版本号，第二轮会被
    唯一约束挡下来。这不是被测的东西，是"一轮能跑两遍"的物理前提：版本号
    照旧从观察里来，**只是每轮往前挪一格**。
    """

    def __init__(self, inner: StubHandlers) -> None:
        self._inner = inner
        self._version = 3

    async def observe(self, state: LoopBState) -> ProjectObservation:
        observation = await self._inner.observe(state)
        self._version += 1
        return observation.model_copy(update={"latest_snapshot_version": self._version})


def _handlers(session: AsyncSession) -> LoopBHandlers:
    """**落库口用真的，其余打桩。**

    **为什么只有落库口是真的。** 这一层要证明的就是"库里多了几行" —— 用
    打桩的落库口，记录的是"被叫了几次"，而"被叫"与"写进去"之间隔着
    外键、唯一约束与事务。**那些错在这一层才暴露。**
    """
    stub = StubHandlers()
    return LoopBHandlers(
        observer=_VersionedObserver(stub),
        analyzer=stub,
        discoverer=stub,
        evaluator=stub,
        decider=stub,
        planner=stub,
        scheduler=stub,
        engineering=stub,
        gap_updater=stub,
        journal=EvolutionJournal(session),
    )


async def _cycle_count(session: AsyncSession) -> int:
    """库里有多少行周期。"""
    result = await session.execute(select(func.count()).select_from(EvolutionCycle))
    return int(result.scalar_one())


def _event(
    *,
    event_type: str = "ci.failed",
    aggregate_id: str = "pipeline-1",
    causation_id: str | None = None,
) -> IncomingEvent:
    return IncomingEvent(
        event_type=event_type,
        aggregate_id=aggregate_id,
        project_id=_PROJECT_ID,
        causation_id=causation_id,
        trigger=TriggerSource.EXTERNAL,
    )


def _trigger(
    session: AsyncSession,
    *,
    cycle_ids: Callable[[], str],
) -> tuple[EvolutionTrigger, CausationTracker]:
    """一套闸门 + 一个真的会跑图并落库的执行口。"""
    chain = CausationTracker(InMemoryCausationChainStore())
    breaker = LoopBreaker(
        window=EventDedupWindow(InMemoryDedupWindowStore()),
        chain=chain,
        stall=StallDetector(InMemoryGapProgressStore()),
    )
    graph = build_loop_b_graph(_handlers(session))
    runner = LoopBCycleRunner(graph, cycle_ids=cycle_ids)
    return EvolutionTrigger(breaker=breaker, runner=runner), chain


class TestTheDuplicateDelivery:
    """T1 ① —— 同一事件重复投递只被消费一次。"""

    async def test_the_same_event_twice_leaves_one_cycle_row(self, session: AsyncSession) -> None:
        """**同一件事投递两遍，库里只该有一行周期。**

        **两行变一行才是证据。** 一行变零行只是巧合 —— 闸门要是把所有事件都
        挡了，这个断言照样是绿的。所以先跑一轮真的，再投一次同样的。
        """
        await _seed_project(session)
        counter = itertools.count(1)
        trigger, _ = _trigger(session, cycle_ids=lambda: f"cycle-{next(counter):04d}")

        first = await trigger.handle(_event())
        await session.commit()
        assert await _cycle_count(session) == 1
        assert first.ran is True

        second = await trigger.handle(_event())
        await session.commit()

        assert second.ran is False
        assert await _cycle_count(session) == 1

    async def test_another_aggregate_leaves_another_cycle_row(
        self,
        session: AsyncSession,
    ) -> None:
        """**去重粒度是 (种类, 主体)** —— 另一条流水线挂了就是另一件事，
        把它挡住，下一条流水线的问题就没人管了。"""
        await _seed_project(session)
        counter = itertools.count(1)
        trigger, _ = _trigger(session, cycle_ids=lambda: f"cycle-{next(counter):04d}")

        await trigger.handle(_event(aggregate_id="pipeline-1"))
        await trigger.handle(_event(aggregate_id="pipeline-2"))
        await session.commit()

        assert await _cycle_count(session) == 2


class TestTheWriteback:
    """T1 ② —— 演进循环触发的执行，其结果回写不再触发新一轮演进循环。"""

    async def test_the_writeback_leaves_the_cycle_table_untouched(
        self,
        session: AsyncSession,
    ) -> None:
        """**回写事件一行周期都不该多。**

        **跑之前先有一行。** 没有那一行做对照，"没多"就分不清是"被挡下了"
        还是"压根没跑过"。
        """
        await _seed_project(session)
        counter = itertools.count(1)
        trigger, chain = _trigger(session, cycle_ids=lambda: f"cycle-{next(counter):04d}")

        # 一轮真的：外部事件 → 一行周期。
        await trigger.handle(_event())
        await session.commit()
        assert await _cycle_count(session) == 1

        # 造一条链：演进 → 执行 → 回写。
        factory = CausationChainFactory(chain)
        await factory.open_cycle(causation_id="E1")
        await factory.open_execution(causation_id="X1", parent_id="E1")
        await factory.open_writeback(causation_id="W1", parent_id="X1")

        # 回写再来一次 —— 它是这一轮的后果，不是新事情。
        outcome = await trigger.handle(_event(causation_id="W1"))
        await session.commit()

        assert outcome.ran is False
        assert await _cycle_count(session) == 1

    async def test_the_same_writeback_twice_is_still_one_cycle(
        self,
        session: AsyncSession,
    ) -> None:
        """**回写自己也不该排队。** 回写挡下来之后再来一遍，还是不该有周期
        行 —— 挡住的判据是"这条是回环"，不是"这条来过一次"。"""
        await _seed_project(session)
        counter = itertools.count(1)
        trigger, chain = _trigger(session, cycle_ids=lambda: f"cycle-{next(counter):04d}")

        factory = CausationChainFactory(chain)
        await factory.open_cycle(causation_id="E1")
        await factory.open_execution(causation_id="X1", parent_id="E1")
        await factory.open_writeback(causation_id="W1", parent_id="X1")

        await trigger.handle(_event(causation_id="W1"))
        await trigger.handle(_event(causation_id="W1"))
        await session.commit()

        assert await _cycle_count(session) == 0

    async def test_an_external_event_after_a_writeback_still_runs(
        self,
        session: AsyncSession,
    ) -> None:
        """**回写不能把门焊死。** 回写被挡下之后，外来的真事件照样要能开
        新一轮 —— 否则一次回环就把整个系统静音了。"""
        await _seed_project(session)
        counter = itertools.count(1)
        trigger, chain = _trigger(session, cycle_ids=lambda: f"cycle-{next(counter):04d}")

        factory = CausationChainFactory(chain)
        await factory.open_cycle(causation_id="E1")
        await factory.open_writeback(causation_id="W1", parent_id="E1")

        await trigger.handle(_event(causation_id="W1"))
        await trigger.handle(_event())
        await session.commit()

        assert await _cycle_count(session) == 1
