"""TASK-P2-015 —— 定向修复的编排：分类完了，还得问全局账本。

设计依据：docs/tasks.md TASK-P2-015（"自动定位失败原因并生成 Targeted
修复指令，**修复次数与预算严格受全局配额限制**"）、TASK-P1-063（三重上限
全局共享）、R-098、I-04。

**这个文件钉住的是"配额"那半句。** 分类归得好不好，看分类那半边；这一半
要看的是：**修复次数与预算是不是真的被全局配额卡住了**。

三组用例：

1. **定向说明要传下去**（``TestTheTargetedInstructionReachesTheTask``）——
   分类器说的"改这三行"要真的出现在排给 Agent 的施工说明里。
2. **配额是全局的**（``TestTheQuotaIsGlobal``）—— 两个任务各修一次，
   上限是 1，那么第二个必须被拒：**上限说的是 1，不是"每个任务 1"**。
3. **不能自动修的不占额度**（``TestTheUnrepairableFailureCostsNothing``）——
   没修就占一格，等于凭空少了一截却没人真的修过。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agents.diagnosis.failure_classifier import (
    FailureType,
    ToolFailure,
    ToolFinding,
    ToolOutput,
    classify,
    tool_failure,
)
from core.task.repair import FailureKind, InMemoryRepairLedger, RepairLimit, RepairLimits
from core.task.state_machine import TaskState
from orchestration.repair import RepairCoordinator, RepairRequest
from orchestration.targeted_repair import TargetedRepairPlanner

_PROJECT = "proj-1"

#: ruff 报的一个未使用的导入。
_RUFF_JSON = """
[
  {
    "code": "F401",
    "message": "`os` imported but unused",
    "filename": "src/pkg/thing.py",
    "location": {"row": 1, "column": 8}
  }
]
"""


def _coordinator(*, max_attempts: int = 1) -> RepairCoordinator:
    return RepairCoordinator(
        InMemoryRepairLedger(),
        limits=RepairLimits(max_attempts=max_attempts),
    )


def _request(task_id: str) -> RepairRequest:
    return RepairRequest(
        task_id=task_id,
        project_id=_PROJECT,
        current_state=TaskState.VERIFYING,
        cost=Decimal("1.00"),
    )


def _lint_failure() -> ToolFailure:
    return tool_failure(
        kind=FailureKind.CHECK_FAILED, tool="ruff", output=ToolOutput(_RUFF_JSON, 1)
    )


class TestTheTargetedInstructionReachesTheTask:
    """分类器说的话要真的排给 Agent。"""

    async def test_the_task_carries_the_targeted_instruction(self) -> None:
        planner = TargetedRepairPlanner(_coordinator())

        plan = await planner.plan(_request("T-1"), failure=_lint_failure())

        assert plan.allowed is True
        assert plan.instruction != ""
        assert "src/pkg/thing.py:1" in plan.instruction
        assert "F401" in plan.instruction

    async def test_the_targets_are_kept_on_the_plan(self) -> None:
        """被拒时也要说得清改哪儿 —— 挂起也要有人能接手。"""
        planner = TargetedRepairPlanner(_coordinator())

        plan = await planner.plan(_request("T-1"), failure=_lint_failure())

        assert plan.targets[0].file == "src/pkg/thing.py"
        assert plan.targets[0].line == 1

    async def test_the_classification_is_visible_on_the_plan(self) -> None:
        planner = TargetedRepairPlanner(_coordinator())

        plan = await planner.plan(_request("T-1"), failure=_lint_failure())

        assert plan.classification.failure_type is FailureType.LINT_VIOLATION


class TestTheQuotaIsGlobal:
    """上限是全局共享的那一份。"""

    async def test_the_second_task_is_refused(self) -> None:
        """两个任务各修一次、上限是 1 —— 第二个必须被拒。"""
        planner = TargetedRepairPlanner(_coordinator(max_attempts=1))

        first = await planner.plan(_request("T-1"), failure=_lint_failure())
        second = await planner.plan(_request("T-2"), failure=_lint_failure())

        assert first.allowed is True
        assert second.allowed is False

    async def test_the_refusal_names_the_limit(self) -> None:
        """ "修复停了"没法行动；"次数超了"才知道该提配额还是认账。"""
        planner = TargetedRepairPlanner(_coordinator(max_attempts=1))
        await planner.plan(_request("T-1"), failure=_lint_failure())

        second = await planner.plan(_request("T-2"), failure=_lint_failure())

        assert second.outcome.limit is RepairLimit.COUNT

    async def test_a_refused_plan_has_no_repair_task(self) -> None:
        """被拒了还排任务 —— 那就等于修了两遍。"""
        planner = TargetedRepairPlanner(_coordinator(max_attempts=1))
        await planner.plan(_request("T-1"), failure=_lint_failure())

        second = await planner.plan(_request("T-2"), failure=_lint_failure())

        assert second.outcome.repair_task is None
        assert second.instruction == ""

    async def test_another_project_has_its_own_quota(self) -> None:
        """全局是"按项目"的 —— 一个项目的额度不该被另一个用掉。"""
        planner = TargetedRepairPlanner(_coordinator(max_attempts=1))
        await planner.plan(_request("T-1"), failure=_lint_failure())

        other = RepairRequest(
            task_id="T-3",
            project_id="proj-2",
            current_state=TaskState.VERIFYING,
            cost=Decimal("1.00"),
        )
        plan = await planner.plan(other, failure=_lint_failure())

        assert plan.allowed is True

    async def test_the_cost_limit_is_enforced_too(self) -> None:
        """次数不多但每次很贵，同样会拖垮项目。"""
        coordinator = RepairCoordinator(
            InMemoryRepairLedger(),
            limits=RepairLimits(max_attempts=5, max_cost=Decimal("1.00")),
        )
        planner = TargetedRepairPlanner(coordinator)

        first = await planner.plan(_request("T-1"), failure=_lint_failure())
        second = await planner.plan(_request("T-2"), failure=_lint_failure())

        assert first.allowed is True
        assert second.allowed is False
        assert second.outcome.limit is RepairLimit.COST


class TestTheUnrepairableFailureCostsNothing:
    """不能自动修的压根不占额度。"""

    async def test_a_cancelled_task_is_refused(self) -> None:
        planner = TargetedRepairPlanner(_coordinator(max_attempts=1))

        plan = await planner.plan(
            _request("T-1"),
            failure=ToolFailure(kind=FailureKind.CANCELLED),
        )

        assert plan.allowed is False
        assert plan.outcome.repair_task is None

    async def test_the_quota_is_still_intact_afterwards(self) -> None:
        """没修就占一格，等于凭空少了一截却没人真的修过。"""
        planner = TargetedRepairPlanner(_coordinator(max_attempts=1))
        await planner.plan(_request("T-1"), failure=ToolFailure(kind=FailureKind.CANCELLED))

        plan = await planner.plan(_request("T-2"), failure=_lint_failure())

        assert plan.allowed is True

    async def test_an_unrepairable_failure_cannot_reach_the_agent(self) -> None:
        """前提不成立 —— 再修也是白修。"""
        planner = TargetedRepairPlanner(_coordinator())

        plan = await planner.plan(
            _request("T-1"),
            failure=ToolFailure(kind=FailureKind.SPEC_CONFLICT),
        )

        assert plan.allowed is False
        assert plan.instruction == ""


class TestTheUnreadableFailureIsNotRepairedBlindly:
    """读不出来就不猜 —— 也不照着猜出来的类别去修。"""

    async def test_a_garbled_failure_needs_a_human(self) -> None:
        planner = TargetedRepairPlanner(_coordinator())
        failure = tool_failure(
            kind=FailureKind.UNKNOWN, tool="ruff", output=ToolOutput("not json", 1)
        )

        plan = await planner.plan(_request("T-1"), failure=failure)

        assert plan.classification.failure_type is FailureType.UNKNOWN
        assert plan.allowed is False

    async def test_the_check_level_failure_is_still_repairable(self) -> None:
        """知道**哪几项**没过就够排一次修复了 —— 那也是结构化的。"""
        planner = TargetedRepairPlanner(_coordinator())
        failure = ToolFailure(
            kind=FailureKind.CHECK_FAILED,
            tool="ruff",
            findings=(),
            failed_checks=("lint_pass",),
        )

        plan = await planner.plan(_request("T-1"), failure=failure)

        assert plan.allowed is True
        assert "lint_pass" in plan.instruction


@pytest.mark.parametrize(
    ("raw_code", "expected"),
    [("invalid-syntax", FailureType.SYNTAX_ERROR), ("F401", FailureType.LINT_VIOLATION)],
)
def test_the_tool_codes_land_in_the_right_class(raw_code: str, expected: FailureType) -> None:
    """工具自己的码要落在对的类别上。"""
    failure = ToolFailure(
        kind=FailureKind.CHECK_FAILED,
        tool="ruff",
        findings=(ToolFinding(file="a.py", line=1, raw_code=raw_code),),
    )

    assert classify(failure).failure_type is expected
