"""TASK-P3-012 —— 扫描器：从索引里推出跨模块的长期积累问题。

**测的是"能不能看出来"，不是"看到了什么格式"。** 断言一律对着
:mod:`tests.support.coupled_repo` 里那个**真能跑、但整体有毛病**的
仓库：耦合枢纽、循环依赖、依赖方向反了、没人测的模块、没人用的接口。

**每条断言都钉在一个具体的数上。** "扇入 ≥ 阈值"这种写法在阈值一改就
失效；这里写死"``pkg.hub`` 的扇入正好是 5"，阈值改了才知道是**规则**
变了还是**结论**变了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.evolution.assessment import GapSeverity
from core.evolution.evidence import EvidenceKind
from core.evolution.scan import ScanKind, ScanPolicy, ScanScope, ScanSignal
from knowledge.architecture.model import ArchitectureModel, build_architecture
from knowledge.repository_index.index import RepositoryIndex, build_index
from orchestration.scan import STATIC_COVERAGE_CONFIDENCE, STRUCTURAL_CONFIDENCE, ProjectScanner
from tests.support.coupled_repo import (
    HUB_FAN_IN,
    HUB_MODULE,
    IMPORTER_MODULE,
    ORPHAN_MODULE,
    ORPHAN_SYMBOL,
    RING_MODULES,
    build_coupled_repo,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """耦合仓库的根目录。"""
    return build_coupled_repo(tmp_path / "coupled")


@pytest.fixture
def index(repo: Path) -> RepositoryIndex:
    """仓库的索引。"""
    return build_index(repo)


@pytest.fixture
def architecture(index: RepositoryIndex) -> ArchitectureModel:
    """仓库的架构模型。"""
    return build_architecture(index)


@pytest.fixture
def scanner(architecture: ArchitectureModel) -> ProjectScanner:
    """默认策略的扫描器。"""
    return ProjectScanner(architecture)


@pytest.fixture
def scope() -> ScanScope:
    """全仓库范围。"""
    return ScanScope(project_id="P1")


def _signal_of(
    scanner: ProjectScanner, scope: ScanScope, kind: ScanKind, module: str
) -> ScanSignal:
    """报告里指定种类与模块的那条信号。"""
    for signal in scanner.scan(scope, scan_id="scan-1").of_kind(kind):
        if signal.module == module:
            return signal
    raise AssertionError(f"没有扫出 {kind.value} / {module}")


class TestTheRepositoryItself:
    """先确认那个仓库确实长成了应有的样子 —— 否则后面的断言都是空的。"""

    def test_the_hub_is_depended_on_by_five_modules(self, architecture: ArchitectureModel) -> None:
        assert len(architecture.dependents(HUB_MODULE)) == HUB_FAN_IN

    def test_the_two_ring_modules_depend_on_each_other(
        self, architecture: ArchitectureModel
    ) -> None:
        first, second = RING_MODULES
        assert second in architecture.dependencies(first)
        assert first in architecture.dependencies(second)

    def test_the_orphan_symbol_is_public(self, architecture: ArchitectureModel) -> None:
        assert ORPHAN_SYMBOL in architecture.public_surface(ORPHAN_MODULE)


class TestTheCycle:
    """循环依赖 —— 改哪一个都像被另一个卡住。"""

    def test_the_cycle_is_reported(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        signal = _signal_of(scanner, scope, ScanKind.DEPENDENCY_CYCLE, RING_MODULES[0])
        assert set(signal.modules) == set(RING_MODULES)

    def test_the_cycle_is_high_severity(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        signal = _signal_of(scanner, scope, ScanKind.DEPENDENCY_CYCLE, RING_MODULES[0])
        assert signal.severity is GapSeverity.HIGH

    def test_the_cycle_reports_its_size(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        signal = _signal_of(scanner, scope, ScanKind.DEPENDENCY_CYCLE, RING_MODULES[0])
        assert signal.fact("cycle_size") == "2"


class TestTheCouplingHub:
    """耦合枢纽 —— 改一处要动很多地方。"""

    def test_the_hub_is_reported(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        signal = _signal_of(scanner, scope, ScanKind.COUPLING_HUB, HUB_MODULE)
        assert signal.module == HUB_MODULE

    def test_the_fan_in_is_the_real_number(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        """扇入是数出来的 —— 五个用户模块。"""
        signal = _signal_of(scanner, scope, ScanKind.COUPLING_HUB, HUB_MODULE)
        assert signal.fact("fan_in") == str(HUB_FAN_IN)

    def test_the_impact_includes_the_transitive_users(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**影响面比扇入大**，因为用例也间接依赖它。

        这两个数不是同一个数 —— 扇入是"直接依赖我的"，影响面是"改了我会
        波及的"。用例 ``tests.test_users`` 依赖 ``pkg.user1``，而
        ``pkg.user1`` 依赖 ``pkg.hub`` —— 所以影响面里也有它。
        """
        signal = _signal_of(scanner, scope, ScanKind.COUPLING_HUB, HUB_MODULE)
        impact = int(signal.fact("impact") or "0")
        assert impact > HUB_FAN_IN

    def test_the_fan_out_is_zero(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        """枢纽自己谁也不依赖 —— 它是叶子，不是环。"""
        signal = _signal_of(scanner, scope, ScanKind.COUPLING_HUB, HUB_MODULE)
        assert signal.fact("fan_out") == "0"

    def test_the_dependents_are_listed(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        signal = _signal_of(scanner, scope, ScanKind.COUPLING_HUB, HUB_MODULE)
        listed = signal.fact("dependents") or ""
        assert "pkg.user1" in listed


class TestTheLayerViolation:
    """依赖方向反了 —— 产品代码依赖了测试代码。"""

    def test_the_importer_is_reported(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        signal = _signal_of(scanner, scope, ScanKind.LAYER_VIOLATION, IMPORTER_MODULE)
        assert signal.module == IMPORTER_MODULE

    def test_the_imported_module_is_named(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        signal = _signal_of(scanner, scope, ScanKind.LAYER_VIOLATION, IMPORTER_MODULE)
        assert signal.fact("imported") == "tests.helpers"

    def test_a_cycle_is_not_reported_twice(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        """环在 ``violations()`` 里也有一条 —— 但同一个问题只该出现一次。"""
        violations = scanner.scan(scope, scan_id="scan-1").of_kind(ScanKind.LAYER_VIOLATION)
        importers = [item.module for item in violations]
        assert len(importers) == len(set(importers))


class TestTheCoverageGaps:
    """没人测的模块、没人用的接口。"""

    def test_the_hub_has_no_test_of_its_own(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**枢纽被用得很广，自己却一条用例都没有。**"""
        signal = _signal_of(scanner, scope, ScanKind.UNTESTED_MODULE, HUB_MODULE)
        assert signal.fact("public_symbols") == "1"

    def test_a_tested_module_is_not_reported_as_untested(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """``pkg.user1`` 有用例引用 —— 被测过的东西不该报成"没人测"。"""
        report = scanner.scan(scope, scan_id="scan-1")
        untested = [item.module for item in report.of_kind(ScanKind.UNTESTED_MODULE)]
        assert "pkg.user1" not in untested

    def test_the_symbol_level_check_cannot_see_attribute_access(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """**一个已知的近似，钉在这儿。**

        ``tests/test_users.py`` 写的是 ``from pkg import user1`` 然后
        ``user1.use()`` —— **模块被引用了，符号没有**（索引只记 import 语句
        里的名字，属性访问看不见）。所以符号级的检查会报
        ``pkg.user1.use`` "没有任何用例引用过"。

        **这不是 bug，是静态分析的边界。** 所以这一类的准确读法是"没有用例
        import 过它"，不是"没人用它"。这条断言把这个边界写下来 —— 哪天
        有人把它改成了真的调用分析，这条断言会先红，提醒他改文档。
        """
        report = scanner.scan(scope, scan_id="scan-1")
        orphaned = {item.module for item in report.of_kind(ScanKind.ORPHANED_PUBLIC_API)}
        assert "pkg.user1" in orphaned

    def test_the_orphan_symbol_is_reported(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        signal = _signal_of(scanner, scope, ScanKind.ORPHANED_PUBLIC_API, ORPHAN_MODULE)
        symbols = signal.fact("symbols") or ""
        assert ORPHAN_SYMBOL in symbols


class TestThePolicy:
    """策略决定扫什么。"""

    def test_a_kind_can_be_switched_off(self, architecture: ArchitectureModel) -> None:
        scanner = ProjectScanner(
            architecture, policy=ScanPolicy(kinds=(ScanKind.DEPENDENCY_CYCLE,))
        )
        report = scanner.scan(ScanScope(project_id="P1"), scan_id="scan-1")
        assert report.of_kind(ScanKind.DEPENDENCY_CYCLE)
        assert not report.of_kind(ScanKind.COUPLING_HUB)
        assert not report.of_kind(ScanKind.UNTESTED_MODULE)

    def test_a_higher_threshold_hides_the_hub(self, architecture: ArchitectureModel) -> None:
        """阈值是仓库默认值，不是从数据里测出来的 —— 所以它可调。"""
        scanner = ProjectScanner(architecture, policy=ScanPolicy(coupling_hub_threshold=6))
        report = scanner.scan(ScanScope(project_id="P1"), scan_id="scan-1")
        assert not report.of_kind(ScanKind.COUPLING_HUB)


class TestTheScope:
    """范围过滤。"""

    def test_a_narrow_scope_only_reports_what_it_covers(self, scanner: ProjectScanner) -> None:
        narrow = ScanScope(project_id="P1", module_prefixes=("pkg.ring_",))
        report = scanner.scan(narrow, scan_id="scan-1")
        modules = {item.module for item in report.signals}
        assert modules
        assert all(item.startswith("pkg.ring_") for item in modules)

    def test_the_scope_decides_how_much_was_examined(
        self, scanner: ProjectScanner, architecture: ArchitectureModel
    ) -> None:
        """**"扫了多少"必须跟着范围走** —— 否则"没有发现"分不清是真干净
        还是压根没看。"""
        narrow = ScanScope(project_id="P1", module_prefixes=("pkg.ring_",))
        report = scanner.scan(narrow, scan_id="scan-1")
        assert report.examined_modules < len(architecture.index.modules)

    def test_a_scope_matching_nothing_examines_nothing(self, scanner: ProjectScanner) -> None:
        """范围写错了 —— 那就是"没扫"，不是"扫了没发现"。"""
        report = scanner.scan(ScanScope(project_id="P1", module_prefixes=("nope.",)), scan_id="s")
        assert report.examined_modules == 0
        assert not report.signals


class TestTheCandidates:
    """信号 → 候选。"""

    def test_every_signal_becomes_a_candidate(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        report = scanner.scan(scope, scan_id="scan-1")
        candidates = scanner.candidates(report)
        assert len(candidates) == len(report.signals)

    def test_the_candidate_ids_are_unique(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        report = scanner.scan(scope, scan_id="scan-1")
        candidates = scanner.candidates(report)
        ids = [item.id for item in candidates]
        assert len(ids) == len(set(ids))

    def test_the_evidence_points_at_a_real_file(
        self, scanner: ProjectScanner, scope: ScanScope, repo: Path
    ) -> None:
        """**证据必须是能打开的文件。** 拿着它就能去看、去改。"""
        report = scanner.scan(scope, scan_id="scan-1")
        for candidate in scanner.candidates(report):
            assert candidate.evidence
            for item in candidate.evidence:
                assert item.kind is EvidenceKind.FILE
                assert (repo / item.ref).exists(), item.ref

    def test_the_structural_confidence_is_higher_than_the_coverage_one(
        self, scanner: ProjectScanner, scope: ScanScope
    ) -> None:
        """环、扇入、依赖方向是算出来的；"没人引用过"是查出来的 —— 后者弱一档。"""
        report = scanner.scan(scope, scan_id="scan-1")
        candidates = {item.id: item for item in scanner.candidates(report)}
        hub = candidates[f"scan:{ScanKind.COUPLING_HUB.value}:{HUB_MODULE}"]
        orphan = candidates[f"scan:{ScanKind.ORPHANED_PUBLIC_API.value}:{ORPHAN_MODULE}"]
        assert hub.confidence == STRUCTURAL_CONFIDENCE
        assert orphan.confidence == STATIC_COVERAGE_CONFIDENCE
        assert hub.confidence > orphan.confidence

    def test_the_scan_is_repeatable(self, scanner: ProjectScanner, scope: ScanScope) -> None:
        """**扫两次要得到同样的报告** —— "今天比昨天多了一个环"这句话
        只有在两次扫描可比时才成立。"""
        first = scanner.scan(scope, scan_id="scan-1")
        second = scanner.scan(scope, scan_id="scan-2")
        assert first.fingerprints == second.fingerprints
