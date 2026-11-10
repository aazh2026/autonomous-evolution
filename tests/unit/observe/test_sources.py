"""TASK-P3-013 —— 六路观察来源：每一路都从**真的东西**里读出现状。

**测的是"读得出读不出"，不是"读出来长什么样"。** 所以这里的断言分两类：

1. **读得出** —— 真的建一个临时仓库、真的跑一次 pytest、真的跑一条会崩的
   命令，看观察是不是真的出现了；
2. **读不出时不报错** —— 台账不在、目录不在、不是 git 仓库，这些都要**安静
   地什么都不产出**，而不是把整个系统带崩。

**"读不出"与"没有"是两件事。** 这一层只负责把"现在什么样"如实说出来；
"要不要因此开一轮演进"是派发那一侧的事。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from apps.observe.sources import (
    ChangeHistorySource,
    DocumentationSource,
    FeedbackSource,
    PytestSuiteSource,
    RuntimeMetricSource,
    SourceReadError,
    StaticAnalysisSource,
)
from apps.readers import RuntimeProbe
from core.evolution.observe import ObservationKind, ObservationSource
from knowledge.architecture.model import build_architecture
from knowledge.repository_index.index import build_index
from tests.support.coupled_repo import build_coupled_repo


def _git(root: Path, *args: str) -> None:
    """在临时仓库里跑一条 git 命令。"""
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def churn_repo(tmp_path: Path) -> Path:
    """一个真的 git 仓库 —— 一个文件被改了三次，另一个被改了一次。"""
    root = tmp_path / "churn"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    _git(root, "config", "user.email", "tester@example.invalid")
    _git(root, "config", "user.name", "Tester")
    _git(root, "config", "commit.gpgsign", "false")
    for index in range(3):
        (root / "hot.py").write_text(f"# 第 {index} 次\n", encoding="utf-8")
        _git(root, "add", "hot.py")
        _git(root, "commit", "-q", "-m", f"改第 {index} 次")
    (root / "cold.py").write_text("# 只改一次\n", encoding="utf-8")
    _git(root, "add", "cold.py")
    _git(root, "commit", "-q", "-m", "只改一次")
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

    def test_every_source_satisfies_the_port(self, churn_repo: Path) -> None:
        """**六路是同一个形状** —— 编排那一侧才不用写六遍。"""
        sources: list[ObservationSource] = [
            ChangeHistorySource(root=churn_repo),
            PytestSuiteSource(root=churn_repo, targets=()),
            StaticAnalysisSource(architecture=build_architecture(build_index(Path()))),
            RuntimeMetricSource(probes=()),
            FeedbackSource(root=churn_repo, ledger=Path("docs/issues.md")),
            DocumentationSource(root=churn_repo, directory=Path("docs/adr")),
        ]
        for source in sources:
            assert isinstance(source, ObservationSource)

    def test_the_sources_cover_all_six_kinds(self, churn_repo: Path) -> None:
        """**六路各报自己的身份** —— 快照里那一列就是它。"""
        sources: list[ObservationSource] = [
            ChangeHistorySource(root=churn_repo),
            PytestSuiteSource(root=churn_repo, targets=()),
            StaticAnalysisSource(architecture=build_architecture(build_index(Path()))),
            RuntimeMetricSource(probes=()),
            FeedbackSource(root=churn_repo, ledger=Path("docs/issues.md")),
            DocumentationSource(root=churn_repo, directory=Path("docs/adr")),
        ]
        assert {source.kind for source in sources} == set(ObservationKind)


class TestTheChangeSource:
    """代码变更那一路 —— 真的 git 历史。"""

    def test_it_finds_the_file_that_changed_often(self, churn_repo: Path) -> None:
        """**改动频繁的文件就是热点** —— 读的是真的 git 历史。"""
        observations = ChangeHistorySource(root=churn_repo).observe()
        assert [item.subject for item in observations] == ["hot.py"]

    def test_the_fact_says_how_often(self, churn_repo: Path) -> None:
        """**事实里带着数** —— "改得频繁"没有数就是一句空话。"""
        observations = ChangeHistorySource(root=churn_repo).observe()
        assert "3 次" in observations[0].fact

    def test_the_evidence_is_the_file(self, churn_repo: Path) -> None:
        """**证据落在那个文件本身** —— 核验的人拿着就能去查。"""
        observations = ChangeHistorySource(root=churn_repo).observe()
        assert observations[0].evidence[0].ref == "hot.py"

    def test_a_file_changed_once_is_not_a_hotspot(self, churn_repo: Path) -> None:
        """**改过一次是常态** —— 改过两次以上才说明在反复折腾。"""
        observations = ChangeHistorySource(root=churn_repo).observe()
        assert "cold.py" not in {item.subject for item in observations}

    def test_a_non_git_directory_produces_nothing(self, tmp_path: Path) -> None:
        """**不是 git 仓库就什么也不产出** —— 而不是报错。"""
        assert ChangeHistorySource(root=tmp_path).observe() == ()

    def test_a_file_that_cannot_be_referenced_is_skipped(self, tmp_path: Path) -> None:
        """**落不了证据的文件不看** —— 造不出来的东西不能硬造。

        ``Makefile`` 这种名字不带扩展名、又不在目录里的文件，与一句自然语
        言描述分不开，所以证据词表不收它 —— 这一路也就看不到它。**但跳过
        了几个要写在观察的细节里**，不然读的人分不清"没有热点"与"有热点但
        说不出证据"。
        """
        root = tmp_path / "makefile_repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        _git(root, "config", "user.email", "tester@example.invalid")
        _git(root, "config", "user.name", "Tester")
        _git(root, "config", "commit.gpgsign", "false")
        for index in range(2):
            (root / "Makefile").write_text(f"all:\n\t# {index}\n", encoding="utf-8")
            _git(root, "add", "Makefile")
            _git(root, "commit", "-q", "-m", f"第 {index} 次")
        assert ChangeHistorySource(root=root).observe() == ()


class TestTheTestSource:
    """测试那一路 —— 真的跑一遍 pytest。"""

    def test_it_finds_the_failing_case(self, failing_project: Path) -> None:
        """**真的跑一遍** —— 不是读一个缓存的结果文件。"""
        observations = PytestSuiteSource(
            root=failing_project, targets=("test_broken.py",)
        ).observe()
        assert [item.subject for item in observations] == ["test_broken.py::test_this_fails"]

    def test_the_evidence_is_the_file_the_case_lives_in(self, failing_project: Path) -> None:
        """**证据落在用例所在的文件上** —— 用例 id 放在事实里。

        用例 id 里带着 ``::``，它不是一个文件路径，而证据的形态是声明出来
        的。
        """
        observations = PytestSuiteSource(
            root=failing_project, targets=("test_broken.py",)
        ).observe()
        assert observations[0].evidence[0].ref == "test_broken.py"

    def test_the_passing_case_is_not_reported(self, failing_project: Path) -> None:
        observations = PytestSuiteSource(root=failing_project, targets=("test_ok.py",)).observe()
        assert observations == ()

    def test_a_target_that_does_not_exist_is_an_error(self, failing_project: Path) -> None:
        """**跑错了地方 = 没验成，不是"质量没问题"。**

        这条是本仓库的三态规矩在观察源上的落点：缺实测值是失败，不是 0。
        """
        source = PytestSuiteSource(root=failing_project, targets=("nope/",))
        with pytest.raises(SourceReadError):
            source.observe()

    def test_the_error_says_it_did_not_run(self, failing_project: Path) -> None:
        """**报错要说清是"没跑成"，不是"没有失败"。**"""
        source = PytestSuiteSource(root=failing_project, targets=("nope/",))
        with pytest.raises(SourceReadError) as caught:
            source.observe()
        assert "没跑成" in str(caught.value)

    def test_an_xfail_case_is_reported_when_it_is_run_for_real(self, tmp_path: Path) -> None:
        """**"这些毛病还在吗"要按真失败来跑。**"""
        root = tmp_path / "xfailed"
        root.mkdir()
        (root / "test_known.py").write_text(
            "import pytest\n"
            "\n"
            "@pytest.mark.xfail(strict=True)\n"
            "def test_known_bug():\n    assert 1 == 2\n",
            encoding="utf-8",
        )
        observations = PytestSuiteSource(root=root, targets=("test_known.py",)).observe()
        assert [item.subject for item in observations] == ["test_known.py::test_known_bug"]

    def test_without_runxfail_the_xfail_case_is_not_a_failure(self, tmp_path: Path) -> None:
        """**默认不把 xfail 算成失败** —— 那是"预期中的失败"。"""
        root = tmp_path / "xfailed2"
        root.mkdir()
        (root / "test_known.py").write_text(
            "import pytest\n"
            "\n"
            "@pytest.mark.xfail(strict=True)\n"
            "def test_known_bug():\n    assert 1 == 2\n",
            encoding="utf-8",
        )
        source = PytestSuiteSource(root=root, targets=("test_known.py",), run_xfail=False)
        assert source.observe() == ()


class TestTheStaticSource:
    """静态分析那一路 —— 真的扫一遍仓库的依赖图。"""

    def test_it_finds_the_dependency_cycle(self, tmp_path: Path) -> None:
        """**环是真的环** —— 那个仓库里 ``pkg.ring_a`` 与 ``pkg.ring_b`` 互相
        依赖。
        """
        repo = build_coupled_repo(tmp_path / "coupled")
        architecture = build_architecture(build_index(repo))
        observations = StaticAnalysisSource(architecture=architecture).observe()
        cycles = [item for item in observations if "->" in item.subject and "ring" in item.subject]
        assert cycles, [item.subject for item in observations]

    def test_the_evidence_is_a_real_file(self, tmp_path: Path) -> None:
        """**证据落在模块所在的文件上** —— 核验器拿它去仓库里找。"""
        repo = build_coupled_repo(tmp_path / "coupled")
        architecture = build_architecture(build_index(repo))
        observations = StaticAnalysisSource(architecture=architecture).observe()
        for observation in observations:
            path = repo / observation.evidence[0].ref
            assert path.exists(), observation.evidence[0].ref

    def test_a_clean_repository_produces_nothing(self, tmp_path: Path) -> None:
        """**没有违规也没有环，这一路就是空的** —— 不是失败的。"""
        root = tmp_path / "clean"
        root.mkdir()
        (root / "a.py").write_text("X = 1\n", encoding="utf-8")
        architecture = build_architecture(build_index(root))
        assert StaticAnalysisSource(architecture=architecture).observe() == ()

    def test_a_cycle_is_reported_only_once(self, tmp_path: Path) -> None:
        """**一个环只出一条观察** —— 报两遍就是两批候选，候选 id 还会撞车。

        违规清单里也有循环依赖那一类，但**环那一遍说得更全**（把环上所有
        的模块都列出来），所以留它、跳过那一类违规。
        """
        repo = build_coupled_repo(tmp_path / "coupled")
        architecture = build_architecture(build_index(repo))
        observations = StaticAnalysisSource(architecture=architecture).observe()
        subjects = [item.subject for item in observations]
        assert len(subjects) == len(set(subjects)), subjects


class TestTheRuntimeSource:
    """运行时那一路 —— 真的跑一条命令。"""

    def test_a_command_that_works_produces_nothing(self, tmp_path: Path) -> None:
        """**"它跑通了"不是一件待办。**"""
        probe = RuntimeProbe(
            id="ok",
            argv=(sys.executable, "-c", "print('fine')"),
            cwd=tmp_path,
            evidence_path="a.py",
        )
        assert RuntimeMetricSource(probes=(probe,)).observe() == ()

    def test_a_command_that_crashes_produces_an_observation(self, tmp_path: Path) -> None:
        """**崩了就是崩了** —— 退出码非零就是事故。"""
        probe = RuntimeProbe(
            id="boom",
            argv=(sys.executable, "-c", "raise SystemExit(3)"),
            cwd=tmp_path,
            evidence_path="a.py",
        )
        observations = RuntimeMetricSource(probes=(probe,)).observe()
        assert len(observations) == 1
        assert observations[0].subject == "boom"

    def test_the_fact_says_the_exit_code(self, tmp_path: Path) -> None:
        probe = RuntimeProbe(
            id="boom",
            argv=(sys.executable, "-c", "raise SystemExit(3)"),
            cwd=tmp_path,
            evidence_path="a.py",
        )
        observations = RuntimeMetricSource(probes=(probe,)).observe()
        assert "3" in observations[0].fact

    def test_a_command_that_hangs_produces_an_observation(self, tmp_path: Path) -> None:
        """**超时也是事故** —— 挂住了跟崩了一样是"没跑成"。"""
        probe = RuntimeProbe(
            id="hang",
            argv=(sys.executable, "-c", "import time; time.sleep(30)"),
            cwd=tmp_path,
            timeout=0.5,
            evidence_path="a.py",
        )
        observations = RuntimeMetricSource(probes=(probe,)).observe()
        assert len(observations) == 1
        assert "超时" in observations[0].fact

    def test_a_command_that_cannot_start_produces_an_observation(self, tmp_path: Path) -> None:
        """**跑不起来也是事故** —— 不是"没问题"，是"没验成"。"""
        probe = RuntimeProbe(
            id="ghost",
            argv=("/no/such/binary",),
            cwd=tmp_path,
            evidence_path="a.py",
        )
        observations = RuntimeMetricSource(probes=(probe,)).observe()
        assert len(observations) == 1

    def test_a_probe_without_a_landing_produces_nothing(self, tmp_path: Path) -> None:
        """**没有落点的观察不产出** —— 一条"命令崩了"没地方指，就是一句话。"""
        probe = RuntimeProbe(
            id="boom",
            argv=(sys.executable, "-c", "raise SystemExit(3)"),
            cwd=tmp_path,
        )
        assert RuntimeMetricSource(probes=(probe,)).observe() == ()


class TestTheFeedbackSource:
    """产品反馈那一路 —— 人写的台账。"""

    def test_it_reads_the_entries(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        (root / "docs").mkdir(parents=True)
        (root / "docs/issues.md").write_text(
            "# 问题清单\n\n### BUG-001 —— 第一次崩了\n\n### BUG-002 —— 第二次崩了\n",
            encoding="utf-8",
        )
        observations = FeedbackSource(root=root, ledger=Path("docs/issues.md")).observe()
        assert [item.subject for item in observations] == ["BUG-001", "BUG-002"]

    def test_the_evidence_is_the_issue_and_the_ledger(self, tmp_path: Path) -> None:
        """**两条证据：台账文件与问题单号** —— 少一条就没法当场核上。"""
        root = tmp_path / "repo"
        (root / "docs").mkdir(parents=True)
        (root / "docs/issues.md").write_text("### BUG-001 —— 第一次崩了\n", encoding="utf-8")
        observations = FeedbackSource(root=root, ledger=Path("docs/issues.md")).observe()
        assert [item.ref for item in observations[0].evidence] == ["BUG-001", "docs/issues.md"]

    def test_a_line_it_does_not_recognise_is_skipped(self, tmp_path: Path) -> None:
        """**宁可漏报，不要报错** —— 台账是人写的。"""
        root = tmp_path / "repo"
        (root / "docs").mkdir(parents=True)
        (root / "docs/issues.md").write_text(
            "## 小标题\n### 缺编号 —— 这条认不出来\n### BUG-001 —— 认得出来\n",
            encoding="utf-8",
        )
        observations = FeedbackSource(root=root, ledger=Path("docs/issues.md")).observe()
        assert [item.subject for item in observations] == ["BUG-001"]

    def test_a_missing_ledger_produces_nothing(self, tmp_path: Path) -> None:
        """**缺台账不是事故** —— 安静地什么都不产出。"""
        root = tmp_path / "repo"
        root.mkdir()
        source = FeedbackSource(root=root, ledger=Path("docs/issues.md"))
        assert source.observe() == ()


class TestTheDocumentationSource:
    """技术文档那一路 —— 决策记录。"""

    def test_it_reads_the_records(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        (root / "docs/adr").mkdir(parents=True)
        (root / "docs/adr/0001-first.md").write_text("# 第一个决策\n\n为什么。\n", encoding="utf-8")
        (root / "docs/adr/0002-second.md").write_text("# 第二个决策\n", encoding="utf-8")
        observations = DocumentationSource(root=root, directory=Path("docs/adr")).observe()
        assert [item.subject for item in observations] == ["0001-first", "0002-second"]

    def test_the_evidence_is_the_record_itself(self, tmp_path: Path) -> None:
        """**一篇 ADR 的存在就是它自己的证据。**"""
        root = tmp_path / "repo"
        (root / "docs/adr").mkdir(parents=True)
        (root / "docs/adr/0001-first.md").write_text("# 第一个决策\n", encoding="utf-8")
        observations = DocumentationSource(root=root, directory=Path("docs/adr")).observe()
        assert observations[0].evidence[0].ref == "docs/adr/0001-first.md"

    def test_the_fact_is_the_title(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        (root / "docs/adr").mkdir(parents=True)
        (root / "docs/adr/0001-first.md").write_text("# 第一个决策\n", encoding="utf-8")
        observations = DocumentationSource(root=root, directory=Path("docs/adr")).observe()
        assert observations[0].fact == "第一个决策"

    def test_a_missing_directory_produces_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        source = DocumentationSource(root=root, directory=Path("docs/adr"))
        assert source.observe() == ()
