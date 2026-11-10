"""试点仓库的**实测** —— 关键结果的那些数字是测出来的，不是写死的。

设计依据：docs/tasks.md TASK-P3-018（连续 10 周期长跑；"每轮产出可验证证据；
达成度呈非负趋势"）、R-130（首个里程碑）、docs/design.md §3.1、§10、§12。

**为什么 M2 那套写死的实测值不能拿来用。** :mod:`apps.director.m2_observer`
按"``tests/test_cli.py`` 在不在"来判断修没修，然后报一组写死的数字 ——
那是**演示**：它要证明的是"观察者能把前后达成度算成差值"，不是"这些数字是
真的"。长跑要量的是**十轮下来达成度有没有真的往上走**，而写死的数字只会证明
"写死的那组数字没变过"。

**长跑的达成度必须来自仓库此刻的样子。** 所以这一模块做四件事：

1. **跑一遍真测试，量真覆盖率**（``pytest tests --cov=src``）—— KR-001 与 KR-002；
2. **数一遍真用例数**（同一遍测试的 JUnit 报告）—— KR-004；
3. **跑一遍复现套件，看还有哪些缺陷没修**（``pytest known_issues --runxfail``）
   —— KR-003 的分子；
4. **按台账逐条判断复核型发现补上没有** —— KR-003 的另一半。

**第 4 件事是最容易被糊弄的一处。** 复核型发现（``review``）复现的是"缺一样
东西"，没有会失败的用例可写 —— 所以"它修好了没有"**不能**靠测试报告回答，
只能靠**覆盖测量**回答：``cli.py`` 覆盖率为 0 → PILOT-010 没修；``Money``
的三条路径没走到 → PILOT-011 没修；没有任何用例调用过
``moving_average(序列, len(序列))`` → PILOT-012 没修。

**覆盖率的口径与基线一模一样。** 基线那列写的是 ``0.740385``（154/208 行），
所以这里算的是 ``covered_lines / num_statements`` 并保留 6 位小数 —— **不是
coverage 报告里那个保留 2 位的 ``percent_covered``**。口径不一致的话，基线与
实测就差了百分之一，而那点差别正好够把"有没有进步"这件事说反。

**量出来的东西写在仓库外面。** 覆盖率的原始数据文件（``.coverage``）与报告
都落在暂存目录里 —— 它们是这一轮的中间产物，不是被验收项目的代码；放进仓库
就会跟着工作树进 diff。
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

from apps.director.cycle import parse_junit
from core.project.models import TargetValue, ValueKind

#: 缺陷台账的路径 —— 相对仓库根。
LEDGER_PATH: Final[str] = "docs/issues.md"

#: 覆盖率的量化精度（6 位小数）—— 与 ``project.yaml`` 的基线列一致。
COVERAGE_PRECISION: Final[Decimal] = Decimal("0.000001")

#: 台账里有效发现的条数 —— KR-003 的分母。
VALID_DEFECT_COUNT: Final[int] = 12

#: 关键结果 id —— 达成度是按 id 对的。
KR_COVERAGE: Final[str] = "KR-001"
KR_CLI_COVERAGE: Final[str] = "KR-002"
KR_DEFECTS_FIXED: Final[str] = "KR-003"
KR_TEST_COUNT: Final[str] = "KR-004"

#: ``cli.py`` 在覆盖报告里的键。
CLI_FILE_KEY: Final[str] = "src/tinylib/cli.py"

#: ``money.py`` 在覆盖报告里的键。
MONEY_FILE_KEY: Final[str] = "src/tinylib/money.py"

#: PILOT-011 点名的那三条路径 —— 每条路径的**返回值那一行**都要被走到。
MONEY_GAP_METHODS: Final[tuple[str, ...]] = ("is_zero", "__hash__", "__eq__")

#: 判定结果 —— 台账里"有效发现"那三个字。
VALID_MARKER: Final[str] = "有效发现"

#: 判定结果 —— 台账里"无效发现"那三个字。
INVALID_MARKER: Final[str] = "无效发现"

#: 从证据行里抠出复现用例名的正则。
_REPRO_PATTERN: Final[re.Pattern[str]] = re.compile(r"::(\w+)")

#: ``moving_average`` 的参数个数 —— 探针只认"传两个参数"的那种调用。
_MOVING_AVERAGE_ARITY: Final[int] = 2


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """台账里的一条发现。

    :param issue: 编号（``PILOT-001``）。
    :param headline: 台账标题原文。
    :param valid: 人给的判定 —— 这条是不是有效发现。
    :param evidence_type: ``repro`` 或 ``review``。
    :param repro_test: 复现用例的函数名。**复核型发现没有**（空串）。
    """

    issue: str
    headline: str
    valid: bool
    evidence_type: str
    repro_test: str


@dataclass(frozen=True, slots=True)
class PilotMeasurement:
    """一次实测的结果。

    :param line_coverage: 整仓行覆盖率。
    :param cli_coverage: ``cli.py`` 的行覆盖率。
    :param covered_lines: 覆盖到的行数。
    :param total_lines: 总行数。
    :param test_count: 自有测试套件的用例数。
    :param defects_fixed: 已修掉的有效发现条数。
    :param failing_repro: **仍在失败**的复现用例名。
    :param money_paths_covered: PILOT-011 点名的三条路径是否都走到了。
    :param boundary_covered: 是否有用例调用过"窗口长度恰好等于序列长度"。
    """

    line_coverage: Decimal
    cli_coverage: Decimal
    covered_lines: int
    total_lines: int
    test_count: int
    defects_fixed: int
    failing_repro: tuple[str, ...]
    money_paths_covered: bool
    boundary_covered: bool

    def actuals(self) -> dict[str, TargetValue]:
        """按关键结果 id 给出实测值。

        **达成度是按 id 对的**（见
        :meth:`core.project.stop_conditions.GoalAttainmentCalculator.evaluate`），
        给错了键就等于"没测" —— 而"没测"会让"值不值得现在做"退回未核实。
        """
        return {
            KR_COVERAGE: TargetValue(kind=ValueKind.NUMBER, number=self.line_coverage),
            KR_CLI_COVERAGE: TargetValue(kind=ValueKind.NUMBER, number=self.cli_coverage),
            KR_DEFECTS_FIXED: TargetValue(
                kind=ValueKind.NUMBER, number=Decimal(self.defects_fixed)
            ),
            KR_TEST_COUNT: TargetValue(kind=ValueKind.NUMBER, number=Decimal(self.test_count)),
        }


def parse_ledger(repo: Path) -> tuple[LedgerEntry, ...]:
    """读台账 ``docs/issues.md``，逐条取出编号、判定与证据类型。

    **台账是人写的，格式不是机器保证的。** 所以这里按"标记词"抠，而不是按固定
    的行号抠 —— 台账前面那一大段说明文字改了行号也不影响解析。

    **判定只认"有效发现"与"无效发现"两个字样。** 认不出判定的那条**当作无效**
    —— 拿一条没判定过的发现去修，比不修更糟。
    """
    path = repo / LEDGER_PATH
    if not path.exists():
        return ()
    text = path.read_text(encoding="utf-8")
    entries: list[LedgerEntry] = []
    for block in text.split("\n### ")[1:]:
        headline = block.splitlines()[0].strip()
        issue = headline.split(" —— ")[0].strip()
        if not issue:
            continue
        entries.append(
            LedgerEntry(
                issue=issue,
                headline=headline,
                valid=_judgement(block) == VALID_MARKER,
                evidence_type=_evidence_type(block),
                repro_test=_repro_test(block),
            )
        )
    return tuple(entries)


def _judgement(block: str) -> str:
    """抠出判定（``有效发现`` / ``无效发现``）。"""
    for line in block.splitlines():
        if "判定" in line and "：" in line:
            value = line.split("：", 1)[1].strip().strip("`")
            if INVALID_MARKER in value:
                return INVALID_MARKER
            if VALID_MARKER in value:
                return VALID_MARKER
    return ""


def _evidence_type(block: str) -> str:
    """抠出证据类型（``repro`` / ``review``）。"""
    for line in block.splitlines():
        if "证据类型" in line and "：" in line:
            return line.split("：", 1)[1].strip().strip("`")
    return ""


def _repro_test(block: str) -> str:
    """抠出复现用例的函数名 —— 复核型发现没有，空串。"""
    for line in block.splitlines():
        if "**证据**" in line:
            found = _REPRO_PATTERN.search(line)
            if found is not None:
                return found.group(1)
    return ""


def measure_repo(repo: Path, python_executable: str, scratch: Path) -> PilotMeasurement:
    """把仓库此刻的样子量一遍。

    **跑两遍测试。** 一遍是自有套件（量覆盖率与用例数），一遍是复现套件
    （看还有哪些缺陷没修）。两遍缺一不可：只看其中一遍，就会漏掉另一半的证据。
    """
    scratch.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "COVERAGE_FILE": str(scratch / ".coverage")}
    coverage_json = scratch / "coverage.json"
    tests_junit = scratch / "tests-junit.xml"
    known_junit = scratch / "known-junit.xml"

    _run_pytest(
        python_executable,
        repo,
        env,
        (
            "tests",
            "--cov=src",
            f"--cov-report=json:{coverage_json}",
            f"--junit-xml={tests_junit}",
            "-q",
        ),
    )
    _run_pytest(
        python_executable,
        repo,
        env,
        ("known_issues", "--runxfail", f"--junit-xml={known_junit}", "-q"),
    )

    coverage = _read_coverage(coverage_json)
    covered, total = _totals(coverage)
    cli_covered, cli_total = _file_summary(coverage, CLI_FILE_KEY)
    failing = _failing_tests(known_junit)
    test_count = len(parse_junit(tests_junit))
    money_covered = _money_paths_covered(repo, coverage)
    boundary = _has_moving_average_boundary(repo)

    defects_fixed = _count_fixed(
        repo,
        failing,
        cli_covered=cli_covered,
        money_covered=money_covered,
        boundary_covered=boundary,
    )

    return PilotMeasurement(
        line_coverage=_ratio(covered, total),
        cli_coverage=_ratio(cli_covered, cli_total),
        covered_lines=covered,
        total_lines=total,
        test_count=test_count,
        defects_fixed=defects_fixed,
        failing_repro=failing,
        money_paths_covered=money_covered,
        boundary_covered=boundary,
    )


def _run_pytest(
    python_executable: str,
    repo: Path,
    env: dict[str, str],
    args: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    """跑一遍 pytest —— **不检查退出码**。

    **退出码在这里不是判据。** 复现套件**本来就该红着**（那些用例是故意失败
    的）；自有套件红了才是问题，而那件事由独立验证引擎管（见
    :func:`apps.director.cycle.verification_plan`）。测量器要的是"跑出来了，
    结果在报告里"，不是"跑成功了"。
    """
    return subprocess.run(
        [python_executable, "-m", "pytest", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _read_coverage(path: Path) -> dict[str, object]:
    """读覆盖报告 —— 读不到就当空报告（后面按"没测到"处理）。

    **不是字典就当没读到。** 覆盖报告是 coverage 自己写的 JSON，正常情况下
    一定是对象；但"读到一个不是字典的东西"这件事一旦发生，后面每一处取值都
    要防一手 —— 与其到处防，不如在这里就把它变成空报告。
    """
    if not path.exists():
        return {}
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {}
    return {str(key): value for key, value in loaded.items()}


def _totals(coverage: dict[str, object]) -> tuple[int, int]:
    """整仓覆盖到的行数与总行数。"""
    totals = coverage.get("totals")
    if not isinstance(totals, dict):
        return (0, 0)
    return (int(totals.get("covered_lines", 0)), int(totals.get("num_statements", 0)))


def _file_summary(coverage: dict[str, object], key: str) -> tuple[int, int]:
    """某个文件的覆盖行数与总行数。"""
    files = coverage.get("files")
    if not isinstance(files, dict):
        return (0, 0)
    entry = files.get(key)
    if not isinstance(entry, dict):
        return (0, 0)
    summary = entry.get("summary")
    if not isinstance(summary, dict):
        return (0, 0)
    return (int(summary.get("covered_lines", 0)), int(summary.get("num_statements", 0)))


def _ratio(covered: int, total: int) -> Decimal:
    """覆盖率 —— 保留 6 位小数。

    **分母为 0 时给 0，不给 None。** 一个没有可执行语句的文件不是"测不出来"，
    它是"没有东西要测" —— 在达成度里那两项就是 0 分。
    """
    if total <= 0:
        return Decimal(0).quantize(COVERAGE_PRECISION)
    return (Decimal(covered) / Decimal(total)).quantize(COVERAGE_PRECISION)


def _failing_tests(junit_path: Path) -> tuple[str, ...]:
    """复现套件里**仍在失败**的用例名。"""
    failed: list[str] = []
    for item in parse_junit(junit_path):
        if item.get("outcome") != "failed":
            continue
        nodeid = item.get("nodeid", "")
        failed.append(nodeid.rsplit("::", 1)[-1])
    return tuple(failed)


def _money_paths_covered(repo: Path, coverage: dict[str, object]) -> bool:
    """PILOT-011 点名的三条路径是不是都被走到了。

    **行号是现查的，不是写死的。** PILOT-005 的修复会往 ``money.py`` 里加属性
    方法，``__eq__`` / ``__hash__`` / ``is_zero`` 的行号因此会往下挪 ——
    写死行号的探针会在那之后就悄悄失灵，而它失灵的方式是"说没修"，那会让人
    以为已经修过。

    **每条路径的**每一行 return**都要被走到。** ``__eq__`` 有两行 return：一行
    是"跟非 ``Money`` 比较"、一行是"跟 ``Money`` 比较" —— PILOT-011 说的是
    前者，但两条都走到才算补齐。
    """
    source_path = repo / "src" / "tinylib" / "money.py"
    if not source_path.exists():
        return False
    wanted = _money_return_lines(source_path.read_text(encoding="utf-8"))
    if not wanted:
        return False
    executed = _executed_lines(coverage, MONEY_FILE_KEY)
    return all(line in executed for line in wanted)


def _money_return_lines(source: str) -> tuple[int, ...]:
    """``money.py`` 里那三条路径的 return 所在行号。"""
    tree = ast.parse(source)
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in MONEY_GAP_METHODS:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Return):
                lines.add(child.lineno)
    return tuple(sorted(lines))


def _executed_lines(coverage: dict[str, object], key: str) -> frozenset[int]:
    """某个文件里被走到的行号。"""
    files = coverage.get("files")
    if not isinstance(files, dict):
        return frozenset()
    entry = files.get(key)
    if not isinstance(entry, dict):
        return frozenset()
    executed = entry.get("executed_lines")
    if not isinstance(executed, list):
        return frozenset()
    return frozenset(int(line) for line in executed)


def _has_moving_average_boundary(repo: Path) -> bool:
    """有没有用例调用过"窗口长度恰好等于序列长度"。

    **看的是调用点，不是实现。** PILOT-012 说缺的是**那一档**的用例 ——
    判据就是"有没有这样的调用"，而不是"实现对不对"。
    """
    tests_dir = repo / "tests"
    if not tests_dir.exists():
        return False
    for path in sorted(tests_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "moving_average" or len(node.args) != _MOVING_AVERAGE_ARITY:
                continue
            window, values = node.args[1], node.args[0]
            if not isinstance(window, ast.Constant) or not isinstance(window.value, int):
                continue
            if not isinstance(values, ast.List):
                continue
            if len(values.elts) == window.value:
                return True
    return False


def _count_fixed(
    repo: Path,
    failing: tuple[str, ...],
    *,
    cli_covered: int,
    money_covered: bool,
    boundary_covered: bool,
) -> int:
    """数一数台账里有多少条有效发现已经修掉了。

    **复现型看"还在不在失败清单里"。** 修好的缺陷，它的复现用例已经从复现
    清单里搬走了 —— 于是它不再出现在失败清单里。

    **复核型看各自的探针。** 三条复核型发现各有一个可测的判据（见模块说明）。
    """
    entries = parse_ledger(repo)
    fixed = 0
    for entry in entries:
        if not entry.valid:
            continue
        if entry.evidence_type == "repro":
            if entry.repro_test not in failing:
                fixed += 1
            continue
        if entry.issue == "PILOT-010":
            if cli_covered > 0:
                fixed += 1
            continue
        if entry.issue == "PILOT-011":
            if money_covered:
                fixed += 1
            continue
        if entry.issue == "PILOT-012" and boundary_covered:
            fixed += 1
    return fixed


__all__ = [
    "CLI_FILE_KEY",
    "COVERAGE_PRECISION",
    "KR_CLI_COVERAGE",
    "KR_COVERAGE",
    "KR_DEFECTS_FIXED",
    "KR_TEST_COUNT",
    "LEDGER_PATH",
    "MONEY_FILE_KEY",
    "VALID_DEFECT_COUNT",
    "LedgerEntry",
    "PilotMeasurement",
    "measure_repo",
    "parse_ledger",
]
