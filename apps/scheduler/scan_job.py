"""定时扫描作业 —— 定期扫一遍项目，把新问题送进演进循环。

设计依据：docs/tasks.md TASK-P3-012（交付物：跨模块、长期积累问题的定期
扫描）；TASK-P3-011（六类触发源里的"定时"那一类）；R-080（事件驱动与定期
扫描结合）；docs/design.md §4（六个触发源：定时 / 代码 / 质量 / 运行 /
外部 / 知识）。

**这一层是六触发源里的"定时"那一路。** 其余五路都是**某件事发生了**才
来的（提交、流水线失败、用户反馈、依赖漏洞）；定时这一路没有事件 —— 它
自己就是事件："到了该扫的时候了"。这两路的差别不在代码，在**触发条件**。

**扫到问题不等于要开一轮演进。** 定时扫描天天跑同一个仓库，同一个问题
天天报一遍没有意义 —— 所以只有**新出现**的信号才值得开一轮。这一层把
"上轮报过的"记在 :class:`ScanHistory` 里，两轮一比就知道哪些是新的。

**为什么新信号要走事件，而不是直接调演进循环。** 因为事件那一侧有去重
窗口（P1-064）：同一种事件在同一个主体上，窗口内只算一次。扫描产出的
事件类型是固定的（``scan.found``）、主体是项目 —— 所以两次扫描报出**同样
**的问题时，第二轮会被窗口挡下来。这正是我们要的：同一件事不该开两轮。

**"没开轮"必须说得清为什么。** 开了是触发，挡了也要有理由：是没到期、
是没有新问题、还是被去重窗口挡了。这三种处置完全不同，混成一句"跳过"
就没人知道该修哪儿。

**这一层不写库。** 扫描结果留在报告里，由调用方决定怎么落。落库是演进
流水线的事（P3-013），不是这一层的事。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, runtime_checkable

from core.evolution.evolution_loop import TriggerSource
from core.evolution.scan import ScanReport, ScanScope, ScanSignal
from orchestration.dedup import IncomingEvent, TriggerOutcome
from orchestration.scan import ProjectScanner

#: **扫描事件的类型**。
#:
#: 为什么是固定的一个类型：去重窗口按 ``(event_type, aggregate_id)`` 去重
#: （P1-064）。事件类型固定、主体是项目，于是"两轮扫描报出同样的问题"就
#: 落在同一个指纹上 —— 第二轮被窗口挡下，正是我们要的行为。
SCAN_EVENT_TYPE: Final[str] = "scan.found"

#: **默认扫描间隔**：一天。
#:
#: 为什么要这么长：扫描读的是索引，不读仓库（快），但它产出的候选是要
#: 进决策层排期的 —— 一天扫一次，问题会以"每天一次"的速度浮上来，而不是
#: 一次涌进来几十条。
DEFAULT_SCAN_INTERVAL: Final[timedelta] = timedelta(days=1)


@runtime_checkable
class ScanTriggerPort(Protocol):
    """把扫描事件送进演进循环的口子。

    **它是一个端口，不是 :class:`~orchestration.dedup.EvolutionTrigger`。**
    这一层只需要"事件 → 结论"这一个动作；具体是谁在另一头（真实现、
    测试替身）不归它管。
    """

    async def handle(self, event: IncomingEvent) -> TriggerOutcome:
        """处理一条事件。"""
        ...


@runtime_checkable
class ScanHistory(Protocol):
    """上一次扫描扫到了什么。

    **它回答的是"哪些是新的"。** 没有它，定时扫描每天都会把同一批问题
    重新报一遍 —— 而每一遍都开一轮演进，预算几天就烧光了。
    """

    @property
    def last_run_at(self) -> datetime | None:
        """上一次扫描的时间。没有就是没扫过。"""
        ...

    @property
    def last_fingerprints(self) -> frozenset[str]:
        """上一次扫出来的所有信号的身份。"""
        ...

    def remember(self, report: ScanReport, *, at: datetime) -> None:
        """记住这次扫到了什么。"""
        ...


@dataclass
class InMemoryScanHistory:
    """进程内的扫描历史。

    **它只活在内存里，所以进程重启就忘了。** 这是**已知不足**，不是设计：
    重启后第一轮扫描会把上轮报过的问题当成新的再报一遍。多报一次不会
    造成损失（决策层会 reject 掉重复的），但它确实是个缺口 —— 真正的做法
    是把这份历史落到库里，与周期记录放在一起。

    **它记得的只有身份，不是信号本身。** 身份由种类与模块集合决定，
    不含计数与时间（见 :attr:`core.evolution.scan.ScanSignal.fingerprint`）
    —— 所以"耦合环还在"这件事跨轮次是可比的。
    """

    _last_run_at: datetime | None = None
    _last_fingerprints: frozenset[str] = field(default_factory=frozenset)

    @property
    def last_run_at(self) -> datetime | None:
        """上一次扫描的时间。"""
        return self._last_run_at

    @property
    def last_fingerprints(self) -> frozenset[str]:
        """上一次扫出来的所有信号的身份。"""
        return self._last_fingerprints

    def remember(self, report: ScanReport, *, at: datetime) -> None:
        """记住这次扫到了什么。"""
        self._last_run_at = at
        self._last_fingerprints = report.fingerprints


def fresh_signals(
    scanner: ProjectScanner,
    scope: ScanScope,
    history: ScanHistory,
    *,
    scan_id: str,
    at: datetime,
) -> tuple[ScanReport, tuple[ScanSignal, ...]]:
    """扫一遍，并挑出**新出现**的信号。

    :param scanner: 扫描器。
    :param scope: 扫描范围。
    :param history: 扫描历史 —— 用来判断哪些信号是新的。
    :param scan_id: 这次扫描的标识。
    :param at: 这次扫描的时刻。
    :return: (报告, 新出现的信号)。

    **"新旧"这件事只有一处知道，所以只有这一处能算。** 定时扫描作业
    （:class:`ScheduledScanner`）与定时触发源（:class:`apps.triggers.sources.
    ScanScheduleSource`）都要"扫一遍、比一比、记下来"—— **两处各写一遍，
    迟早有一处改了另一处没改**，于是"什么算新问题"在系统里有了两个答案。

    **扫完就记住。** 历史在这一步就更新 —— 因为"这次扫到了什么"与"这次有
    什么是新的"是同一次扫描的两面，中间隔着就会有人插进来再扫一遍。
    """
    report = scanner.scan(scope, scan_id=scan_id)
    known = history.last_fingerprints
    fresh = tuple(item for item in report.signals if item.fingerprint not in known)
    history.remember(report, at=at)
    return report, fresh


@dataclass(frozen=True, slots=True)
class ScheduledScanOutcome:
    """一次定时扫描的结果。

    :attr:`reason` 是人话。**"没开轮"必须有理由** —— 是没到期、是没有新
    问题、还是被去重窗口挡了？三种处置完全不同。
    """

    #: 这次的扫描报告。
    report: ScanReport
    #: **新出现**的信号（上轮没报过的）。
    new_signals: tuple[ScanSignal, ...]
    #: 开了演进循环没有。
    triggered: bool
    #: 为什么开 / 为什么没开。
    reason: str
    #: 触发时被去重窗口挡下 —— 与"压根没有新问题"是两回事。
    suppressed_by_window: bool = False


@dataclass(frozen=True, slots=True)
class ScanJobSpec:
    """定时扫描作业的配置。

    **为什么要单独一个配置对象。** 作业要的东西有六样（扫描器、范围、
    历史、触发器、间隔、时钟），而 ruff 的参数上限是五个（PLR0913）。
    硬凑成五个参数就得砍掉一样 —— 而"砍掉时钟"会让测试没法拨钟，"砍掉
    触发器"会让"只出报告不触发"这个用法没法表达。写成配置对象，六样东西
    一个不少，参数还是一个。
    """

    #: 扫描器 —— 真正扫出信号的东西。
    scanner: ProjectScanner
    #: 扫描范围。
    scope: ScanScope
    #: 扫描历史 —— 用来判断哪些是新的。
    history: ScanHistory
    #: 事件 → 演进循环的入口。**没有就不触发**（只出报告）。
    trigger: ScanTriggerPort | None = None
    #: 多久扫一次。
    interval: timedelta = DEFAULT_SCAN_INTERVAL
    #: 现在的时刻。``None`` 表示用系统时钟。
    clock: Callable[[], datetime] | None = None


class ScheduledScanner:
    """定时扫描作业。

    :param spec: 作业的配置。

    **它是"到点就跑"的那种。** :meth:`due` 回答"现在该不该扫"，
    :meth:`run` 回答"扫了之后要不要开一轮"。两件事分开，是因为"该不该
    扫"是调度器的责任，"要不要开轮"是这一层的责任。
    """

    __slots__ = ("_clock", "_history", "_interval", "_scanner", "_scope", "_trigger")

    def __init__(self, spec: ScanJobSpec) -> None:
        self._scanner = spec.scanner
        self._scope = spec.scope
        self._history = spec.history
        self._trigger = spec.trigger
        self._interval = spec.interval
        self._clock = spec.clock or (lambda: datetime.now(UTC))

    @property
    def interval(self) -> timedelta:
        """多久扫一次。"""
        return self._interval

    @property
    def scope(self) -> ScanScope:
        """扫描范围。"""
        return self._scope

    def due(self, *, now: datetime | None = None) -> bool:
        """现在该不该扫。

        **没扫过 → 该扫。** 第一次没有"距上次多久"可言。
        """
        moment = now or self._clock()
        last = self._history.last_run_at
        if last is None:
            return True
        return moment - last >= self._interval

    async def run(self, *, scan_id: str) -> ScheduledScanOutcome:
        """扫一遍，并按需开一轮演进。

        :param scan_id: 这次扫描的标识。
        """
        now = self._clock()
        report, fresh = fresh_signals(
            self._scanner,
            self._scope,
            self._history,
            scan_id=scan_id,
            at=now,
        )
        if not fresh:
            return ScheduledScanOutcome(
                report=report,
                new_signals=(),
                triggered=False,
                reason="这次扫描没有新问题 —— 上轮报过的都还在，没有新冒出来的",
            )
        if self._trigger is None:
            return ScheduledScanOutcome(
                report=report,
                new_signals=fresh,
                triggered=False,
                reason="有新的扫描结果，但没有接演进触发器 —— 出了报告，没有开轮",
            )
        outcome = await self._trigger.handle(self._event())
        return ScheduledScanOutcome(
            report=report,
            new_signals=fresh,
            triggered=outcome.ran,
            reason=outcome.decision.reason,
            suppressed_by_window=outcome.decision.suppressed,
        )

    def _event(self) -> IncomingEvent:
        """这次扫描对应的事件。

        **没有因果链 id** —— 定时扫描不是任何事的后果，它自己就是起因。
        """
        return IncomingEvent(
            event_type=SCAN_EVENT_TYPE,
            aggregate_id=self._scope.project_id,
            project_id=self._scope.project_id,
            trigger=TriggerSource.SCHEDULE,
        )


__all__ = [
    "DEFAULT_SCAN_INTERVAL",
    "SCAN_EVENT_TYPE",
    "InMemoryScanHistory",
    "ScanHistory",
    "ScanJobSpec",
    "ScanTriggerPort",
    "ScheduledScanOutcome",
    "ScheduledScanner",
    "fresh_signals",
]
