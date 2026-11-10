"""TASK-P3-011 —— 触发路由：六路事件只有一扇门。

**测的是"同一件事只开一轮"。** 这是 TASK-P3-011 验收条件的另一半 ——
另一半（六类事件各能触发一轮演进）在 :mod:`tests.system.test_six_triggers`
里用真的演进循环验。

**这一层为什么值得单独测。** 六路源各读各的来源、各产各的事件，但**派发只
有一条路** —— 因为去重窗口只有一份。因果链也只有一份，六路各判各的就会出
现"代码事件不回环、外部事件回环"这种说不通的局面。所以这里断言的是**门
的数目**：不管从哪一路来，都得从这扇门过。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.events.dedup import DEFAULT_DEDUP_WINDOW as DEDUP_WINDOW
from core.evolution.evolution_loop import LoopBState, TriggerSource
from core.evolution.triggers import TriggerEvent, TriggerEventType
from orchestration.dedup import IncomingEvent
from orchestration.triggers import DEFAULT_TRIGGER_WINDOW, TriggerRouter, build_router

#: 投了几次。
DISPATCHES: int = 5


@dataclass
class CountingRunner:
    """数着跑了多少轮的假执行口。

    **它数的是"跑了多少轮"，不是"被叫了多少次"。** 被挡下的事件压根不会
    叫到它 —— 所以这个数就是"实际开轮数"，正是验收条件要的那个数。
    """

    calls: int = 0
    seen: list[IncomingEvent] = field(default_factory=list)

    async def run(self, event: IncomingEvent) -> LoopBState:
        self.calls += 1
        self.seen.append(event)
        return LoopBState(
            project_id=event.project_id,
            cycle_id=f"cycle-{self.calls}",
            trigger=event.trigger,
        )


def _event(
    source: TriggerSource = TriggerSource.CODE,
    *,
    aggregate_id: str = "a-1",
) -> TriggerEvent:
    """一条触发事件。"""
    event_type = {
        TriggerSource.SCHEDULE: TriggerEventType.SCHEDULE_SCAN,
        TriggerSource.CODE: TriggerEventType.CODE_COMMIT,
        TriggerSource.QUALITY: TriggerEventType.QUALITY_FAILURE,
        TriggerSource.RUNTIME: TriggerEventType.RUNTIME_INCIDENT,
        TriggerSource.EXTERNAL: TriggerEventType.EXTERNAL_REPORT,
        TriggerSource.KNOWLEDGE: TriggerEventType.KNOWLEDGE_UPDATE,
    }[source]
    return TriggerEvent(
        source=source,
        event_type=event_type,
        aggregate_id=aggregate_id,
        project_id="P1",
        summary="一件事",
    )


@pytest.fixture
def runner() -> CountingRunner:
    """假执行口。"""
    return CountingRunner()


@pytest.fixture
def router(runner: CountingRunner) -> TriggerRouter:
    """一台路由。"""
    return build_router(runner)


class TestOneRound:
    """一件事 → 一轮。"""

    async def test_the_first_event_starts_a_round(self, router: TriggerRouter) -> None:
        outcome = await router.dispatch(_event())
        assert outcome.ran
        assert outcome.decision.should_trigger

    async def test_the_round_really_ran(
        self, router: TriggerRouter, runner: CountingRunner
    ) -> None:
        await router.dispatch(_event())
        assert runner.calls == 1

    async def test_the_event_type_is_not_flattened_into_the_source(
        self, router: TriggerRouter, runner: CountingRunner
    ) -> None:
        """**去重指纹用的是事件类型的值，不是来源的名字。**

        用来源的名字的话，"一次提交"与"一次质量失败"只要主体撞上就互相挡
        掉 —— 那是把两件事当成一件。
        """
        await router.dispatch(_event(TriggerSource.CODE, aggregate_id="same"))
        await router.dispatch(_event(TriggerSource.QUALITY, aggregate_id="same"))
        assert runner.calls == 2
        assert runner.seen[0].event_type == "code.commit"
        assert runner.seen[1].event_type == "quality.failure"

    async def test_the_trigger_source_is_carried_into_the_cycle(
        self, router: TriggerRouter, runner: CountingRunner
    ) -> None:
        """**周期记录里写的那一列，得是这一路。**"""
        await router.dispatch(_event(TriggerSource.RUNTIME))
        assert runner.seen[0].trigger is TriggerSource.RUNTIME


class TestTheDedupWindow:
    """同一件事去重后只触发一次。"""

    async def test_the_same_event_twice_starts_one_round(
        self, router: TriggerRouter, runner: CountingRunner
    ) -> None:
        await router.dispatch(_event())
        await router.dispatch(_event())
        assert runner.calls == 1

    async def test_the_same_event_many_times_starts_one_round(
        self, router: TriggerRouter, runner: CountingRunner
    ) -> None:
        """**投递 N 次，实际开轮数是 1。** 这是验收条件里的那个数。"""
        for _ in range(DISPATCHES):
            await router.dispatch(_event())
        assert runner.calls == 1

    async def test_the_second_one_says_why_it_was_stopped(self, router: TriggerRouter) -> None:
        """**"不开"必须说得清为什么** —— 是重复，不是回环。"""
        await router.dispatch(_event())
        outcome = await router.dispatch(_event())
        assert not outcome.ran
        assert outcome.decision.suppressed
        assert "去重" in outcome.decision.reason

    async def test_a_different_aggregate_is_a_different_event(
        self, router: TriggerRouter, runner: CountingRunner
    ) -> None:
        """**主体不同就是两件事** —— 两次不同的提交该开两轮。"""
        await router.dispatch(_event(aggregate_id="a-1"))
        await router.dispatch(_event(aggregate_id="a-2"))
        assert runner.calls == 2

    async def test_a_different_source_is_a_different_event(
        self, router: TriggerRouter, runner: CountingRunner
    ) -> None:
        """**同一主体上的不同事件类型不互相挡。**"""
        await router.dispatch(_event(TriggerSource.CODE, aggregate_id="same"))
        await router.dispatch(_event(TriggerSource.EXTERNAL, aggregate_id="same"))
        assert runner.calls == 2

    async def test_the_window_is_the_default_one(self) -> None:
        """**默认窗口是十五分钟** —— 与 :data:`core.events.dedup.
        DEFAULT_DEDUP_WINDOW` 同值，两处不许各写一个。"""
        assert DEFAULT_TRIGGER_WINDOW == DEDUP_WINDOW
        assert DEFAULT_TRIGGER_WINDOW.total_seconds() == 15 * 60


class TestTheCounts:
    """数着的 —— "谁真的开了一轮"。"""

    async def test_the_started_count_is_per_source(self, router: TriggerRouter) -> None:
        await router.dispatch(_event(TriggerSource.CODE))
        await router.dispatch(_event(TriggerSource.QUALITY))
        assert router.started[TriggerSource.CODE] == 1
        assert router.started[TriggerSource.QUALITY] == 1

    async def test_the_suppressed_count_is_per_source(self, router: TriggerRouter) -> None:
        await router.dispatch(_event(TriggerSource.CODE))
        await router.dispatch(_event(TriggerSource.CODE))
        assert router.suppressed[TriggerSource.CODE] == 1
        assert router.started[TriggerSource.CODE] == 1

    async def test_the_counts_add_up(self, router: TriggerRouter) -> None:
        """**投了 N 次 = 开了 k 轮 + 挡了 N-k 次。** 少算一次就说明有人漏了
        一条路径。"""
        for _ in range(DISPATCHES):
            await router.dispatch(_event())
        assert router.started[TriggerSource.CODE] == 1
        assert router.suppressed[TriggerSource.CODE] == DISPATCHES - 1


class TestTheBatch:
    """一批事件。"""

    async def test_a_batch_is_dispatched_in_order(
        self, router: TriggerRouter, runner: CountingRunner
    ) -> None:
        """**顺序有意义** —— 同一批里若有互相重复的事件，先来的先占去重窗口。"""
        outcomes = await router.dispatch_all([_event(), _event(), _event(aggregate_id="a-2")])
        assert len(outcomes) == 3
        assert [item.ran for item in outcomes] == [True, False, True]
        assert runner.calls == 2

    async def test_an_empty_batch_does_nothing(
        self, router: TriggerRouter, runner: CountingRunner
    ) -> None:
        assert await router.dispatch_all([]) == ()
        assert runner.calls == 0
