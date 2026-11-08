"""TASK-P1-056 —— 把审查报告翻译成放行凭据。

这一层是**角色层与领域层之间的那一步**。领域层（``core/planning``）不认识
审查报告 —— 反向依赖就把层次搞反了；所以领域层只认一个中性的"放行 / 不放行
+ 理由"，由这里把报告翻译过去。

**翻译的时候不能丢信息。** 两件事：

- 不放行的理由里要带着**是哪几问否决的、结论是什么** —— 否决理由读不到，
  这次审查就等于白做了；
- 放行的理由里要写清**有没有未核实的项** —— "九项全通过"与"九项里三项没
  查成"都放行，但不是同一回事，说出来不一样。

另外守着一件最要紧的事：**报告与决策对不上时必须报错**。审的是 A 候选、
放行的是 B 决策 —— 那是一份张冠李戴的报告，而它张冠李戴的时候**不会自己
报错**，所以这里必须自己核。
"""

from __future__ import annotations

import ast
import pathlib
from decimal import Decimal

import pytest

from agents.critic.approval import ReportMismatchError, approval_for
from agents.critic.checks import (
    CheckOutcome,
    CriticCheck,
    CriticEvidence,
    EvidenceKind,
    Verdict,
)
from agents.critic.report import CriticReport
from core.evolution.candidate import EvolutionCandidate
from core.evolution.decision import DecisionPolicy, DecisionRound, EvolutionDecision
from core.evolution.evidence import Evidence
from core.evolution.evidence import EvidenceKind as CandidateEvidenceKind
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from core.planning.roadmap import DecisionApproval, TaskDecomposer, TaskDraft, TaskType

#: 领域层的源文件路径 —— 层次断言要读的是它，不是运行时碰一碰看得出来的东西。
_ROADMAP_SOURCE = pathlib.Path(__file__).resolve().parents[4] / "core" / "planning" / "roadmap.py"


def _decision(
    candidate_id: str = "CAND-001", decision_id: str = "EVD-cycle-0007-001"
) -> EvolutionDecision:
    """一条 ``record`` 决策。"""
    candidate = EvolutionCandidate(
        id=candidate_id,
        category=OpportunityCategory.QUALITY,
        title="登录超时偶发失败",
        evidence=(Evidence(kind=CandidateEvidenceKind.FILE, ref="core/auth.py"),),
        hypothesis="登录响应在慢依赖上卡住",
        expected_outcome=("登录失败率降到 0.1% 以下",),
        risk=RiskLevel.MEDIUM,
        confidence=Decimal("0.8"),
    )
    factors = PriorityFactors.from_candidate(
        candidate,
        value=Decimal("0.8"),
        impact=Decimal("0.6"),
        urgency=Decimal("0.5"),
        cost=Decimal("0.5"),
    )
    scored = ScoredCandidate(candidate_id=candidate_id, factors=factors)
    return DecisionPolicy().decide(scored, decision_id=decision_id)


def _passed(check: CriticCheck) -> CheckOutcome:
    return CheckOutcome(check=check, verdict=Verdict.PASSED, summary="查过了，没问题")


def _unverified(check: CriticCheck) -> CheckOutcome:
    return CheckOutcome(check=check, verdict=Verdict.UNVERIFIED, summary="没配工具")


def _vetoed(check: CriticCheck) -> CheckOutcome:
    """一个构成否决的结论 —— 必须带证据，否则模型层就会拒掉它。"""
    return CheckOutcome(
        check=check,
        verdict=Verdict.FAILED,
        summary="候选触碰了项目 Non-goals",
        evidence=(CriticEvidence(kind=EvidenceKind.NON_GOAL, reference="NG-001"),),
        vetoes=True,
    )


def _non_goal_veto() -> CheckOutcome:
    """一项构成否决的结论 —— 触碰了 Non-goal。"""
    return _vetoed(CriticCheck.RESPECTS_NON_GOALS)


def _stop_condition_veto() -> CheckOutcome:
    """一项构成否决的结论 —— 触发了停止条件。"""
    return _vetoed(CriticCheck.DOES_NOT_TRIGGER_STOP_CONDITIONS)


def _report(
    candidate_id: str = "CAND-001",
    *,
    overrides: dict[CriticCheck, CheckOutcome] | None = None,
) -> CriticReport:
    """一份九项齐全的报告；``overrides`` 替换掉某几项的结论。"""
    outcomes = {check: _passed(check) for check in CriticCheck}
    for check, outcome in (overrides or {}).items():
        outcomes[check] = outcome
    return CriticReport(
        candidate_id=candidate_id,
        outcomes=tuple(outcomes[check] for check in CriticCheck),
    )


class TestAVetoedReportProducesANonApproval:
    """被否决 → 不放行，而且理由要能读。"""

    def test_a_vetoed_report_is_not_approved(self) -> None:
        decision = _decision()
        report = _report(overrides={CriticCheck.RESPECTS_NON_GOALS: _non_goal_veto()})

        approval = approval_for(decision, report)

        assert isinstance(approval, DecisionApproval)
        assert approval.approved is False

    def test_the_reason_names_the_check_that_vetoed(self) -> None:
        """读的人要能对上 design.md §5 的第几问 —— 只写"被否决"没有用。"""
        decision = _decision()
        report = _report(overrides={CriticCheck.RESPECTS_NON_GOALS: _non_goal_veto()})

        approval = approval_for(decision, report)

        assert "是否触碰项目 Non-goals" in approval.reason
        assert "候选触碰了项目 Non-goals" in approval.reason

    def test_the_reason_names_every_vetoing_check(self) -> None:
        """两问都否决时，两问都要说 —— 只说第一问会让人以为第二问不存在。"""
        decision = _decision()
        report = _report(
            overrides={
                CriticCheck.RESPECTS_NON_GOALS: _non_goal_veto(),
                CriticCheck.DOES_NOT_TRIGGER_STOP_CONDITIONS: _stop_condition_veto(),
            }
        )

        approval = approval_for(decision, report)

        assert "是否触碰项目 Non-goals" in approval.reason
        assert "是否触发 Stop Conditions" in approval.reason

    def test_the_credential_points_back_at_the_decision(self) -> None:
        decision = _decision(candidate_id="CAND-001", decision_id="EVD-cycle-0007-009")
        report = _report(overrides={CriticCheck.RESPECTS_NON_GOALS: _non_goal_veto()})

        approval = approval_for(decision, report)

        assert approval.decision_id == "EVD-cycle-0007-009"
        assert approval.candidate_id == "CAND-001"


class TestACleanReportProducesAnApproval:
    """九项全过 → 放行。"""

    def test_a_clean_report_is_approved(self) -> None:
        approval = approval_for(_decision(), _report())

        assert approval.approved is True
        assert approval.decision_id == "EVD-cycle-0007-001"
        assert approval.candidate_id == "CAND-001"

    def test_the_reason_says_everything_passed(self) -> None:
        approval = approval_for(_decision(), _report())

        assert "全部通过" in approval.reason
        assert "否决" in approval.reason

    def test_unverified_checks_are_still_reported(self) -> None:
        """放行不等于"全都查过了" —— 有未核实的项，理由里要写出来。

        这是"没审"与"审了没否决"的区别：两者都放行，但读的人需要知道
        这次审查的成色。
        """
        report = _report(
            overrides={
                CriticCheck.HAS_LOWER_COST_ALTERNATIVE: _unverified(
                    CriticCheck.HAS_LOWER_COST_ALTERNATIVE
                ),
                CriticCheck.NO_UNNECESSARY_COMPLEXITY: _unverified(
                    CriticCheck.NO_UNNECESSARY_COMPLEXITY
                ),
            }
        )

        approval = approval_for(_decision(), report)

        assert approval.approved is True
        assert "未核实" in approval.reason
        assert "2 项未核实" in approval.reason
        assert "有没有更低成本的解决方案？" in approval.reason

    def test_a_report_without_unverified_checks_does_not_claim_there_are_none(self) -> None:
        """对照组 —— 干净的报告不该说"没有未核实项"这种多余的话。"""
        approval = approval_for(_decision(), _report())

        assert "未核实" not in approval.reason


class TestAMismatchedReportIsRefused:
    """报告与决策对不上 —— 直接报错。"""

    def test_a_report_for_another_candidate_is_refused(self) -> None:
        decision = _decision(candidate_id="CAND-001")
        report = _report(candidate_id="CAND-002")

        with pytest.raises(ReportMismatchError) as caught:
            approval_for(decision, report)

        assert "CAND-002" in str(caught.value)
        assert "CAND-001" in str(caught.value)

    def test_a_mismatch_is_refused_even_when_the_report_is_clean(self) -> None:
        """干净的报告照样可能是审错了对象的 —— 干净不是通行证。"""
        decision = _decision(candidate_id="CAND-001")
        report = _report(candidate_id="CAND-999")

        with pytest.raises(ReportMismatchError):
            approval_for(decision, report)


class TestTheCredentialIsNeutral:
    """凭据是领域层的东西 —— 领域层不该认识审查报告。"""

    def test_the_credential_is_a_domain_type(self) -> None:
        approval = approval_for(_decision(), _report())

        assert isinstance(approval, DecisionApproval)

    def test_the_domain_layer_does_not_import_the_role_layer(self) -> None:
        """层次不能反过来 —— 领域层反向依赖角色层就把它搞反了。

        这条断言是**静态**的：它读的是 ``core/planning/roadmap.py`` 的
        导入表，而不是运行时碰一碰就看得出来。
        """
        tree = ast.parse(_ROADMAP_SOURCE.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        assert not any(name.startswith("agents") for name in imported), imported

    def test_the_decision_decides_the_decision_not_the_critic(self) -> None:
        """凭据只回答"审没否决"，不回答"该不该做" —— 那是决策层的活。

        换句话说：同一条决策 + 同一份报告，凭据是确定的；而决策本身由
        六因子算出来，与审查无关。
        """
        decision = _decision()
        report = _report()

        first = approval_for(decision, report)
        second = approval_for(decision, report)

        assert first == second
        assert decision.produces_task is True


class TestTheApprovalFeedsTheDomain:
    """凭据不是终点 —— 它要能被规划层直接吃掉。"""

    def test_the_credential_can_be_used_by_the_decomposer(self) -> None:
        """翻译出来的东西得真的接得上，否则这一层是白写的。"""
        decision = _decision()
        approval = approval_for(decision, _report())

        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0007",
            decision_round=DecisionRound(cycle_id="cycle-0007", decisions=(decision,)),
            approvals=[approval],
            drafts=[
                TaskDraft(
                    key="T-1",
                    decision_id=decision.id,
                    task_type=TaskType.FEATURE,
                    title="修复登录超时",
                )
            ],
        )

        assert roadmap.total == 1
        assert roadmap.skipped == ()

    def test_a_veto_means_no_task_is_planned(self) -> None:
        """否决 → 放行凭据说"不放行" → 领域层就不拆。"""
        decision = _decision()
        report = _report(overrides={CriticCheck.RESPECTS_NON_GOALS: _non_goal_veto()})
        approval = approval_for(decision, report)

        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0007",
            decision_round=DecisionRound(cycle_id="cycle-0007", decisions=(decision,)),
            approvals=[approval],
            drafts=[
                TaskDraft(
                    key="T-1",
                    decision_id=decision.id,
                    task_type=TaskType.FEATURE,
                    title="修复登录超时",
                )
            ],
        )

        assert roadmap.total == 0
        assert len(roadmap.skipped) == 1


class TestTheAdapterDoesNotDependOnTheReportInternals:
    """适配器只读报告上那几个公开结论。"""

    def test_a_decision_that_is_not_record_still_gets_a_credential(self) -> None:
        """凭据只回答"审没否决" —— 决策自己是不是 record，是另一件事。

        这两件事分开，领域层才能自己判断"是不是 record"（它有决策三态的
        知识），而不是让适配器越权替它决定。
        """
        decision = _decision()
        report = _report()

        approval = approval_for(decision, report)

        assert approval.decision_id == decision.id
        assert approval.candidate_id == decision.candidate_id


def test_the_decision_helper_produces_a_record_decision() -> None:
    """守住测试自身的假设 —— 这条决策必须是 record，否则下面几条都不成立。"""
    decision: EvolutionDecision = _decision()
    assert decision.produces_task is True
