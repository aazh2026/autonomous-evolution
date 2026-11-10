"""TASK-P3-011 —— 六路触发源：各自从真的东西里读出事件。

**测的是"读得出读不出"，不是"读出来长什么样"。** 所以这里的断言分两类：

1. **读得出** —— 真的建一个临时仓库、真的跑一次 pytest、真的跑一条会崩的
   命令，看事件是不是真的出现了；
2. **读不出时不报错** —— 台账格式变了、目录没了、命令跑不起来，这些都要
   **安静地什么都不产出**，而不是把整个系统带崩。

**"读不出"与"没有"是两件事。** 这一层只负责把"现在有什么"如实说出来；
"要不要因此开一轮演进"是派发那一侧的事。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from apps.scheduler.scan_job import InMemoryScanHistory
from apps.triggers.sources import (
    GitCommitSource,
    KnowledgeRecordSource,
    PytestFailureSource,
    QualitySourceError,
    ReportInboxSource,
    RuntimeProbe,
    RuntimeProbeSource,
    ScanScheduleSource,
    TriggerSourcePort,
)
from core.evolution.evolution_loop import TriggerSource
from core.evolution.scan import ScanScope
from knowledge.architecture.model import build_architecture
from knowledge.repository_index.index import build_index
from orchestration.scan import ProjectScanner
from tests.support.coupled_repo import build_coupled_repo

#: 跑哪些东西 —— **必须说清楚**（理由见 :class:`PytestFailureSource`）。
BROKEN_TARGETS: tuple[str, ...] = ("test_broken.py",)
KNOWN_TARGETS: tuple[str, ...] = ("test_known.py",)


def _git(root: Path, *args: str) -> None:
    """在临时仓库里跑一条 git 命令。"""
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """一个真的 git 仓库，里面有两次提交。"""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(root)],
        check=True,
        capture_output=True,
    )
    _git(root, "config", "user.email", "tester@example.invalid")
    _git(root, "config", "user.name", "Tester")
    _git(root, "config", "commit.gpgsign", "false")
    for index in range(2):
        (root / f"file{index}.txt").write_text(f"第 {index} 次\n", encoding="utf-8")
        _git(root, "add", f"file{index}.txt")
        _git(root, "commit", "-q", "-m", f"第 {index} 次提交")
    return root


@pytest.fixture
def failing_project(tmp_path: Path) -> Path:
    """一个真的、跑起来会失败的 pytest 项目。"""
    root = tmp_path / "broken"
    root.mkdir()
    (root / "test_broken.py").write_text(
        "def test_this_fails():\n    assert 1 == 2\n",
        encoding="utf-8",
    )
    (root / "test_ok.py").write_text(
        "def test_this_passes():\n    assert 1 == 1\n",
        encoding="utf-8",
    )
    return root


class TestTheProtocol:
    """六路都守同一个口子。"""

    def test_every_source_satisfies_the_port(self) -> None:
        """**六路是同一个形状** —— 派发那一侧才不用写六遍。"""
        sources: list[TriggerSourcePort] = [
            ScanScheduleSource(
                scanner=ProjectScanner(build_architecture(build_index(Path()))),
                scope=ScanScope(project_id="P1"),
                history=InMemoryScanHistory(),
            ),
            GitCommitSource(repo=Path()),
            PytestFailureSource(repo=Path(), targets=()),
            RuntimeProbeSource(probes=()),
            ReportInboxSource(path=Path()),
            KnowledgeRecordSource(directory=Path()),
        ]
        for source in sources:
            assert isinstance(source, TriggerSourcePort)

    def test_the_sources_cover_all_six_kinds(self) -> None:
        """**六路各报自己的身份** —— 周期记录里那一列就是它。"""
        sources: list[TriggerSourcePort] = [
            ScanScheduleSource(
                scanner=ProjectScanner(build_architecture(build_index(Path()))),
                scope=ScanScope(project_id="P1"),
                history=InMemoryScanHistory(),
            ),
            GitCommitSource(repo=Path()),
            PytestFailureSource(repo=Path(), targets=()),
            RuntimeProbeSource(probes=()),
            ReportInboxSource(path=Path()),
            KnowledgeRecordSource(directory=Path()),
        ]
        assert {source.source for source in sources} == set(TriggerSource)


class TestTheCodeSource:
    """代码那一路 —— 真的 git 历史。"""

    def test_it_reads_the_commits(self, git_repo: Path) -> None:
        events = GitCommitSource(repo=git_repo).collect()
        assert len(events) == 2

    def test_the_aggregate_is_the_commit_sha(self, git_repo: Path) -> None:
        """**主体是提交 sha** —— 一次提交是一件事。"""
        events = GitCommitSource(repo=git_repo).collect()
        for event in events:
            assert len(event.aggregate_id) == 40
            assert event.aggregate_id.isalnum()

    def test_the_summary_is_the_commit_subject(self, git_repo: Path) -> None:
        events = GitCommitSource(repo=git_repo).collect()
        assert {event.summary for event in events} == {"第 0 次提交", "第 1 次提交"}

    def test_the_limit_is_respected(self, git_repo: Path) -> None:
        events = GitCommitSource(repo=git_repo, limit=1).collect()
        assert len(events) == 1

    def test_a_non_git_directory_produces_nothing(self, tmp_path: Path) -> None:
        """**不是 git 仓库就什么也不产出** —— 而不是报错。"""
        assert GitCommitSource(repo=tmp_path).collect() == ()

    def test_the_events_carry_the_code_source(self, git_repo: Path) -> None:
        for event in GitCommitSource(repo=git_repo).collect():
            assert event.source is TriggerSource.CODE


class TestTheQualitySource:
    """质量那一路 —— 真的跑一遍 pytest。"""

    def test_it_finds_the_failing_case(self, failing_project: Path) -> None:
        events = PytestFailureSource(repo=failing_project, targets=BROKEN_TARGETS).collect()
        assert len(events) == 1

    def test_the_aggregate_is_the_node_id(self, failing_project: Path) -> None:
        """**主体是那条用例的 id** —— 哪一条没过，说得出来。"""
        events = PytestFailureSource(repo=failing_project, targets=BROKEN_TARGETS).collect()
        assert events[0].aggregate_id == "test_broken.py::test_this_fails"

    def test_the_passing_case_is_not_reported(self, failing_project: Path) -> None:
        events = PytestFailureSource(repo=failing_project, targets=BROKEN_TARGETS).collect()
        assert "test_ok.py::test_this_passes" not in {item.aggregate_id for item in events}

    def test_an_xfail_case_is_reported_when_it_is_run_for_real(self, tmp_path: Path) -> None:
        """**"这些毛病还在吗"要按真失败来跑。**"""
        root = tmp_path / "xfailed"
        root.mkdir()
        (root / "test_known.py").write_text(
            "import pytest\n"
            "\n"
            "@pytest.mark.xfail(strict=True)\n"
            "def test_known_bug():\n"
            "    assert 1 == 2\n",
            encoding="utf-8",
        )
        events = PytestFailureSource(repo=root, targets=KNOWN_TARGETS).collect()
        assert [item.aggregate_id for item in events] == ["test_known.py::test_known_bug"]

    def test_without_runxfail_the_xfail_case_is_not_a_failure(self, tmp_path: Path) -> None:
        """**默认不把 xfail 算成失败** —— 那是"预期中的失败"，不是质量事件。"""
        root = tmp_path / "xfailed2"
        root.mkdir()
        (root / "test_known.py").write_text(
            "import pytest\n"
            "\n"
            "@pytest.mark.xfail(strict=True)\n"
            "def test_known_bug():\n"
            "    assert 1 == 2\n",
            encoding="utf-8",
        )
        source = PytestFailureSource(repo=root, targets=KNOWN_TARGETS, run_xfail=False)
        assert source.collect() == ()

    def test_a_target_that_does_not_exist_is_an_error(self, failing_project: Path) -> None:
        """**跑错了地方 = 没验成，不是"质量没问题"。**

        这条是本仓库的三态规矩在触发源上的落点：缺实测值是失败，不是 0。
        静悄悄地返回空，等于把一个跑不起来的测试套件报成"没有用例没过"。
        """
        source = PytestFailureSource(repo=failing_project, targets=("nope/",))
        with pytest.raises(QualitySourceError):
            source.collect()

    def test_the_error_says_it_did_not_run(self, failing_project: Path) -> None:
        """**报错要说清是"没跑成"，不是"没有失败"。**"""
        source = PytestFailureSource(repo=failing_project, targets=("nope/",))
        with pytest.raises(QualitySourceError) as caught:
            source.collect()
        assert "没跑成" in str(caught.value)


class TestTheRuntimeSource:
    """运行那一路 —— 真的跑一条命令。"""

    def test_a_command_that_works_produces_nothing(self, tmp_path: Path) -> None:
        probe = RuntimeProbe(
            id="ok",
            argv=(sys.executable, "-c", "print('fine')"),
            cwd=tmp_path,
        )
        assert RuntimeProbeSource(probes=(probe,)).collect() == ()

    def test_a_command_that_crashes_produces_an_event(self, tmp_path: Path) -> None:
        """**崩了就是崩了** —— 退出码非零就是事故。"""
        probe = RuntimeProbe(
            id="boom",
            argv=(sys.executable, "-c", "raise SystemExit(3)"),
            cwd=tmp_path,
        )
        events = RuntimeProbeSource(probes=(probe,)).collect()
        assert len(events) == 1
        assert events[0].aggregate_id == "boom"

    def test_the_summary_says_the_exit_code(self, tmp_path: Path) -> None:
        probe = RuntimeProbe(
            id="boom",
            argv=(sys.executable, "-c", "raise SystemExit(3)"),
            cwd=tmp_path,
        )
        events = RuntimeProbeSource(probes=(probe,)).collect()
        assert "3" in events[0].summary

    def test_a_command_that_hangs_produces_an_event(self, tmp_path: Path) -> None:
        """**超时也是事故** —— 挂住了跟崩了一样是"没跑成"。"""
        probe = RuntimeProbe(
            id="hang",
            argv=(sys.executable, "-c", "import time; time.sleep(30)"),
            cwd=tmp_path,
            timeout=0.5,
        )
        events = RuntimeProbeSource(probes=(probe,)).collect()
        assert len(events) == 1
        assert "超时" in events[0].summary

    def test_a_command_that_cannot_start_produces_an_event(self, tmp_path: Path) -> None:
        """**跑不起来也是事故** —— 不是"没问题"，是"没验成"。"""
        probe = RuntimeProbe(id="ghost", argv=("/no/such/binary",), cwd=tmp_path)
        events = RuntimeProbeSource(probes=(probe,)).collect()
        assert len(events) == 1

    def test_the_events_carry_the_runtime_source(self) -> None:
        probe = RuntimeProbe(id="boom", argv=(sys.executable, "-c", "raise SystemExit(1)"))
        events = RuntimeProbeSource(probes=(probe,)).collect()
        assert events[0].source is TriggerSource.RUNTIME


class TestTheExternalSource:
    """外部那一路 —— 人写的台账。"""

    def test_it_reads_the_entries(self, tmp_path: Path) -> None:
        ledger = tmp_path / "issues.md"
        ledger.write_text(
            "# 问题清单\n\n### BUG-001 —— 第一次崩了\n\n### BUG-002 —— 第二次崩了\n",
            encoding="utf-8",
        )
        events = ReportInboxSource(path=ledger, project_id="P1").collect()
        assert [item.aggregate_id for item in events] == ["BUG-001", "BUG-002"]

    def test_the_summary_is_the_title(self, tmp_path: Path) -> None:
        ledger = tmp_path / "issues.md"
        ledger.write_text("### BUG-001 —— 第一次崩了\n", encoding="utf-8")
        events = ReportInboxSource(path=ledger, project_id="P1").collect()
        assert events[0].summary == "第一次崩了"

    def test_a_line_it_does_not_recognise_is_skipped(self, tmp_path: Path) -> None:
        """**宁可漏报，不要报错** —— 台账是人写的。"""
        ledger = tmp_path / "issues.md"
        ledger.write_text(
            "## 小标题\n### 缺编号 —— 这条认不出来\n### BUG-001 —— 认得出来\n",
            encoding="utf-8",
        )
        events = ReportInboxSource(path=ledger, project_id="P1").collect()
        assert [item.aggregate_id for item in events] == ["BUG-001"]

    def test_a_missing_ledger_produces_nothing(self, tmp_path: Path) -> None:
        source = ReportInboxSource(path=tmp_path / "nope.md", project_id="P1")
        assert source.collect() == ()


class TestTheKnowledgeSource:
    """知识那一路 —— 决策记录。"""

    def test_it_reads_the_records(self, tmp_path: Path) -> None:
        directory = tmp_path / "adr"
        directory.mkdir()
        (directory / "0001-first.md").write_text("# 第一个决策\n\n为什么。\n", encoding="utf-8")
        (directory / "0002-second.md").write_text("# 第二个决策\n", encoding="utf-8")
        events = KnowledgeRecordSource(directory=directory, project_id="P1").collect()
        assert [item.aggregate_id for item in events] == ["0001-first", "0002-second"]

    def test_the_summary_is_the_title(self, tmp_path: Path) -> None:
        directory = tmp_path / "adr"
        directory.mkdir()
        (directory / "0001-first.md").write_text("# 第一个决策\n", encoding="utf-8")
        events = KnowledgeRecordSource(directory=directory, project_id="P1").collect()
        assert events[0].summary == "第一个决策"

    def test_a_missing_directory_produces_nothing(self, tmp_path: Path) -> None:
        source = KnowledgeRecordSource(directory=tmp_path / "nope", project_id="P1")
        assert source.collect() == ()


class TestTheScheduleSource:
    """定时那一路 —— 扫一遍项目。"""

    def test_the_first_scan_finds_problems(self, tmp_path: Path) -> None:
        repo = build_coupled_repo(tmp_path / "coupled")
        architecture = build_architecture(build_index(repo))
        source = ScanScheduleSource(
            scanner=ProjectScanner(architecture),
            scope=ScanScope(project_id="P1"),
            history=InMemoryScanHistory(),
        )
        events = source.collect()
        assert len(events) == 1
        assert events[0].source is TriggerSource.SCHEDULE

    def test_the_second_scan_finds_nothing_new(self, tmp_path: Path) -> None:
        """**扫过一遍之后，同样的问题不再报。**

        这不是"第二遍不用扫" —— 扫还是扫了，只是**没有新的**。少了这一条，
        定时扫描天天开轮，预算几天就烧光。
        """
        repo = build_coupled_repo(tmp_path / "coupled")
        architecture = build_architecture(build_index(repo))
        source = ScanScheduleSource(
            scanner=ProjectScanner(architecture),
            scope=ScanScope(project_id="P1"),
            history=InMemoryScanHistory(),
        )
        source.collect()
        assert source.collect() == ()

    def test_the_aggregate_is_the_project(self, tmp_path: Path) -> None:
        repo = build_coupled_repo(tmp_path / "coupled")
        architecture = build_architecture(build_index(repo))
        source = ScanScheduleSource(
            scanner=ProjectScanner(architecture),
            scope=ScanScope(project_id="P1"),
            history=InMemoryScanHistory(),
        )
        events = source.collect()
        assert events[0].aggregate_id == "P1"
