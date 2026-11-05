"""TASK-P1-021 —— Task 状态机（14 态）。

验证：非法迁移全部抛错、合法路径走得通、重复迁移幂等，
以及 R-051 的验收契约准入闸门。
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from core.task.state_machine import (
    ALLOWED_ACTIONS,
    TERMINAL_STATES,
    TRANSITIONS,
    AcceptanceContractRequiredError,
    IllegalTransitionError,
    RepairBudgetExhaustedError,
    TaskAction,
    TaskState,
    TransitionContext,
    apply_action,
    can_apply,
    happy_path,
    legal_actions,
    target_of,
)

# 进入 RUNNING 需要验收契约；其余动作不受影响
OPEN_CONTEXT = TransitionContext(task_id="TASK-001", acceptance_contract_bound=True, max_repairs=3)


def _context(**overrides: object) -> TransitionContext:
    base = {
        "task_id": "TASK-001",
        "acceptance_contract_bound": True,
        "repair_count": 0,
        "max_repairs": 3,
    }
    base.update(overrides)
    return TransitionContext(**base)  # type: ignore[arg-type]


class TestStateSet:
    """14 个状态。"""

    def test_exactly_fourteen_states(self) -> None:
        assert len(TaskState) == 14

    def test_state_names_match_design_doc(self) -> None:
        assert {state.value for state in TaskState} == {
            "DISCOVERED",
            "EVALUATING",
            "DEFERRED",
            "APPROVED",
            "PLANNED",
            "READY",
            "RUNNING",
            "VERIFYING",
            "PASSED",
            "REPAIRING",
            "BLOCKED",
            "INTEGRATING",
            "MERGED",
            "COMPLETED",
        }

    def test_completed_is_terminal(self) -> None:
        assert TaskState.COMPLETED in TERMINAL_STATES
        assert len(TERMINAL_STATES) == 1
        assert legal_actions(TaskState.COMPLETED) == frozenset()


class TestIllegalTransitions:
    """① 遍历所有状态对，非法迁移全部抛错。"""

    def test_every_illegal_state_action_pair_raises(self) -> None:
        """穷举 14 状态 × 15 动作 = 210 个组合。

        对每个组合：动作不在该状态的允许集合里、且动作的目标态不是当前态
        （后者是幂等空操作，见 TestIdempotency）时，必须抛 IllegalTransitionError。
        """
        checked = 0
        for source in TaskState:
            for action in TaskAction:
                if action in ALLOWED_ACTIONS[source]:
                    continue
                if target_of(action) is source:
                    continue  # 幂等空操作
                checked += 1
                with pytest.raises(IllegalTransitionError) as exc_info:
                    apply_action(source, action, _context())
                assert exc_info.value.current is source
                assert exc_info.value.action is action

        assert checked > 0, "穷举没有覆盖到任何非法迁移，说明表有问题"

    def test_every_illegal_state_pair_raises(self) -> None:
        """穷举 14×14 = 196 个状态对。"""
        checked = 0
        for source in TaskState:
            for target in TaskState:
                if source is target:
                    continue
                if target in TRANSITIONS[source].values():
                    continue  # 合法迁移
                checked += 1
                with pytest.raises(IllegalTransitionError):
                    apply_action(source, _action_reaching(target, source))
        assert checked > 0

    def test_illegal_transition_error_lists_allowed_actions(self) -> None:
        with pytest.raises(IllegalTransitionError) as exc_info:
            apply_action(TaskState.DISCOVERED, TaskAction.MERGE)
        assert "evaluate" in exc_info.value.allowed
        assert TaskState.DISCOVERED.value in str(exc_info.value)

    def test_no_action_is_allowed_from_completed(self) -> None:
        """COMPLETED 是终态。除了幂等空操作外，任何动作都不该被接受。"""
        for action in TaskAction:
            if target_of(action) is TaskState.COMPLETED:
                continue
            with pytest.raises(IllegalTransitionError):
                apply_action(TaskState.COMPLETED, action)

    def test_cannot_skip_from_discovered_to_running(self) -> None:
        """不允许跳过评估、审批、计划直接执行。"""
        with pytest.raises(IllegalTransitionError):
            apply_action(TaskState.DISCOVERED, TaskAction.START)


class TestHappyPath:
    """② 合法路径 DISCOVERED → … → COMPLETED 可走通。"""

    def test_full_path_is_walkable(self) -> None:
        path = happy_path()
        current = path[0]
        for next_state in path[1:]:
            action = _action_reaching(next_state, source=current)
            result = apply_action(current, action, _context())
            assert result.changed is True
            assert result.current is next_state
            current = next_state
        assert current is TaskState.COMPLETED

    def test_every_state_is_reachable_from_discovered(self) -> None:
        """14 个状态都应从 DISCOVERED 可达 —— 不可达的状态是死代码。"""
        reachable = {TaskState.DISCOVERED}
        frontier = [TaskState.DISCOVERED]
        while frontier:
            source = frontier.pop()
            for target in TRANSITIONS[source].values():
                if target not in reachable:
                    reachable.add(target)
                    frontier.append(target)
        assert reachable == set(TaskState)

    def test_happy_path_states_are_all_connected(self) -> None:
        path = happy_path()
        for source, target in pairwise(path):
            assert target in TRANSITIONS[source].values()


class TestIdempotency:
    """③ 重复执行同一迁移幂等。"""

    def test_repeated_action_is_a_noop(self) -> None:
        """任务已在目标态时重复投递同一动作：不报错、不重复副作用。"""
        result = apply_action(TaskState.RUNNING, TaskAction.START, _context())
        assert result.changed is False
        assert result.current is TaskState.RUNNING
        assert result.is_idempotent_noop is True

    def test_repeated_action_keeps_previous_state(self) -> None:
        result = apply_action(TaskState.RUNNING, TaskAction.START, _context())
        assert result.previous is TaskState.RUNNING
        assert result.current is TaskState.RUNNING

    def test_noop_does_not_consume_repair_budget(self) -> None:
        """幂等空操作不应消耗修复次数 —— 否则重投会耗尽预算。"""
        result = apply_action(
            TaskState.REPAIRING,
            TaskAction.REPAIR,
            _context(repair_count=3, max_repairs=3),
        )
        assert result.changed is False

    def test_running_task_receiving_duplicate_start_stays_running(self) -> None:
        """消息重投的典型场景。"""
        first = apply_action(TaskState.READY, TaskAction.START, _context())
        assert first.current is TaskState.RUNNING
        second = apply_action(first.current, TaskAction.START, _context())
        assert second.current is TaskState.RUNNING
        assert second.changed is False


class TestAcceptanceContractGate:
    """R-051 —— 验收契约必须在 READY→RUNNING 之前存在并绑定 task_id。"""

    def test_cannot_start_without_acceptance_contract(self) -> None:
        with pytest.raises(AcceptanceContractRequiredError) as exc_info:
            apply_action(
                TaskState.READY,
                TaskAction.START,
                _context(acceptance_contract_bound=False),
            )
        assert exc_info.value.gate == "acceptance_contract"
        assert "TASK-001" in str(exc_info.value)

    def test_can_start_with_acceptance_contract(self) -> None:
        result = apply_action(TaskState.READY, TaskAction.START, _context())
        assert result.current is TaskState.RUNNING

    def test_gate_applies_only_to_running(self) -> None:
        """其它迁移不被验收契约卡住 —— 闸门只卡执行入口。"""
        result = apply_action(
            TaskState.PLANNED,
            TaskAction.READY,
            _context(acceptance_contract_bound=False),
        )
        assert result.current is TaskState.READY


class TestRepairBudget:
    """§6 —— 修复失败必须有次数上限。"""

    def test_repair_beyond_limit_is_rejected(self) -> None:
        with pytest.raises(RepairBudgetExhaustedError) as exc_info:
            apply_action(
                TaskState.VERIFYING,
                TaskAction.REPAIR,
                _context(repair_count=3, max_repairs=3),
            )
        assert exc_info.value.limit == 3
        assert exc_info.value.used == 3

    def test_repair_within_limit_is_allowed(self) -> None:
        result = apply_action(
            TaskState.VERIFYING,
            TaskAction.REPAIR,
            _context(repair_count=2, max_repairs=3),
        )
        assert result.current is TaskState.REPAIRING


class TestDeferAndReplan:
    """R-034/R-035 —— defer 后必须能重新评估；Re-plan 回到 PLANNED。"""

    def test_defer_then_reevaluate(self) -> None:
        deferred = apply_action(TaskState.EVALUATING, TaskAction.DEFER, _context())
        assert deferred.current is TaskState.DEFERRED

        reevaluated = apply_action(TaskState.DEFERRED, TaskAction.REEVALUATE, _context())
        assert reevaluated.current is TaskState.EVALUATING

    def test_replan_targets_planned(self) -> None:
        result = apply_action(TaskState.READY, TaskAction.REPLAN, _context())
        assert result.current is TaskState.PLANNED
        assert target_of(TaskAction.REPLAN) is TaskState.PLANNED

    def test_reevaluate_targets_evaluating(self) -> None:
        assert target_of(TaskAction.REEVALUATE) is TaskState.EVALUATING

    def test_blocked_task_can_be_rescheduled(self) -> None:
        """被阻塞的任务要么重新评估，要么重规划 —— 不能卡死。"""
        assert can_apply(TaskState.BLOCKED, TaskAction.REEVALUATE)
        assert can_apply(TaskState.BLOCKED, TaskAction.REPLAN)


class TestTableIntegrity:
    """迁移表自身的完整性。"""

    def test_every_state_has_an_entry(self) -> None:
        assert set(TRANSITIONS) == set(TaskState)
        assert set(ALLOWED_ACTIONS) == set(TaskState)

    def test_no_action_maps_to_a_missing_state(self) -> None:
        for source, actions in TRANSITIONS.items():
            for action, target in actions.items():
                assert target in TaskState, f"{source}.{action} 指向了不存在的状态 {target}"

    def test_all_states_are_terminal_or_have_an_exit(self) -> None:
        """除终态外，每个状态都要有出路 —— 否则任务会卡在中间态。"""
        for state in TaskState:
            if state is TaskState.COMPLETED:
                continue
            assert TRANSITIONS[state], f"{state.value} 没有任何出边"


def _action_reaching(target: TaskState, source: TaskState) -> TaskAction:
    """找出能到达 ``target`` 的动作。

    穷举非法迁移时需要"一个本该非法、但语法上存在"的动作。

    ``DISCOVERED`` 是初始态，没有任何动作指向它 —— 这种目标态只能退而
    选一个"目标不是 source 且在 source 下非法"的动作来验证会抛错。
    """
    candidates = [action for action in TaskAction if target_of(action) is target]
    if candidates:
        return candidates[0]
    fallback = [
        action
        for action in TaskAction
        if target_of(action) is not source and action not in ALLOWED_ACTIONS[source]
    ]
    assert fallback, f"找不到可用于验证 {source.value} → {target.value} 的动作"
    return fallback[0]
