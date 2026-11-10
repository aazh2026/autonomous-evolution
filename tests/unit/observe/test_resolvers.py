"""TASK-P3-013 —— 证据核验：这个引用指的东西，真的在这儿吗。

**测的是"三态分得开"。** 找到了、找过了不在、查不了 —— 这三件事的补救
动作完全不同，混成一种就等于没记。

**这一层是候选的关口。** 发现层拿着每一条证据引用来问，问完才决定这条候
选算不算数。
"""

from __future__ import annotations

from pathlib import Path

from apps.observe.resolvers import RepositoryEvidenceResolver
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.opportunity import ResolutionStatus


class TestFileReferences:
    """文件引用。"""

    def test_a_file_that_exists_resolves(self, tmp_path: Path) -> None:
        """**找到了。**"""
        (tmp_path / "src").mkdir()
        (tmp_path / "src/money.py").write_text("X = 1\n", encoding="utf-8")
        resolver = RepositoryEvidenceResolver(root=tmp_path)
        resolution = resolver.resolve(Evidence(kind=EvidenceKind.FILE, ref="src/money.py"))
        assert resolution.status is ResolutionStatus.RESOLVED

    def test_a_file_that_is_not_there_does_not_resolve(self, tmp_path: Path) -> None:
        """**找过了，不在** —— 证据指错了地方。"""
        resolver = RepositoryEvidenceResolver(root=tmp_path)
        resolution = resolver.resolve(Evidence(kind=EvidenceKind.FILE, ref="src/nope.py"))
        assert resolution.status is ResolutionStatus.MISSING

    def test_the_reason_says_where_it_looked(self, tmp_path: Path) -> None:
        """**"没找到"要说得清去哪儿找过了。**"""
        resolver = RepositoryEvidenceResolver(root=tmp_path)
        resolution = resolver.resolve(Evidence(kind=EvidenceKind.FILE, ref="src/nope.py"))
        assert "src/nope.py" in resolution.detail

    def test_a_path_that_escapes_the_repository_is_rejected(self, tmp_path: Path) -> None:
        """**路径不许跑出仓库** —— 它在别处，而"在别处"跟"在这儿"不是一回
        事。
        """
        resolver = RepositoryEvidenceResolver(root=tmp_path / "repo")
        resolution = resolver.resolve(Evidence(kind=EvidenceKind.FILE, ref="../outside.py"))
        assert resolution.status is ResolutionStatus.MISSING

    def test_a_directory_counts_as_a_hit(self, tmp_path: Path) -> None:
        """**目录也是"在这儿"** —— 证据指向一个目录不算指错。"""
        (tmp_path / "docs/adr").mkdir(parents=True)
        resolver = RepositoryEvidenceResolver(root=tmp_path)
        resolution = resolver.resolve(Evidence(kind=EvidenceKind.FILE, ref="docs/adr"))
        assert resolution.status is ResolutionStatus.RESOLVED


class TestIssueReferences:
    """问题单引用。"""

    def _ledger(self, root: Path) -> Path:
        """建一份台账。"""
        (root / "docs").mkdir(parents=True, exist_ok=True)
        (root / "docs/issues.md").write_text(
            "# 问题清单\n\n### PILOT-001 —— 第一次崩了\n",
            encoding="utf-8",
        )
        return Path("docs/issues.md")

    def test_an_issue_in_the_ledger_resolves(self, tmp_path: Path) -> None:
        """**找到了。**"""
        ledger = self._ledger(tmp_path)
        resolver = RepositoryEvidenceResolver(root=tmp_path, issue_ledger=ledger)
        resolution = resolver.resolve(Evidence(kind=EvidenceKind.ISSUE, ref="PILOT-001"))
        assert resolution.status is ResolutionStatus.RESOLVED

    def test_an_issue_that_is_not_in_the_ledger_does_not_resolve(self, tmp_path: Path) -> None:
        """**查过了，不在。**"""
        ledger = self._ledger(tmp_path)
        resolver = RepositoryEvidenceResolver(root=tmp_path, issue_ledger=ledger)
        resolution = resolver.resolve(Evidence(kind=EvidenceKind.ISSUE, ref="PILOT-999"))
        assert resolution.status is ResolutionStatus.MISSING

    def test_without_a_ledger_it_cannot_be_checked(self, tmp_path: Path) -> None:
        """**查不了，不是"不在"** —— 没接台账就不知道有没有。"""
        resolver = RepositoryEvidenceResolver(root=tmp_path)
        resolution = resolver.resolve(Evidence(kind=EvidenceKind.ISSUE, ref="PILOT-001"))
        assert resolution.status is ResolutionStatus.UNAVAILABLE

    def test_a_ledger_that_is_not_here_cannot_be_checked(self, tmp_path: Path) -> None:
        """**台账不在本机 —— 那是查不了，不是"没有这个问题"。**"""
        resolver = RepositoryEvidenceResolver(root=tmp_path, issue_ledger=Path("docs/nope.md"))
        resolution = resolver.resolve(Evidence(kind=EvidenceKind.ISSUE, ref="PILOT-001"))
        assert resolution.status is ResolutionStatus.UNAVAILABLE

    def test_the_bare_id_is_accepted_too(self, tmp_path: Path) -> None:
        """**两种写法都认** —— 形态已经由 ``kind`` 声明过了。"""
        ledger = self._ledger(tmp_path)
        resolver = RepositoryEvidenceResolver(root=tmp_path, issue_ledger=ledger)
        resolution = resolver.resolve(Evidence(kind=EvidenceKind.ISSUE, ref="issue:PILOT-001"))
        assert resolution.status is ResolutionStatus.RESOLVED


class TestArtifactReferences:
    """产物引用。"""

    def test_an_artifact_cannot_be_checked_locally(self, tmp_path: Path) -> None:
        """**本机没有产物存储** —— 查不了，不是不在。"""
        resolver = RepositoryEvidenceResolver(root=tmp_path)
        resolution = resolver.resolve(
            Evidence(kind=EvidenceKind.ARTIFACT, ref="artifact://report-001")
        )
        assert resolution.status is ResolutionStatus.UNAVAILABLE

    def test_a_test_report_cannot_be_checked_locally(self, tmp_path: Path) -> None:
        resolver = RepositoryEvidenceResolver(root=tmp_path)
        resolution = resolver.resolve(
            Evidence(kind=EvidenceKind.TEST_REPORT, ref="artifact://report-001")
        )
        assert resolution.status is ResolutionStatus.UNAVAILABLE
