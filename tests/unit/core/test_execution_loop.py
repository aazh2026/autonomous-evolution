"""循环 A 的领域契约测试（TASK-P1-060）。

设计依据：docs/design.md §4（"不能所有任务都走相同的验收流程"）、§6
（修复失败必须有次数、成本和时间上限）、docs/tasks.md TASK-P1-060。

**为什么领域层要单独测。** 图的用例验的是"整张图走得通"；但图能走通
**不等于契约是对的** —— 图只是调用方。真正被调用的是这里的规则：闸门在
什么条件下响、验收重点按什么填、走过的节点按什么记。这几条规则各有各的
坏法，而坏法在图的层面看不见。

**闸门被绕过的后果是隐性的。** 没有验收契约也能开工，图照样跑完 —— 只是
跑出来的东西没人能说清"做成"是什么意思。所以闸门要单独断一次。

**验收重点不填，所有任务就都走同一套验收流程** —— design.md §4 明写
"不能所有任务都走相同的验收流程"。重点由**任务类型**决定，不由任务自己
说；自己说的话，任务就会挑对自己有利的那个重点。

**走过的节点必须按顺序记，允许重复。** 记成集合，返修那一轮就看不出是
第二圈 —— 而"跑了几圈"恰恰是修复预算要盯的东西。
"""

from __future__ import annotations

from typing import Final

import pytest
from pydantic import ValidationError

from core.planning.roadmap import ACCEPTANCE_FOCUS, TaskType
from core.task.execution_loop import (
    LOOP_A_ORDER,
    ExecutionPlan,
    FailureCategory,
    FailureDiagnosis,
    LoopANode,
    LoopAState,
    VerificationOutcome,
    advance_task_state,
    record_node,
)
from core.task.outcome import VerificationReport, VerificationVerdict
from core.task.state_machine import (
    AcceptanceContractRequiredError,
    RepairBudgetExhaustedError,
    TaskAction,
    TaskState,
)


def _state(**overrides: object) -> LoopAState:
    """一份就绪的循环 A 状态。"""
    base = LoopAState(
        task_id="task-1",
        project_id="p-demo",
        agent_id="agent-1",
        contract_bound=True,
    )
    if not overrides:
        return base
    return base.model_copy(update=overrides)


class TestTheAcceptanceContractGate:
    """R-051：进入 RUNNING 前必须已绑定验收契约。"""

    def test_starting_without_a_contract_is_refused(self) -> None:
        state = _state(contract_bound=False)

        with pytest.raises(AcceptanceContractRequiredError):
            advance_task_state(state, TaskAction.START)

    def test_starting_with_a_contract_is_allowed(self) -> None:
        state = _state(contract_bound=True)

        moved = advance_task_state(state, TaskAction.START)

        assert moved.task_state is TaskState.RUNNING

    def test_the_task_stays_ready_when_the_gate_refuses(self) -> None:
        """被拒之后任务**仍在** READY —— 不是"移动到一半"。"""
        state = _state(contract_bound=False)

        with pytest.raises(AcceptanceContractRequiredError):
            advance_task_state(state, TaskAction.START)

        assert state.task_state is TaskState.READY

    def test_the_error_says_which_task_it_is_about(self) -> None:
        state = _state(task_id="task-42", contract_bound=False)

        with pytest.raises(AcceptanceContractRequiredError) as caught:
            advance_task_state(state, TaskAction.START)

        assert caught.value.task_id == "task-42"


class TestTheRepairBudgetGate:
    """§6：修复失败必须有次数上限。"""

    def test_repairing_below_the_limit_is_allowed(self) -> None:
        state = _state(task_state=TaskState.VERIFYING, repair_count=1, max_repairs=3)

        moved = advance_task_state(state, TaskAction.REPAIR)

        assert moved.task_state is TaskState.REPAIRING

    def test_repairing_at_the_limit_is_refused(self) -> None:
        """次数用尽就不再自动重试 —— 这是"有界"的含义。"""
        state = _state(task_state=TaskState.VERIFYING, repair_count=3, max_repairs=3)

        with pytest.raises(RepairBudgetExhaustedError):
            advance_task_state(state, TaskAction.REPAIR)

    def test_the_error_carries_the_numbers(self) -> None:
        """报错要带"用了几次、上限几次" —— 只说"超限了"没法排查。"""
        state = _state(task_state=TaskState.VERIFYING, repair_count=4, max_repairs=3)

        with pytest.raises(RepairBudgetExhaustedError) as caught:
            advance_task_state(state, TaskAction.REPAIR)

        assert caught.value.used == 4
        assert caught.value.limit == 3


class TestTheAcceptanceFocus:
    """验收重点由任务类型决定，不由任务自己说。"""

    @pytest.mark.parametrize("task_type", list(TaskType))
    def test_the_focus_comes_from_the_task_type(self, task_type: TaskType) -> None:
        plan = ExecutionPlan(task_id="task-1", instruction="做事", task_type=task_type)

        assert plan.acceptance_focus == ACCEPTANCE_FOCUS[task_type]

    def test_the_default_type_gets_its_own_focus(self) -> None:
        """不写任务类型就按默认类型（feature）走 —— 默认值也是一条规则。"""
        plan = ExecutionPlan(task_id="task-1", instruction="做事")

        assert plan.task_type is TaskType.FEATURE
        assert plan.acceptance_focus == ACCEPTANCE_FOCUS[TaskType.FEATURE]

    def test_an_explicit_focus_is_kept(self) -> None:
        """自己写了就认自己的 —— 但这属于"任务自己说"，只是允许。"""
        plan = ExecutionPlan(
            task_id="task-1",
            instruction="做事",
            task_type=TaskType.FEATURE,
            acceptance_focus="只看冒烟用例",
        )

        assert plan.acceptance_focus == "只看冒烟用例"

    def test_both_construction_paths_fill_the_same_way(self) -> None:
        """**两条构造路必须给出同一个结果。**

        ``mode="after"`` 的校验器在 ``__init__`` 那条路上会被**丢掉返回值**
        —— 只有 ``model_validate`` 认它。于是同一个模型因构造方式不同而有
        两副面孔：一半的调用方拿到的验收重点是空的。这一条盯的就是这个。
        """
        payload: dict[str, object] = {
            "task_id": "task-1",
            "instruction": "做事",
            "task_type": TaskType.REFACTORING,
        }

        built = ExecutionPlan(
            task_id="task-1",
            instruction="做事",
            task_type=TaskType.REFACTORING,
        )
        validated = ExecutionPlan.model_validate(payload)

        assert built.acceptance_focus == validated.acceptance_focus
        assert built.acceptance_focus == ACCEPTANCE_FOCUS[TaskType.REFACTORING]

    def test_a_non_string_focus_is_rejected(self) -> None:
        """类型不对就报错 —— 静默改掉一个类型错误的输入比报错更糟。"""
        with pytest.raises(ValidationError):
            ExecutionPlan(task_id="task-1", instruction="做事", acceptance_focus=123)  # type: ignore[arg-type]


class TestTheVerificationOutcome:
    """验证结论在状态里是 Pydantic 模型 —— 因为 checkpoint 只认这个。"""

    def test_the_round_trip_keeps_everything(self) -> None:
        report = VerificationReport(
            verdict=VerificationVerdict.FAILED,
            reasons=("用例没过",),
            checks=(("unit", False),),
        )

        outcome = VerificationOutcome.from_report(report)

        assert outcome.to_report() == report

    def test_passed_is_only_true_for_a_passed_verdict(self) -> None:
        assert VerificationOutcome(verdict=VerificationVerdict.PASSED).passed is True
        assert VerificationOutcome(verdict=VerificationVerdict.FAILED).passed is False

    def test_an_error_verdict_is_not_passed(self) -> None:
        """验证引擎**自己**出错 ≠ 验证通过 —— 没验过的东西不算验过了。"""
        assert VerificationOutcome(verdict=VerificationVerdict.ERROR).passed is False


class TestTheFailureCategory:
    """失败归类决定"能不能自己修"。"""

    def test_only_auto_repairable_is_auto_repairable(self) -> None:
        for category in FailureCategory:
            diagnosis = FailureDiagnosis(reason="坏了", category=category)
            assert diagnosis.auto_repairable is (category is FailureCategory.AUTO_REPAIRABLE)


class TestTheVisitedNodes:
    """走过的节点按顺序记，允许重复。"""

    def test_the_nodes_are_recorded_in_order(self) -> None:
        state = _state()

        moved = record_node(record_node(state, LoopANode.TASK_READY), LoopANode.TASK_PLANNER)

        assert moved.visited_nodes == (LoopANode.TASK_READY, LoopANode.TASK_PLANNER)

    def test_a_node_may_be_visited_twice(self) -> None:
        """返修那一轮会再走一遍 ``AGENT_EXECUTES`` —— 那是**顺序**，不是集合。"""
        state = _state()

        moved = record_node(record_node(state, LoopANode.AGENT_EXECUTES), LoopANode.AGENT_EXECUTES)

        assert moved.visited_nodes == (LoopANode.AGENT_EXECUTES, LoopANode.AGENT_EXECUTES)

    def test_recording_a_node_does_not_change_the_task_state(self) -> None:
        """记节点与推进任务状态是两件事 —— 混在一起就说不清了。"""
        state = _state()

        moved = record_node(state, LoopANode.TASK_READY)

        assert moved.task_state is state.task_state


class TestTheStateIsAValue:
    """状态是值，不是可变容器 —— 它要被写进 checkpoint。"""

    def test_the_state_is_frozen(self) -> None:
        state = _state()

        with pytest.raises(ValidationError):
            state.task_state = TaskState.RUNNING

    def test_unknown_fields_are_rejected(self) -> None:
        """多出来的字段说明模型与调用方对不上 —— 早报错比静默丢掉好。"""
        with pytest.raises(ValidationError):
            LoopAState(task_id="task-1", surprise=1)  # type: ignore[call-arg]

    def test_the_documented_order_is_the_declared_order(self) -> None:
        """节点枚举的声明顺序就是步骤的顺序 —— 两边不一致会转红。"""
        assert tuple(LoopANode) == LOOP_A_ORDER

    def test_the_order_has_fifteen_nodes(self) -> None:
        assert len(LOOP_A_ORDER) == 15


class TestTheAcceptedFocusIsNotInvented:
    """验收重点不能被编造。"""

    def test_every_task_type_has_a_focus(self) -> None:
        """七种任务类型都要有各自的验收重点 —— 缺一种就有一类任务没标准。"""
        assert set(ACCEPTANCE_FOCUS) == set(TaskType)

    def test_the_focuses_are_not_all_the_same(self) -> None:
        """全都一样就等于没有按类型区分。"""
        focuses: Final[set[str]] = set(ACCEPTANCE_FOCUS.values())
        assert len(focuses) == len(TaskType)
