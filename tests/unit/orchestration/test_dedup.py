"""TASK-P1-064 —— 事件去重、因果链与回环熔断的编排测试。

设计依据：docs/tasks.md TASK-P1-064（T1 ① 同一事件重复投递只被消费一次；
T1 ② 演进循环触发的执行，其结果回写**不再触发新一轮演进循环**；T1 ③ 同一 gap
连续 2 轮未改善 → 熔断，产生 ``stalled`` 标记而非继续规划）、I-11。

**这里验的是"三道闸门接在一起时，挡没挡住"。** 领域规则在
``tests/unit/core/test_dedup.py`` 与 ``tests/unit/core/test_stall.py`` 里各
自验过；那一层不知道"闸门"是什么，也不需要知道。**这一层验的是组合** ——
顺序错了、入口接错了，规则再对也没用。

**为什么"没产生新的 cycle"要盯"跑都没跑"，而不是盯"跑出一个标记"。** 一轮
"我决定不做"留一行周期记录，看上去也能说明"这一轮被挡了"。但事后数周期的人
会以为真跑过一轮 —— 而**它压根没跑**，那些"跑过"的记录会让"这项目一周跑了
多少轮演进"这个问题得到一个错的答案。所以断言的是**周期行一行都不多**。

**为什么"回环要先判"要单独钉一条。** 两道闸门挨在一起，顺序反了不会报错 ——
回环事件会先占掉去重窗口，于是真正该被去重的那条事件反倒被放行。两道闸门
互相削弱，而**症状是"系统偶尔多跑一轮"，看起来像偶发**。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.events.dedup import (
    CausationTracker,
    ChainKind,
    EventDedupWindow,
    InMemoryCausationChainStore,
    InMemoryDedupWindowStore,
)
from core.evolution.assessment import GapSeverity, OverallStatus
from core.evolution.evolution_loop import LoopBState, TriggerSource
from core.evolution.stall import InMemoryGapProgressStore, StallDetector
from core.planning.stages import GapAnalysis
from orchestration.dedup import (
    CausationChainFactory,
    EvolutionTrigger,
    IncomingEvent,
    LoopBreaker,
    exclude_stalled,
)
from tests.support.evolution_loop import demo_gaps

_PROJECT_ID = "p-dedup"


@dataclass
class _RecordingRunner:
    """数着跑了多少轮的假执行口。

    **它记的是"被叫了几次"，不是"跑成功了几次"。** 因为要挡的正是**被叫**
    —— 一轮被叫了但没跑完，仍然是"多了一轮"。
    """

    calls: list[IncomingEvent] = field(default_factory=list)

    async def run(self, event: IncomingEvent) -> LoopBState:
        self.calls.append(event)
        return LoopBState(
            project_id=event.project_id,
            cycle_id=f"cycle-{len(self.calls):04d}",
            trigger=event.trigger,
        )


def _breaker() -> tuple[LoopBreaker, CausationTracker]:
    """一套闸门 —— 去重窗口、因果链、停滞检测各用内存实现。"""
    chain = CausationTracker(InMemoryCausationChainStore())
    breaker = LoopBreaker(
        window=EventDedupWindow(InMemoryDedupWindowStore()),
        chain=chain,
        stall=StallDetector(InMemoryGapProgressStore()),
    )
    return breaker, chain


def _event(
    *,
    event_type: str = "ci.failed",
    aggregate_id: str = "pipeline-1",
    causation_id: str | None = None,
    trigger: TriggerSource = TriggerSource.EXTERNAL,
) -> IncomingEvent:
    return IncomingEvent(
        event_type=event_type,
        aggregate_id=aggregate_id,
        project_id=_PROJECT_ID,
        causation_id=causation_id,
        trigger=trigger,
    )


def _two_gaps() -> GapAnalysis:
    """两条差距的分析 —— 用来验"熔断一条不影响另一条"。"""
    gap = demo_gaps().gaps[0]
    return GapAnalysis(
        project_id=_PROJECT_ID,
        snapshot="snap-1",
        overall_status=OverallStatus.EVOLVING,
        gaps=(gap, gap.model_copy(update={"id": "gap-2"})),
    )


class TestTheTrigger:
    """事件 → 演进循环那道门。"""

    async def test_an_external_event_starts_a_cycle(self) -> None:
        """**外面来的事件要能开新一轮** —— 否则系统自己永远不会动。"""
        breaker, _ = _breaker()
        runner = _RecordingRunner()
        trigger = EvolutionTrigger(breaker=breaker, runner=runner)

        outcome = await trigger.handle(_event())

        assert outcome.decision.should_trigger is True
        assert outcome.ran is True
        assert len(runner.calls) == 1

    async def test_the_same_event_twice_starts_only_one_cycle(self) -> None:
        """**T1 ① —— 同一事件重复投递只被消费一次。**

        **这里盯的是"跑了几轮"，不是"第二次返回了什么"。** 第二次被挡下的
        表现可以是"返回 False"、"抛异常"、"什么都不做"—— 但只要它还开了一轮
        演进，T1 ① 就没有成立。
        """
        breaker, _ = _breaker()
        runner = _RecordingRunner()
        trigger = EvolutionTrigger(breaker=breaker, runner=runner)

        await trigger.handle(_event())
        second = await trigger.handle(_event())

        assert len(runner.calls) == 1
        assert second.decision.should_trigger is False
        assert second.ran is False

    async def test_a_different_aggregate_starts_its_own_cycle(self) -> None:
        """**去重粒度是 (种类, 主体)** —— 另一条流水线挂了就是另一件事。"""
        breaker, _ = _breaker()
        runner = _RecordingRunner()
        trigger = EvolutionTrigger(breaker=breaker, runner=runner)

        await trigger.handle(_event(aggregate_id="pipeline-1"))
        await trigger.handle(_event(aggregate_id="pipeline-2"))

        assert len(runner.calls) == 2

    async def test_a_writeback_does_not_start_a_new_cycle(self) -> None:
        """**T1 ② —— 演进循环触发的执行，其结果回写不再触发新一轮演进循环。**

        **断言的是"没跑"。** 不是"跑出一个标记说被挡了"—— 那一轮压根不该
        开始，周期记录里也就不该多一行。
        """
        breaker, chain = _breaker()
        factory = CausationChainFactory(chain)
        await factory.open_cycle(causation_id="E1")
        await factory.open_execution(causation_id="X1", parent_id="E1")
        await factory.open_writeback(causation_id="W1", parent_id="X1")

        runner = _RecordingRunner()
        trigger = EvolutionTrigger(breaker=breaker, runner=runner)

        outcome = await trigger.handle(_event(causation_id="W1"))

        assert len(runner.calls) == 0
        assert outcome.decision.should_trigger is False
        assert outcome.ran is False
        assert outcome.state is None

    async def test_the_writeback_says_why_it_was_stopped(self) -> None:
        """**"不开"必须说得清为什么** —— 是回环还是重复，两者的处置完全
        不同：回环要改的是"别让它转回来"，重复要改的是"别让它来这么多次"。"""
        breaker, chain = _breaker()
        factory = CausationChainFactory(chain)
        await factory.open_cycle(causation_id="E1")
        await factory.open_execution(causation_id="X1", parent_id="E1")
        await factory.open_writeback(causation_id="W1", parent_id="X1")

        trigger = EvolutionTrigger(breaker=breaker, runner=_RecordingRunner())

        outcome = await trigger.handle(_event(causation_id="W1"))

        assert "回环" in outcome.decision.reason

    async def test_a_writeback_does_not_consume_the_dedup_window(self) -> None:
        """**回环要先判 —— 顺序反了，两道闸门会互相削弱。**

        先判去重的话，回环事件会先把去重窗口占掉；于是真正该被去重的那条
        事件（外部来的同一种）反倒被放行。**症状是"系统偶尔多跑一轮"** ——
        看起来像偶发，实际上每一轮都在发生。
        """
        breaker, chain = _breaker()
        factory = CausationChainFactory(chain)
        await factory.open_cycle(causation_id="E1")
        await factory.open_writeback(causation_id="W1", parent_id="E1")

        runner = _RecordingRunner()
        trigger = EvolutionTrigger(breaker=breaker, runner=runner)

        await trigger.handle(_event(causation_id="W1"))
        after = await trigger.handle(_event())

        assert len(runner.calls) == 1
        assert after.decision.should_trigger is True

    async def test_an_event_without_a_chain_is_not_an_echo(self) -> None:
        """**没有因果链就是外面来的** —— 人点的、定时器触发的、外部系统推来的
        都该开新一轮。挡它等于系统再也不会自己动。"""
        breaker, _ = _breaker()
        runner = _RecordingRunner()
        trigger = EvolutionTrigger(breaker=breaker, runner=runner)

        await trigger.handle(_event(trigger=TriggerSource.SCHEDULE))

        assert len(runner.calls) == 1


class TestTheCausationChainFactory:
    """演进 → 执行 → 回写，串成一条链。"""

    async def test_the_writeback_is_an_echo(self) -> None:
        """**回写带着它上游那条链** —— 链上有演进循环，它就是回环。"""
        tracker = CausationTracker(InMemoryCausationChainStore())
        factory = CausationChainFactory(tracker)

        await factory.open_cycle(causation_id="E1")
        await factory.open_execution(causation_id="X1", parent_id="E1")
        await factory.open_writeback(causation_id="W1", parent_id="X1")

        assert await tracker.is_echo("W1") is True

    async def test_the_cycle_link_is_an_echo_too(self) -> None:
        """**由这一环直接引起的事件也是回环** —— 它是这次演进的后果。

        **判据是"链上有演进循环"，不是"链头是演进循环"。** 演进循环那一环
        自己就带着 ``evolution``，所以它的下游（执行、回写、以及由这一环
        直接引起的事件）全都在链上留着它 —— 全都算回环。只有"链头是演进
        循环"那种写法会漏掉绕回来的那几种。
        """
        tracker = CausationTracker(InMemoryCausationChainStore())
        factory = CausationChainFactory(tracker)

        await factory.open_cycle(causation_id="E1")

        assert await tracker.is_echo("E1") is True

    async def test_the_execution_is_already_an_echo(self) -> None:
        """**执行那一环就是回环的起点** —— 它是演进循环发起的后续。"""
        tracker = CausationTracker(InMemoryCausationChainStore())
        factory = CausationChainFactory(tracker)

        await factory.open_cycle(causation_id="E1")
        await factory.open_execution(causation_id="X1", parent_id="E1")

        assert await tracker.is_echo("X1") is True

    async def test_a_cycle_derived_from_an_event_keeps_the_parent(self) -> None:
        """**演进循环也可以挂在一条事件下面** —— 那条事件是它上游的来源，
        链不能断在这里。"""
        tracker = CausationTracker(InMemoryCausationChainStore())
        factory = CausationChainFactory(tracker)

        await tracker.start(causation_id="E1", kind=ChainKind.ROOT)
        await factory.open_cycle(causation_id="E2", parent_id="E1")

        links = await tracker.chain("E2")

        assert [link.causation_id for link in links] == ["E2", "E1"]
        assert await tracker.is_echo("E2") is True


class TestTheStall:
    """熔断：连续几轮没改善就不再规划这条差距。"""

    async def test_two_unchanged_rounds_mark_the_gap_as_stalled(self) -> None:
        """**T1 ③ —— 同一 gap 连续 2 轮未改善 → 熔断，产生 ``stalled`` 标记。**"""
        breaker, _ = _breaker()

        await breaker.observe_gap("gap-1", severity=GapSeverity.HIGH)
        await breaker.observe_gap("gap-1", severity=GapSeverity.HIGH)
        verdict = await breaker.observe_gap("gap-1", severity=GapSeverity.HIGH)

        assert verdict.stalled is True
        assert verdict.marker.value == "stalled"

    async def test_the_stalled_gap_is_not_planned_again(self) -> None:
        """**熔断不能只停在一句标记上** —— 规划器要是还能看见它，就会照旧
        为它排任务，而那就是"继续规划"了。"""
        breaker, _ = _breaker()
        await breaker.observe_gap("gap-1", severity=GapSeverity.HIGH)
        await breaker.observe_gap("gap-1", severity=GapSeverity.HIGH)
        await breaker.observe_gap("gap-1", severity=GapSeverity.HIGH)

        gaps = exclude_stalled(demo_gaps(), await breaker.stalled_gaps())

        assert tuple(gap.id for gap in gaps.gaps) == ()

    async def test_the_other_gaps_are_still_planned(self) -> None:
        """**熔断是按差距算的，不是按项目算的** —— 一条停滞了不该把另一条
        一起挡掉。"""
        breaker, _ = _breaker()
        await breaker.observe_gap("gap-1", severity=GapSeverity.HIGH)
        await breaker.observe_gap("gap-1", severity=GapSeverity.HIGH)
        await breaker.observe_gap("gap-1", severity=GapSeverity.HIGH)

        gaps = exclude_stalled(_two_gaps(), await breaker.stalled_gaps())

        assert tuple(gap.id for gap in gaps.gaps) == ("gap-2",)
        assert gaps.overall_status is OverallStatus.EVOLVING

    async def test_the_overall_status_becomes_stalled_when_all_gaps_are(self) -> None:
        """**差距全被熔断时，整体状态是 ``stalled``。** 因为那正是这个状态的
        定义：能推进，但连续几轮没有可验证的改善。留着 ``evolving`` 会让人
        以为"下一步很明确" —— 而实际上下一步是**换方向**，那要人参与。"""
        gaps = exclude_stalled(demo_gaps(), ("gap-1",))

        assert gaps.overall_status is OverallStatus.STALLED

    def test_nothing_stalled_means_nothing_changes(self) -> None:
        """**没熔断就原样返回** —— 免得每次都复制一份分析，把"这一轮改了什么"
        搅浑。"""
        original = demo_gaps()

        assert exclude_stalled(original, ()) is original


class TestTheGapAnalysis:
    """``exclude_stalled`` 对分析本身的处理。"""

    def test_the_kept_gaps_keep_their_order(self) -> None:
        """**顺序是分析给的** —— 排除几条不该重排剩下的。"""
        analysis = GapAnalysis(
            project_id=_PROJECT_ID,
            snapshot="snap-1",
            overall_status=OverallStatus.EVOLVING,
            gaps=demo_gaps().gaps,
        )

        result = exclude_stalled(analysis, ("gap-1",))

        assert result.gaps == ()

    def test_an_unknown_gap_id_changes_nothing(self) -> None:
        """**名单里有一条不存在的差距，不该动别的。**"""
        analysis = demo_gaps()

        result = exclude_stalled(analysis, ("gap-999",))

        assert result.gaps == analysis.gaps


@pytest.mark.parametrize(
    ("severity", "expected_marker"),
    [
        (GapSeverity.HIGH, "no_change"),
        (GapSeverity.LOW, "improved"),
    ],
)
async def test_the_marker_follows_the_observation(
    severity: GapSeverity,
    expected_marker: str,
) -> None:
    """**三种结局缺一不可**：只说"没动"，就分不出"再等等"与"该叫人来看了"。"""
    breaker, _ = _breaker()

    await breaker.observe_gap("gap-1", severity=GapSeverity.HIGH)
    verdict = await breaker.observe_gap("gap-1", severity=severity)

    assert verdict.marker.value == expected_marker
