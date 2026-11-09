"""五阶段决策链的测试构造器。

设计依据：docs/tasks.md TASK-P1-050。

**这里造的是"真的能跑"的对象，不是随手捏的假货。** 每个字段都按领域规则填
（目标六要素齐全、差距带证据、决策带优先级因子、路线图带排期）—— 因为
checkpoint 要能把它们**原样**存进去再读出来，少填一个字段就等于少验一条
序列化路径。

**打桩处理器会记录自己被调用了几次。** "恢复后不重复执行已完成的节点"这条
验收重点，靠的就是这个计数：光看最终状态看不出哪个节点重跑过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from core.evolution.assessment import (
    DeferredOpportunity,
    GapSeverity,
    GoalGap,
    Opportunity,
    OverallStatus,
    ProjectAssessment,
)
from core.evolution.candidate import DiscoveryResult, EvolutionCandidate
from core.evolution.decision import DecisionRound, DecisionState, EvolutionDecision
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.priority import PriorityFactors
from core.evolution.taxonomy import OpportunityCategory, PriorityLevel, RiskLevel
from core.planning.roadmap import (
    DecisionApproval,
    EvolutionRoadmap,
    PlannedTask,
    ScheduleSlot,
    TaskType,
)
from core.planning.stages import (
    DecisionOutcome,
    GapAnalysis,
    ProjectContext,
    StageHandlers,
)
from core.policy.autonomy import default_policy
from core.project.models import (
    ComparisonOperator,
    Constraint,
    KeyResult,
    NonGoal,
    Objective,
    ProjectGoal,
    StopCondition,
    StopConditionKind,
    TargetExpression,
    TargetValue,
    ValueKind,
)


def project_goal() -> ProjectGoal:
    """一份六要素齐全的目标。

    **六要素必须齐全** —— ``ProjectGoal`` 自己会拒绝不完整的，而
    ``ProjectContext`` 把目标设成必填，正是为了让"没有目标的项目评估"这种
    假象在链上根本出现不了。
    """
    return ProjectGoal(
        objective=(Objective(id="OBJ-1", description="把演进循环跑通"),),
        key_results=(
            KeyResult(
                id="KR-1",
                metric="cycle_success_rate",
                target=TargetExpression(
                    operator=ComparisonOperator.GTE,
                    value=TargetValue(kind=ValueKind.NUMBER, number=Decimal("0.9")),
                ),
                unit="ratio",
            ),
        ),
        constraints=(Constraint(id="C-1", description="不绕过人工审批"),),
        non_goals=(NonGoal(id="NG-1", description="不改生产环境"),),
        autonomy=default_policy(),
        stop_conditions=(
            StopCondition(
                id="SC-1",
                description="连续三次周期失败就停",
                kind=StopConditionKind.CONSECUTIVE_FAILURES_EXCEEDED,
                threshold=Decimal("3"),
            ),
        ),
    )


def project_context() -> ProjectContext:
    """认知阶段的输入。"""
    return ProjectContext(
        project_id="p-demo",
        snapshot="snapshot-1",
        goal=project_goal(),
        history=("cycle-0001 完成", "cycle-0002 完成"),
    )


def assessment() -> ProjectAssessment:
    """认知阶段的产物。"""
    return ProjectAssessment(
        project_id="p-demo",
        snapshot="snapshot-1",
        overall_status=OverallStatus.EVOLVING,
        goal_gap=(
            GoalGap(
                id="GAP-1",
                description="演进循环的采纳率低于阈值",
                evidence=(
                    Evidence(kind=EvidenceKind.TEST_REPORT, ref="artifact://report/cycle-7"),
                ),
                severity=GapSeverity.HIGH,
            ),
        ),
        opportunities=(
            Opportunity(
                id="OPP-1",
                category=OpportunityCategory.ARCHITECTURE,
                action="拆分权限模块",
                priority=PriorityLevel.HIGH,
                addresses=("GAP-1",),
            ),
        ),
        recommendations=("OPP-1",),
        deferred=(DeferredOpportunity(reason="证据不足", candidate="CAND-9"),),
    )


def gap_analysis() -> GapAnalysis:
    """诊断阶段的产物：评估的收窄。"""
    return GapAnalysis.from_assessment(assessment())


def candidate() -> EvolutionCandidate:
    """一条演进候选。"""
    return EvolutionCandidate(
        id="CAND-1",
        category=OpportunityCategory.ARCHITECTURE,
        title="拆分权限模块",
        evidence=(Evidence(kind=EvidenceKind.ARTIFACT, ref="artifact://coupling"),),
        hypothesis="拆分权限领域服务可以降低模块耦合。",
        expected_outcome=("降低模块依赖数量",),
        risk=RiskLevel.MEDIUM,
        confidence=Decimal("0.9"),
    )


def discovery_result() -> DiscoveryResult:
    """探索阶段的产物。"""
    return DiscoveryResult(candidates=(candidate(),))


def factors() -> PriorityFactors:
    """决策的优先级因子。"""
    return PriorityFactors(
        value=Decimal("0.8"),
        impact=Decimal("0.6"),
        confidence=Decimal("0.9"),
        urgency=Decimal("0.5"),
        cost=Decimal("0.5"),
        risk=Decimal("0.3"),
    )


def decision() -> EvolutionDecision:
    """一条 ``record`` 决策。"""
    return EvolutionDecision(
        id="EVD-cycle-0007-001",
        candidate_id="CAND-1",
        decision=DecisionState.RECORD,
        rationale="证据充分，收益高于成本",
        factors=factors(),
    )


def decision_outcome() -> DecisionOutcome:
    """决策阶段的产物：决策与放行凭据。"""
    decided = decision()
    return DecisionOutcome(
        decisions=DecisionRound(cycle_id="cycle-0007", decisions=(decided,)),
        approvals=(
            DecisionApproval(
                decision_id=decided.id,
                candidate_id=decided.candidate_id,
                approved=True,
                reason="九项检查全部通过，无否决",
            ),
        ),
    )


def roadmap() -> EvolutionRoadmap:
    """规划阶段的产物。"""
    decided = decision()
    return EvolutionRoadmap(
        cycle_id="cycle-0007",
        tasks=(
            PlannedTask(
                key="T-1",
                decision_id=decided.id,
                candidate_id=decided.candidate_id,
                title="拆分权限模块",
                task_type=TaskType.REFACTORING,
                priority=Decimal("0.8"),
                rationale="耦合度高于阈值",
            ),
        ),
        schedule=(ScheduleSlot(position=1, key="T-1", wave=1),),
    )


@dataclass
class RecordingHandlers:
    """会记录调用次数的打桩处理器。

    **记录调用次数，而不是只记录"调用过"。** "恢复后不重复执行已完成的节点"
    验的是**次数** —— 一个节点跑两次，状态可能看着一样，但那是白干了一遍
    （而且像"调用模型"这样的节点，第二遍不是白干，是**又花钱又可能给出不同
    的结论**）。
    """

    calls: dict[str, int] = field(default_factory=dict)
    fail_on: str | None = None

    def _record(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        if self.fail_on == name:
            raise RuntimeError(f"{name} 阶段故意失败")

    @property
    def handlers(self) -> StageHandlers:
        return StageHandlers(
            understand=_Stub(self, "understand"),
            diagnose=_Stub(self, "diagnose"),
            explore=_Stub(self, "explore"),
            decide=_Stub(self, "decide"),
            plan=_Stub(self, "plan"),
        )


class _Stub:
    """把"记一次数"与"造产物"绑在一起的处理器。"""

    def __init__(self, owner: RecordingHandlers, stage: str) -> None:
        self._owner = owner
        self._stage = stage

    def understand(self, context: ProjectContext) -> ProjectAssessment:
        self._owner._record("understand")
        return assessment()

    def diagnose(self, value: ProjectAssessment) -> GapAnalysis:
        self._owner._record("diagnose")
        return GapAnalysis.from_assessment(value)

    def explore(self, gaps: GapAnalysis) -> DiscoveryResult:
        self._owner._record("explore")
        return discovery_result()

    def decide(self, discovery: DiscoveryResult) -> DecisionOutcome:
        self._owner._record("decide")
        return decision_outcome()

    def plan(self, outcome: DecisionOutcome) -> EvolutionRoadmap:
        self._owner._record("plan")
        return roadmap()


__all__ = [
    "RecordingHandlers",
    "assessment",
    "candidate",
    "decision",
    "decision_outcome",
    "discovery_result",
    "factors",
    "gap_analysis",
    "project_context",
    "project_goal",
    "roadmap",
]
