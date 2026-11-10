"""TASK-P3-012 —— 定时扫描作业：到点就扫，扫出新的才开轮。

**这一层测的是"处置"，不是"发现了什么"。** 发现什么由
:mod:`tests.unit.orchestration.test_scan` 负责；这一层管的是四件事：

1. **该不该扫** —— 没扫过该扫；距上次不到间隔就不该扫。
2. **新不新** —— 同一个问题天天报，天天开轮，预算几天就烧光了。
3. **开不开** —— 没接触发器就只出报告，不开轮。
4. **为什么不开** —— "没开"必须说得清是没到期、没新问题、还是被去重挡了。

**时钟是注进去的。** 所以"隔了一天"这件事在测试里是真的把钟拨了一天，
不是靠 sleep 等出来的。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.scheduler.scan_job import (
    DEFAULT_SCAN_INTERVAL,
    SCAN_EVENT_TYPE,
    InMemoryScanHistory,
    ScanJobSpec,
    ScheduledScanner,
)
from core.evolution.evolution_loop import LoopBState, TriggerSource
from core.evolution.scan import ScanKind, ScanPolicy, ScanReport, ScanScope
from knowledge.architecture.model import build_architecture
from knowledge.repository_index.index import build_index
from orchestration.dedup import IncomingEvent, TriggerDecision, TriggerOutcome
from orchestration.scan import ProjectScanner
from tests.support.coupled_repo import HUB_MODULE, build_coupled_repo

#: 一个固定的时间点 —— 测试里所有"现在"都是它。
START: datetime = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


class RecordingTrigger:
    """记下收到的事件，按需要给出结论。

    **它替身的是演进循环的入口** —— 真实现要跑一整张图，测试里要的是
    "有没有被叫起来"和"被叫起来时带了什么"。
    """

    def __init__(self, *, should_trigger: bool = True) -> None:
        self.events: list[IncomingEvent] = []
        self._should_trigger = should_trigger

    async def handle(self, event: IncomingEvent) -> TriggerOutcome:
        """处理一条事件。"""
        self.events.append(event)
        decision = TriggerDecision(
            should_trigger=self._should_trigger,
            reason="允许开新一轮演进" if self._should_trigger else "被去重窗口挡下了",
            fingerprint=event.fingerprint,
            causation_id=event.causation_id,
        )
        state = (
            LoopBState(project_id=event.project_id, cycle_id="cycle-1", trigger=event.trigger)
            if self._should_trigger
            else None
        )
        return TriggerOutcome(decision=decision, state=state)


@pytest.fixture
def scanner(tmp_path: Path) -> ProjectScanner:
    """对着耦合仓库的扫描器。"""
    root = build_coupled_repo(tmp_path / "coupled")
    return ProjectScanner(build_architecture(build_index(root)))


@pytest.fixture
def scope() -> ScanScope:
    """扫描范围。"""
    return ScanScope(project_id="P1")


def _history_that_ran_at(
    scanner: ProjectScanner,
    scope: ScanScope,
    moment: datetime,
    *,
    reported_nothing: bool = False,
) -> InMemoryScanHistory:
    """造一份"上一次在某个时刻扫过"的历史。

    :param reported_nothing: 假装上次什么都没报 —— 用来模拟"仓库里刚
        冒出一个新问题"。
    """
    history = InMemoryScanHistory()
    report = ScanReport(
        scan_id="earlier",
        scope=scope,
        policy=ScanPolicy(),
        signals=(),
        examined_modules=0,
    )
    if not reported_nothing:
        report = scanner.scan(scope, scan_id="earlier")
    history.remember(report, at=moment)
    return history


def _job(
    scanner: ProjectScanner,
    scope: ScanScope,
    history: InMemoryScanHistory,
    *,
    trigger: RecordingTrigger | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ScheduledScanner:
    """装一个定时扫描作业。"""
    spec = ScanJobSpec(
        scanner=scanner,
        scope=scope,
        history=history,
        trigger=trigger,
        clock=clock,
    )
    return ScheduledScanner(spec)


class TestTheSchedule:
    """该不该扫。"""

    def test_a_job_that_never_ran_is_due(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        job = _job(scanner, scope, InMemoryScanHistory(), clock=lambda: START)
        assert job.due()

    def test_a_job_is_not_due_before_the_interval(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        history = _history_that_ran_at(scanner, scope, START)
        job = _job(scanner, scope, history, clock=lambda: START + timedelta(hours=1))
        assert not job.due()

    def test_a_job_is_due_after_the_interval(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        history = _history_that_ran_at(scanner, scope, START)
        job = _job(scanner, scope, history, clock=lambda: START + DEFAULT_SCAN_INTERVAL)
        assert job.due()

    def test_the_due_check_can_be_given_an_explicit_moment(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        history = _history_that_ran_at(scanner, scope, START)
        job = _job(scanner, scope, history, clock=lambda: START)
        assert job.due(now=START + timedelta(days=2))
        assert not job.due(now=START + timedelta(minutes=1))


class TestTheFirstRun:
    """第一轮扫到的东西全是新的。"""

    async def test_the_first_run_triggers(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        trigger = RecordingTrigger()
        job = _job(scanner, scope, InMemoryScanHistory(), trigger=trigger, clock=lambda: START)
        outcome = await job.run(scan_id="scan-1")
        assert outcome.triggered
        assert outcome.new_signals

    async def test_the_event_says_it_came_from_the_schedule(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**触发源要写对** —— 事后看到周期记录，得知道这一轮是定时扫出来的。"""
        trigger = RecordingTrigger()
        job = _job(scanner, scope, InMemoryScanHistory(), trigger=trigger, clock=lambda: START)
        await job.run(scan_id="scan-1")
        assert trigger.events[0].trigger is TriggerSource.SCHEDULE
        assert trigger.events[0].event_type == SCAN_EVENT_TYPE
        assert trigger.events[0].aggregate_id == scope.project_id

    async def test_the_event_has_no_causation_chain(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """定时扫描不是任何事的后果 —— 它自己就是起因。"""
        trigger = RecordingTrigger()
        job = _job(scanner, scope, InMemoryScanHistory(), trigger=trigger, clock=lambda: START)
        await job.run(scan_id="scan-1")
        assert trigger.events[0].causation_id is None


class TestTheSecondRun:
    """第二轮 —— 同样的问题不该再开一轮。"""

    async def test_the_second_run_finds_no_new_problem(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**仓库没变，所以问题还是那些问题 —— 不是新的。**"""
        history = InMemoryScanHistory()
        trigger = RecordingTrigger()
        job = _job(scanner, scope, history, trigger=trigger, clock=lambda: START)
        await job.run(scan_id="scan-1")
        outcome = await job.run(scan_id="scan-2")
        assert not outcome.new_signals
        assert not outcome.triggered

    async def test_the_second_run_says_why_it_did_not_run(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**"没开轮"必须有理由** —— 否则没人知道该修哪儿。"""
        history = InMemoryScanHistory()
        job = _job(scanner, scope, history, clock=lambda: START)
        await job.run(scan_id="scan-1")
        outcome = await job.run(scan_id="scan-2")
        assert "没有新问题" in outcome.reason

    async def test_the_second_run_does_not_call_the_trigger(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        trigger = RecordingTrigger()
        history = InMemoryScanHistory()
        job = _job(scanner, scope, history, trigger=trigger, clock=lambda: START)
        await job.run(scan_id="scan-1")
        await job.run(scan_id="scan-2")
        assert len(trigger.events) == 1

    async def test_the_report_still_lists_the_problems(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**"没有新问题"不等于"没有问题"** —— 报告里还挂着，只是不新。"""
        history = InMemoryScanHistory()
        job = _job(scanner, scope, history, clock=lambda: START)
        await job.run(scan_id="scan-1")
        outcome = await job.run(scan_id="scan-2")
        assert outcome.report.of_kind(ScanKind.COUPLING_HUB)

    async def test_a_new_problem_after_the_first_run_does_trigger(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**新问题必须能触发** —— 否则"只报新的"就变成了"永远不报"。"""
        history = _history_that_ran_at(scanner, scope, START, reported_nothing=True)
        trigger = RecordingTrigger()
        job = _job(scanner, scope, history, trigger=trigger, clock=lambda: START)
        outcome = await job.run(scan_id="scan-2")
        assert outcome.new_signals
        assert outcome.triggered


class TestWithoutATrigger:
    """没接触发器 —— 只出报告，不开轮。"""

    async def test_the_report_is_still_produced(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        job = _job(scanner, scope, InMemoryScanHistory(), clock=lambda: START)
        outcome = await job.run(scan_id="scan-1")
        assert outcome.report.signals
        assert outcome.new_signals

    async def test_it_does_not_trigger(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        job = _job(scanner, scope, InMemoryScanHistory(), clock=lambda: START)
        outcome = await job.run(scan_id="scan-1")
        assert not outcome.triggered

    async def test_it_says_why(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        job = _job(scanner, scope, InMemoryScanHistory(), clock=lambda: START)
        outcome = await job.run(scan_id="scan-1")
        assert "没有接演进触发器" in outcome.reason


class TestSuppressedByTheWindow:
    """被去重窗口挡下 —— 与"压根没有新问题"是两回事。"""

    async def test_a_suppressed_trigger_is_reported_as_suppressed(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**有新的，但窗口说不行** —— 这两件事必须分开记。"""
        trigger = RecordingTrigger(should_trigger=False)
        job = _job(scanner, scope, InMemoryScanHistory(), trigger=trigger, clock=lambda: START)
        outcome = await job.run(scan_id="scan-1")
        assert outcome.new_signals
        assert not outcome.triggered
        assert outcome.suppressed_by_window

    async def test_a_suppressed_run_still_counts_as_seen(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**被挡下也算"报过了"** —— 否则下一轮还会再报一遍。"""
        history = InMemoryScanHistory()
        trigger = RecordingTrigger(should_trigger=False)
        job = _job(scanner, scope, history, trigger=trigger, clock=lambda: START)
        await job.run(scan_id="scan-1")
        outcome = await job.run(scan_id="scan-2")
        assert not outcome.new_signals
        assert not outcome.suppressed_by_window


class TestTheHistory:
    """扫描历史本身。"""

    async def test_the_history_remembers_when_it_ran(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        history = InMemoryScanHistory()
        job = _job(scanner, scope, history, clock=lambda: START)
        await job.run(scan_id="scan-1")
        assert history.last_run_at == START

    async def test_the_history_remembers_the_identities(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**记住的是身份，不是信号本身** —— 身份不含计数，所以跨轮次可比。"""
        history = InMemoryScanHistory()
        job = _job(scanner, scope, history, clock=lambda: START)
        await job.run(scan_id="scan-1")
        assert f"coupling_hub:{HUB_MODULE}" in history.last_fingerprints


class TestTheHubIsReallyThere:
    """这个作业扫的那个仓库 —— 确实有耦合枢纽。"""

    async def test_the_scan_reports_the_coupling_hub(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        job = _job(scanner, scope, InMemoryScanHistory(), clock=lambda: START)
        outcome = await job.run(scan_id="scan-1")
        hubs = outcome.report.of_kind(ScanKind.COUPLING_HUB)
        assert [item.module for item in hubs] == [HUB_MODULE]
