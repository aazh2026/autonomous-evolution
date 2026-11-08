"""TASK-P1-056 —— Roadmap 生成与任务拆解。

四条 T1 对应四组用例：

1. **每个任务都能追溯到一个 EvolutionDecision** —— 拆解出来的每个任务都带着
   决策 id，且那条决策确实在这一轮里。
2. **依赖图无环** —— 有环的依赖图在模型层就被拒（它排不出"先做哪个"，
   而且**不会报错**，只会让每一件任务都在等别人）。
3. **七种任务类型均可构造** —— 逐类构造，且每一类都有它自己的验收重点。
4. **未被 Critic 放行的 Decision 不产生 Task** —— 没凭据与被否决都不拆。

另外守着两件容易漏的事：**没拆的决策也要说清楚为什么**（不做也要留痕），
以及**放行凭据缺失时是"不放行"而不是"默认放行"**。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.evolution.candidate import EvolutionCandidate
from core.evolution.decision import DecisionPolicy, DecisionRound, DecisionState, EvolutionDecision
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from core.planning.roadmap import (
    ACCEPTANCE_FOCUS,
    ConflictingApprovalError,
    DecisionApproval,
    DependencyCycleError,
    DependencyType,
    DuplicateTaskKeyError,
    EvolutionRoadmap,
    PlannedTask,
    TaskDecomposer,
    TaskDependency,
    TaskDraft,
    TaskType,
    UnknownDecisionError,
    UnknownDependencyError,
)


def _candidate(candidate_id: str, *, confidence: str = "0.9") -> EvolutionCandidate:
    """一条候选。"""
    return EvolutionCandidate(
        id=candidate_id,
        category=OpportunityCategory.ENGINEERING,
        title=f"候选 {candidate_id}",
        evidence=(Evidence(kind=EvidenceKind.ARTIFACT, ref="artifact://demo"),),
        hypothesis="做了这件事项目会更好",
        expected_outcome=("指标改善",),
        risk=RiskLevel.MEDIUM,
        confidence=Decimal(confidence),
    )


def _decision(
    candidate_id: str,
    decision_id: str,
    *,
    confidence: str = "0.9",
    value: str = "0.8",
) -> EvolutionDecision:
    """一条决策。``confidence`` 低 → defer；``value`` 低 → reject。"""
    candidate = _candidate(candidate_id, confidence=confidence)
    factors = PriorityFactors.from_candidate(
        candidate,
        value=Decimal(value),
        impact=Decimal("0.6"),
        urgency=Decimal("0.5"),
        cost=Decimal("0.5"),
    )
    scored = ScoredCandidate(candidate_id=candidate_id, factors=factors)
    return DecisionPolicy().decide(scored, decision_id=decision_id)


def _approved(decision: EvolutionDecision, *, reason: str = "审查通过") -> DecisionApproval:
    return DecisionApproval(
        decision_id=decision.id,
        candidate_id=decision.candidate_id,
        approved=True,
        reason=reason,
    )


def _rejected(decision: EvolutionDecision, *, reason: str = "Critic 否决") -> DecisionApproval:
    return DecisionApproval(
        decision_id=decision.id,
        candidate_id=decision.candidate_id,
        approved=False,
        reason=reason,
    )


def _draft(
    decision: EvolutionDecision, *, key: str | None = None, **overrides: object
) -> TaskDraft:
    """一份拆解草稿。"""
    defaults: dict[str, object] = {
        "key": key or f"T-{decision.id}",
        "decision_id": decision.id,
        "task_type": TaskType.FEATURE,
        "title": f"做 {decision.candidate_id} 这件事",
    }
    return TaskDraft.model_validate({**defaults, **overrides})


class TestEveryTaskTracesBackToADecision:
    """T1 ① —— 每个任务都能追溯到一个 EvolutionDecision。"""

    def test_every_planned_task_carries_its_decision_id(self) -> None:
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[_draft(decision)],
        )

        assert roadmap.total == 1
        assert roadmap.tasks[0].decision_id == "EVD-1"

    def test_the_decision_really_is_in_this_round(self) -> None:
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[_draft(decision)],
        )

        known = {item.id for item in (decision,)}
        assert all(task.decision_id in known for task in roadmap.tasks)
        assert roadmap.decision_ids == {"EVD-1"}

    def test_one_decision_may_produce_several_tasks(self) -> None:
        """一个候选说"重构并更新文档"，那是两件事、两种验收重点。"""
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[
                _draft(decision, key="T-refactor", task_type=TaskType.REFACTORING),
                _draft(decision, key="T-docs", task_type=TaskType.DOCS),
            ],
        )

        assert roadmap.total == 2
        assert {task.decision_id for task in roadmap.tasks} == {"EVD-1"}
        assert {task.key for task in roadmap.tasks} == {"T-refactor", "T-docs"}

    def test_a_draft_for_an_unknown_decision_is_refused(self) -> None:
        """引用不存在的决策是写错了 —— 报错，不猜。"""
        decision = _decision("C-1", "EVD-1")
        draft = _draft(decision, decision_id="EVD-不存在")

        with pytest.raises(UnknownDecisionError):
            TaskDecomposer().decompose(
                cycle_id="cycle-0001",
                decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
                approvals=[_approved(decision)],
                drafts=[draft],
            )


class TestTheDependencyGraphIsAcyclic:
    """T1 ② —— 依赖图无环。"""

    def test_a_cycle_is_refused(self) -> None:
        first = PlannedTask(
            key="T-1",
            decision_id="EVD-1",
            candidate_id="C-1",
            title="甲",
            task_type=TaskType.FEATURE,
        )
        second = PlannedTask(
            key="T-2",
            decision_id="EVD-2",
            candidate_id="C-2",
            title="乙",
            task_type=TaskType.FEATURE,
        )

        with pytest.raises(DependencyCycleError):
            EvolutionRoadmap(
                cycle_id="cycle-0001",
                tasks=(first, second),
                dependencies=(
                    TaskDependency(predecessor_key="T-1", successor_key="T-2"),
                    TaskDependency(predecessor_key="T-2", successor_key="T-1"),
                ),
            )

    def test_the_error_names_the_cycle(self) -> None:
        """报错要说得清是哪几个任务在互相等。"""
        first = PlannedTask(
            key="T-1",
            decision_id="EVD-1",
            candidate_id="C-1",
            title="甲",
            task_type=TaskType.FEATURE,
        )
        second = PlannedTask(
            key="T-2",
            decision_id="EVD-2",
            candidate_id="C-2",
            title="乙",
            task_type=TaskType.FEATURE,
        )

        with pytest.raises(DependencyCycleError) as caught:
            EvolutionRoadmap(
                cycle_id="cycle-0001",
                tasks=(first, second),
                dependencies=(
                    TaskDependency(predecessor_key="T-1", successor_key="T-2"),
                    TaskDependency(predecessor_key="T-2", successor_key="T-1"),
                ),
            )

        assert set(caught.value.cycle) == {"T-1", "T-2"}

    def test_a_task_cannot_depend_on_itself(self) -> None:
        with pytest.raises(ValueError, match="不能依赖自己"):
            TaskDependency(predecessor_key="T-1", successor_key="T-1")

    def test_a_normal_graph_is_accepted(self) -> None:
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[
                _draft(decision, key="T-a"),
                _draft(decision, key="T-b", depends_on=("T-a",)),
            ],
        )

        assert len(roadmap.dependencies) == 1
        assert roadmap.predecessors_of("T-b") == ("T-a",)
        assert roadmap.successors_of("T-a") == ("T-b",)

    def test_the_schedule_puts_the_predecessor_first(self) -> None:
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[
                _draft(decision, key="T-b", depends_on=("T-a",)),
                _draft(decision, key="T-a"),
            ],
        )

        assert roadmap.execution_order == ("T-a", "T-b")

    def test_tasks_without_a_dependency_between_them_share_a_wave(self) -> None:
        """同一波 = 可以并行开工。"""
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[
                _draft(decision, key="T-a"),
                _draft(decision, key="T-b"),
                _draft(decision, key="T-c", depends_on=("T-a", "T-b")),
            ],
        )

        waves = {slot.key: slot.wave for slot in roadmap.schedule}
        assert waves["T-a"] == waves["T-b"] == 1
        assert waves["T-c"] == 2

    def test_a_dependency_on_an_unknown_task_is_refused(self) -> None:
        decision = _decision("C-1", "EVD-1")

        with pytest.raises(UnknownDependencyError):
            TaskDecomposer().decompose(
                cycle_id="cycle-0001",
                decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
                approvals=[_approved(decision)],
                drafts=[_draft(decision, key="T-a", depends_on=("T-打错了",))],
            )

    def test_duplicate_task_keys_are_refused(self) -> None:
        decision = _decision("C-1", "EVD-1")

        with pytest.raises(DuplicateTaskKeyError):
            TaskDecomposer().decompose(
                cycle_id="cycle-0001",
                decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
                approvals=[_approved(decision)],
                drafts=[
                    _draft(decision, key="T-a"),
                    _draft(decision, key="T-a"),
                ],
            )


class TestTheSevenTaskTypes:
    """T1 ③ —— 七种任务类型均可构造。"""

    def test_there_are_exactly_seven(self) -> None:
        assert len(list(TaskType)) == 7

    @pytest.mark.parametrize("task_type", list(TaskType))
    def test_every_type_can_be_planned(self, task_type: TaskType) -> None:
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[_draft(decision, task_type=task_type)],
        )

        assert roadmap.total == 1
        assert roadmap.tasks[0].task_type is task_type

    @pytest.mark.parametrize("task_type", list(TaskType))
    def test_every_type_has_its_own_acceptance_focus(self, task_type: TaskType) -> None:
        """七种类型、七种验收重点 —— 不是同一套流程换个名字。"""
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[_draft(decision, task_type=task_type)],
        )

        assert roadmap.tasks[0].acceptance_focus == ACCEPTANCE_FOCUS[task_type]
        assert roadmap.tasks[0].acceptance_focus.strip()

    def test_the_refactoring_focus_is_about_behaviour(self) -> None:
        """design.md §3.2 的原话：重构强调行为保持和回归测试。"""
        assert "行为保持" in ACCEPTANCE_FOCUS[TaskType.REFACTORING]
        assert "回归测试" in ACCEPTANCE_FOCUS[TaskType.REFACTORING]

    def test_the_performance_focus_is_about_the_baseline(self) -> None:
        """design.md §3.2 的原话：性能优化比较优化前后的基准。"""
        assert "基准" in ACCEPTANCE_FOCUS[TaskType.PERFORMANCE]

    def test_the_bugfix_focus_covers_the_security_case(self) -> None:
        """design.md §3.2 提到安全修复要漏洞复测 —— 安全修复落在 bugfix 这一类。"""
        assert "漏洞复测" in ACCEPTANCE_FOCUS[TaskType.BUGFIX]

    def test_the_seven_types_are_the_ones_the_design_document_lists(self) -> None:
        assert {item.value for item in TaskType} == {
            "feature",
            "bugfix",
            "refactoring",
            "test",
            "performance",
            "dependency",
            "docs",
        }


class TestOnlyApprovedDecisionsProduceTasks:
    """T1 ④ —— 未被 Critic 放行的 Decision 不产生 Task。"""

    def test_a_decision_without_an_approval_produces_no_task(self) -> None:
        """**缺凭据与被否决一样**，都是不放行 —— 关不上就不开。"""
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[],
            drafts=[_draft(decision)],
        )

        assert roadmap.total == 0
        assert [item.decision_id for item in roadmap.skipped] == ["EVD-1"]

    def test_a_vetoed_decision_produces_no_task(self) -> None:
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_rejected(decision, reason="触碰 Non-goal NG-001")],
            drafts=[_draft(decision)],
        )

        assert roadmap.total == 0
        assert "NG-001" in roadmap.skipped[0].reason

    def test_a_deferred_decision_produces_no_task(self) -> None:
        """defer 是"这一轮不做" —— 它本来就该什么都不产生。"""
        decision = _decision("C-1", "EVD-1", confidence="0.1")
        assert decision.decision is DecisionState.DEFER

        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[_draft(decision)],
        )

        assert roadmap.total == 0
        assert "defer" in roadmap.skipped[0].reason

    def test_a_rejected_decision_produces_no_task(self) -> None:
        decision = _decision("C-1", "EVD-1", value="0.05")
        assert decision.decision is DecisionState.REJECT

        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[_draft(decision)],
        )

        assert roadmap.total == 0
        assert "reject" in roadmap.skipped[0].reason

    def test_an_approved_decision_does_produce_a_task(self) -> None:
        """对照组 —— 放行了就该拆。"""
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[_draft(decision)],
        )

        assert roadmap.total == 1
        assert roadmap.skipped == ()

    def test_the_skipped_decision_says_why(self) -> None:
        """不做也要说清楚为什么 —— 否则分不清"不值得"与"没看见"。"""
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[],
            drafts=[_draft(decision)],
        )

        assert roadmap.skipped[0].reason.strip()
        assert "放行" in roadmap.skipped[0].reason

    def test_conflicting_approvals_are_refused(self) -> None:
        """同一决策既放行又不放行 —— 取哪一份都是猜，而猜错的方向是放行。"""
        decision = _decision("C-1", "EVD-1")

        with pytest.raises(ConflictingApprovalError):
            TaskDecomposer().decompose(
                cycle_id="cycle-0001",
                decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
                approvals=[_approved(decision), _rejected(decision)],
                drafts=[_draft(decision)],
            )

    def test_a_rejection_without_a_reason_is_refused(self) -> None:
        """否决也要说清楚为什么 —— 否则读的人只看到"没做"。"""
        with pytest.raises(ValueError, match="理由"):
            DecisionApproval(
                decision_id="EVD-1",
                candidate_id="C-1",
                approved=False,
                reason="  ",
            )


class TestTheRoadmapItself:
    """路线图自身的那些不变量。"""

    def test_the_priority_defaults_to_the_decision_score(self) -> None:
        """优先级是决策那一层算出来的 —— 另起一套数就有两个口径。"""
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[_draft(decision)],
        )

        assert roadmap.tasks[0].priority == decision.score

    def test_an_explicit_priority_wins(self) -> None:
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[_draft(decision, priority=Decimal("0.25"))],
        )

        assert roadmap.tasks[0].priority == Decimal("0.25")

    def test_a_dropped_predecessor_detaches_the_edge(self) -> None:
        """前置任务不做了，后继还在等它就是死等 —— 但摘掉要留痕。"""
        kept = _decision("C-1", "EVD-1")
        dropped = _decision("C-2", "EVD-2")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(kept, dropped)),
            approvals=[_approved(kept), _rejected(dropped)],
            drafts=[
                _draft(kept, key="T-kept"),
                _draft(dropped, key="T-dropped"),
                _draft(kept, key="T-after", depends_on=("T-dropped",)),
            ],
        )

        assert {task.key for task in roadmap.tasks} == {"T-kept", "T-after"}
        assert roadmap.dependencies == ()
        assert len(roadmap.detached_dependencies) == 1
        assert roadmap.detached_dependencies[0].predecessor_key == "T-dropped"

    def test_the_roadmap_refuses_a_schedule_that_misses_a_task(self) -> None:
        task = PlannedTask(
            key="T-1",
            decision_id="EVD-1",
            candidate_id="C-1",
            title="甲",
            task_type=TaskType.FEATURE,
        )

        with pytest.raises(ValueError, match="排期"):
            EvolutionRoadmap(cycle_id="cycle-0001", tasks=(task,), schedule=())

    def test_the_roadmap_refuses_a_dependency_on_an_unknown_task(self) -> None:
        task = PlannedTask(
            key="T-1",
            decision_id="EVD-1",
            candidate_id="C-1",
            title="甲",
            task_type=TaskType.FEATURE,
        )

        with pytest.raises(UnknownDependencyError):
            EvolutionRoadmap(
                cycle_id="cycle-0001",
                tasks=(task,),
                dependencies=(TaskDependency(predecessor_key="T-1", successor_key="T-没有"),),
            )

    def test_the_roadmap_groups_tasks_by_type(self) -> None:
        decision = _decision("C-1", "EVD-1")
        roadmap = TaskDecomposer().decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[
                _draft(decision, key="T-a", task_type=TaskType.FEATURE),
                _draft(decision, key="T-b", task_type=TaskType.TEST),
            ],
        )

        assert set(roadmap.by_type) == {TaskType.FEATURE, TaskType.TEST}
        assert roadmap.by_type[TaskType.TEST][0].key == "T-b"

    def test_the_dependency_type_is_recorded(self) -> None:
        decision = _decision("C-1", "EVD-1")
        decomposer = TaskDecomposer(dependency_type=DependencyType.START_TO_START)
        roadmap = decomposer.decompose(
            cycle_id="cycle-0001",
            decision_round=DecisionRound(cycle_id="cycle-0001", decisions=(decision,)),
            approvals=[_approved(decision)],
            drafts=[
                _draft(decision, key="T-a"),
                _draft(decision, key="T-b", depends_on=("T-a",)),
            ],
        )

        assert roadmap.dependencies[0].dependency_type is DependencyType.START_TO_START
