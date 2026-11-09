"""TASK-P2-016 —— 仓库索引：一次构建、多次查询。

**这里验的是三件事，每一件都对着一条要求**：

1. **TASK-P2-016 的验收条件** —— "系统能回答'某模块被哪些模块依赖'且答案
   来自已构建的索引而非每次重读仓库"。所以最要紧的那条用例是：把仓库
   **删掉**，再从落盘的索引里问 —— 答案还在，才说明答案真的来自索引。
2. **R-064（不每轮重读仓库重建认知）** —— 刷新时**没变的文件一个字节都
   不读**。这一条用"数读了多少次文件"来验，而不是靠感觉。
3. **索引本身不能骗人** —— 解析失败的文件要留在索引里（带着错误信息），
   外部依赖不能混进内部依赖边，格式版本对不上必须报错而不是硬读。
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path

import pytest

from knowledge.repository_index.index import (
    IndexBuildError,
    IndexFormatError,
    ModuleLayer,
    build_index,
    refresh_index,
)
from knowledge.repository_index.store import RepositoryIndexStore


def _write(root: Path, relative: str, text: str) -> Path:
    """写一个文件（父目录一并建好）。"""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个小仓库：两个源模块、一个测试模块、一个缺陷复现模块。"""
    root = tmp_path / "repo"
    _write(
        root,
        "src/tinylib/__init__.py",
        '"""tinylib。"""\n',
    )
    _write(
        root,
        "src/tinylib/stats.py",
        '"""统计。"""\n\n\ndef mean(values):\n    """算术平均。"""\n'
        "    return sum(values) / len(values)\n",
    )
    _write(
        root,
        "src/tinylib/cli.py",
        '"""命令行。"""\n\nfrom tinylib.stats import mean\n\n\ndef main(argv=None):\n'
        '    """入口。"""\n    return mean([1])\n',
    )
    _write(
        root,
        "tests/test_stats.py",
        "from tinylib.stats import mean\n\n\ndef test_mean():\n    assert mean([1]) == 1\n",
    )
    _write(
        root,
        "known_issues/test_known_issues.py",
        "from tinylib import cli\n\n\ndef test_cli():\n    assert cli.main([]) == 1\n",
    )
    return root


class TestIndexContent:
    def test_module_name_strips_the_source_root(self, repo: Path) -> None:
        """``src/`` 之下才是真正的包 —— 模块名不带 ``src``。"""
        index = build_index(repo)
        assert "tinylib.cli" in index.module_names()
        assert "src.tinylib.cli" not in index.module_names()

    def test_symbols_and_docstrings_are_collected(self, repo: Path) -> None:
        index = build_index(repo)
        names = {symbol.name for symbol in index.symbols_of("tinylib.stats")}
        assert names == {"mean"}
        assert index.symbols_of("tinylib.stats")[0].doc == "算术平均。"

    def test_underscore_prefix_is_not_public(self, repo: Path) -> None:
        _write(
            repo,
            "src/tinylib/secret.py",
            "def _hidden():\n    pass\n\n\ndef shown():\n    pass\n",
        )
        index = build_index(repo)
        public = {symbol.name for symbol in index.symbols_of("tinylib.secret") if symbol.is_public}
        assert public == {"shown"}

    def test_layer_is_decided_by_directory(self, repo: Path) -> None:
        index = build_index(repo)
        cli = index.record("tinylib.cli")
        stats_test = index.record("tests.test_stats")
        known = index.record("known_issues.test_known_issues")
        assert cli is not None
        assert stats_test is not None
        assert known is not None
        assert cli.layer is ModuleLayer.SOURCE
        assert stats_test.layer is ModuleLayer.TEST
        assert known.layer is ModuleLayer.KNOWN_ISSUE

    def test_external_imports_are_not_internal_edges(self, repo: Path) -> None:
        """``import os`` 是外部依赖 —— 它不进依赖图。"""
        _write(repo, "src/tinylib/osmod.py", "import os\n\n\ndef f():\n    return os.sep\n")
        index = build_index(repo)
        assert index.dependencies("tinylib.osmod") == ()

    def test_unparsable_file_stays_in_the_index(self, repo: Path) -> None:
        """坏掉的文件是**事实** —— 索引里要看得见它坏了。"""
        _write(repo, "src/tinylib/broken.py", "def oops(:\n")
        index = build_index(repo)
        record = index.record("tinylib.broken")
        assert record is not None
        assert record.parse_error is not None
        assert record.symbols == ()

    def test_relative_import_resolves_inside_the_package(self, tmp_path: Path) -> None:
        root = tmp_path / "rel"
        _write(root, "src/pkg/__init__.py", "")
        _write(root, "src/pkg/leaf.py", "VALUE = 1\n")
        _write(root, "src/pkg/parent.py", "from . import leaf\nfrom .leaf import VALUE\n")
        index = build_index(root)
        assert "pkg.leaf" in index.dependencies("pkg.parent")


class TestDependencyQueries:
    def test_who_depends_on_a_module(self, repo: Path) -> None:
        index = build_index(repo)
        assert index.dependents("tinylib.stats") == ("tests.test_stats", "tinylib.cli")

    def test_transitive_dependents_are_computed(self, repo: Path) -> None:
        """改 stats 会影响到 cli、那条用例，以及**间接**用到 cli 的复现套件。"""
        index = build_index(repo)
        assert set(index.transitive_dependents("tinylib.stats")) == {
            "tinylib.cli",
            "tests.test_stats",
            "known_issues.test_known_issues",
        }

    def test_the_answer_comes_from_the_index_not_the_repo(self, repo: Path) -> None:
        """**TASK-P2-016 的验收问题。**

        仓库整个删掉，索引还在 —— 那么"谁依赖它"这句话就只可能来自索引。
        """
        index = build_index(repo)
        store = RepositoryIndexStore(repo.parent / "index.json")
        store.save(index)

        for path in sorted(repo.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()

        loaded = store.load()
        assert loaded is not None
        assert loaded.dependents("tinylib.stats") == ("tests.test_stats", "tinylib.cli")


class TestRefresh:
    def test_unchanged_files_are_not_read_at_all(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R-064 的落法：刷新不是重建。"""
        index = build_index(repo)
        reads: list[str] = []
        original = pathlib.Path.read_bytes

        def spy(self: pathlib.Path) -> bytes:
            reads.append(str(self))
            return original(self)

        monkeypatch.setattr(pathlib.Path, "read_bytes", spy)
        refreshed = refresh_index(repo, index)
        assert reads == []
        assert refreshed.digest == index.digest

    def test_changed_files_are_reparsed(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        index = build_index(repo)
        _write(
            repo,
            "src/tinylib/stats.py",
            "def mean(values):\n    return 0.0\n\n\ndef added():\n    return 1\n",
        )
        reads: list[str] = []
        original = pathlib.Path.read_bytes

        def spy(self: pathlib.Path) -> bytes:
            reads.append(str(self))
            return original(self)

        monkeypatch.setattr(pathlib.Path, "read_bytes", spy)
        refreshed = refresh_index(repo, index)
        assert len(reads) == 1
        assert reads[0].endswith("stats.py")
        assert {symbol.name for symbol in refreshed.symbols_of("tinylib.stats")} == {
            "mean",
            "added",
        }

    def test_new_modules_resolve_dependency_targets(self, repo: Path) -> None:
        """新增一个模块之后，"谁依赖它"要算对 —— 而不用重读所有文件。"""
        index = build_index(repo)
        _write(repo, "src/tinylib/extra.py", "VALUE = 1\n")
        _write(repo, "src/tinylib/cli.py", "from tinylib.extra import VALUE\n")
        refreshed = refresh_index(repo, index)
        assert "tinylib.extra" in refreshed.dependencies("tinylib.cli")


class TestPersistence:
    def test_digest_survives_a_round_trip(self, repo: Path) -> None:
        index = build_index(repo)
        store = RepositoryIndexStore(repo.parent / "index.json")
        store.save(index)
        loaded = store.load()
        assert loaded is not None
        assert loaded.digest == index.digest

    def test_missing_index_file_returns_none(self, tmp_path: Path) -> None:
        store = RepositoryIndexStore(tmp_path / "missing.json")
        assert store.load() is None
        assert not store.exists()

    def test_corrupt_index_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "index.json"
        path.write_text("{不是 JSON", encoding="utf-8")
        store = RepositoryIndexStore(path)
        with pytest.raises(IndexFormatError):
            store.load()

    def test_wrong_format_version_raises(self, repo: Path) -> None:
        index = build_index(repo)
        document = dict(index.to_document())
        document["format_version"] = 999
        store = RepositoryIndexStore(repo.parent / "index.json")
        store.save(index)
        path = store.path
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["index"] = document
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(IndexFormatError):
            store.load()

    def test_missing_repo_root_raises(self, tmp_path: Path) -> None:
        with pytest.raises(IndexBuildError):
            build_index(tmp_path / "没有这个目录")
