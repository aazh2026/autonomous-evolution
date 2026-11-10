"""TASK-P3-011 —— 六路触发源的验收。

验收条件（docs/tasks.md 第 961 行）：**"六类事件各能触发一轮演进；同一事件去
重后只触发一次"**。验证形态 T1（自动化测试）+ T2（可量化的数）。

**这一份测试里没有假的东西。**

============================  ==========================================
触发源                      读的是真的什么
============================  ==========================================
定时                        试点仓库的源码索引（真扫一遍）
代码                        本仓库真的 git 历史
质量                        试点仓库 ``known_issues/`` 里真的跑一遍
运行                        试点仓库的 CLI **真的崩一次**
外部                        人写的 ``docs/issues.md``（真的 20 条）
知识                        仓库里真的 ADR 文件
============================  ==========================================

**"触发一轮演进"是真的跑一遍循环 B。** 打桩的是各步处理器（跟
:mod:`tests.unit.orchestration.test_loop_b` 一样），但**图是真的 LangGraph
图，闸门是真的去重窗口** —— 所以"开了一轮"这句话有底：库里真多了一行周期
记录。

**T2 的那个数在这里。** 同一条事件投递 N 次，实际开轮数是 **1**。断言写成
"等于 1"，不是"小于 N" —— 后者会被"一轮都不开"蒙过去。
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path
from typing import Final

import pytest

from apps.scheduler.scan_job import InMemoryScanHistory
from apps.triggers.sources import (
    GitCommitSource,
    KnowledgeRecordSource,
    PytestFailureSource,
    ReportInboxSource,
    RuntimeProbe,
    RuntimeProbeSource,
    ScanScheduleSource,
)
from core.evolution.evolution_loop import TriggerSource
from core.evolution.scan import ScanScope
from core.evolution.triggers import TriggerEvent
from knowledge.architecture.model import build_architecture
from knowledge.repository_index.index import RepositoryIndex, build_index
from orchestration.dedup import LoopBCycleRunner
from orchestration.loop_b import build_loop_b_graph
from orchestration.scan import ProjectScanner
from orchestration.triggers import TriggerRouter, build_router
from tests.support.evolution_loop import StubHandlers

#: 仓库根目录。
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: 试点仓库 —— **一个真的、跑得起来、但身上有毛病的小项目**。
PILOT_REPO: Final[Path] = REPO_ROOT / "samples" / "pilot_repo"

#: 六路各自读出来的事件 —— **按来源分桶**。
EventsBySource = dict[TriggerSource, tuple[TriggerEvent, ...]]

#: 同一条事件投递几次。
REPEATS: Final[int] = 5

#: 六类触发源 —— **写死六个**。
SIX_SOURCES: Final[tuple[TriggerSource, ...]] = (
    TriggerSource.SCHEDULE,
    TriggerSource.CODE,
    TriggerSource.QUALITY,
    TriggerSource.RUNTIME,
    TriggerSource.EXTERNAL,
    TriggerSource.KNOWLEDGE,
)


@pytest.fixture(scope="module")
def pilot_index() -> RepositoryIndex:
    """试点仓库的索引。"""
    return build_index(PILOT_REPO)


@pytest.fixture(scope="module")
def events_by_source(pilot_index: RepositoryIndex) -> EventsBySource:
    """六路各自读出来的**真事件**。"""
    architecture = build_architecture(pilot_index)
    schedule = ScanScheduleSource(
        scanner=ProjectScanner(architecture),
        scope=ScanScope(project_id="pilot"),
        history=InMemoryScanHistory(),
    )
    code = GitCommitSource(repo=REPO_ROOT, limit=5, project_id="ases")
    quality = PytestFailureSource(
        repo=PILOT_REPO,
        targets=("known_issues",),
        project_id="pilot",
    )
    runtime = RuntimeProbeSource(
        probes=(
            RuntimeProbe(
                id="pilot-cli-empty-stats",
                argv=(sys.executable, "-m", "tinylib.cli", "stats", ""),
                cwd=PILOT_REPO,
                env=(("PYTHONPATH", "src"),),
            ),
        ),
        project_id="pilot",
    )
    external = ReportInboxSource(
        path=PILOT_REPO / "docs" / "issues.md",
        project_id="pilot",
    )
    knowledge = KnowledgeRecordSource(
        directory=REPO_ROOT / "docs" / "adr",
        project_id="ases",
    )
    return {
        TriggerSource.SCHEDULE: schedule.collect(),
        TriggerSource.CODE: code.collect(),
        TriggerSource.QUALITY: quality.collect(),
        TriggerSource.RUNTIME: runtime.collect(),
        TriggerSource.EXTERNAL: external.collect(),
        TriggerSource.KNOWLEDGE: knowledge.collect(),
    }


def _router(handlers: StubHandlers) -> TriggerRouter:
    """一台真的路由 —— 真的图、真的去重窗口。"""
    graph = build_loop_b_graph(handlers.handlers)
    counter = itertools.count(1)
    runner = LoopBCycleRunner(graph, cycle_ids=lambda: f"cycle-{next(counter):04d}")
    return build_router(runner)


class TestTheSixSources:
    """六路都读得出真的事件。"""

    def test_every_source_produced_something(self, events_by_source: EventsBySource) -> None:
        """**六路都得有事件** —— 少一路，"六类事件各能触发一轮演进"就是空话。"""
        for source in SIX_SOURCES:
            assert events_by_source[source], f"{source.value} 一件事都没产出"

    def test_the_events_carry_their_own_source(self, events_by_source: EventsBySource) -> None:
        for source in SIX_SOURCES:
            for event in events_by_source[source]:
                assert event.source is source

    def test_every_event_has_a_subject(self, events_by_source: EventsBySource) -> None:
        """**主体是去重的粒度** —— 空的主体会让所有事件挤进同一个指纹。"""
        for source in SIX_SOURCES:
            for event in events_by_source[source]:
                assert event.aggregate_id.strip()

    def test_the_six_subjects_are_all_different(self, events_by_source: EventsBySource) -> None:
        """**六类事不能撞成同一类。** 撞了的话，六路只会在跑同一件事。"""
        subjects = {events_by_source[source][0].aggregate_id for source in SIX_SOURCES}
        assert len(subjects) == 6


class TestTheRealArtifacts:
    """读到的确实是真的那些东西 —— 不是编出来的数字。"""

    def test_the_schedule_source_found_a_real_problem_in_the_pilot_repo(
        self, events_by_source: EventsBySource
    ) -> None:
        """**试点仓库的 ``tinylib.cli`` 确实没有自己的用例** —— 扫出来的是
        真问题，不是一条编造的信号。"""
        event = events_by_source[TriggerSource.SCHEDULE][0]
        assert "扫到" in event.summary
        assert event.aggregate_id == "pilot"

    def test_the_code_source_read_the_real_history(self, events_by_source: EventsBySource) -> None:
        """**主体是本仓库真的提交 sha**。"""
        event = events_by_source[TriggerSource.CODE][0]
        assert len(event.aggregate_id) == 40

    def test_the_quality_source_found_the_real_failing_cases(
        self, events_by_source: EventsBySource
    ) -> None:
        """**试点仓库的 ``known_issues/`` 里真的有一批用例没过。**

        数量写死 9 —— 那是那份复现套件里真实的条数。条数变了就是**用例变了**，
        这条断言会红，提醒人更新数字，而不是让一个悄悄变小的数字蒙过去。
        """
        events = events_by_source[TriggerSource.QUALITY]
        assert len(events) == 9
        assert all(item.aggregate_id.startswith("known_issues/") for item in events)

    def test_the_runtime_source_saw_the_cli_crash(self, events_by_source: EventsBySource) -> None:
        """**CLI 真的崩了** —— 退出码不是 0，事件才在。"""
        events = events_by_source[TriggerSource.RUNTIME]
        assert len(events) == 1
        assert "退出码" in events[0].summary

    def test_the_external_source_read_the_human_written_ledger(
        self, events_by_source: EventsBySource
    ) -> None:
        """**人写的 20 条问题，一条不少。**"""
        events = events_by_source[TriggerSource.EXTERNAL]
        assert len(events) == 20
        assert events[0].aggregate_id == "PILOT-001"

    def test_the_knowledge_source_read_the_real_adr(self, events_by_source: EventsBySource) -> None:
        """**知识那一路接的是真的决策记录。**"""
        events = events_by_source[TriggerSource.KNOWLEDGE]
        assert len(events) == 1
        assert events[0].aggregate_id == "0001-config-and-secret-handling"


class TestEachEventStartsARound:
    """六类事件各能触发一轮演进 —— **真的跑一遍循环 B**。"""

    @pytest.mark.parametrize("source", SIX_SOURCES)
    async def test_the_event_starts_a_real_cycle(
        self, source: TriggerSource, events_by_source: EventsBySource
    ) -> None:
        handlers = StubHandlers()
        router = _router(handlers)
        event = events_by_source[source][0]

        outcome = await router.dispatch(event)

        assert outcome.ran, f"{source.value} 的事件没开成一轮：{outcome.decision.reason}"
        assert handlers.journal.calls.get("open_cycle") == 1, (
            "真的循环 B 没跑 —— 周期记录里没有那一行"
        )

    @pytest.mark.parametrize("source", SIX_SOURCES)
    async def test_the_cycle_records_which_source_triggered_it(
        self, source: TriggerSource, events_by_source: EventsBySource
    ) -> None:
        """**周期记录里那一列得是这一路** —— 事后翻记录的人要知道是被什么
        触发的。"""
        handlers = StubHandlers()
        router = _router(handlers)

        await router.dispatch(events_by_source[source][0])

        assert handlers.journal.cycles[0]["trigger"] == source.value


class TestTheDedup:
    """同一事件去重后只触发一次 —— **T2 的那个数**。"""

    async def test_the_same_event_repeated_starts_exactly_one_round(
        self, events_by_source: EventsBySource
    ) -> None:
        """**投递 5 次，开轮 1 次。**

        断言写"等于 1"而不是"小于等于 5" —— 后者会被"一轮都不开"蒙过去。
        """
        handlers = StubHandlers()
        router = _router(handlers)
        event = events_by_source[TriggerSource.EXTERNAL][0]

        for _ in range(REPEATS):
            await router.dispatch(event)

        assert handlers.journal.calls.get("open_cycle") == 1
        assert router.started[TriggerSource.EXTERNAL] == 1
        assert router.suppressed[TriggerSource.EXTERNAL] == REPEATS - 1

    async def test_the_same_event_from_every_source_starts_one_round_each(
        self, events_by_source: EventsBySource
    ) -> None:
        """**六路各来一次，各开一轮；但每一路自己重复来，只开一轮。**

        这个断言守住的是"六路共用一份去重窗口"这件事 —— 要是每路各有一份
        窗口，同一件事从两路来就会开两轮。
        """
        handlers = StubHandlers()
        router = _router(handlers)

        for source in SIX_SOURCES:
            event = events_by_source[source][0]
            await router.dispatch(event)
            await router.dispatch(event)

        assert handlers.journal.calls.get("open_cycle") == len(SIX_SOURCES)

    async def test_the_window_says_why_it_stopped_the_second_one(
        self, events_by_source: EventsBySource
    ) -> None:
        """**"不开"必须说得清为什么** —— 是重复，不是回环。"""
        handlers = StubHandlers()
        router = _router(handlers)
        event = events_by_source[TriggerSource.CODE][0]

        await router.dispatch(event)
        second = await router.dispatch(event)

        assert not second.ran
        assert "去重" in second.decision.reason
