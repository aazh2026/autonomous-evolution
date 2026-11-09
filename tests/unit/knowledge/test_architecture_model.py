"""TASK-P2-016 —— 架构模型：分层、依赖环、没人碰的公开接口。

**这一层最要紧的性质是"不读仓库"。** 模型的所有结论都从索引推导 ——
所以这里先建索引再问架构，模型自己一个文件都不碰。

**三类结论各有各的用处，所以各有各的用例**：

1. **分层与依赖方向** —— 产品代码依赖测试代码，是架构问题；
2. **依赖环** —— 环会让"先改哪个"这个问题没有起点；
3. **没人碰的公开接口** —— 结构性证据：这个对外接口没有测试在用它。

**第 3 条是试点仓库 KR-002 的静态版本。** 那边用覆盖率量（``cli`` 模块
0%），这边用引用关系量（没有任何一条自有用例 import 过 ``tinylib.cli``）。
两种量法指向同一件事，而这一种不用跑测试。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge.architecture.model import (
    ArchitectureModel,
    ViolationKind,
    build_architecture,
)
from knowledge.repository_index.index import ModuleLayer, build_index


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个小仓库：两个源模块、两个测试模块。"""
    root = tmp_path / "repo"
    _write(root, "src/pkg/__init__.py", "")
    _write(root, "src/pkg/core.py", "def helper():\n    return 1\n")
    _write(
        root,
        "src/pkg/edge.py",
        "from pkg.core import helper\n\n\ndef use():\n    return helper()\n",
    )
    _write(root, "tests/test_core.py", "from pkg.core import helper\n")
    return root


@pytest.fixture
def model(repo: Path) -> ArchitectureModel:
    return build_architecture(build_index(repo))


class TestLayers:
    def test_modules_are_grouped_by_layer(self, model: ArchitectureModel) -> None:
        assert model.modules_in_layer(ModuleLayer.SOURCE) == ("pkg", "pkg.core", "pkg.edge")
        assert model.modules_in_layer(ModuleLayer.TEST) == ("tests.test_core",)

    def test_layer_of_a_module(self, model: ArchitectureModel) -> None:
        assert model.layer_of("pkg.core") is ModuleLayer.SOURCE
        assert model.layer_of("pkg.没有这个") is None


class TestDependencies:
    def test_dependents(self, model: ArchitectureModel) -> None:
        assert model.dependents("pkg.core") == (
            "pkg.edge",
            "tests.test_core",
        )

    def test_dependencies(self, model: ArchitectureModel) -> None:
        assert model.dependencies("pkg.edge") == ("pkg.core",)

    def test_impact_is_transitive(self, model: ArchitectureModel) -> None:
        assert set(model.impact_of("pkg.core")) == {"pkg.edge", "tests.test_core"}


class TestCycles:
    def test_a_cycle_is_found(self, tmp_path: Path) -> None:
        root = tmp_path / "cycle"
        _write(root, "src/a.py", "import b\n")
        _write(root, "src/b.py", "import a\n")
        model = build_architecture(build_index(root))
        cycles = model.cycles()
        assert len(cycles) == 1
        assert set(cycles[0].modules) == {"a", "b"}

    def test_no_cycle_is_reported(self, model: ArchitectureModel) -> None:
        assert model.cycles() == ()

    def test_a_cycle_becomes_a_violation(self, tmp_path: Path) -> None:
        root = tmp_path / "cycle"
        _write(root, "src/a.py", "import b\n")
        _write(root, "src/b.py", "import a\n")
        model = build_architecture(build_index(root))
        kinds = {item.kind for item in model.violations()}
        assert ViolationKind.CYCLIC_DEPENDENCY in kinds


class TestLayerViolations:
    def test_source_importing_test_is_a_violation(self, tmp_path: Path) -> None:
        root = tmp_path / "bad"
        _write(root, "src/app.py", "import tests.test_core\n")
        _write(root, "tests/test_core.py", "VALUE = 1\n")
        model = build_architecture(build_index(root))
        kinds = {item.kind for item in model.violations()}
        assert ViolationKind.SOURCE_IMPORTS_TEST in kinds

    def test_test_importing_source_is_not_a_violation(self, model: ArchitectureModel) -> None:
        """测试依赖产品是对的 —— 它本来就是用来依赖产品的。"""
        assert model.violations() == ()

    def test_source_importing_known_issue_is_a_violation(self, tmp_path: Path) -> None:
        root = tmp_path / "bad"
        _write(root, "src/app.py", "import known_issues.test_known\n")
        _write(root, "known_issues/test_known.py", "VALUE = 1\n")
        model = build_architecture(build_index(root))
        kinds = {item.kind for item in model.violations()}
        assert ViolationKind.SOURCE_IMPORTS_KNOWN_ISSUE in kinds


class TestPublicSurface:
    def test_unreferenced_public_symbols_are_found(self, tmp_path: Path) -> None:
        """**这是覆盖率缺口的静态证据。**"""
        root = tmp_path / "gap"
        _write(root, "src/api.py", "def used():\n    return 1\n\n\ndef orphan():\n    return 2\n")
        _write(root, "tests/test_api.py", "from api import used\n")
        model = build_architecture(build_index(root))
        # 用例 import 了 ``api`` 这个模块，所以它算被引用了 ——
        # 但 ``orphan`` 这个公开符号仍然没有任何用例碰过。
        assert model.unreferenced_public_symbols() == {"api": ("orphan",)}

    def test_a_referenced_symbol_is_not_a_gap(self, model: ArchitectureModel) -> None:
        """``pkg.core.helper`` 被用例引用了 —— 它不算缺口。

        但 ``pkg.edge.use`` 没有任何用例碰过（用例只 import 了
        ``pkg.core``），所以它是缺口。
        """
        assert model.unreferenced_public_symbols() == {"pkg.edge": ("use",)}

    def test_untested_modules_are_found(self, tmp_path: Path) -> None:
        root = tmp_path / "gap"
        _write(root, "src/api.py", "def entry():\n    return 1\n")
        _write(root, "tests/test_other.py", "VALUE = 1\n")
        model = build_architecture(build_index(root))
        assert model.untested_modules() == ("api",)

    def test_entry_points_are_recognised(self, tmp_path: Path) -> None:
        root = tmp_path / "entry"
        _write(root, "src/app.py", "def main(argv=None):\n    return 0\n")
        _write(root, "src/lib.py", "def helper():\n    return 1\n")
        model = build_architecture(build_index(root))
        assert model.entry_points() == ("app",)


class TestSummary:
    def test_summary_is_serialisable(self, model: ArchitectureModel) -> None:
        summary = model.summary()
        assert summary["modules"] == 4
        assert summary["entry_points"] == []
        assert "cycles" in summary
        assert "violations" in summary
