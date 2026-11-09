"""项目快照的口径 —— **当时是多少，事后还能不能取回**。

设计依据：docs/tasks.md TASK-P1-023（T1 ① 对同一 commit 连续构建两次快照，
元数据一致；T1 ② 快照记录可重现：仅凭快照记录 + 产物 URI 即可取回当时的
指标数值）、R-068、R-069。

**这一层只测口径。** 真的建一个 git 仓库、真的跑 pytest 出报告 —— 那是
:mod:`tests.system.test_snapshot_collection` 的事。口径必须能被单独证伪：
口径错了，跑得再真也是错的。

**每条断言都在盯一件"看起来差不多但含义相反"的事。** "没采到"与"采到是
0"、"记录一致"与"元数据一致"、"取不到"与"取到是空的" —— 这些在报表上常常
长得一模一样，而这正是要守住的地方。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.project.snapshot import (
    CodeStatistics,
    CoverageReport,
    ProjectSnapshotRecord,
    SnapshotArtifact,
    SnapshotBuilder,
    SnapshotError,
    SnapshotIdentity,
    SnapshotMetrics,
    SuiteOutcome,
)

#: 固定的时间 —— **两次构建要比的是元数据，不是采集时点**。
COLLECTED_AT = "2026-01-01T00:00:00+00:00"


def _artifact() -> SnapshotArtifact:
    return SnapshotArtifact(
        kind="test_report",
        uri="file:///tmp/reports/junit.xml",
        sha256="a" * 64,
    )


def _record(
    *,
    collected_at: str | None = COLLECTED_AT,
    tests: SuiteOutcome | None = None,
    coverage: CoverageReport | None = None,
    code: CodeStatistics | None = None,
) -> ProjectSnapshotRecord:
    """一份"采到了东西"的快照。"""
    return ProjectSnapshotRecord(
        identity=SnapshotIdentity(
            project_id="proj-1",
            version=3,
            commit="abc123def456",
            branch="main",
        ),
        metrics=SnapshotMetrics(
            code=code if code is not None else CodeStatistics(files=12, lines=340, modules=4),
            tests=tests,
            coverage=coverage,
        ),
        collected_at=collected_at,
    )


class TestTheIdentity:
    """身份 —— **快照得说得清是谁的、哪一版、指向哪个 commit**。"""

    def test_a_snapshot_without_a_project_is_refused(self) -> None:
        with pytest.raises(SnapshotError):
            SnapshotIdentity(project_id="  ", version=1, commit="abc")

    def test_a_snapshot_without_a_commit_is_refused(self) -> None:
        """**没有 commit 的快照指向不了任何代码** —— 它只是一张表。"""
        with pytest.raises(SnapshotError):
            SnapshotIdentity(project_id="proj-1", version=1, commit="  ")

    def test_a_version_below_one_is_refused(self) -> None:
        with pytest.raises(SnapshotError):
            SnapshotIdentity(project_id="proj-1", version=0, commit="abc")

    def test_the_identity_is_exposed(self) -> None:
        record = _record()
        assert record.project_id == "proj-1"
        assert record.version == 3
        assert record.commit == "abc123def456"
        assert record.branch == "main"


class TestTheNumbers:
    """采集到的那些数 —— **没采到的是 ``None``，不是 0**。"""

    def test_a_negative_file_count_is_refused(self) -> None:
        with pytest.raises(SnapshotError):
            CodeStatistics(files=-1)

    def test_a_negative_test_count_is_refused(self) -> None:
        with pytest.raises(SnapshotError):
            SuiteOutcome(passed=-1, failed=0)

    def test_a_coverage_above_one_is_refused(self) -> None:
        with pytest.raises(SnapshotError):
            CoverageReport(overall=1.5)

    def test_the_module_coverage_is_frozen(self) -> None:
        """**快照是只读的。** 事后改一个数，就是改证据。"""
        report = CoverageReport(overall=0.5, by_module={"core": 0.4})
        with pytest.raises(TypeError):
            report.by_module["core"] = 0.9  # type: ignore[index]

    def test_an_unknown_module_has_no_coverage(self) -> None:
        """**没这个模块就是没有，不是 0。**"""
        report = CoverageReport(overall=0.5, by_module={"core": 0.4})
        assert report.module_coverage("agents") is None
        assert report.module_coverage("core") == 0.4

    def test_the_test_count_is_the_sum_of_the_kinds(self) -> None:
        outcome = SuiteOutcome(passed=3, failed=1, skipped=2, errors=1)
        assert outcome.total == 7

    def test_a_run_with_failures_is_not_green(self) -> None:
        """**跑挂了也是一份结果，照记。**"""
        assert not SuiteOutcome(passed=1, failed=1).green
        assert SuiteOutcome(passed=1, failed=0).green

    def test_zero_files_is_not_the_same_as_no_count(self) -> None:
        """0 个文件是事实；没数是另一回事。"""
        assert CodeStatistics(files=0).files == 0
        assert CodeStatistics().files is None

    def test_an_artifact_without_a_uri_is_refused(self) -> None:
        """**产物指了个空地方** —— 回头核对的时候才发现，那就晚了。"""
        with pytest.raises(SnapshotError):
            SnapshotArtifact(kind="test_report", uri="  ")

    def test_an_artifact_without_a_kind_is_refused(self) -> None:
        with pytest.raises(SnapshotError):
            SnapshotArtifact(kind="  ", uri="file:///tmp/junit.xml")


class TestTheRecord:
    """一份快照 —— **元数据是什么、怎么取回当时的数**。"""

    def test_the_metadata_does_not_carry_the_collection_time(self) -> None:
        """**采集时点是"什么时候查的"，不是"这个 commit 当时是什么样"。**

        把它放进元数据，"同一 commit 两次一致"这条要求就永远立不住。
        """
        metadata = _record().metadata()
        assert "collected_at" not in metadata
        assert "collected_at" not in str(metadata)

    def test_two_records_of_the_same_commit_have_the_same_metadata(self) -> None:
        """T1 ① 的口径 —— **同一 commit，元数据必须一致**。"""
        first = _record(collected_at="2026-01-01T00:00:00+00:00")
        second = _record(collected_at="2026-06-01T12:30:00+00:00")

        assert first.metadata() == second.metadata()
        assert first.metadata_digest() == second.metadata_digest()

    def test_a_different_commit_has_a_different_metadata(self) -> None:
        """指纹得能分出"不是同一个 commit"。"""
        first = _record()
        second = ProjectSnapshotRecord(
            identity=SnapshotIdentity(
                project_id="proj-1", version=3, commit="999999", branch="main"
            ),
            metrics=first.metrics,
            collected_at=COLLECTED_AT,
        )

        assert first.metadata_digest() != second.metadata_digest()

    def test_the_digest_is_stable_across_calls(self) -> None:
        record = _record()
        assert record.metadata_digest() == record.metadata_digest()

    def test_the_metric_can_be_retrieved(self) -> None:
        """T1 ② 的口径 —— **只认快照里记下的**。"""
        record = _record(
            tests=SuiteOutcome(passed=5, failed=0, artifact=_artifact()),
            coverage=CoverageReport(overall=0.82),
        )

        assert record.metric("coverage") == 0.82
        assert record.metric("tests_passed") == 5
        assert record.metric("lines") == 340

    def test_a_metric_that_was_not_collected_is_none(self) -> None:
        """**没采到的不是 0。**"""
        record = _record()

        assert record.metric("coverage") is None
        assert record.metric("tests_passed") is None

    def test_an_unknown_metric_is_refused(self) -> None:
        """**问一个快照里没有的东西，要报错 —— 不是悄悄返回 0。**"""
        with pytest.raises(SnapshotError):
            _record().metric("revenue")

    def test_the_artifacts_are_listed_in_a_fixed_order(self) -> None:
        """顺序固定 —— 两份快照的文档才能逐字节比较。"""
        record = _record(
            tests=SuiteOutcome(passed=1, failed=0, artifact=_artifact()),
            coverage=CoverageReport(
                overall=0.5,
                artifact=SnapshotArtifact(kind="coverage_report", uri="file:///tmp/c.json"),
            ),
        )

        assert [item.kind for item in record.artifacts()] == [
            "test_report",
            "coverage_report",
        ]


class TestTheDocument:
    """落盘的那份 —— **凭它就能重建当时的快照**。"""

    def test_the_round_trip_preserves_the_numbers(self) -> None:
        """T1 ② 的口径 —— **重建出来的是同一份，不是"重新算一遍"**。"""
        record = _record(
            tests=SuiteOutcome(passed=5, failed=1, skipped=2, artifact=_artifact()),
            coverage=CoverageReport(overall=0.82, by_module={"core": 0.8}),
        )

        restored = ProjectSnapshotRecord.from_document(record.to_document())

        assert restored.metadata_digest() == record.metadata_digest()
        assert restored.metric("coverage") == 0.82
        assert restored.metric("tests_failed") == 1
        assert restored.metrics.tests is not None
        assert restored.metrics.tests.artifact is not None
        assert restored.metrics.tests.artifact.uri == "file:///tmp/reports/junit.xml"

    def test_the_round_trip_preserves_the_collection_time(self) -> None:
        """**少了它，重建出来的快照就不知道自己是什么时候的了。**"""
        record = _record()
        restored = ProjectSnapshotRecord.from_document(record.to_document())
        assert restored.collected_at == COLLECTED_AT

    def test_a_document_without_a_coverage_stays_without_one(self) -> None:
        """**"没量"在往返之后还是"没量"** —— 不能变成 0。"""
        record = _record()
        restored = ProjectSnapshotRecord.from_document(record.to_document())
        assert restored.metrics.coverage is None

    def test_a_document_without_code_statistics_stays_without_them(self) -> None:
        """**没数出来的文件数在往返之后还是没数出来** —— 不能变成 0。

        变成 0 的话，重建出来的快照比原来多了一个事实：**它声称这个仓库
        一个文件都没有** —— 而真相是"没人数"。
        """
        record = _record(code=CodeStatistics())
        restored = ProjectSnapshotRecord.from_document(record.to_document())

        assert restored.metrics.code.files is None
        assert restored.metrics.code.lines is None
        assert restored.metrics.code.modules is None
        assert restored.metric("files") is None
        assert restored.metric("coverage") is None

    def test_a_document_with_a_broken_number_is_refused(self) -> None:
        """**读不懂就是读不懂** —— 不能悄悄塞个 0 进去。"""
        document = _record().to_document()
        document["code"] = {"files": "很多", "lines": None, "modules": None}

        with pytest.raises(SnapshotError):
            ProjectSnapshotRecord.from_document(document)

    def test_a_boolean_is_not_a_count(self) -> None:
        """``True`` 是 ``int`` 的子类 —— 不挡一下，"通过数是 True"会被当成 1。"""
        document = _record().to_document()
        document["code"] = {"files": True, "lines": None, "modules": None}

        with pytest.raises(SnapshotError):
            ProjectSnapshotRecord.from_document(document)


class _FakeCommits:
    """假的 commit 口 —— **固定答案**。"""

    __slots__ = ("_branch", "_commit")

    def __init__(self, commit: str = "abc123", branch: str | None = "main") -> None:
        self._commit = commit
        self._branch = branch

    async def head_commit(self, workspace: Path) -> str:
        return self._commit

    async def current_branch(self, workspace: Path) -> str | None:
        return self._branch


class _FakeCode:
    """假的代码统计口。"""

    __slots__ = ("_stats",)

    def __init__(self, stats: CodeStatistics) -> None:
        self._stats = stats

    async def statistics(self, workspace: Path) -> CodeStatistics:
        return self._stats


class _FakeTests:
    """假的测试口 —— **可以是"没有"**。"""

    __slots__ = ("_outcome",)

    def __init__(self, outcome: SuiteOutcome | None) -> None:
        self._outcome = outcome

    async def outcome(self, workspace: Path) -> SuiteOutcome | None:
        return self._outcome


class _FakeCoverage:
    """假的覆盖率口 —— **可以是"没有"**。"""

    __slots__ = ("_report",)

    def __init__(self, report: CoverageReport | None) -> None:
        self._report = report

    async def coverage(self, workspace: Path) -> CoverageReport | None:
        return self._report


class _FakeClock:
    """假的钟 —— **时间从这儿来**。"""

    __slots__ = ("_moment",)

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


def _builder(
    *,
    tests: SuiteOutcome | None = None,
    coverage: CoverageReport | None = None,
) -> SnapshotBuilder:
    return SnapshotBuilder(
        commits=_FakeCommits(),
        code=_FakeCode(CodeStatistics(files=3, lines=30, modules=1)),
        tests=_FakeTests(tests),
        coverage=_FakeCoverage(coverage),
        clock=_FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )


class TestTheBuilder:
    """构建器 —— **采一遍，落成一份记录**。"""

    async def test_the_record_is_assembled(self) -> None:
        builder = _builder(
            tests=SuiteOutcome(passed=2, failed=0),
            coverage=CoverageReport(overall=0.5),
        )

        record = await builder.build(project_id="proj-1", version=1, workspace=Path("/tmp"))

        assert record.commit == "abc123"
        assert record.branch == "main"
        assert record.metrics.tests is not None
        assert record.metrics.tests.passed == 2
        assert record.metrics.coverage is not None
        assert record.metrics.coverage.overall == 0.5
        assert record.collected_at == "2026-01-01T00:00:00+00:00"

    async def test_a_missing_test_report_stays_missing(self) -> None:
        """**没跑过测试就是没跑过** —— 不是"跑了但零个用例"。"""
        builder = _builder()

        record = await builder.build(project_id="proj-1", version=1, workspace=Path("/tmp"))

        assert record.metrics.tests is None
        assert record.metric("tests_passed") is None

    async def test_a_missing_coverage_stays_missing(self) -> None:
        """**没量过覆盖率不是 0。**"""
        builder = _builder()

        record = await builder.build(project_id="proj-1", version=1, workspace=Path("/tmp"))

        assert record.metrics.coverage is None
        assert record.metric("coverage") is None

    async def test_two_builds_of_the_same_commit_agree(self) -> None:
        """T1 ① —— **同一个 commit，两次构建的元数据一致**。"""
        builder = _builder(tests=SuiteOutcome(passed=2, failed=0))

        first = await builder.build(project_id="proj-1", version=1, workspace=Path("/tmp"))
        second = await builder.build(project_id="proj-1", version=2, workspace=Path("/tmp"))

        assert first.metadata_digest() == second.metadata_digest()
