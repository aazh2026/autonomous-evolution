"""TASK-P3-013 —— 演进机会发现流水线：六路观察接上，一批候选。

**这是验收测试。** 验收条件是两条：

1. **六类数据源各接入 ≥1 个真实来源**；
2. **候选的 evidence 指向真实可解析的证据**。

**这一份测试里没有假的东西。** 打桩的只有时钟，**仓库是真的、git 历史是
真的、pytest 是真的跑、命令是真的跑、台账是人写的样子** —— 所以"六路都
接上了"这句话有底。

**第二条怎么验。** 不是"证据看起来像个路径"，而是**拿核验器把每一条证据
重新核一遍**，要求全部是"找到了"。**光有证据字段不算有证据** —— 一个指
向不存在文件的引用，核验的时候就得露馅。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from apps.observe.pipeline import PipelineConfig, build_pipeline
from apps.observe.resolvers import RepositoryEvidenceResolver
from apps.readers import RuntimeProbe
from core.evolution.observe import ObservationKind, ObservationSnapshot
from core.evolution.opportunity import OpportunityDiscovery, ResolutionStatus
from core.evolution.taxonomy import OpportunityCategory

#: 台账里的两条反馈。
LEDGER = """# 问题清单

### PILOT-001 —— `stats` 子命令收到空输入时抛未捕获的异常

### PILOT-002 —— `load_config` 遇到重复键时静默取最后一个
"""

#: 仓库里各处的文件。
FILES: dict[str, str] = {
    "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
    "src/pkg/__init__.py": '"""一个看起来很正常的包。"""\n',
    "src/pkg/a.py": (
        '"""环上的第一个模块。"""\n\nfrom pkg import b\n\n\ndef one() -> int:\n    return 1\n'
    ),
    "src/pkg/b.py": (
        '"""环上的第二个模块。"""\n\nfrom pkg import a\n\n\ndef two() -> int:\n    return 2\n'
    ),
    "src/pkg/importer.py": (
        '"""反过来依赖测试代码的模块。"""\n\n'
        "from tests import helpers\n\n\n"
        "def use() -> int:\n    return helpers.helper()\n"
    ),
    "src/pkg/hot.py": '"""改得很频繁的那个文件。"""\n\nVALUE = 1\n',
    "tests/__init__.py": "",
    "tests/helpers.py": '"""测试侧的帮手。"""\n\n\ndef helper() -> int:\n    return 0\n',
    "tests/test_ok.py": '"""一条过的用例。"""\n\n\ndef test_it_works() -> None:\n    assert True\n',
    "known_issues/__init__.py": "",
    "known_issues/test_known.py": (
        '"""一个已知缺陷的复现用例。"""\n\nimport pytest\n\n\n'
        "@pytest.mark.xfail(strict=True)\n"
        "def test_the_known_bug_is_still_there() -> None:\n    assert 1 == 2\n"
    ),
    "docs/issues.md": LEDGER,
    "docs/adr/0001-first.md": "# 第一个决策\n\n为什么这么定。\n",
}


def _git(root: Path, *args: str) -> None:
    """在临时仓库里跑一条 git 命令。"""
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """一个真的仓库 —— 六路都能从它身上读出东西。"""
    root = tmp_path / "project"
    for relative, content in FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    _git(root, "config", "user.email", "tester@example.invalid")
    _git(root, "config", "user.name", "Tester")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "第一版")
    for index in range(3):
        (root / "src/pkg/hot.py").write_text(
            f'"""改得很频繁的那个文件。"""\n\nVALUE = {index + 2}\n',
            encoding="utf-8",
        )
        _git(root, "add", "src/pkg/hot.py")
        _git(root, "commit", "-q", "-m", f"第 {index} 次改热文件")
    return root


@pytest.fixture
def pipeline(project: Path) -> tuple[ObservationSnapshot, OpportunityDiscovery]:
    """接好的流水线 —— 六路都接上，跑一遍。"""
    probe = RuntimeProbe(
        id="boom",
        argv=(sys.executable, "-c", "raise SystemExit(1)"),
        cwd=project,
        evidence_path="src/pkg/a.py",
    )
    built = build_pipeline(
        project,
        PipelineConfig(
            project_id="P1",
            targets=("known_issues/", "tests/"),
            probes=(probe,),
        ),
    )
    return built.run()


class TestTheSixSources:
    """六类数据源各接入 ≥1 个真实来源。"""

    def test_all_six_kinds_produced_something(
        self, pipeline: tuple[ObservationSnapshot, OpportunityDiscovery]
    ) -> None:
        """**六路都读到了东西** —— 这就是验收条件的第一句。

        断言写的是"六类全在"，不是"至少一类" —— 后者会被"只有一路接上了"
        蒙过去。
        """
        snapshot, _ = pipeline
        assert set(snapshot.kinds_covered) == set(ObservationKind)

    def test_no_source_failed(
        self, pipeline: tuple[ObservationSnapshot, OpportunityDiscovery]
    ) -> None:
        """**没有哪一路没读成** —— 缺实测值是失败，不是 0。"""
        snapshot, _ = pipeline
        assert snapshot.failed_kinds == ()

    def test_the_snapshot_has_one_row_per_kind(
        self, pipeline: tuple[ObservationSnapshot, OpportunityDiscovery]
    ) -> None:
        """**六路都要在，一个都不能少。**"""
        snapshot, _ = pipeline
        assert len(snapshot.statuses) == 6

    def test_each_kind_produced_at_least_one_observation(
        self, pipeline: tuple[ObservationSnapshot, OpportunityDiscovery]
    ) -> None:
        """**"接上了"不等于"读到东西了"** —— 每一类都得真有观察。"""
        snapshot, _ = pipeline
        for kind in ObservationKind:
            assert snapshot.by_kind(kind), kind


class TestTheEvidence:
    """候选的 evidence 指向真实可解析的证据。"""

    def test_every_candidate_has_evidence(
        self, pipeline: tuple[ObservationSnapshot, OpportunityDiscovery]
    ) -> None:
        """**没有证据的候选不该存在。**"""
        _, discovery = pipeline
        assert discovery.candidates
        for candidate in discovery.candidates:
            assert candidate.evidence

    def test_every_evidence_resolves(
        self,
        pipeline: tuple[ObservationSnapshot, OpportunityDiscovery],
        project: Path,
    ) -> None:
        """**拿核验器把每一条证据重新核一遍** —— 全部是"找到了"。

        **光有证据字段不算有证据。** 一个指向不存在文件的引用，核验的时候
        就得露馅 —— 这是 R-029 的落点。
        """
        _, discovery = pipeline
        resolver = RepositoryEvidenceResolver(root=project, issue_ledger=Path("docs/issues.md"))
        for candidate in discovery.candidates:
            for evidence in candidate.evidence:
                resolution = resolver.resolve(evidence)
                assert resolution.status is ResolutionStatus.RESOLVED, (
                    candidate.id,
                    evidence.ref,
                    resolution.detail,
                )

    def test_the_file_evidence_points_at_a_file_that_exists(
        self,
        pipeline: tuple[ObservationSnapshot, OpportunityDiscovery],
        project: Path,
    ) -> None:
        """**证据指的地方真的在那儿** —— 不是"形状像个路径"。"""
        _, discovery = pipeline
        for candidate in discovery.candidates:
            for evidence in candidate.evidence:
                if evidence.kind.value == "file":
                    assert (project / evidence.ref).exists(), evidence.ref

    def test_the_issue_evidence_points_at_a_real_entry(
        self,
        pipeline: tuple[ObservationSnapshot, OpportunityDiscovery],
        project: Path,
    ) -> None:
        """**问题单编号在台账里真的有一条**。"""
        _, discovery = pipeline
        ledger = (project / "docs/issues.md").read_text(encoding="utf-8")
        for candidate in discovery.candidates:
            for evidence in candidate.evidence:
                if evidence.kind.value == "issue":
                    assert evidence.identifier in ledger


class TestTheCandidates:
    """产出的候选。"""

    def test_they_belong_to_the_five_categories(
        self, pipeline: tuple[ObservationSnapshot, OpportunityDiscovery]
    ) -> None:
        """**五类演进机会分类法定死了**（R-024）。"""
        _, discovery = pipeline
        for candidate in discovery.candidates:
            assert candidate.category in set(OpportunityCategory)

    def test_they_cover_four_of_the_five_categories(
        self, pipeline: tuple[ObservationSnapshot, OpportunityDiscovery]
    ) -> None:
        """**四类产出，技术类不在这儿** —— 技术类要的是趋势与收益论证。

        技术候选需要一条**趋势**，观察给不出趋势 —— 所以它不在这一侧。
        """
        _, discovery = pipeline
        categories = {candidate.category for candidate in discovery.candidates}
        assert categories == {
            OpportunityCategory.ARCHITECTURE,
            OpportunityCategory.ENGINEERING,
            OpportunityCategory.PRODUCT,
            OpportunityCategory.QUALITY,
        }

    def test_the_ids_are_unique(
        self, pipeline: tuple[ObservationSnapshot, OpportunityDiscovery]
    ) -> None:
        """**候选 id 是主键** —— 撞了落库就炸。"""
        _, discovery = pipeline
        ids = [candidate.id for candidate in discovery.candidates]
        assert len(ids) == len(set(ids))

    def test_each_one_has_a_hypothesis_and_an_expected_outcome(
        self, pipeline: tuple[ObservationSnapshot, OpportunityDiscovery]
    ) -> None:
        """**没有预期结果就无法事后核验**（R-033）。"""
        _, discovery = pipeline
        for candidate in discovery.candidates:
            assert candidate.hypothesis
            assert candidate.expected_outcome

    def test_the_docs_observation_is_dropped_with_a_reason(
        self, pipeline: tuple[ObservationSnapshot, OpportunityDiscovery]
    ) -> None:
        """**决策记录写的是已经做过的决定，不是待办** —— 丢掉，但留痕。"""
        _, discovery = pipeline
        dropped = [item for item in discovery.dropped if item.subject == "0001-first"]
        assert len(dropped) == 1
        assert "不产出候选" in dropped[0].reason


class TestTheCounts:
    """实测的那些数。"""

    def test_the_observation_counts(
        self, pipeline: tuple[ObservationSnapshot, OpportunityDiscovery]
    ) -> None:
        """**每一类观察到了几个** —— 架构文档里的数就是这些。"""
        snapshot, _ = pipeline
        counts = {kind: len(snapshot.by_kind(kind)) for kind in ObservationKind}
        assert counts == {
            ObservationKind.CHANGES: 1,
            ObservationKind.TESTS: 1,
            ObservationKind.STATIC: 2,
            ObservationKind.RUNTIME: 1,
            ObservationKind.FEEDBACK: 2,
            ObservationKind.DOCS: 1,
        }

    def test_the_candidate_and_drop_counts(
        self, pipeline: tuple[ObservationSnapshot, OpportunityDiscovery]
    ) -> None:
        """**观察 8 条 → 候选 7 条 → 丢掉 1 条**（那条是决策记录）。"""
        snapshot, discovery = pipeline
        assert len(snapshot.observations) == 8
        assert len(discovery.candidates) == 7
        assert len(discovery.dropped) == 1
