"""事件去重、因果链与回环熔断的编排。

设计依据：docs/tasks.md TASK-P1-064（交付物：事件去重窗口（按 ``event_type``
+ ``aggregate_id`` + 时间窗）、因果链 ID 贯穿演进→执行→回写、熔断规则）；T1 ①
同一事件重复投递只被消费一次；T1 ② 演进循环触发的执行，其结果回写**不再触发
新一轮演进循环**；T1 ③ 同一 gap 连续 2 轮未改善 → 熔断，产生 ``stalled`` 标记
而非继续规划；I-11。

**这一层做三件事，顺序不能换。**

1. **判回环** —— 这条事件是不是我们自己触发的后果（因果链上有演进循环）。
2. **判去重** —— 同一种事件在同一个主体上，窗口内是不是已经来过一次。
3. **判熔断** —— 这一轮里那些连续没改善的差距，要不要继续规划。

**为什么回环要判在去重前面。** 回环是"**根本不该发生**"，去重是"发生得太多
了"。把去重放在前面，回环事件会先占掉去重窗口 —— 于是真正该被去重的那条
事件反倒被放行了：窗口被一条**根本不该处理**的事件占了。顺序反了，两道闸
门就互相削弱。

**闸门只给结论，不改状态。** 与 :mod:`orchestration.gates`、
:mod:`orchestration.repair` 同一套做法：结论交出去，写状态的是调用方。两边
都写就会有一边写丢。

**"不再触发新一轮演进"是「不产生新的周期」，不是「产生一个标记」。** 所以这里
的入口是"事件 → 演进循环"那道门：被挡下的事件**根本不进循环**，连周期行都
不会多一行。用标记表示"这一轮我决定不做"是另一种做法，但那样数据库里会
多出一行"被拒绝的周期"—— 事后数周期的人会以为真跑过一轮。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from langgraph.graph.state import CompiledStateGraph

from core.events.dedup import (
    CausationLink,
    CausationTracker,
    ChainKind,
    DedupDecision,
    EventDedupWindow,
    EventFingerprint,
)
from core.evolution.assessment import GapSeverity, OverallStatus
from core.evolution.evolution_loop import LoopBState, TriggerSource
from core.evolution.stall import StallDetector, StallVerdict
from core.planning.stages import GapAnalysis
from orchestration.loop_b import run_loop_b


@dataclass(frozen=True, slots=True)
class IncomingEvent:
    """一条想触发演进循环的事件。

    :attr:`causation_id` 是**因果链 id** —— 没有它就是外部触发（人、定时
    器、外部系统）；有它就要查链上有没有演进循环。

    :attr:`trigger` 会写进周期记录（``evolution_cycle.trigger``）。**它要说
    得出这一轮是被什么触发的** —— 说不清的话，事后没人能判断"该不该在这一
    轮做这件事"。
    """

    #: 事件的种类（如 ``ci.failed``）。
    event_type: str
    #: 事件的主体（如某个任务、某个流水线）。
    aggregate_id: str
    #: 事件属于哪个项目。
    project_id: str
    #: 因果链 id。没有就是外部触发。
    causation_id: str | None = None
    #: 触发源。
    trigger: TriggerSource = TriggerSource.EXTERNAL

    @property
    def fingerprint(self) -> EventFingerprint:
        """去重用的指纹 —— (种类, 主体)。"""
        return EventFingerprint(event_type=self.event_type, aggregate_id=self.aggregate_id)


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    """要不要为这条事件开一轮演进。

    :attr:`reason` 是人话。**"不开"必须说得清为什么** —— 是回环还是重复，
    两者的处置完全不同：回环要改的是"别让它转回来"，重复要改的是"别让它
    来这么多次"。
    """

    #: 开不开。
    should_trigger: bool
    #: 为什么。
    reason: str
    #: 这条事件的指纹。
    fingerprint: EventFingerprint
    #: 事件的因果链 id。
    causation_id: str | None = None

    @property
    def suppressed(self) -> bool:
        """是不是被挡下了。"""
        return not self.should_trigger


@dataclass(frozen=True, slots=True)
class TriggerOutcome:
    """一次触发的结果。

    :attr:`state` 是跑出来的那一轮 —— **被挡下时是空的**。那一轮压根没
    开始，所以没有状态可言；给一个空的壳反而会让调用方以为"跑了但没跑成"。
    """

    #: 判定结论。
    decision: TriggerDecision
    #: 跑出来的那一轮（被挡下时为空）。
    state: LoopBState | None = None

    @property
    def ran(self) -> bool:
        """这一轮真的跑了没有。"""
        return self.state is not None


@runtime_checkable
class CycleRunner(Protocol):
    """跑一轮演进循环。

    **它是一个端口，不是循环 B 的实现。** 编排层不知道循环 B 怎么跑 ——
    它只知道"判完了要跑的时候，叫它跑"。
    """

    async def run(self, event: IncomingEvent) -> LoopBState:
        """为这条事件跑一轮演进。"""
        ...


class CausationChainFactory:
    """因果链的开头与派生 —— **把演进→执行→回写串起来**。

    **为什么用三个方法而不是让调用方自己拼。** 链上每一环的 ``kind`` 是判定
    回环的依据（链上有 ``evolution`` 就是回环）—— 拼错一个 kind，回环判定
    就失效了，而**失效的样子是"什么都不挡"**，看上去一切正常。
    """

    __slots__ = ("_tracker",)

    def __init__(self, tracker: CausationTracker) -> None:
        self._tracker = tracker

    async def open_cycle(
        self,
        *,
        causation_id: str,
        parent_id: str | None = None,
    ) -> CausationLink:
        """一轮演进的因果链起点。

        :param parent_id: 触发它的那条事件的链 id。没有就是这条事件自己就是
            链头（外部触发）。
        """
        if parent_id is None:
            return await self._tracker.start(
                causation_id=causation_id,
                kind=ChainKind.EVOLUTION,
            )
        return await self._tracker.derive(
            causation_id=causation_id,
            parent_id=parent_id,
            kind=ChainKind.EVOLUTION,
        )

    async def open_execution(
        self,
        *,
        causation_id: str,
        parent_id: str,
    ) -> CausationLink:
        """一轮执行的因果链。"""
        return await self._tracker.derive(
            causation_id=causation_id,
            parent_id=parent_id,
            kind=ChainKind.EXECUTION,
        )

    async def open_writeback(
        self,
        *,
        causation_id: str,
        parent_id: str,
    ) -> CausationLink:
        """一次结果回写的因果链。**回写是回环的起点** —— 它最容易被当成
        "一件新事情"而触发新一轮。"""
        return await self._tracker.derive(
            causation_id=causation_id,
            parent_id=parent_id,
            kind=ChainKind.WRITEBACK,
        )


class LoopBreaker:
    """回环熔断器 —— 三道闸门合成一个结论。

    :param window: 事件去重窗口。
    :param chain: 因果链的追踪器。
    :param stall: 差距停滞的检测器。

    **它是有状态的**（窗口与链都记着东西），所以一个项目一个实例。
    """

    __slots__ = ("_chain", "_stall", "_window")

    def __init__(
        self,
        *,
        window: EventDedupWindow,
        chain: CausationTracker,
        stall: StallDetector,
    ) -> None:
        self._window = window
        self._chain = chain
        self._stall = stall

    async def consider(self, event: IncomingEvent) -> TriggerDecision:
        """这条事件要不要开新一轮演进。

        **顺序是：先回环，后去重。** 理由见模块说明。
        """
        fingerprint = event.fingerprint

        # 一、回环：这条事件是不是我们自己触发的后果。
        if await self._chain.is_echo(event.causation_id):
            return TriggerDecision(
                should_trigger=False,
                reason="因果链上有演进循环 —— 这是回环，不再开新一轮",
                fingerprint=fingerprint,
                causation_id=event.causation_id,
            )

        # 二、去重窗口：同一种事件在同一个主体上，窗口内只算一次。
        decision: DedupDecision = await self._window.admit(fingerprint)
        if decision.duplicate:
            return TriggerDecision(
                should_trigger=False,
                reason=decision.reason,
                fingerprint=fingerprint,
                causation_id=event.causation_id,
            )

        return TriggerDecision(
            should_trigger=True,
            reason="允许开新一轮演进",
            fingerprint=fingerprint,
            causation_id=event.causation_id,
        )

    async def observe_gap(self, gap_id: str, *, severity: GapSeverity | None) -> StallVerdict:
        """记一轮差距观察，给出它现在的结论。"""
        return await self._stall.observe(gap_id, severity=severity)

    async def stalled_gaps(self) -> tuple[str, ...]:
        """已经熔断的那些差距 id。"""
        return await self._stall.stalled_gaps()


class EvolutionTrigger:
    """事件 → 演进循环的入口。

    :param breaker: 回环熔断器。
    :param runner: 跑一轮演进的端口。

    **它只做"判 + 叫跑"，不自己跑图。** 循环 B 的图归 :mod:`orchestration.
    loop_b` 管；这里管的是"这件事该不该进循环"。
    """

    __slots__ = ("_breaker", "_runner")

    def __init__(self, *, breaker: LoopBreaker, runner: CycleRunner) -> None:
        self._breaker = breaker
        self._runner = runner

    async def handle(self, event: IncomingEvent) -> TriggerOutcome:
        """处理一条事件 —— **该跑才跑**。

        **被挡下的时候，周期行一行都不多。** 跑也不跑的区别不是"少了一次
        规划"，而是库里多了一行周期记录 —— 事后数周期的人会以为真跑过一轮。
        """
        decision = await self._breaker.consider(event)
        if not decision.should_trigger:
            return TriggerOutcome(decision=decision, state=None)
        state = await self._runner.run(event)
        return TriggerOutcome(decision=decision, state=state)


def exclude_stalled(gaps: GapAnalysis, stalled: tuple[str, ...]) -> GapAnalysis:
    """把熔断的差距从这一轮的分析里去掉。

    **熔断的意思是"停止重规划这条差距"** —— 所以它不能只停在一句标记上：
    规划器要是还能看见它，就会照旧为它排任务。

    **差距全被熔断时，整体状态改成 ``stalled``。** 因为那正是这个状态的
    定义：**能推进，但连续几轮没有可验证的改善**（见
    :class:`core.evolution.assessment.OverallStatus`）。留着 ``evolving``
    会让人以为"下一步很明确" —— 而实际上下一步是**换方向**，那要人参与。
    """
    if not stalled:
        return gaps
    kept = tuple(gap for gap in gaps.gaps if gap.id not in stalled)
    if len(kept) == len(gaps.gaps):
        return gaps
    return gaps.model_copy(
        update={
            "gaps": kept,
            "overall_status": (gaps.overall_status if kept else OverallStatus.STALLED),
        }
    )


class LoopBCycleRunner:
    """把循环 B 的图包成 :class:`CycleRunner`。

    :param graph: 循环 B 的图。
    :param cycle_ids: 周期 id 的来源。**每一轮都要新的** —— 同一个
        ``cycle_id`` 开两次，库会直接报错（那说明两条流程在抢同一轮）。

    **它只跑到"跑完"为止，不解释结果。** 这一层管的是"该不该跑"，跑出来
    什么由循环 B 自己负责。
    """

    __slots__ = ("_cycle_ids", "_graph")

    def __init__(
        self,
        graph: CompiledStateGraph[LoopBState, None, LoopBState, LoopBState],
        *,
        cycle_ids: Callable[[], str],
    ) -> None:
        self._graph = graph
        self._cycle_ids = cycle_ids

    async def run(self, event: IncomingEvent) -> LoopBState:
        """跑一轮循环 B。"""
        state = LoopBState(
            project_id=event.project_id,
            cycle_id=self._cycle_ids(),
            trigger=event.trigger,
        )
        return await run_loop_b(self._graph, state)


__all__ = [
    "CausationChainFactory",
    "CycleRunner",
    "EvolutionTrigger",
    "IncomingEvent",
    "LoopBCycleRunner",
    "LoopBreaker",
    "TriggerDecision",
    "TriggerOutcome",
    "exclude_stalled",
]
