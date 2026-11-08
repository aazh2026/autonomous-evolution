"""TASK-P1-055 的 T3 评审 —— 抽历史候选，看 Critic 的否决理由站不站得住。

跑法::

    uv run python -m scripts.demo_critic_review

**这里没有假的东西。** 目标用的是仓库里真实的 ``docs/examples/project.yaml``；
仓库搜索与 Git 历史查的是**这个仓库自己**；任务表查的是**这个仓库真实的
任务清单** ``docs/tasks.md``。五条候选取自本仓库真实做过的事 —— 审完之后要
能对着提交记录核对。

**这个演示要看的是什么。** Critic 最容易出的错不是"门不工作"，而是**门乱
开或乱关**：该拦的没拦（漏放），不该拦的拦了（误杀），或者拦的理由站不住
（读的人没法复核）。所以每条候选都跑完之后，脚本会把"否决理由"原样打出来，
由人对着仓库的真实历史核对 —— **站不站得住，是人要下的结论，脚本给不了**。

脚本不自己判定"通过"，它只把每一步摆在明面上：

1. 九项检查各自的结论（通过 / 不通过 / 未核实）；
2. 每项结论的证据引用 —— 凭什么这么说；
3. 否决闸门（:func:`require_no_veto`）拦不拦得住。
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from agents.critic.report import Critic, CriticVetoError, require_no_veto
from agents.critic.sources import (
    GitHistoryTool,
    ProjectStatusTool,
    RepositorySearchTool,
    TaskTableTool,
    TestReportTool,
)
from agents.critic.tools import FactChecker, TaskSummary
from core.evolution.candidate import EvolutionCandidate
from core.evolution.evidence import Evidence
from core.evolution.evidence import EvidenceKind as CandidateEvidenceKind
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from core.project.models import load_project

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILE = REPO_ROOT / "docs" / "examples" / "project.yaml"
TASKS_FILE = REPO_ROOT / "docs" / "tasks.md"

#: ``docs/tasks.md`` 里的任务条目 —— 勾选状态、id、标题。
_TASK_HEADING = re.compile(r"^### \[(.|x)\] (TASK-[A-Z0-9-]+) (.+)$", re.MULTILINE)

#: 至少共享几个二字组才算"像"。
#:
#: **一个字太容易撞，两个字才算"同一个说法"。** 阈值定得太低会把一堆不相干
#: 的任务也算进来 —— 而"算进来"本身不是错（端口只承诺"像的都给"），错的是
#: 拿它当"已经解决"的结论。
_MIN_SHARED_BIGRAMS: int = 2


class DocsTaskLookup:
    """任务表端口的一个实现 —— 查的是**本仓库真实的任务清单**。

    **相似度就是共享的二字组。** 这是个粗糙的算法，但它是**如实**的：端口
    承诺"像的都给"，判"是不是同一件事"是 Critic 与人的事。
    """

    def __init__(self, path: Path) -> None:
        self._tasks = tuple(
            TaskSummary(id=task_id, title=title, status="DONE" if done == "x" else "TODO")
            for done, task_id, title in _TASK_HEADING.findall(path.read_text(encoding="utf-8"))
        )

    async def find_similar(self, text: str, *, limit: int = 20) -> tuple[TaskSummary, ...]:
        needle = _bigrams(text)
        if not needle:
            return ()
        scored = [(len(needle & _bigrams(task.title)), task) for task in self._tasks]
        matches = [
            task
            for score, task in sorted(scored, key=lambda item: -item[0])
            if score >= _MIN_SHARED_BIGRAMS
        ]
        return tuple(matches[:limit])


def _bigrams(text: str) -> set[str]:
    """中文按二字组切 —— 一个字太容易撞，两个字才算"同一个说法"。"""
    cleaned = "".join(char for char in text if not char.isspace())
    return {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}


def _real_test_report(workdir: Path) -> Path:
    """跑一遍**真实的**单元测试，换成测试报告工具认得的形状。

    **为什么不用造出来的报告。** 第一问要回答的是"这个问题是否真实存在"，
    而它唯一的硬事实是"有一条提到这件事的**失败**用例"。造一份报告去喂它，
    验的就不是"报告里有没有失败用例"这件事了。

    仓库里没有现成的测试报告（`pytest-json-report` 没装），所以这里用 pytest
    自带的 ``--junit-xml`` 跑一遍，再转成 ``{"tests": [...]}`` 那个形状。
    跑出来的结论多半是"没有失败的用例" —— 那正是这一问该说的话：**没有失败
    用例，说明不了问题存在，也说明不了它不存在。**
    """
    junit = workdir / "report.xml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit",
            "--junit-xml",
            str(junit),
            "-p",
            "no:cacheprovider",
            "-q",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    report = workdir / "report.json"
    report.write_text(_junit_to_report(junit), encoding="utf-8")
    return report


def _junit_to_report(junit: Path) -> str:
    """JUnit XML → ``{"tests": [{"nodeid", "outcome"}]}``。"""
    if not junit.exists():
        return json.dumps({"tests": []})
    root = ET.parse(junit).getroot()
    tests: list[dict[str, str]] = []
    for case in root.iter("testcase"):
        nodeid = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        tests.append({"nodeid": nodeid, "outcome": outcome})
    return json.dumps({"tests": tests})


def _candidate(
    candidate_id: str,
    title: str,
    hypothesis: str,
    *,
    evidence: Sequence[Evidence],
) -> EvolutionCandidate:
    return EvolutionCandidate(
        id=candidate_id,
        category=OpportunityCategory.QUALITY,
        title=title,
        evidence=tuple(evidence),
        hypothesis=hypothesis,
        expected_outcome=("候选描述的问题消失",),
        risk=RiskLevel.MEDIUM,
        confidence=Decimal("0.7"),
    )


def _file_evidence(ref: str) -> Evidence:
    return Evidence(kind=CandidateEvidenceKind.FILE, ref=ref)


#: 五条候选 —— 都取自本仓库真实做过或明确不做的事。
CANDIDATES: tuple[EvolutionCandidate, ...] = (
    _candidate(
        "CAND-HIST-055",
        "实现独立 Critic 的九项检查与结构化报告",
        "决策链上少一道独立的核验，候选的质量只能靠生成它的那一方保证",
        evidence=(_file_evidence("agents/critic/report.py"),),
    ),
    _candidate(
        "CAND-HIST-002",
        "重构与演进目标无关的历史模块，统一目录结构",
        "历史模块的目录结构与现在的分层不一致，改起来容易踩错层",
        evidence=(_file_evidence("core/project/models.py"),),
    ),
    _candidate(
        "CAND-HIST-003",
        "为移动端原生适配补一套界面层",
        "主要用户用手机，现在没有适配",
        evidence=(_file_evidence("docs/examples/project.yaml"),),
    ),
    _candidate(
        "CAND-HIST-004",
        "把验收标准的判定逻辑挪到执行器里，省一次跨进程调用",
        "判定放在编排层每次都要跨进程，延迟高",
        evidence=(_file_evidence("harness/verification/contract.py"),),
    ),
    _candidate(
        "CAND-HIST-005",
        "把 e2e_success_rate 从 0.72 提到 0.95",
        "端到端生成成功率是本项目的关键结果，现在离目标还差得远",
        evidence=(_file_evidence("docs/examples/project.yaml"),),
    ),
)


async def review_one(candidate: EvolutionCandidate, report_path: Path) -> bool:
    """审一条候选，把每一步打在屏幕上。返回"是否被否决"。"""
    project = load_project(PROJECT_FILE)

    checker = FactChecker()
    checker.register(RepositorySearchTool(REPO_ROOT))
    checker.register(GitHistoryTool(REPO_ROOT))
    checker.register(TestReportTool(report_path))
    checker.register(ProjectStatusTool(project.goal))
    checker.register(TaskTableTool(DocsTaskLookup(TASKS_FILE)))

    report = await Critic(checker).review(candidate)

    print("=" * 78)
    print(f"候选 {candidate.id}：{candidate.title}")
    print("-" * 78)
    for outcome in report.outcomes:
        mark = {"passed": "通过", "failed": "不通过", "unverified": "未核实"}[outcome.verdict]
        flag = "【否决】" if outcome.vetoes else ""
        print(f"  {outcome.check.value:<32} {mark}{flag}")
        print(f"      {outcome.summary}")
        for item in outcome.evidence:
            print(f"      · [{item.kind.value}] {item.reference} —— {item.detail}")

    print("-" * 78)
    tools = sorted(kind.value for kind in checker.called_kinds)
    print(f"  调用账本：{len(checker.calls)} 次，工具 {tools}")
    try:
        require_no_veto(report)
    except CriticVetoError as exc:
        print(f"  否决闸门：拦住了 —— {exc}")
        return True
    print("  否决闸门：放行")
    return False


async def main() -> int:
    print("TASK-P1-055 的 T3 评审 —— 用真实目标、真实仓库、真实任务清单审历史候选\n")
    print("（先跑一遍真实的单元测试，生成测试报告 —— 第一问要有硬事实可查）\n")
    with tempfile.TemporaryDirectory(prefix="ases-critic-review-") as workdir:
        report_path = _real_test_report(Path(workdir))
        vetoed = 0
        for candidate in CANDIDATES:
            if await review_one(candidate, report_path):
                vetoed += 1
    print("=" * 78)
    print(f"共审 {len(CANDIDATES)} 条候选，否决 {vetoed} 条，放行 {len(CANDIDATES) - vetoed} 条。")
    print("理由站不站得住，由人对着提交记录核对 —— 脚本只负责把理由摆出来。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
