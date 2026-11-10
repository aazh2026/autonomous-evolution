"""TASK-P3-018 —— 十条修复在试点仓库上**真打一遍、真跑一遍**。

设计依据：docs/tasks.md TASK-P3-018（"连续 10 轮"）、R-128（每轮产生可验证
的改善）、R-130（首个里程碑）。

**为什么这里要跑真的 pytest。** 长跑的每一轮都是"改源码 → 搬用例 → 删用例 →
清 import"这四件事。**四件事少一件，那一轮的修复就是假的** —— 只改源码而
不搬用例，缺陷"修好了"却没有回归保护；只搬用例而不改源码，用例会永远红着；
忘了清 import，验证阶段的 lint 会直接让这一轮失败。所以这里不是查补丁**写
得对不对**，而是查**打完之后仓库是不是真的好了**。

**为什么必须按顺序打。** 清 import 那一步依赖前面的删除：``import pytest``
是复现清单里所有用例的装饰器共用的，最后一条用例搬走之后它才没人用。**单
独打第 9 条会让清单里剩下的用例失去装饰器** —— 所以这十条是**一轮接一轮**
的关系，不是十条独立的补丁。

**为什么用仓库的副本。** 补丁要改的是真实文件，而 ``samples/pilot_repo`` 是
基线的来源 —— 在它上面打补丁就等于改了基线。所以每条用例都在 ``tmp_path``
里的一份副本上打。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apps.director.pilot_fixes import FIX_BY_ISSUE, FIXES, IssueFix

REPO_ROOT = Path(__file__).resolve().parents[3]
PILOT_DIR = REPO_ROOT / "samples" / "pilot_repo"

#: 复现清单在仓库里的路径。
KNOWN_ISSUES = "known_issues/test_known_issues.py"


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """试点仓库的一份副本 —— 补丁打在这里。"""
    target = tmp_path / "repo"
    shutil.copytree(PILOT_DIR, target)
    return target


def _pytest(repo: Path, *targets: str) -> subprocess.CompletedProcess[str]:
    """在副本里跑一遍 pytest —— **不检查退出码**，结果在输出里。"""
    return subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-q", "-p", "no:langsmith_plugin"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _ruff(repo: Path) -> subprocess.CompletedProcess[str]:
    """在副本里跑一遍验证计划里那套 lint。"""
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "E4,E7,E9,F", "."],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _created_tests(fix: IssueFix) -> tuple[str, ...]:
    """这条修复新建了哪些回归用例文件。"""
    return tuple(
        patch.path for patch in fix.patches if not patch.old and patch.path.startswith("tests/")
    )


class TestFixRegistry:
    """修复清单本身的性质。"""

    def test_there_are_ten_fixes(self) -> None:
        """九条复现型 + 一条复核型 = 十轮。"""
        assert len(FIXES) == 10

    def test_issue_numbers_are_unique(self) -> None:
        assert len(FIX_BY_ISSUE) == len(FIXES)

    def test_every_fix_has_evidence(self) -> None:
        """证据的引用必须指得到真的东西 —— Critic 的第二问会去仓库里核。"""
        for fix in FIXES:
            assert fix.evidence, f"{fix.issue} 没有证据"
            for ref, _note in fix.evidence:
                assert (PILOT_DIR / ref).exists(), f"{fix.issue} 的证据引用 {ref} 不存在"

    def test_repro_fixes_are_named_after_their_repro_test(self) -> None:
        """**标题就是复现用例的函数名** —— Critic 靠词面匹配去报告里找它。"""
        for fix in FIXES:
            if not fix.repro_test:
                continue
            assert fix.title == fix.repro_test

    def test_the_review_fix_has_no_repro_test(self) -> None:
        """复核型发现复现的是"缺一样东西" —— 写不成一条会失败的用例。"""
        review = FIX_BY_ISSUE["PILOT-011"]
        assert review.repro_test == ""


class TestFixesApply:
    """**真打一遍** —— 补丁与试点仓库逐字节对得上。"""

    def test_all_ten_fixes_apply_in_order(self, repo_copy: Path) -> None:
        """按顺序打十轮，每一轮的补丁都对得上。"""
        for fix in FIXES:
            for patch in fix.patches:
                patch.apply(repo_copy)

    def test_the_known_issues_list_is_empty_at_the_end(self, repo_copy: Path) -> None:
        """十条打完之后，复现清单里不该剩下任何用例。"""
        for fix in FIXES:
            for patch in fix.patches:
                patch.apply(repo_copy)
        text = (repo_copy / KNOWN_ISSUES).read_text(encoding="utf-8")
        assert "def test_" not in text, "复现清单里还有用例没搬走"
        assert "import pytest" not in text, "复现清单里还有没人用的 import"


class TestFixesActuallyFix:
    """**真跑一遍** —— 打完之后仓库真的是好的。"""

    @pytest.mark.parametrize("fix", FIXES, ids=lambda item: item.issue)
    def test_the_new_regression_test_passes(self, repo_copy: Path, fix: IssueFix) -> None:
        """每一条修复搬过去的回归用例都必须是绿的。

        **按顺序打。** 清 import 依赖前面的删除 —— 单独打一条会让清单里剩下
        的用例失去装饰器。
        """
        for previous in FIXES:
            for patch in previous.patches:
                patch.apply(repo_copy)
            if previous.issue == fix.issue:
                break
        targets = _created_tests(fix)
        assert targets, f"{fix.issue} 没有搬回归用例"
        result = _pytest(repo_copy, *targets)
        assert result.returncode == 0, f"{fix.issue} 的回归用例没过：\n{result.stdout}"

    def test_the_whole_suite_is_green_after_ten_rounds(self, repo_copy: Path) -> None:
        """十轮之后：自有套件全绿，复现清单空着，lint 干净。"""
        for fix in FIXES:
            for patch in fix.patches:
                patch.apply(repo_copy)

        suite = _pytest(repo_copy, "tests")
        assert suite.returncode == 0, f"自有套件没过：\n{suite.stdout}"

        known = _pytest(repo_copy, "known_issues", "--runxfail")
        assert known.returncode in (0, 5), f"复现清单里还有红的：\n{known.stdout}"

        lint = _ruff(repo_copy)
        assert lint.returncode == 0, f"lint 没过：\n{lint.stdout}"
