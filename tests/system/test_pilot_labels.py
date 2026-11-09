"""TASK-P1-083 —— 试点仓库的标注基线与复现套件（端到端）。

设计依据：docs/tasks.md TASK-P1-083 的三条验收（T1 ①②③ 与 T3/T4）。

**这里跑的是真的东西。** 真的读标注集、真的解析人写的清单、真的起子进程跑
试点仓库的测试套件、真的量一遍覆盖率。**不联网、不花钱、不碰真仓库。**

**为什么清单与标注集要互相对账。** 同一件事有两份记录：一份是人读的
（``samples/pilot_repo/docs/issues.md``），一份是机器读的
（``evaluation/labels/pilot_labels.jsonl``）。两份各写各的，就会有**改了
一边忘了另一边**的时候 —— 而那时候误报率的分母与它依据的清单已经对不上了，
**没有人会发现**，因为机器读的那份照样能加载。

**为什么复现套件要真的跑一遍。** 一条"会失败的用例"如果只是写在文件里，
它可能**根本没失败**（断言写错了、或者那个缺陷早就被修掉了）。所以这里起
子进程跑一遍，要求**全部 xfail**：全 xpass 说明缺陷其实不存在（标注错了），
有 error 说明复现本身坏了 —— 两种都得查。
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.project.models import load_project
from evaluation.labels.pilot import EXPECTED_LABEL_COUNT, pilot_label_store

#: 仓库根目录。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: 试点仓库 —— 就是仓库里那一份。
PILOT = REPO_ROOT / "samples" / "pilot_repo"

#: 人读的问题清单。
ISSUES_MD = PILOT / "docs" / "issues.md"

#: 复现用例。
KNOWN_ISSUES = PILOT / "known_issues" / "test_known_issues.py"

#: T4 交付文档。
PILOT_DOC = REPO_ROOT / "docs" / "evaluation" / "pilot-repo.md"

#: 试点仓库的目标 YAML。
GOAL_YAML = PILOT / "project.yaml"

#: 清单里一条问题的标题行。
_HEADING = re.compile(r"^### (PILOT-\d{3}) —— (.+)$", re.MULTILINE)

#: 清单里的一条字段。
_FIELD = re.compile(r"^- \*\*(\S+?)\*\*：(.*)$", re.MULTILINE)

#: 文档里标注样本集一览的一行。
_TABLE_ROW = re.compile(
    r"^\| (PILOT-\d{3}) \| `([^`]+)` \| (\w+) \| (有效发现|无效发现) \| (repro|review) \|$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Issue:
    """清单里的一条问题。"""

    ref: str
    title: str
    location: str
    category: str
    verdict: str
    evidence_kind: str
    evidence: str


def _unquote(text: str) -> str:
    """去掉反引号 —— 清单里带反引号的是代码片段。"""
    return text.strip().strip("`").strip()


def _parse_issues(text: str) -> list[Issue]:
    """把人读的清单解析成一条条问题。"""
    headings = list(_HEADING.finditer(text))
    issues: list[Issue] = []
    for index, match in enumerate(headings):
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[start:end]
        fields = {name: _unquote(value) for name, value in _FIELD.findall(block)}
        issues.append(
            Issue(
                ref=match.group(1),
                title=match.group(2).strip(),
                location=fields["位置"],
                category=fields["类别"],
                verdict=fields["判定"],
                evidence_kind=fields["证据类型"],
                evidence=fields["证据"],
            )
        )
    return issues


def _run_pilot_pytest(*args: str) -> subprocess.CompletedProcess[str]:
    """在试点仓库里跑一次 pytest。

    ``-p no:langsmith_plugin``：langsmith 的 pytest 插件在导入时碰
    ``asyncio.iscoroutinefunction``，而 Python 3.14 把那个弃用警告升级成了
    错误 —— 于是插件还没加载，pytest 就退出了。这个插件只做 trace 上报，
    试点仓库不用它。

    **不传 ``-q``**：试点仓库自己的 ``addopts`` 里已经有 ``-q``，再来一个就
    成了 ``-qq`` —— 双倍安静会把**摘要行**（"9 xfailed in 0.41s"）一起吞掉，
    而摘要行正是这里要读的东西。
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:langsmith_plugin", *args],
        cwd=PILOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _counts(output: str) -> dict[str, int]:
    """从 pytest 的摘要行里取出各类结果的条数。"""
    found: dict[str, int] = {}
    for count, word in re.findall(
        r"(\d+) (xfailed|xpassed|passed|failed|error|errors|skipped)", output
    ):
        found[word] = found.get(word, 0) + int(count)
    return found


class TestTheIssueList:
    """人读的清单本身是站得住的。"""

    def test_the_list_has_exactly_twenty_issues(self) -> None:
        issues = _parse_issues(ISSUES_MD.read_text(encoding="utf-8"))

        assert len(issues) == EXPECTED_LABEL_COUNT

    def test_the_issue_refs_are_unique(self) -> None:
        issues = _parse_issues(ISSUES_MD.read_text(encoding="utf-8"))

        assert len({issue.ref for issue in issues}) == len(issues)

    def test_every_issue_says_where_it_is_and_what_kind_it_is(self) -> None:
        issues = _parse_issues(ISSUES_MD.read_text(encoding="utf-8"))

        for issue in issues:
            assert issue.location.startswith("src/tinylib/"), issue.ref
            assert issue.category.strip(), issue.ref
            assert issue.title.strip(), issue.ref

    def test_the_evidence_kind_is_one_of_the_two_allowed_ones(self) -> None:
        """**两类证据不许混** —— "有复现"与"人工评审"是两种不同的主张。"""
        issues = _parse_issues(ISSUES_MD.read_text(encoding="utf-8"))

        assert {issue.evidence_kind for issue in issues} == {"repro", "review"}

    def test_every_repro_issue_names_an_existing_test(self) -> None:
        """说了"有复现"就得指出是哪个用例 —— 指不出来的等于没说。"""
        issues = _parse_issues(ISSUES_MD.read_text(encoding="utf-8"))
        source = KNOWN_ISSUES.read_text(encoding="utf-8")

        for issue in issues:
            if issue.evidence_kind != "repro":
                continue
            match = re.search(r"::(test_[A-Za-z0-9_]+)", issue.evidence)
            assert match is not None, f"{issue.ref} 没说复现用例在哪"
            test_id = match.group(1)
            assert f"def {test_id}(" in source, f"{issue.ref} 指向的用例不存在：{test_id}"

    def test_every_repro_test_is_marked_xfail(self) -> None:
        """复现用例必须标着 ``xfail`` —— 否则它会把仓库的套件跑红。"""
        source = KNOWN_ISSUES.read_text(encoding="utf-8")

        assert source.count("@pytest.mark.xfail(strict=True") == 9


class TestTheLabelSetAndTheListAgree:
    """**清单与标注集必须对得上。**"""

    def test_every_issue_has_a_label(self) -> None:
        issues = _parse_issues(ISSUES_MD.read_text(encoding="utf-8"))
        label_set = pilot_label_store().load()

        assert {issue.ref for issue in issues} == set(label_set.refs)

    def test_every_label_has_an_issue(self) -> None:
        issues = _parse_issues(ISSUES_MD.read_text(encoding="utf-8"))
        label_set = pilot_label_store().load()

        assert set(label_set.refs) <= {issue.ref for issue in issues}

    def test_the_categories_agree(self) -> None:
        issues = _parse_issues(ISSUES_MD.read_text(encoding="utf-8"))
        label_set = pilot_label_store().load()

        for issue in issues:
            label = label_set.label_of(issue.ref)
            assert label is not None
            assert label.category == issue.category, issue.ref

    def test_the_verdicts_agree(self) -> None:
        """清单说"有效发现"，标注集就必须说 ``is_valid_finding: true``。"""
        issues = _parse_issues(ISSUES_MD.read_text(encoding="utf-8"))
        label_set = pilot_label_store().load()

        for issue in issues:
            label = label_set.label_of(issue.ref)
            assert label is not None
            assert label.is_valid_finding is (issue.verdict == "有效发现"), issue.ref

    def test_the_split_is_twelve_and_eight(self) -> None:
        issues = _parse_issues(ISSUES_MD.read_text(encoding="utf-8"))
        valid = [issue for issue in issues if issue.verdict == "有效发现"]

        assert len(valid) == 12
        assert len(issues) - len(valid) == 8

    def test_the_repro_issues_are_exactly_the_valid_ones_with_a_repro(self) -> None:
        """有复现的必须是有效发现 —— 无效发现不能有"复现"，它复现的是文档。"""
        issues = _parse_issues(ISSUES_MD.read_text(encoding="utf-8"))

        for issue in issues:
            if issue.evidence_kind == "repro":
                assert issue.verdict == "有效发现", issue.ref


class TestTheReproductionSuite:
    """复现套件要真的跑得起来，而且真的失败。"""

    def test_every_reproduction_fails(self) -> None:
        """**全部 xfail** —— 全 xpass 说明缺陷不存在，有 error 说明复现本身坏了。"""
        result = _run_pilot_pytest("known_issues", "--no-header", "-W", "ignore")

        assert result.returncode == 0, result.stdout + result.stderr
        counts = _counts(result.stdout)
        assert counts.get("xfailed") == 9, result.stdout
        assert not counts.get("xpassed"), result.stdout
        assert not counts.get("failed"), result.stdout
        assert not counts.get("error"), result.stdout
        assert not counts.get("errors"), result.stdout

    def test_the_pilot_suite_is_green(self) -> None:
        """仓库自己的套件必须绿 —— 那些会失败的用例不在里面。"""
        result = _run_pilot_pytest("--no-header", "-W", "ignore")

        assert result.returncode == 0, result.stdout + result.stderr
        counts = _counts(result.stdout)
        assert counts.get("passed") == 43, result.stdout
        assert not counts.get("failed"), result.stdout

    def test_the_reproduction_suite_is_outside_the_default_test_paths(self) -> None:
        """**已发布的覆盖率基线不能被复现用例弄脏。**

        基线是仓库**自有套件**跑出来的；把会失败的用例混进去，基线测的就
        不是同一个东西了。
        """
        pyproject = (PILOT / "pyproject.toml").read_text(encoding="utf-8")

        assert 'testpaths = ["tests"]' in pyproject
        assert KNOWN_ISSUES.parent.name not in pyproject

    def test_the_cli_module_really_has_no_coverage(self) -> None:
        """PILOT-010 —— cli 模块覆盖率真的是 0。

        这条是**人工评审**得出的结论（"没有用例"没法用会失败的用例复现），
        所以这里用**覆盖测量**来复核它。
        """
        result = _run_pilot_pytest(
            "--no-header",
            "-W",
            "ignore",
            "--cov=src/tinylib",
            "--cov-report=term-missing",
        )

        assert result.returncode == 0, result.stdout + result.stderr
        cli_line = next(line for line in result.stdout.splitlines() if "cli.py" in line)
        assert re.search(r"\b0%", cli_line), cli_line


class TestTheGoalYaml:
    """目标 YAML 要能被真的载入 —— 六要素缺一不可。"""

    def test_the_goal_yaml_loads(self) -> None:
        project = load_project(GOAL_YAML)

        assert project.id == "pilot-tinylib"
        assert project.goal.objective
        assert project.goal.key_results
        assert project.goal.constraints
        assert project.goal.non_goals
        assert project.goal.stop_conditions

    def test_the_key_results_are_machine_judgable(self) -> None:
        """目标要可量化 —— 每个关键结果都得有目标值和基线。"""
        project = load_project(GOAL_YAML)

        for key_result in project.goal.key_results:
            assert key_result.target, key_result.id
            assert key_result.metric, key_result.id
            assert key_result.baseline is not None, key_result.id

    def test_the_goal_does_not_hand_out_tasks(self) -> None:
        """**只给目标，不给任务** —— 这是 TASK-P1-082 的输入形态。"""
        project = load_project(GOAL_YAML)

        metrics = {key_result.metric for key_result in project.goal.key_results}
        assert "fix_bug_in_cli" not in metrics


class TestThePilotDocument:
    """T4 —— 文档含目标 YAML、基线指标、标注样本集。"""

    def test_the_document_embeds_the_goal_yaml_verbatim(self) -> None:
        """文档里那份必须是文件的**逐字副本** —— 改了一份忘了另一份会被测出来。"""
        text = PILOT_DOC.read_text(encoding="utf-8")
        start = text.index("```yaml\n") + len("```yaml\n")
        end = text.index("\n```", start)

        assert text[start:end] == GOAL_YAML.read_text(encoding="utf-8").rstrip("\n")

    def test_the_document_lists_the_whole_label_set(self) -> None:
        text = PILOT_DOC.read_text(encoding="utf-8")
        rows = {match.group(1): match for match in _TABLE_ROW.finditer(text)}
        label_set = pilot_label_store().load()

        assert set(rows) == set(label_set.refs)

    def test_the_document_agrees_with_the_label_set(self) -> None:
        text = PILOT_DOC.read_text(encoding="utf-8")
        label_set = pilot_label_store().load()

        for match in _TABLE_ROW.finditer(text):
            ref, _location, category, verdict, evidence_kind = match.groups()
            label = label_set.label_of(ref)
            assert label is not None
            assert label.category == category, ref
            assert label.is_valid_finding is (verdict == "有效发现"), ref
            assert evidence_kind in {"repro", "review"}, ref

    def test_the_document_states_the_baseline_metrics(self) -> None:
        text = PILOT_DOC.read_text(encoding="utf-8")

        assert "0.740385" in text
        assert "43" in text
        assert "0.000000" in text


@pytest.mark.parametrize("name", ["PILOT-001", "PILOT-010", "PILOT-020"])
def test_the_labels_of_three_kinds_are_readable(name: str) -> None:
    """三类各取一条 —— 有效且可复现、有效但只有评审、无效。"""
    label = pilot_label_store().load().label_of(name)

    assert label is not None
    assert label.annotated_by
    assert label.annotated_at.year == 2026
