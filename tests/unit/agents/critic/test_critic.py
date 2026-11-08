"""TASK-P1-055 —— 独立 Critic 的九项检查与结构化报告。

四条 T1 对应四组用例：

1. **九项检查全部被执行** —— 断言事实核验的**调用记录**：九项检查一项不少，
   六类工具都被用上（I-18 要求"必须调用代码仓库、Git 历史、测试报告和项目
   状态进行事实核验"）。这一条**只验执行，不验判定质量**。
2. **违反 Non-goals 的候选被否决，且引用具体条目** —— 否决理由要指到是哪
   一条，读的人才复核得了。
3. **"已经有别的任务在解决"被检出** —— 靠查询任务表这个**事实**，不靠
   Critic 的记忆。
4. **被否决的候选不得进入 ``APPROVED``** —— 否决闸门抛错，不返回布尔。

另外守着一件容易漏的事：**没配工具时结论是"未核实"，不是"没问题"** ——
把"没查"报成"没问题"，Critic 就成了一台会点头的机器。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from agents.critic.checks import (
    VETOING_CHECKS,
    CheckOutcome,
    CriticCheck,
    CriticEvidence,
    EvidenceKind,
    Verdict,
)
from agents.critic.report import Critic, CriticReport, CriticVetoError, require_no_veto
from agents.critic.sources import ProjectStatusTool
from agents.critic.tools import FactChecker, TaskSummary, ToolKind, ToolResult
from core.evolution.candidate import EvolutionCandidate
from core.evolution.evidence import Evidence
from core.evolution.evidence import EvidenceKind as CandidateEvidenceKind
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from core.project.models import ProjectGoal, TargetValue, ValueKind

#: 一份合法目标要带的自主权策略 —— 六要素校验要求它非空。
_AUTONOMY: dict[str, object] = {
    "rules": {
        "analyze_project": {"level": "L4", "permission": "autonomous"},
        "plan_tasks": {"level": "L4", "permission": "autonomous"},
        "code_changes": {"level": "L4", "permission": "autonomous"},
        "test_execution": {"level": "L4", "permission": "autonomous"},
        "branch_creation": {"level": "L4", "permission": "autonomous"},
        "merge_to_main": {"level": "L2", "permission": "approval_required"},
        "production_deployment": {"level": "L1", "permission": "advisory_only"},
        "modify_governance": {"level": "L1", "permission": "advisory_only"},
    }
}

#: 一份六要素齐全的目标 —— 每一项都能被单独替换掉。
_BASE_GOAL: dict[str, object] = {
    "objective": [{"id": "OBJ-001", "description": "把系统跑通"}],
    "key_results": [{"id": "KR-001", "metric": "e2e_success_rate", "target": ">= 0.95"}],
    "constraints": [
        {
            "id": "C-001",
            "description": "变更必须通过测试",
            "kind": "all_changes_must_pass_tests",
            "machine_checkable": True,
            "expression": "tests_passed == true",
        }
    ],
    "non_goals": [{"id": "NG-001", "description": "不做移动端适配"}],
    "autonomy": _AUTONOMY,
    "stop_conditions": [
        {"id": "SC-BASE", "description": "人工请求停止", "kind": "human_stop_requested"}
    ],
}


def _goal(**overrides: object) -> ProjectGoal:
    """一份合法的目标；``overrides`` 替换掉某一项要素。"""
    return ProjectGoal.model_validate({**_BASE_GOAL, **overrides})


def _candidate(
    *,
    title: str = "登录超时偶发失败",
    hypothesis: str = "登录响应在慢依赖上卡住",
    evidence: tuple[Evidence, ...] | None = None,
) -> EvolutionCandidate:
    """一个待审的候选。"""
    return EvolutionCandidate(
        id="CAND-001",
        category=OpportunityCategory.QUALITY,
        title=title,
        evidence=(
            evidence
            if evidence is not None
            else (Evidence(kind=CandidateEvidenceKind.FILE, ref="core/auth.py"),)
        ),
        hypothesis=hypothesis,
        expected_outcome=("登录失败率降到 0.1% 以下",),
        risk=RiskLevel.MEDIUM,
        confidence=Decimal("0.8"),
    )


class _StubTool:
    """按剧本回答的工具 —— 这一组用例验的是编排，不是工具本身。"""

    def __init__(
        self,
        kind: ToolKind,
        result: ToolResult,
        *,
        by_argument: dict[str, ToolResult] | None = None,
    ) -> None:
        self.kind = kind
        self._result = result
        self._by_argument = by_argument or {}

    async def query(self, argument: str) -> ToolResult:
        return self._by_argument.get(argument, self._result)


def _checker(
    goal: ProjectGoal,
    *,
    tasks: tuple[TaskSummary, ...] = (),
    artifacts: tuple[str, ...] = (),
    failing_tests: tuple[str, ...] = (),
    git_commits: tuple[str, ...] = (),
    violated_constraints: tuple[str, ...] = (),
    actuals: dict[str, TargetValue] | None = None,
    facts: dict[str, Decimal | bool] | None = None,
) -> FactChecker:
    """一台六类工具配齐的核验器。"""
    checker = FactChecker()
    checker.register(
        ProjectStatusTool(
            goal,
            actuals,
            facts=facts,
            violated_constraints=violated_constraints,
        )
    )
    checker.register(
        _StubTool(
            ToolKind.REPOSITORY_SEARCH,
            ToolResult(
                found=True,
                summary="仓库里有 2 处相关",
                references=("core/auth.py", "core/session.py"),
            ),
            by_argument={
                "core/auth.py": ToolResult(
                    found=True,
                    summary="仓库里有 1 处相关",
                    references=("core/auth.py",),
                )
            },
        )
    )
    checker.register(
        _StubTool(
            ToolKind.GIT_HISTORY,
            ToolResult(
                found=bool(git_commits),
                summary=f"Git 历史里有 {len(git_commits)} 条相关提交",
                references=git_commits,
            ),
        )
    )
    checker.register(
        _StubTool(
            ToolKind.TEST_REPORT,
            ToolResult(
                found=bool(failing_tests),
                summary=f"测试报告里有 {len(failing_tests)} 个失败的用例",
                references=failing_tests,
            ),
        )
    )
    checker.register(
        _StubTool(
            ToolKind.TASK_TABLE,
            ToolResult(
                found=bool(tasks),
                summary=f"任务表里有 {len(tasks)} 条相关任务",
                references=tuple(task.id for task in tasks),
            ),
        )
    )
    checker.register(
        _StubTool(
            ToolKind.ARTIFACT_STORE,
            ToolResult(found=False, summary="产物登记处里没有该产物"),
            by_argument={
                artifact_id: ToolResult(
                    found=True,
                    summary=f"产物 {artifact_id} 已登记",
                    references=(artifact_id,),
                )
                for artifact_id in artifacts
            },
        )
    )
    return checker


# ---------------------------------------------------------------------------
# T1 ① —— 九项检查全部被执行
# ---------------------------------------------------------------------------


async def test_all_nine_checks_run_and_leave_call_records() -> None:
    """九项检查一项不少，每一项都真的调过工具。"""
    checker = _checker(_goal(), failing_tests=("tests/test_login.py::test_timeout",))
    report = await Critic(checker).review(_candidate())

    assert len(report.outcomes) == 9
    assert {outcome.check for outcome in report.outcomes} == set(CriticCheck)

    # 少一次调用，就说明有一项检查没查 —— 这条断言盯的是"执行"，
    # 不是判定质量（判定质量由下面每一项各自的用例盯）。
    assert len(checker.calls) >= 9

    # I-18 要求的四类核验手段，外加任务表查询（"是不是已经有别的任务在
    # 做了"），每一次审查都要用到。
    assert {
        ToolKind.REPOSITORY_SEARCH,
        ToolKind.GIT_HISTORY,
        ToolKind.TEST_REPORT,
        ToolKind.PROJECT_STATUS,
        ToolKind.TASK_TABLE,
    } <= checker.called_kinds


async def test_evidence_pointing_at_an_artifact_queries_the_artifact_store() -> None:
    """证据指向产物时，产物登记处必须被问到。"""
    evidence = (Evidence(kind=CandidateEvidenceKind.ARTIFACT, ref="artifact://report-001"),)
    checker = _checker(_goal(), artifacts=("report-001",))

    await Critic(checker).review(_candidate(evidence=evidence))

    assert ToolKind.ARTIFACT_STORE in checker.called_kinds


async def test_every_check_is_answered_exactly_once() -> None:
    """报告里的九项结论各出现一次，不多不少。"""
    report = await Critic(_checker(_goal())).review(_candidate())

    answered = [outcome.check for outcome in report.outcomes]
    assert len(answered) == len(set(answered)) == len(CriticCheck)


async def test_the_report_refuses_to_be_missing_a_check() -> None:
    """少一项的报告不是"少查了一项"，是"读的人会以为查过了"。"""
    outcome = CheckOutcome(
        check=CriticCheck.PROBLEM_EXISTS,
        verdict=Verdict.PASSED,
        summary="查过了",
    )
    with pytest.raises(ValidationError, match="九项检查没有全部给出结论"):
        CriticReport(candidate_id="CAND-001", outcomes=(outcome,))


async def test_the_report_refuses_a_duplicated_check() -> None:
    """九项齐全**再加**一个重复 —— 只可能是重复这条规则在拦。

    少一项 + 一个重复凑成两项，是分不清是哪条规则拒的：把"缺项"那条规则
    拿掉，这张报告照样会被拒（它确实还缺七项）。所以这里先把九项都凑齐，
    让重复成为唯一的毛病。
    """
    outcomes = tuple(
        CheckOutcome(check=check, verdict=Verdict.PASSED, summary="查过了") for check in CriticCheck
    )
    with pytest.raises(ValidationError, match="同一项检查只能有一条结论"):
        CriticReport(candidate_id="CAND-001", outcomes=(*outcomes, outcomes[0]))


# ---------------------------------------------------------------------------
# T1 ② —— 违反 Non-goals 的候选被否决
# ---------------------------------------------------------------------------


async def test_a_candidate_touching_a_non_goal_is_vetoed_with_that_non_goal_cited() -> None:
    goal = _goal(non_goals=[{"id": "NG-MOBILE", "description": "不做移动端适配"}])
    report = await Critic(_checker(goal)).review(_candidate(title="移动端适配改造"))

    outcome = report.outcome_for(CriticCheck.RESPECTS_NON_GOALS)
    assert outcome.verdict is Verdict.FAILED
    assert outcome.vetoes is True
    assert report.vetoed is True

    # 否决要指到**哪一条** —— 只说"违反了非目标"，读的人无从复核。
    assert [evidence.reference for evidence in outcome.evidence] == ["NG-MOBILE"]
    assert "移动端适配" in outcome.evidence[0].detail


async def test_a_candidate_not_touching_any_non_goal_passes_that_check() -> None:
    report = await Critic(_checker(_goal())).review(_candidate())

    outcome = report.outcome_for(CriticCheck.RESPECTS_NON_GOALS)
    assert outcome.verdict is Verdict.PASSED
    assert outcome.vetoes is False


# ---------------------------------------------------------------------------
# T1 ③ —— "已经有别的任务在解决"被检出
# ---------------------------------------------------------------------------


async def test_a_candidate_already_covered_by_a_task_is_detected() -> None:
    """检出即可 —— **相似不等于同一件事**，所以这一问不否决。

    T3 评审里五条历史候选全部被这一问否决，其中至少三条是误报 —— 拿
    相似度当"已解决"，误报率高到这条规则没法用。所以它只把任务 id 摆出来，
    判"是不是同一件事"交给人。
    """
    checker = _checker(
        _goal(),
        tasks=(TaskSummary(id="TASK-009", title="修复登录超时", status="APPROVED"),),
    )
    report = await Critic(checker).review(_candidate())

    outcome = report.outcome_for(CriticCheck.NOT_ALREADY_SOLVED)
    assert outcome.verdict is Verdict.UNVERIFIED
    assert outcome.vetoes is False
    assert report.vetoed is False
    assert [evidence.reference for evidence in outcome.evidence] == ["TASK-009"]


async def test_the_task_table_check_is_not_allowed_to_veto() -> None:
    """能不能否决是政策 —— 第三问被写进了"不否决"那一档。"""
    assert CriticCheck.NOT_ALREADY_SOLVED not in VETOING_CHECKS


async def test_the_task_table_was_actually_queried() -> None:
    """检出靠的是查任务表这个事实，不是 Critic 的记忆。"""
    checker = _checker(_goal())
    await Critic(checker).review(_candidate())

    assert ToolKind.TASK_TABLE in checker.called_kinds


async def test_a_candidate_with_no_task_in_the_table_passes_that_check() -> None:
    report = await Critic(_checker(_goal())).review(_candidate())

    outcome = report.outcome_for(CriticCheck.NOT_ALREADY_SOLVED)
    assert outcome.verdict is Verdict.PASSED
    assert outcome.vetoes is False


# ---------------------------------------------------------------------------
# T1 ④ —— 被否决的候选不得进入 APPROVED
# ---------------------------------------------------------------------------


async def test_a_vetoed_candidate_cannot_pass_the_approval_gate() -> None:
    goal = _goal(non_goals=[{"id": "NG-MOBILE", "description": "不做移动端适配"}])
    report = await Critic(_checker(goal)).review(_candidate(title="移动端适配改造"))

    assert report.vetoed is True
    with pytest.raises(CriticVetoError) as excinfo:
        require_no_veto(report)

    # 报错要说清为什么 —— 否则读的人还得回头翻报告。
    assert "NG-MOBILE" in str(excinfo.value)
    # 而且要说清是**哪一问**否决的 —— 读的人要能对上 design.md §5。
    assert "是否触碰项目 Non-goals" in str(excinfo.value)


async def test_an_unvetoed_candidate_passes_the_approval_gate() -> None:
    report = await Critic(_checker(_goal())).review(_candidate())

    assert report.vetoed is False
    require_no_veto(report)


# ---------------------------------------------------------------------------
# 三态 —— 没查就是没查
# ---------------------------------------------------------------------------


async def test_without_any_tool_every_check_is_unverified() -> None:
    """没有工具就没有结论 —— 绝不报"没问题"。"""
    report = await Critic(FactChecker()).review(_candidate())

    assert all(outcome.verdict is Verdict.UNVERIFIED for outcome in report.outcomes)
    assert len(report.unverified) == 9
    assert report.vetoed is False


async def test_a_missing_tool_only_affects_the_checks_that_need_it() -> None:
    """工具没配 → 用到它的检查"未核实"，用不到它的照常。"""
    checker = FactChecker()
    checker.register(ProjectStatusTool(_goal()))
    report = await Critic(checker).review(_candidate())

    assert report.outcome_for(CriticCheck.RESPECTS_NON_GOALS).verdict is Verdict.PASSED
    assert report.outcome_for(CriticCheck.NOT_ALREADY_SOLVED).verdict is Verdict.UNVERIFIED


def test_the_checker_knows_which_tools_it_has() -> None:
    """配没配工具是能问出来的 —— 部署的时候要靠它检查。"""
    checker = FactChecker()

    assert checker.has(ToolKind.PROJECT_STATUS) is False

    checker.register(ProjectStatusTool(_goal()))

    assert checker.has(ToolKind.PROJECT_STATUS) is True


def test_the_critic_hands_out_its_checker() -> None:
    """报告之外还要能看账本 —— "查过什么"得能被读的人拿到。"""
    checker = _checker(_goal())

    assert Critic(checker).checker is checker


async def test_a_problem_without_a_failing_test_is_unverified() -> None:
    """搜不到失败用例**证明不了问题不存在** —— 结论是"未核实"。"""
    report = await Critic(_checker(_goal())).review(_candidate())

    outcome = report.outcome_for(CriticCheck.PROBLEM_EXISTS)
    assert outcome.verdict is Verdict.UNVERIFIED
    assert outcome.vetoes is False


async def test_key_results_that_are_not_fully_measured_are_unverified() -> None:
    """关键结果没测全 → 说不出现在做还值不值得。"""
    report = await Critic(_checker(_goal())).review(_candidate())

    outcome = report.outcome_for(CriticCheck.WORTH_DOING_NOW)
    assert outcome.verdict is Verdict.UNVERIFIED
    assert outcome.vetoes is False


async def test_the_two_lead_checks_are_never_conclusions() -> None:
    """第五问与第六问给的是线索，不是结论 —— 哪怕查到了东西。"""
    checker = _checker(
        _goal(),
        git_commits=("a1b2c3 修复登录超时",),
    )
    report = await Critic(checker).review(_candidate())

    for check in (
        CriticCheck.HAS_LOWER_COST_ALTERNATIVE,
        CriticCheck.NO_UNNECESSARY_COMPLEXITY,
    ):
        outcome = report.outcome_for(check)
        assert outcome.verdict is Verdict.UNVERIFIED, check
        assert outcome.vetoes is False, check


async def test_the_evidence_list_is_capped() -> None:
    """一次搜索命中几十个文件，全列进报告就没法读了 —— 上限要生效。"""
    checker = FactChecker()
    checker.register(
        _StubTool(
            ToolKind.REPOSITORY_SEARCH,
            ToolResult(
                found=True,
                summary="仓库里有 12 处相关",
                references=tuple(f"core/module_{index}.py" for index in range(12)),
            ),
        )
    )
    report = await Critic(checker).review(_candidate())

    outcome = report.outcome_for(CriticCheck.NO_UNNECESSARY_COMPLEXITY)
    assert len(outcome.evidence) == 5


# ---------------------------------------------------------------------------
# 其它几项否决
# ---------------------------------------------------------------------------


async def test_a_triggered_stop_condition_vetoes_and_cites_the_condition() -> None:
    goal = _goal(
        stop_conditions=[{"id": "SC-BUDGET", "description": "预算耗尽", "kind": "budget_exhausted"}]
    )
    checker = _checker(goal, facts={"budget_exhausted": True})
    report = await Critic(checker).review(_candidate())

    outcome = report.outcome_for(CriticCheck.DOES_NOT_TRIGGER_STOP_CONDITIONS)
    assert outcome.verdict is Verdict.FAILED
    assert outcome.vetoes is True
    assert [evidence.reference for evidence in outcome.evidence] == ["SC-BUDGET"]


async def test_all_key_results_met_vetoes_doing_anything_now() -> None:
    checker = _checker(
        _goal(),
        actuals={"KR-001": TargetValue(kind=ValueKind.NUMBER, number=Decimal("0.98"))},
    )
    report = await Critic(checker).review(_candidate())

    outcome = report.outcome_for(CriticCheck.WORTH_DOING_NOW)
    assert outcome.verdict is Verdict.FAILED
    assert outcome.vetoes is True
    assert [evidence.reference for evidence in outcome.evidence] == ["KR-001"]


async def test_a_violated_constraint_vetoes_and_cites_the_constraint() -> None:
    checker = _checker(_goal(), violated_constraints=("C-001",))
    report = await Critic(checker).review(_candidate())

    outcome = report.outcome_for(CriticCheck.RESPECTS_GOALS_AND_CONSTRAINTS)
    assert outcome.verdict is Verdict.FAILED
    assert outcome.vetoes is True
    assert [evidence.reference for evidence in outcome.evidence] == ["C-001"]


async def test_evidence_pointing_at_an_unregistered_artifact_vetoes() -> None:
    evidence = (Evidence(kind=CandidateEvidenceKind.ARTIFACT, ref="artifact://report-001"),)
    report = await Critic(_checker(_goal())).review(_candidate(evidence=evidence))

    outcome = report.outcome_for(CriticCheck.EVIDENCE_SUFFICIENT)
    assert outcome.verdict is Verdict.FAILED
    assert outcome.vetoes is True
    assert [item.reference for item in outcome.evidence] == ["artifact://report-001"]


async def test_evidence_pointing_at_a_registered_artifact_passes() -> None:
    evidence = (Evidence(kind=CandidateEvidenceKind.ARTIFACT, ref="artifact://report-001"),)
    checker = _checker(_goal(), artifacts=("report-001",))
    report = await Critic(checker).review(_candidate(evidence=evidence))

    outcome = report.outcome_for(CriticCheck.EVIDENCE_SUFFICIENT)
    assert outcome.verdict is Verdict.PASSED
    assert outcome.vetoes is False


# ---------------------------------------------------------------------------
# 结论本身的规矩
# ---------------------------------------------------------------------------


def test_a_veto_must_carry_evidence() -> None:
    """没有证据的否决，读的人只能选择相信或不信。"""
    with pytest.raises(ValidationError):
        CheckOutcome(
            check=CriticCheck.RESPECTS_NON_GOALS,
            verdict=Verdict.FAILED,
            summary="触碰了非目标",
            vetoes=True,
        )


def test_a_non_vetoing_check_cannot_veto() -> None:
    """能不能否决是政策，不是临场判断。"""
    with pytest.raises(ValidationError):
        CheckOutcome(
            check=CriticCheck.PROBLEM_EXISTS,
            verdict=Verdict.FAILED,
            summary="问题不存在",
            evidence=(CriticEvidence(kind=EvidenceKind.CANDIDATE, reference="CAND-001"),),
            vetoes=True,
        )


def test_an_unverified_check_cannot_veto() -> None:
    """没查成不构成否决。"""
    with pytest.raises(ValidationError):
        CheckOutcome(
            check=CriticCheck.RESPECTS_NON_GOALS,
            verdict=Verdict.UNVERIFIED,
            summary="没查",
            evidence=(CriticEvidence(kind=EvidenceKind.NON_GOAL, reference="NG-001"),),
            vetoes=True,
        )


def test_an_evidence_reference_cannot_be_blank() -> None:
    """ "查了一下"不是证据引用。"""
    with pytest.raises(ValidationError):
        CriticEvidence(kind=EvidenceKind.REPOSITORY, reference="   ")
