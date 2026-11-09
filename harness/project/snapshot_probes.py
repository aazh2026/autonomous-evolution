"""快照的采集口 —— **真的去问 git、真的去读报告**。

设计依据：docs/tasks.md TASK-P1-023（采集时点的 commit SHA、代码统计、
测试结果、覆盖率）、R-068、R-069。

**为什么在 harness 而不是 core。** ``core/project/snapshot.py`` 说"我要一个
commit、一个代码统计、一份测试结果、一份覆盖率"，怎么拿不是领域的事。这里
是那个"怎么拿"：调 git、读 pytest 的 XML、读 coverage 的 JSON。与
``harness/sandbox/cancellation.py`` 实现 ``core/task/cancellation.py`` 的端口
同一套做法。

**读不到就是读不到。** 没有测试报告、没有覆盖率文件 —— 对应的那一项就是
"没采到"，**不是 0**。一个从没量过覆盖率的仓库，写 0 进去，读的人会以为
它一行都没被测到，而真相是"没人量"。这条与 ``core/project/snapshot.py``
和 ``evaluation/baselines.py`` 是同一条规矩。

**产物要记下 URI 与 hash。** 快照里记的那些数，在快照之外还有一份原始报告
—— 记下 URI 与 sha256，事后能回去核对那个数是不是真的。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from core.evidence.artifact_hash import compute_file_hash
from core.project.snapshot import (
    CodeStatistics,
    CoverageReport,
    SnapshotArtifact,
    SuiteOutcome,
)
from harness.workspace.git import GitError

#: 算作"代码"的文件后缀 —— **README 不算代码**。
#:
#: 代码统计是拿来比较规模变化的，所以口径必须窄而稳：一份文档不算代码，
#: 一个二进制更不算。
SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".py",
        ".pyi",
        ".rb",
        ".rs",
        ".sh",
        ".ts",
        ".tsx",
    }
)

#: 数代码时跳过的目录 —— **它们不是这个项目的代码**。
#:
#: 依赖目录与构建产物的行数会随一次安装变一次 —— 把它们算进来，"代码规模"
#: 每次采集都不一样，同一个 commit 的两次快照就对不上了。
IGNORED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
    }
)

#: 测试报告的产物类别。
TEST_REPORT_ARTIFACT = "test_report"

#: 覆盖率报告的产物类别。
COVERAGE_REPORT_ARTIFACT = "coverage_report"


class GitCommitProbe:
    """真的去问 git —— **当前 HEAD 在哪一版**。

    **游离 HEAD 时分支是 ``None``，不是空串。** 游离 HEAD 是一种真实状态
    （检出了某个 commit 但不在任何分支上），空串看着像"分支名是空的"。
    """

    __slots__ = ("_git", "_timeout")

    def __init__(self, *, git_binary: str = "git", timeout_seconds: int = 60) -> None:
        self._git = git_binary
        self._timeout = timeout_seconds

    def _run(self, args: tuple[str, ...], workspace: Path) -> tuple[int, str]:
        command = (self._git, *args)
        try:
            proc = subprocess.run(
                command,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(command, -1, f"git 命令超时（{self._timeout}s）") from exc
        return proc.returncode, proc.stdout

    async def head_commit(self, workspace: Path) -> str:
        """当前 HEAD 的 commit SHA。"""
        code, output = self._run(("rev-parse", "HEAD"), workspace)
        if code != 0:
            raise GitError((self._git, "rev-parse", "HEAD"), code, "取不到 HEAD 的 commit")
        return output.strip()

    async def current_branch(self, workspace: Path) -> str | None:
        """当前分支名 —— **游离 HEAD 时是 ``None``**。

        ``git symbolic-ref -q`` 在游离 HEAD 时退出码是 1（"这里不是一个符号
        引用"），在"这不是一个 git 仓库"时是 128。**这两个不是一回事** ——
        前者是合法状态，后者是出错了。
        """
        code, output = self._run(("symbolic-ref", "--short", "-q", "HEAD"), workspace)
        if code == 1:
            return None
        if code != 0:
            raise GitError((self._git, "symbolic-ref", "--short", "-q", "HEAD"), code, "取不到分支")
        return output.strip() or None


class SourceTreeStatisticsProbe:
    """真的去数代码 —— **多少文件、多少行、多少模块**。

    **模块按顶层目录算。** 一个模块就是一个含有源文件的顶层目录；根目录下的
    散装文件不算模块。**口径窄而稳**比"什么都算进去"重要：口径一变，同一个
    commit 的两次快照就对不上了。
    """

    __slots__ = ("_suffixes",)

    def __init__(self, *, suffixes: frozenset[str] = SOURCE_SUFFIXES) -> None:
        self._suffixes = suffixes

    async def statistics(self, workspace: Path) -> CodeStatistics:
        """数这个工作区。"""
        files = 0
        lines = 0
        modules: set[str] = set()
        for path in self._source_files(workspace):
            files += 1
            lines += self._count_lines(path)
            relative = path.relative_to(workspace)
            if len(relative.parts) > 1:
                modules.add(relative.parts[0])
        return CodeStatistics(files=files, lines=lines, modules=len(modules))

    def _source_files(self, workspace: Path) -> list[Path]:
        """工作区里的源文件 —— **按路径排好序**。

        排序不是为了好看，是为了让"数出来的行数"与"数出来的文件列表"是同一
        个顺序 —— 目录遍历的顺序在不同的文件系统上是不一样的。
        """
        found: list[Path] = []
        for path in sorted(workspace.rglob("*")):
            if not path.is_file():
                continue
            if any(part in IGNORED_DIRECTORIES for part in path.relative_to(workspace).parts):
                continue
            if path.suffix in self._suffixes:
                found.append(path)
        return found

    def _count_lines(self, path: Path) -> int:
        """数一个文件有多少行 —— **读不出来的当 0 行，不当出错**。

        二进制文件或者编码不对的文件不该让整次采集失败 —— 少算一个文件
        比整个快照建不出来好。
        """
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return 0
        return len(content.splitlines())


class PytestReportProbe:
    """读 pytest 的 JUnit 报告 —— **跑过才有**。

    **读不到报告就是"没跑过测试"** —— 不是"跑了但零个用例"。这两种在报表上
    长得一模一样：一个空仓库跑一次 pytest 确实会得到 0 个用例，而没跑过
    的仓库根本没有报告。**把没跑过记成 0，等于说"我们验证过这个仓库，
    一个用例都没有"** —— 那是一句假话。
    """

    __slots__ = ("_report_name",)

    def __init__(self, report_name: str = "reports/junit.xml") -> None:
        self._report_name = report_name

    async def outcome(self, workspace: Path) -> SuiteOutcome | None:
        """这次的测试结果；没有报告返回 ``None``。"""
        path = workspace / self._report_name
        if not path.is_file():
            return None
        try:
            root = ElementTree.parse(path).getroot()
        except ElementTree.ParseError:
            return None
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        if not suites:
            return None
        passed = failed = skipped = errors = 0
        duration = 0.0
        for suite in suites:
            passed += _count_int(suite, "passed")
            failed += _count_int(suite, "failures")
            skipped += _count_int(suite, "skipped")
            errors += _count_int(suite, "errors")
            duration += _count_attribute(suite, "time")
        total = _count_int(suites[0], "tests")
        if total:
            # 有些报告不写 passed，用总数减掉其它几种算出来。
            passed = max(passed, total - failed - skipped - errors)
        return SuiteOutcome(
            passed=passed,
            failed=failed,
            skipped=skipped,
            errors=errors,
            duration_ms=int(duration * 1000),
            artifact=_artifact(TEST_REPORT_ARTIFACT, path),
        )


class CoverageJsonProbe:
    """读 coverage.py 的 JSON 报告 —— **量过才有**。

    **按模块分开。** coverage.py 的 JSON 是按文件记的；这里按文件所在目录
    归成模块 —— 整体覆盖率会把"新增一个完全没测的模块"藏起来，而那个模块
    恰恰是最该被看见的。
    """

    __slots__ = ("_report_name",)

    def __init__(self, report_name: str = "reports/coverage.json") -> None:
        self._report_name = report_name

    async def coverage(self, workspace: Path) -> CoverageReport | None:
        """这次的覆盖率；没有报告返回 ``None``。"""
        path = workspace / self._report_name
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, Mapping):
            return None
        files = raw.get("files")
        if not isinstance(files, Mapping):
            return None
        by_module: dict[str, float] = {}
        covered_by_module: dict[str, float] = {}
        total_by_module: dict[str, float] = {}
        for name, entry in files.items():
            if not isinstance(name, str) or not isinstance(entry, Mapping):
                continue
            summary = entry.get("summary")
            if not isinstance(summary, Mapping):
                continue
            covered = _as_number(summary.get("covered_lines"))
            total = _as_number(summary.get("num_statements"))
            if total <= 0:
                continue
            module = _module_of(name)
            covered_by_module[module] = covered_by_module.get(module, 0.0) + covered
            total_by_module[module] = total_by_module.get(module, 0.0) + total
        for module, total in total_by_module.items():
            by_module[module] = covered_by_module[module] / total
        totals = raw.get("totals")
        overall: float | None = None
        if isinstance(totals, Mapping):
            percent = _as_number(totals.get("percent_covered"))
            if percent >= 0:
                overall = percent / 100
        return CoverageReport(
            overall=overall,
            by_module={name: by_module[name] for name in sorted(by_module)},
            artifact=_artifact(COVERAGE_REPORT_ARTIFACT, path),
        )


class SystemSnapshotClock:
    """系统时钟 —— **UTC**。

    **统一 UTC。** 快照存的是 ISO 8601 字符串，不带时区的话，同一份快照在
    两台机器上会被读成两个时刻。
    """

    __slots__ = ()

    def now(self) -> datetime:
        """现在的时刻（UTC）。"""
        return datetime.now(UTC)


def _artifact(kind: str, path: Path) -> SnapshotArtifact:
    """给一份产物记上 URI 与 hash。"""
    return SnapshotArtifact(
        kind=kind,
        uri=path.as_uri(),
        sha256=compute_file_hash(path) if path.is_file() else None,
    )


def _count_attribute(element: ElementTree.Element, name: str) -> float:
    """读报告里的一个数字属性 —— **读不到当 0**。"""
    value = element.get(name)
    if value is None:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _count_int(element: ElementTree.Element, name: str) -> int:
    """读报告里的一个计数属性 —— **读不到当 0**。

    **计数必须是整数。** 报告里写的是 ``"1"``，解析出来是 ``1.0``，落进
    JSON 又是 ``1.0`` —— 快照里一个"通过数"带着小数点，读的人会以为
    报告算错了。
    """
    value = element.get(name)
    if value is None:
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def _as_number(raw: object) -> float:
    """读一个数 —— **读不到当 -1**（-1 表示"没有这个数"）。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return -1.0
    return float(raw)


def _module_of(file_name: str) -> str:
    """文件属于哪个模块 —— **按它所在的目录**。"""
    parent = str(Path(file_name).parent)
    return "." if parent in {"", "."} else parent.replace("\\", "/")


__all__ = [
    "COVERAGE_REPORT_ARTIFACT",
    "IGNORED_DIRECTORIES",
    "SOURCE_SUFFIXES",
    "TEST_REPORT_ARTIFACT",
    "CoverageJsonProbe",
    "GitCommitProbe",
    "PytestReportProbe",
    "SourceTreeStatisticsProbe",
    "SystemSnapshotClock",
]
