"""触发事件的派发 —— 把事件送进演进循环，并记住谁真的开了一轮。

设计依据：docs/tasks.md TASK-P3-011（验收条件"六类事件各能触发一轮演进；
同一事件去重后只触发一次"；验证 T1 + T2）、R-079、R-080、I-11；
docs/design.md §4。

**这一层是六路触发源与演进循环之间的唯一通道。** 六路源各读各的来源、各产
各的事件，但**派发只有一条路** —— 因为去重窗口只有一份。因果链（回环判定）
也只有一份，六路各判各的就会出现"代码事件不回环、外部事件回环"这种说不通
的局面。

**派发这一层不判断"该不该触发"。** 判断在 :class:`~orchestration.dedup.
EvolutionTrigger` 里（回环 → 去重 → 熔断，顺序不能换）。这一层做的是
**把领域事件翻译成编排层的事件**，以及**把结果记下来**。

**为什么记数。** 验收条件里"同一事件去重后只触发一次"是一个**可量化的
数**：同一条事件投递 N 次，实际开轮数必须是 1。这个数只有一个地方能算准 ——
派发这一层，因为它同时看得见"投了几次"与"跑了没跑"。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

from core.events.dedup import (
    DEFAULT_DEDUP_WINDOW,
    CausationTracker,
    EventDedupWindow,
    InMemoryCausationChainStore,
    InMemoryDedupWindowStore,
)
from core.evolution.evolution_loop import TriggerSource
from core.evolution.stall import InMemoryGapProgressStore, StallDetector
from core.evolution.triggers import TriggerEvent
from orchestration.dedup import (
    CycleRunner,
    EvolutionTrigger,
    IncomingEvent,
    LoopBreaker,
    TriggerOutcome,
)


class TriggerRouter:
    """把触发事件派发进演进循环。

    :param trigger: 事件 → 演进循环的入口。

    **它是唯一的一扇门。** 六路源把事件交给它，它交给
    :class:`~orchestration.dedup.EvolutionTrigger` 判回环与去重，然后才谈
    上"要不要开一轮"。
    """

    __slots__ = ("_started", "_suppressed", "_trigger")

    def __init__(self, trigger: EvolutionTrigger) -> None:
        self._trigger = trigger
        self._started: dict[TriggerSource, int] = {}
        self._suppressed: dict[TriggerSource, int] = {}

    async def dispatch(self, event: TriggerEvent) -> TriggerOutcome:
        """派发一条事件。

        :param event: 要派发的触发事件。
        :return: 这次触发的结论。**被挡下时 :attr:`TriggerOutcome.ran` 是
            ``False``** —— 那一轮压根没开始。
        """
        outcome = await self._trigger.handle(self._as_incoming(event))
        source = event.source
        if outcome.ran:
            self._started[source] = self._started.get(source, 0) + 1
        else:
            self._suppressed[source] = self._suppressed.get(source, 0) + 1
        return outcome

    async def dispatch_all(self, events: Iterable[TriggerEvent]) -> tuple[TriggerOutcome, ...]:
        """依次派发一批事件。

        **顺序有意义** —— 同一批里若有互相重复的事件，先来的先占去重窗口。
        """
        results: list[TriggerOutcome] = []
        for event in events:
            results.append(await self.dispatch(event))
        return tuple(results)

    @property
    def started(self) -> Mapping[TriggerSource, int]:
        """各类触发源**实际开轮**的次数。"""
        return MappingProxyType(dict(self._started))

    @property
    def suppressed(self) -> Mapping[TriggerSource, int]:
        """各类触发源**被挡下**的次数。"""
        return MappingProxyType(dict(self._suppressed))

    def _as_incoming(self, event: TriggerEvent) -> IncomingEvent:
        """领域事件 → 编排层的事件。

        **事件类型取的是事件类型的值，不是来源的名字。** 去重指纹是
        ``(event_type, aggregate_id)`` —— 事件类型必须**细到能区分事情**，
        用来源的名字会把六种事压成一种。
        """
        return IncomingEvent(
            event_type=event.event_type.value,
            aggregate_id=event.aggregate_id,
            project_id=event.project_id,
            causation_id=event.causation_id,
            trigger=event.source,
        )


def build_router(
    runner: CycleRunner,
    *,
    window: timedelta = DEFAULT_DEDUP_WINDOW,
    clock: Callable[[], datetime] | None = None,
    max_unchanged_rounds: int = 2,
) -> TriggerRouter:
    """装一台默认的触发路由。

    :param runner: 跑一轮演进的端口。
    :param window: 去重窗口的长度。
    :param clock: 现在的时刻。**同一个钟要同时喂给去重窗口与熔断器** ——
        两边各看各的时间，"窗口里"与"连续几轮没改善"就对不上了。
    :param max_unchanged_rounds: 连续几轮没改善就熔断。

    **默认用内存存储。** 这是**已知不足**：进程重启，去重窗口就空了，同一
    件事再来一遍会被当成新的。真正的做法是把窗口与链落库（与周期记录放在
    一起）。这一层不写库，所以只能这样。
    """
    trigger = EvolutionTrigger(
        breaker=LoopBreaker(
            window=EventDedupWindow(
                InMemoryDedupWindowStore(),
                window=window,
                clock=clock,
            ),
            chain=CausationTracker(InMemoryCausationChainStore()),
            stall=StallDetector(
                InMemoryGapProgressStore(),
                max_unchanged_rounds=max_unchanged_rounds,
                clock=clock,
            ),
        ),
        runner=runner,
    )
    return TriggerRouter(trigger)


#: 触发路由的默认去重窗口。
#:
#: **为什么是十五分钟。** 与 :data:`core.events.dedup.DEFAULT_DEDUP_WINDOW`
#: 同值 —— 短于一次持续集成重试的间隔（那样一次故障只算一次），长于一轮
#: 演进从触发到出结论的时间（那样同一件事不会在两轮之间被算两次）。
DEFAULT_TRIGGER_WINDOW: Final[timedelta] = DEFAULT_DEDUP_WINDOW


__all__ = [
    "DEFAULT_TRIGGER_WINDOW",
    "TriggerRouter",
    "build_router",
]
