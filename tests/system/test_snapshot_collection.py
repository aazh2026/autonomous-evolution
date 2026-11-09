"""快照的端到端 —— **真的 git、真的 pytest 报告、真的 coverage、真的落盘**。

设计依据：docs/tasks.md TASK-P1-023 的两条验收：

- **T1 ①** 对同一 commit 连续构建两次快照，元数据一致；
- **T1 ②** 快照记录可重现：仅凭快照记录 + 产物 URI 即可取回当时的指标数值。

**这里跑的都是真的东西。** git 是真的 git 仓库，测试报告是**真的 pytest 跑
出来的**（不是手写的 XML —— 手写的格式一旦与 pytest 实际产出的对不上，解析器
就会一直绿着，而它已经解析不了真的报告了），覆盖率是**真的 coverage.py 算出
来的**，落盘是真的写到磁盘上的文件。

**"两次一致"要真的经得起试。** 两次构建读的是同一个 commit、同一个工作区
里同一份报告 —— 如果采集口径里混进了"什么时候查的"，两次就一定对不上，而
那正是这条验收要防的事。

**"凭记录取回当时的数"要真的不用再查。** 取回之后把报告文件删掉，再取一次
—— 数还在。**数在快照里，不在报告里**；报告只是用来核对的。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.project.snapshot import (
    CodeStatistics,
    CoverageReport,
    ProjectSnapshotRecord,
    SnapshotBuilder,
    SnapshotIdentity,
    SnapshotMetrics,
)
from harness.project.snapshot_probes import (
    CoverageJsonProbe,
    GitCommitProbe,
    PytestReportProbe,
    SourceTreeStatisticsProbe,
    SystemSnapshotClock,
)
from infrastructure.project.snapshot_documents import SnapshotDocumentStore

#: 报告在仓库里的位置 —— 与采集口默认的相对路径一致。
REPORT_DIR = "reports"
JUNIT_REPORT = f"{REPORT_DIR}/junit.xml"
COVERAGE_REPORT = f"{REPORT_DIR}/coverage.json"


def _git(repo: Path, *args: str) -> str:
    """跑一条 git 命令。"""
    done = subprocess.run(("git", *args), cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一个真 git 仓库 —— **有提交、有代码、有真的测试报告**。

    报告是**真的跑出来的**：一条 ``pytest`` 同时产出 JUnit XML 与 coverage
    JSON。手写这两份文件看着一样，但那是"照着我以为的格式写的"—— 解析器
    解析不了真的报告时，用例照样绿。
    """
    path = tmp_path_factory.mktemp("snapshot_repo")
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "ases@example.invalid")
    _git(path, "config", "user.name", "ASES")
    (path / "money").mkdir()
    (path / "money" / "__init__.py").write_text("", encoding="utf-8")
    (path / "money" / "core.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8"
    )
    (path / "test_money.py").write_text(
        "from money.core import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    _git(path, "add", ".")
    _git(path, "commit", "-m", "初始提交")

    (path / REPORT_DIR).mkdir()
    subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:langsmith_plugin",
            f"--junitxml={JUNIT_REPORT}",
            "--cov=money",
            f"--cov-report=json:{COVERAGE_REPORT}",
            "test_money.py",
        ),
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return path


def _builder() -> SnapshotBuilder:
    """真的采集口 —— **每一个都会真的去干活**。"""
    return SnapshotBuilder(
        commits=GitCommitProbe(),
        code=SourceTreeStatisticsProbe(),
        tests=PytestReportProbe(JUNIT_REPORT),
        coverage=CoverageJsonProbe(COVERAGE_REPORT),
        clock=SystemSnapshotClock(),
    )


class TestTheGitProbe:
    """真的 git —— **代码在哪一版**。"""

    async def test_the_commit_is_the_real_one(self, repo: Path) -> None:
        expected = _git(repo, "rev-parse", "HEAD").strip()

        assert await GitCommitProbe().head_commit(repo) == expected

    async def test_the_branch_is_the_real_one(self, repo: Path) -> None:
        assert await GitCommitProbe().current_branch(repo) == "main"

    async def test_a_detached_head_has_no_branch(self, repo: Path) -> None:
        """**游离 HEAD 是一种真实状态** —— 空串看着像"分支名是空的"。"""
        commit = _git(repo, "rev-parse", "HEAD").strip()
        detached = repo / "detached"
        _git(repo, "clone", "-q", str(repo), str(detached))
        subprocess.run(
            ("git", "checkout", "-q", commit), cwd=detached, capture_output=True, check=True
        )

        assert await GitCommitProbe().current_branch(detached) is None


class TestTheReports:
    """真的报告 —— **跑过才有**。"""

    async def test_the_test_counts_come_from_the_real_report(self, repo: Path) -> None:
        outcome = await PytestReportProbe(JUNIT_REPORT).outcome(repo)

        assert outcome is not None
        assert outcome.passed == 1
        assert outcome.failed == 0
        assert outcome.green

    async def test_the_artifact_points_at_the_real_file(self, repo: Path) -> None:
        """**产物要能回去核对** —— URI 指的地方得真有那个文件。"""
        outcome = await PytestReportProbe(JUNIT_REPORT).outcome(repo)

        assert outcome is not None
        assert outcome.artifact is not None
        assert outcome.artifact.uri == (repo / JUNIT_REPORT).as_uri()
        assert outcome.artifact.sha256 is not None

    async def test_a_missing_report_means_no_tests(self, repo: Path) -> None:
        """**没跑过测试就是没跑过** —— 不是"跑了但零个用例"。"""
        assert await PytestReportProbe("reports/never-ran.xml").outcome(repo) is None

    async def test_the_coverage_comes_from_the_real_report(self, repo: Path) -> None:
        report = await CoverageJsonProbe(COVERAGE_REPORT).coverage(repo)

        assert report is not None
        assert report.overall is not None
        assert report.overall > 0
        assert report.module_coverage("money") is not None

    async def test_a_missing_coverage_means_no_coverage(self, repo: Path) -> None:
        """**没量过不是 0。**"""
        assert await CoverageJsonProbe("reports/never-measured.json").coverage(repo) is None


class TestTheSnapshot:
    """T1 ① 与 T1 ② —— **两次一致，凭记录取回**。"""

    async def test_two_builds_of_the_same_commit_agree(self, repo: Path) -> None:
        """T1 ① —— **同一 commit，元数据一致**。"""
        builder = _builder()

        first = await builder.build(project_id="proj-1", version=1, workspace=repo)
        second = await builder.build(project_id="proj-1", version=2, workspace=repo)

        assert first.metadata() == second.metadata()
        assert first.metadata_digest() == second.metadata_digest()

    async def test_the_record_carries_the_real_numbers(self, repo: Path) -> None:
        record = await _builder().build(project_id="proj-1", version=1, workspace=repo)

        assert record.commit == _git(repo, "rev-parse", "HEAD").strip()
        assert record.metrics.tests is not None
        assert record.metrics.tests.passed == 1
        assert record.metrics.coverage is not None
        assert record.metrics.coverage.overall is not None
        assert record.metrics.code.files is not None
        assert record.metrics.code.files >= 2

    async def test_the_numbers_survive_without_the_reports(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """T1 ② —— **数在快照里，不在报告里**。

        存下快照，把报告删掉，再取回来 —— 数还在，而且和原来一样。

        **删的是副本里的报告。** 仓库是模块级的夹具，真把它删了，后面的用例
        就没得采了 —— 而"报告还在"这件事不该由用例的执行顺序来保证。
        """
        workspace = tmp_path / "workspace"
        shutil.copytree(repo, workspace)
        store = SnapshotDocumentStore(tmp_path / "snapshots")
        record = await _builder().build(project_id="proj-1", version=1, workspace=workspace)
        await store.save(record)

        (workspace / JUNIT_REPORT).unlink()
        (workspace / COVERAGE_REPORT).unlink()

        restored = await store.latest("proj-1")

        assert restored is not None
        assert restored.metadata_digest() == record.metadata_digest()
        assert restored.metric("coverage") == record.metric("coverage")
        assert restored.metric("tests_passed") == record.metric("tests_passed")

    async def test_the_document_is_json_on_disk(self, repo: Path, tmp_path: Path) -> None:
        """**落盘的那份是人能读的** —— 数字就在里面。"""
        store = SnapshotDocumentStore(tmp_path / "snapshots")
        record = await _builder().build(project_id="proj-1", version=1, workspace=repo)
        uri = await store.save(record)

        path = Path(uri.removeprefix("file://"))
        payload = json.loads(path.read_text(encoding="utf-8"))

        assert payload["commit"] == record.commit
        assert payload["code"]["files"] == record.metrics.code.files
        assert record.metrics.coverage is not None
        assert payload["coverage"]["overall"] == record.metrics.coverage.overall

    async def test_a_snapshot_without_reports_still_builds(self, repo: Path) -> None:
        """**没采到的那几项是空的，不是 0** —— 快照照样建得出来。"""
        builder = SnapshotBuilder(
            commits=GitCommitProbe(),
            code=SourceTreeStatisticsProbe(),
            tests=PytestReportProbe("reports/never-ran.xml"),
            coverage=CoverageJsonProbe("reports/never-measured.json"),
            clock=SystemSnapshotClock(),
        )

        record = await builder.build(project_id="proj-1", version=1, workspace=repo)

        assert record.metrics.tests is None
        assert record.metrics.coverage is None
        assert record.metric("coverage") is None

    async def test_an_unknown_project_has_no_snapshot(self, tmp_path: Path) -> None:
        """**"没有快照"与"有一份空快照"是两回事。**"""
        store = SnapshotDocumentStore(tmp_path / "snapshots")

        assert await store.latest("proj-404") is None
        assert await store.load("proj-404", 1) is None

    async def test_the_latest_snapshot_is_the_newest_one(self, repo: Path, tmp_path: Path) -> None:
        """**"最新"是版本号最大的那份** —— 不是碰巧先落的那份。"""
        store = SnapshotDocumentStore(tmp_path / "snapshots")
        builder = _builder()
        await store.save(await builder.build(project_id="proj-1", version=1, workspace=repo))
        await store.save(await builder.build(project_id="proj-1", version=2, workspace=repo))

        restored = await store.latest("proj-1")

        assert restored is not None
        assert restored.version == 2


class TestTheRebuild:
    """重建 —— **凭记录，不凭其它**。"""

    def test_a_record_is_rebuilt_from_its_document(self) -> None:
        record = ProjectSnapshotRecord(
            identity=SnapshotIdentity(
                project_id="proj-1", version=1, commit="abc123", branch="main"
            ),
            metrics=SnapshotMetrics(
                code=CodeStatistics(files=2, lines=2, modules=1),
                tests=None,
                coverage=CoverageReport(overall=1.0),
            ),
            collected_at="2026-01-01T00:00:00+00:00",
        )

        restored = ProjectSnapshotRecord.from_document(record.to_document())

        assert restored.metadata_digest() == record.metadata_digest()
        assert restored.collected_at == "2026-01-01T00:00:00+00:00"
