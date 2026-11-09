"""循环 B 的领域契约测试（TASK-P1-061）。

设计依据：docs/tasks.md TASK-P1-061（T1 ①~④ 的领域侧）、docs/design.md §4
（节点链、六种触发源）、§3.3（候选必须有证据）、§12（每一轮都要有可验证的
改善）。

**这一层验的是"规则本身"，不是"图跑起来了"。** 图跑起来了是编排层的事
（``tests/unit/orchestration/test_loop_b.py``）；这里验的是那些**换了实现也
必须成立**的规则：证据收拢的账要平、排期不许漏人、结局必须是判定出来的。

**为什么"证据收拢"要单独一组。** 收拢的产物是**一份清单**，而清单最容易出的
问题是"少了一项" —— 而且少的那一项不会报错：候选被悄悄丢掉，读的人只看到
"这轮没发现什么"。所以这里盯的是 :attr:`EvidenceBundle.accounted_for`：
**每一个候选都要有说法**。

**为什么"没有证据的候选"要用 ``model_construct`` 造。** 候选模型本身要求
"至少一条证据"（R-029），正常构造根本造不出一个没有证据的候选。那这道
防线守的是谁？守的是**绕过校验的那条路**：``model_construct``、反序列化、
或者哪天有人放宽了那条约束。这道防线在"输入不合法"时必须仍然给出一个
**说得清**的答案 —— 而不是崩掉，也不是装作没看见。

**为什么"结局"不能由调用方填。** ``classify_cycle`` 只看三样东西：周期怎么
结束的、完成了几个任务、前后达成度。它不看"我觉得这轮还不错" —— 那是
"某个人声称的结果"，不是判定结果。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.evolution.candidate import DiscoveryResult, EvolutionCandidate
from core.evolution.decision import DecisionPolicy, DecisionRound
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.evolution_loop import (
    LOOP_B_ORDER,
    TRIGGER_LABELS,
    CycleExecution,
    LoopBNode,
    LoopBState,
    ProjectObservation,
    ResourceAssignment,
    ResourceSchedule,
    ScheduleError,
    SnapshotView,
    TaskExecution,
    TriggerSource,
    check_resource_schedule,
    empty_cycle_outcome,
    evaluate_outcome,
    gather_evidence,
    record_node,
)
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from core.metrics.cycle import CycleOutcome, CycleStatus
from core.planning.roadmap import (
    EvolutionRoadmap,
    PlannedTask,
    ScheduleSlot,
    TaskDependency,
    TaskType,
)
from core.task.outcome import TaskOutcome

CANDIDATE_ID = "CAND-0001"
CANDIDATE_ID_2 = "CAND-0002"


def _candidate(
    candidate_id: str = CANDIDATE_ID,
    *,
    refs: tuple[str, ...] = ("issue:ISSUE-1",),
) -> EvolutionCandidate:
    """一份带证据的候选。"""
    return EvolutionCandidate(
        id=candidate_id,
        category=OpportunityCategory.ENGINEERING,
        title="把落库口补齐",
        evidence=tuple(
            Evidence(kind=EvidenceKind.ISSUE, ref=ref, note="落库口缺失") for ref in refs
        ),
        hypothesis="补上落库口之后，这一轮的产出就能被查到",
        expected_outcome=("这一轮的候选与决策能在库里查到",),
        risk=RiskLevel.LOW,
        confidence=Decimal("0.8"),
    )


def _evidence_less_candidate(candidate_id: str = CANDIDATE_ID_2) -> EvolutionCandidate:
    """一个**没有证据**的候选 —— 只能绕过校验才造得出来。

    **为什么要专门造一个非法候选。** 候选模型要求"至少一条证据"（R-029），
    所以这条防线守的不是正常路径，而是"有人绕过校验"的那条路 —— 而那条路
    一旦存在，收拢证据的那一步就必须仍然给出一个说得清的答案。
    """
    return EvolutionCandidate.model_construct(
        id=candidate_id,
        category=OpportunityCategory.ENGINEERING,
        title="我觉得该重构一下",
        evidence=(),
        hypothesis="重构之后代码会更好读",
        expected_outcome=("代码更好读",),
        risk=RiskLevel.LOW,
        confidence=Decimal("0.5"),
    )


def _scored(
    candidate_id: str = CANDIDATE_ID,
    *,
    value: Decimal = Decimal("0.7"),
) -> ScoredCandidate:
    """一份评分。"""
    return ScoredCandidate(
        candidate_id=candidate_id,
        category=OpportunityCategory.ENGINEERING,
        factors=PriorityFactors(
            value=value,
            impact=Decimal("0.6"),
            confidence=Decimal("0.8"),
            urgency=Decimal("0.4"),
            cost=Decimal("0.3"),
            risk=Decimal("0.2"),
        ),
    )


def _recorded_round(candidate_id: str = CANDIDATE_ID) -> DecisionRound:
    """一轮只有一条 record 决策的决策轮。"""
    decision = DecisionPolicy().decide(_scored(candidate_id), decision_id="EVD-1")
    return DecisionRound(cycle_id="cycle-0007", decisions=(decision,))


def _roadmap(
    *,
    candidate_id: str = CANDIDATE_ID,
    keys: tuple[str, ...] = ("T-1",),
    dependencies: tuple[TaskDependency, ...] = (),
) -> EvolutionRoadmap:
    """一份路线图。"""
    tasks = tuple(
        PlannedTask(
            key=key,
            decision_id="EVD-1",
            candidate_id=candidate_id,
            title=f"任务 {key}",
            task_type=TaskType.FEATURE,
        )
        for key in keys
    )
    return EvolutionRoadmap(
        cycle_id="cycle-0007",
        tasks=tasks,
        dependencies=dependencies,
        schedule=tuple(
            ScheduleSlot(position=index, key=key, wave=index)
            for index, key in enumerate(keys, start=1)
        ),
    )


def _observation(
    *,
    attainment: Decimal | None = Decimal("0.7"),
    previous: Decimal | None = Decimal("0.6"),
) -> ProjectObservation:
    return ProjectObservation(
        project_id="p-demo",
        commit="c" * 40,
        branch="main",
        goal="让项目持续演进",
        attainment=attainment,
        previous_attainment=previous,
        latest_snapshot_version=3,
    )


def _snapshot(*, attainment: Decimal | None = Decimal("0.7")) -> SnapshotView:
    return SnapshotView(
        project_id="p-demo",
        version=4,
        commit="c" * 40,
        branch="main",
        attainment=attainment,
    )


class TestTheNodeChain:
    """节点链 —— 顺序就是设计文档 §4 的顺序。"""

    def test_the_order_matches_the_enum(self) -> None:
        """枚举的**声明顺序**与 :data:`LOOP_B_ORDER` 一致。

        两者不一致时，说明有人改了其中一处而没改另一处 —— 而"步骤的顺序"
        是这件事的全部内容。
        """
        assert tuple(LoopBNode) == LOOP_B_ORDER

    def test_the_chain_has_fifteen_nodes(self) -> None:
        """**数目也要对。** 少一个节点就是少做了一步，而"少做一步"在图上
        看上去仍然是完整的。"""
        assert len(LOOP_B_ORDER) == 15

    def test_the_branch_node_is_in_the_chain(self) -> None:
        """分支点（``WORTH_DOING``）必须在链上 —— 它是"这一轮做不做事"的
        分岔口。"""
        assert LoopBNode.WORTH_DOING in LOOP_B_ORDER

    def test_the_decision_node_comes_after_the_branch(self) -> None:
        """**决策记录在分支之后。** 先记下"我们决定做什么"，再决定要不要
        继续往下 —— 顺序反过来的话，"不做"那一轮就没留下任何决策。"""
        assert LOOP_B_ORDER.index(LoopBNode.RECORD_DEFER_REJECT) > LOOP_B_ORDER.index(
            LoopBNode.WORTH_DOING
        )

    def test_every_trigger_has_a_label(self) -> None:
        """六种触发源都要有中文名 —— "这一轮是被什么触发的"这句话得说得
        出画面，而不是六个英文单词。"""
        assert set(TRIGGER_LABELS) == set(TriggerSource)
        assert len(TriggerSource) == 6

    def test_a_trigger_label_is_not_blank(self) -> None:
        for label in TRIGGER_LABELS.values():
            assert label.strip()


class TestGatherEvidence:
    """证据收拢 —— 每一个候选都要有说法。"""

    def test_a_candidate_with_evidence_is_kept(self) -> None:
        bundle = gather_evidence(DiscoveryResult(candidates=(_candidate(),)))

        assert [item.candidate_id for item in bundle.kept] == [CANDIDATE_ID]
        assert bundle.kept[0].refs == ("issue:ISSUE-1",)

    def test_a_candidate_without_evidence_is_dropped_with_a_reason(self) -> None:
        """**没有证据的候选不是候选，是猜测。** 它连"值不值得做"都没资格
        参与判断。"""
        bundle = gather_evidence(DiscoveryResult(candidates=(_evidence_less_candidate(),)))

        assert bundle.kept == ()
        assert [item.candidate_id for item in bundle.dropped] == [CANDIDATE_ID_2]
        assert bundle.dropped[0].reason

    def test_every_candidate_is_accounted_for(self) -> None:
        """**谁都不许凭空消失。** 悄悄丢掉的候选，事后看着就是"这轮没发现
        什么"，而实际上它被发现过。"""
        bundle = gather_evidence(
            DiscoveryResult(candidates=(_candidate(), _evidence_less_candidate()))
        )

        assert bundle.accounted_for == {CANDIDATE_ID, CANDIDATE_ID_2}

    def test_the_same_reference_is_counted_once(self) -> None:
        """**引用去重。** 同一份证据被数两次，读的人会以为有两份独立的
        佐证。"""
        bundle = gather_evidence(
            DiscoveryResult(candidates=(_candidate(refs=("issue:ISSUE-1", "issue:ISSUE-1")),))
        )

        assert bundle.kept[0].refs == ("issue:ISSUE-1",)
        assert bundle.refs == ("issue:ISSUE-1",)

    def test_the_original_order_of_references_is_kept(self) -> None:
        """**去重，但保留次序。** 证据的强弱有别 —— 第一条往往是直接指向
        问题的那一条。"""
        bundle = gather_evidence(
            DiscoveryResult(candidates=(_candidate(refs=("issue:ISSUE-2", "issue:ISSUE-1")),))
        )

        assert bundle.kept[0].refs == ("issue:ISSUE-2", "issue:ISSUE-1")

    def test_the_bundle_refs_are_sorted_and_deduplicated(self) -> None:
        """汇总清单是**排序**的 —— 两轮跑出来的清单可以直接比。"""
        bundle = gather_evidence(
            DiscoveryResult(
                candidates=(
                    _candidate(refs=("issue:ISSUE-2",)),
                    _candidate(CANDIDATE_ID_2, refs=("issue:ISSUE-1", "issue:ISSUE-2")),
                )
            )
        )

        assert bundle.refs == ("issue:ISSUE-1", "issue:ISSUE-2")


class TestCheckResourceSchedule:
    """排期 —— 派工必须覆盖每一个任务，且不得让后继早于前驱。"""

    def test_a_complete_schedule_is_accepted(self) -> None:
        roadmap = _roadmap(keys=("T-1", "T-2"))
        schedule = ResourceSchedule(
            assignments=(
                ResourceAssignment(task_key="T-1", wave=1),
                ResourceAssignment(task_key="T-2", wave=2),
            ),
            budget=Decimal("10"),
        )

        check_resource_schedule(roadmap, schedule)

    def test_a_task_without_an_assignment_is_refused(self) -> None:
        """**少派一个任务，那个任务就没人做** —— 而排期表看上去仍然完整。"""
        roadmap = _roadmap(keys=("T-1", "T-2"))
        schedule = ResourceSchedule(
            assignments=(ResourceAssignment(task_key="T-1", wave=1),),
            budget=Decimal("10"),
        )

        with pytest.raises(ScheduleError, match="没有派工"):
            check_resource_schedule(roadmap, schedule)

    def test_an_assignment_for_an_unknown_task_is_refused(self) -> None:
        """派了路线图上没有的任务 —— 那条派工挂不到任何东西上。"""
        roadmap = _roadmap(keys=("T-1",))
        schedule = ResourceSchedule(
            assignments=(
                ResourceAssignment(task_key="T-1", wave=1),
                ResourceAssignment(task_key="T-GHOST", wave=1),
            ),
            budget=Decimal("10"),
        )

        with pytest.raises(ScheduleError, match="路线图上没有的任务"):
            check_resource_schedule(roadmap, schedule)

    def test_a_successor_in_the_same_wave_is_refused(self) -> None:
        """**后继不得早于前驱。** 同波开工等于在一件还没做完的事上动土，
        而且**不会报错** —— 每一件任务都看起来正常地做完了。"""
        roadmap = _roadmap(
            keys=("T-1", "T-2"),
            dependencies=(TaskDependency(predecessor_key="T-1", successor_key="T-2"),),
        )
        schedule = ResourceSchedule(
            assignments=(
                ResourceAssignment(task_key="T-1", wave=1),
                ResourceAssignment(task_key="T-2", wave=1),
            ),
            budget=Decimal("10"),
        )

        with pytest.raises(ScheduleError, match="不能早于它的前置"):
            check_resource_schedule(roadmap, schedule)


class TestEvaluateOutcome:
    """实际结果 —— 当初的判断准不准。"""

    def _state(self, **overrides: object) -> LoopBState:
        base = LoopBState(
            project_id="p-demo",
            cycle_id="cycle-0007",
            observation=_observation(),
            snapshot=_snapshot(),
            decisions=_recorded_round(),
            roadmap=_roadmap(),
            execution=CycleExecution(
                tasks=(
                    TaskExecution(
                        task_key="T-1",
                        task_id="task-row-1",
                        accepted=True,
                        outcome=TaskOutcome.COMPLETED,
                    ),
                )
            ),
        )
        return base.model_copy(update=overrides) if overrides else base

    def test_the_realized_value_is_the_passed_ratio(self) -> None:
        """**实际收益必须是一个数。** "做完了，感觉不错"没法用来校准任何
        东西 —— 收益要落在 0~1，与当初的 ``V`` 同一量纲。"""
        report = evaluate_outcome(self._state())

        assert len(report.outcomes) == 1
        assert report.outcomes[0].candidate_id == CANDIDATE_ID
        assert report.outcomes[0].realized_value == Decimal(1)

    def test_a_half_passed_candidate_gets_half_the_value(self) -> None:
        """一个候选派生出两个任务、只有一个过了 —— 拿到的是一半。"""
        roadmap = _roadmap(keys=("T-1", "T-2"))
        execution = CycleExecution(
            tasks=(
                TaskExecution(task_key="T-1", task_id="r1", accepted=True),
                TaskExecution(task_key="T-2", task_id="r2", accepted=False),
            )
        )

        report = evaluate_outcome(self._state(roadmap=roadmap, execution=execution))

        assert report.outcomes[0].realized_value == Decimal("0.5")

    def test_the_verdict_is_improved_when_attainment_rises(self) -> None:
        report = evaluate_outcome(self._state())

        assert report.verdict is not None
        assert report.verdict.outcome is CycleOutcome.IMPROVED

    def test_the_verdict_is_regressed_when_attainment_falls(self) -> None:
        report = evaluate_outcome(self._state(snapshot=_snapshot(attainment=Decimal("0.5"))))

        assert report.verdict is not None
        assert report.verdict.outcome is CycleOutcome.REGRESSED

    def test_a_missing_baseline_is_no_change_not_a_fabricated_delta(self) -> None:
        """**缺数据不是零。** 没有可比的前值时，算不出差值 —— 那就只能说
        "没有可验证的改善"，不能说"达成度没变"。"""
        report = evaluate_outcome(self._state(observation=_observation(previous=None)))

        assert report.verdict is not None
        assert report.verdict.outcome is CycleOutcome.NO_CHANGE
        assert report.attainment_before is None

    def test_a_task_that_is_not_in_the_roadmap_is_refused(self) -> None:
        """执行结果里有路线图上没有的任务 —— 那份结果挂不到任何东西上。"""
        execution = CycleExecution(
            tasks=(TaskExecution(task_key="T-GHOST", task_id="r1", accepted=True),)
        )

        with pytest.raises(ScheduleError, match="路线图上没有的任务"):
            evaluate_outcome(self._state(execution=execution))

    def test_evaluating_without_the_inputs_is_refused(self) -> None:
        """**缺输入就说缺输入。** 缺了执行结果却给出"这轮的实际收益"，
        等于凭空编一个数出来。"""
        state = self._state()
        empty = state.model_copy(update={"execution": None})

        with pytest.raises(ScheduleError):
            evaluate_outcome(empty)


class TestSnapshotView:
    """快照 —— 观察的定格。"""

    def test_the_version_is_the_latest_plus_one(self) -> None:
        """**版本号在项目内单调递增。** 重复的版本号会当场炸出来，而不是
        悄悄覆盖掉上一份快照。"""
        snapshot = SnapshotView.from_observation(_observation())

        assert snapshot.version == 4

    def test_the_first_snapshot_is_version_one(self) -> None:
        observation = _observation()
        fresh = observation.model_copy(update={"latest_snapshot_version": 0})

        assert SnapshotView.from_observation(fresh).version == 1

    def test_the_observation_is_copied_not_shared(self) -> None:
        """快照是**定格** —— 之后观察到的度量变了，不该反过来改掉已经定格的
        那一份。"""
        observation = _observation()
        snapshot = SnapshotView.from_observation(observation)

        assert snapshot.metrics is not observation.metrics
        assert snapshot.commit == observation.commit


class TestEmptyCycleOutcome:
    """一轮"什么都没做"的结局。"""

    def _state(self, **overrides: object) -> LoopBState:
        base = LoopBState(
            project_id="p-demo",
            cycle_id="cycle-0007",
            observation=_observation(),
            snapshot=_snapshot(),
            discovery=DiscoveryResult(candidates=(_candidate(),)),
            decisions=_recorded_round(),
        )
        return base.model_copy(update=overrides) if overrides else base

    def test_the_verdict_is_stalled(self) -> None:
        """**"什么都没落地"是一条结论**（stalled），不是"没有结论"。"""
        report = empty_cycle_outcome(self._state())

        assert report.verdict is not None
        assert report.verdict.outcome is CycleOutcome.STALLED

    def test_there_is_no_realized_value(self) -> None:
        """没有执行就没有实际收益 —— 这一栏该是空的，而不是 0。"""
        report = empty_cycle_outcome(self._state())

        assert report.outcomes == ()

    def test_the_candidates_and_decisions_are_still_counted(self) -> None:
        """**那一轮发现了什么、决定了什么，都是真实发生的事。** 把前两项
        也清零，周期记录就会少掉这一轮唯一说清的东西。"""
        report = empty_cycle_outcome(self._state())

        assert report.verdict is not None
        assert "没有任何任务完成" in report.verdict.reason

    def test_the_attainment_is_still_carried(self) -> None:
        """前后达成度照样要带着 —— 那是"这一轮之后项目变成什么样了"。"""
        report = empty_cycle_outcome(self._state())

        assert report.attainment_before == Decimal("0.6")
        assert report.attainment_after == Decimal("0.7")


class TestRecordNode:
    """走过的节点 —— 是次序，不是集合。"""

    def test_the_node_is_appended(self) -> None:
        state = LoopBState(project_id="p-demo", cycle_id="cycle-0007")

        marked = record_node(state, LoopBNode.OBSERVE_PROJECT)

        assert marked.visited_nodes == (LoopBNode.OBSERVE_PROJECT,)

    def test_a_repeated_node_is_kept(self) -> None:
        """**节点可以重复出现。** "这一轮被触发了几次"这件事要看得出差别 ——
        所以这是"走过的顺序"，不是"走过的集合"。"""
        state = LoopBState(project_id="p-demo", cycle_id="cycle-0007")

        once = record_node(state, LoopBNode.OBSERVE_PROJECT)
        twice = record_node(once, LoopBNode.OBSERVE_PROJECT)

        assert twice.visited_nodes == (LoopBNode.OBSERVE_PROJECT, LoopBNode.OBSERVE_PROJECT)

    def test_the_state_is_not_mutated(self) -> None:
        """**状态是不可变的。** 改回去的那份是另一个对象 —— 不然"记录走过的
        节点"就变成了"改写状态"。"""
        state = LoopBState(project_id="p-demo", cycle_id="cycle-0007")

        record_node(state, LoopBNode.OBSERVE_PROJECT)

        assert state.visited_nodes == ()


class TestTheStateShape:
    """状态本身的形状 —— 它要被写进 checkpoint。"""

    def test_the_status_defaults_to_completed(self) -> None:
        state = LoopBState(project_id="p-demo", cycle_id="cycle-0007")

        assert state.status is CycleStatus.COMPLETED

    def test_the_state_carries_no_executor(self) -> None:
        """**状态里没有执行器、没有会话、没有 diff。** 那些是循环 A 的东西。

        这里断言的是"字段名里没有那些东西" —— 不是"运行时没塞进去"。
        状态模型是 ``extra="forbid"`` 的，塞不进去。
        """
        fields = set(LoopBState.model_fields)

        assert "executor" not in fields
        assert "executors" not in fields
        assert "session_id" not in fields
        assert "diff" not in fields

    def test_the_state_has_a_cycle_id(self) -> None:
        """**每一轮都要说得清是哪一轮。** 没有编号的话，"上一轮为什么没动"
        就无从查起。"""
        state = LoopBState(project_id="p-demo", cycle_id="cycle-0007")

        assert state.cycle_id == "cycle-0007"
