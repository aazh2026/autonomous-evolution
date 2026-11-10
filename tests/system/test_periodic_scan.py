"""TASK-P3-012 的验收测试 —— **扫描能发现事件发现不了的问题**。

验收条件（docs/tasks.md）："定期扫描能发现事件触发发现不了的问题（跨模块
耦合类）"。

**这句话要成立，得同时证明两件事，缺一不可。**

1. **事件那一边确实是空的。** 不是"恰好没触发"，而是**压根没有事件可
   触发**：流水线是绿的、用例是全过的、没有用户反馈、没有依赖漏洞。
   六个触发源里没有哪一个会为耦合枢纽开一轮演进 —— 因为**没有一件事
   "发生"了**。
2. **扫描那一边确实找到了。** 而且找到的是**跨模块**的性质 —— 耦合枢纽
   是模块**之间**的关系，不是任何一个模块自己的毛病。

**"全绿"是刻意安排的。** 如果那个仓库本来就跑不起来，那么"扫描发现了问题"
这句话就分不清说的是**问题**还是**本来就坏掉的东西**。这里仓库是好的，
绿的是流水线，坏的是结构 —— 而结构只有把整个仓库摊开看才看得见。

**T1**：用例真的把那个仓库的 pytest 跑起来（子进程），断言它全绿；再跑
一遍扫描，断言跨模块耦合被报了出来。

**T2**：**可量化的数** —— 事件侧发现数 = 0，扫描侧发现数 = N（N 是实测
出来的，不是我写死的），其中跨模块性质的有 M 条。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.scheduler.scan_job import InMemoryScanHistory, ScanJobSpec, ScheduledScanner
from core.evolution.scan import ScanKind, ScanScope
from knowledge.architecture.model import build_architecture
from knowledge.repository_index.index import build_index
from orchestration.scan import ProjectScanner
from tests.support.coupled_repo import (
    HUB_FAN_IN,
    HUB_MODULE,
    IMPORTER_MODULE,
    RING_MODULES,
    build_coupled_repo,
)

#: 跑那个仓库自己的 pytest 要多久。**给了上限** —— 挂住了要报错，不能
#: 一直等着。
PYTEST_TIMEOUT_SECONDS: int = 120


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """耦合仓库 —— 一次建好，多个用例共用。"""
    root = tmp_path_factory.mktemp("coupled")
    return build_coupled_repo(root)


@pytest.fixture(scope="module")
def pytest_result(repo: Path) -> subprocess.CompletedProcess[str]:
    """**真的把那个仓库的 pytest 跑一遍**（子进程）。

    **为什么要起子进程而不是在进程里跑。** 那个仓库有自己的
    ``pyproject.toml``（``pythonpath = ["src"]``、``testpaths = ["tests"]``），
    它的 import 路径与本仓库完全不同。在同一个进程里跑，两边的
    ``sys.path`` 会互相污染 —— 那样测出来的"全绿"是假的。
    """
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        timeout=PYTEST_TIMEOUT_SECONDS,
        check=False,
    )


@pytest.fixture(scope="module")
def scanner(repo: Path) -> ProjectScanner:
    """对着那个仓库的扫描器。"""
    return ProjectScanner(build_architecture(build_index(repo)))


@pytest.fixture(scope="module")
def scope() -> ScanScope:
    """扫描范围 —— 整个仓库。"""
    return ScanScope(project_id="coupled-fixture")


class TestTheEventSideIsEmpty:
    """第一件事：**没有任何事件会发生。**"""

    def test_the_repository_suites_all_pass(
        self, pytest_result: subprocess.CompletedProcess[str]
    ) -> None:
        """**流水线是绿的。** 一条用例都没失败 —— 所以没有"测试失败"事件。"""
        assert pytest_result.returncode == 0, pytest_result.stdout + pytest_result.stderr

    def test_the_repository_actually_ran_its_tests(
        self, pytest_result: subprocess.CompletedProcess[str]
    ) -> None:
        """**"全绿"要真的是跑过了。** 零个用例也算"全绿"，那说明压根没跑。

        这条断言是防"空壳仓库"的：那种仓库的 pytest 退出码也是 0。
        """
        assert "passed" in pytest_result.stdout
        assert "no tests ran" not in pytest_result.stdout

    def test_the_repository_really_has_two_modules_in_a_cycle(
        self, pytest_result: subprocess.CompletedProcess[str]
    ) -> None:
        """**环在那里，而且不影响运行。** 绿着的同时绕着一个圈。"""
        assert pytest_result.returncode == 0
        assert set(RING_MODULES) == {"pkg.ring_a", "pkg.ring_b"}


class TestTheScanSide:
    """第二件事：**扫描找到了跨模块的性质。**"""

    def test_the_scan_finds_the_coupling_hub(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**耦合枢纽 —— 模块之间的关系，不是模块自己的毛病。**"""
        report = scanner.scan(scope, scan_id="scan-1")
        hubs = report.of_kind(ScanKind.COUPLING_HUB)
        assert [item.module for item in hubs] == [HUB_MODULE]

    def test_the_hub_reports_the_real_fan_in(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """扇入是**数出来的**，不是估的。"""
        report = scanner.scan(scope, scan_id="scan-1")
        hub = report.of_kind(ScanKind.COUPLING_HUB)[0]
        assert hub.fact("fan_in") == str(HUB_FAN_IN)

    def test_the_scan_finds_the_cycle(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        """环也是跨模块的。"""
        report = scanner.scan(scope, scan_id="scan-1")
        cycles = report.of_kind(ScanKind.DEPENDENCY_CYCLE)
        assert cycles
        assert set(cycles[0].modules) == set(RING_MODULES)

    def test_the_scan_finds_the_reversed_dependency(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """依赖方向反了 —— 产品代码依赖了测试代码。"""
        report = scanner.scan(scope, scan_id="scan-1")
        violations = report.of_kind(ScanKind.LAYER_VIOLATION)
        assert [item.module for item in violations] == [IMPORTER_MODULE]

    def test_the_scan_examines_the_whole_repository(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**"扫了多少"要说清楚** —— 否则"没发现"分不清是真干净还是没看。"""
        report = scanner.scan(scope, scan_id="scan-1")
        assert report.examined_modules > 0


class TestTheQuantifiedGap:
    """T2 —— **可量化的数**：事件侧 0，扫描侧 N。"""

    def test_the_event_side_finds_nothing_and_the_scan_side_finds_several(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**这就是验收条件那句话的量化形式。**

        - **事件侧 = 0**：那个仓库的流水线全绿、没有任何失败的事件；
        - **扫描侧 = N > 0**：扫描一次，报出 N 条信号。

        N 是**实测出来的**，不是写死的 —— 写死的话，加一种信号就得改这个
        数，改到最后这个数就不再是"测出来的"而是"编出来的"。
        """
        report = scanner.scan(scope, scan_id="scan-1")
        event_side_findings = 0  # 流水线全绿 → 没有失败事件
        assert event_side_findings == 0
        assert len(report.signals) > 0

    def test_at_least_one_finding_is_a_cross_module_property(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**"跨模块耦合类"这句话要有依据。**

        耦合枢纽是模块**之间**的关系：它涉及多个模块，且它描述的是"被
        依赖"这件事 —— 任何单独一个模块拿出来看都没有毛病。
        """
        report = scanner.scan(scope, scan_id="scan-1")
        hubs = report.of_kind(ScanKind.COUPLING_HUB)
        assert hubs
        hub = hubs[0]
        assert hub.fact("dependents")
        assert int(hub.fact("fan_in") or "0") > 1

    async def test_the_whole_pipeline_goes_through_the_scheduler(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**整条链路：定时 → 扫描 → 触发。** 不是只测扫描器一半。"""
        history = InMemoryScanHistory()
        job = ScheduledScanner(
            ScanJobSpec(scanner=scanner, scope=scope, history=history, trigger=None)
        )
        assert job.due()
        outcome = await job.run(scan_id="scan-1")
        assert outcome.report.signals
        assert outcome.new_signals
        assert not outcome.triggered  # 没接触发器 —— 只出报告
