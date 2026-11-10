"""TASK-P4-030 —— 按实测矩阵选型：执行器与模型**来自同一格**。

验证编排层最要紧的一件事：

- **一次选型，两个端口** —— 问执行器与问模型必须给出**同一格**里的两个值。
  分开问两次就会挑出一个**没测过**的组合，那正是二维矩阵要防的事。
- **挑不出来就炸** —— 不退回"随便挑一个"，也不静默换个模型顶上。
- **任务类型取自计划** —— 规划器写的是 ``state.plan``，状态上那个
  ``state.task_type`` 没人写，停在默认值。
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from core.planning.roadmap import TaskType
from core.policy.capability import CapabilityCell, CapabilityMatrix, SelectionRequest
from core.task.execution_loop import ExecutionPlan, LoopAState
from orchestration.capability_resolver import (
    CapabilitySelectionError,
    MatrixCapabilitySelection,
    load_capability_matrix,
)
from orchestration.loop_a import build_loop_a_graph, run_loop_a
from tests.support.execution_loop import StubHandlers, ready_state


def _cell(
    executor: str,
    model: str,
    *,
    succeeded: int = 9,
    cost: str | None = None,
) -> CapabilityCell:
    """造一格 —— **默认是"跑过 9 次、成过 9 次"的那种好格子**。"""
    return CapabilityCell(
        executor=executor,
        model=model,
        runs=9,
        executed=9,
        succeeded=succeeded,
        measured_runs=9,
        regressed=0,
        cost_median=Decimal(cost) if cost is not None else None,
        duration_median=Decimal("76"),
        tokens_median=1000,
        currency="USD" if cost is not None else None,
    )


def _state(task_type: TaskType = TaskType.FEATURE, *, project_id: str = "p1") -> LoopAState:
    """造一个**有计划**的状态 —— 规划器已经跑过。"""
    return LoopAState(
        task_id="t1",
        project_id=project_id,
        plan=ExecutionPlan(task_id="t1", instruction="做点事", task_type=task_type),
    )


class TestSelection:
    """选型 —— **有据才选**。"""

    async def test_select_returns_an_executor(self) -> None:
        matrix = CapabilityMatrix.of([_cell("claude-code", "m1")])
        selection = MatrixCapabilitySelection(matrix)
        assert await selection.select(_state(), ("claude-code", "codex")) == "claude-code"

    async def test_resolve_returns_the_model_of_the_same_cell(self) -> None:
        """**执行器与模型必须来自同一格。**"""
        matrix = CapabilityMatrix.of(
            [
                _cell("claude-code", "m1", succeeded=9),
                _cell("codex", "m2", succeeded=5),
            ]
        )
        selection = MatrixCapabilitySelection(matrix)
        executor = await selection.select(_state(), ("claude-code", "codex"))
        model = await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)
        assert (executor, model) == ("claude-code", "m1")

    async def test_candidates_are_respected(self) -> None:
        """**候选是编排层给的** —— 选择器只能在候选里挑。"""
        matrix = CapabilityMatrix.of(
            [
                _cell("claude-code", "m1", succeeded=9),
                _cell("codex", "m2", succeeded=5),
            ]
        )
        selection = MatrixCapabilitySelection(matrix)
        executor = await selection.select(_state(), ("codex",))
        model = await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)
        assert (executor, model) == ("codex", "m2")

    async def test_without_evidence_it_raises(self) -> None:
        """**没有实测值的格子不进候选** —— 挑不出来就不跑。"""
        matrix = CapabilityMatrix.of([CapabilityCell(executor="claude-code", model="m1")])
        selection = MatrixCapabilitySelection(matrix)
        with pytest.raises(CapabilitySelectionError):
            await selection.select(_state(), ("claude-code",))

    async def test_rejection_reasons_are_reported(self) -> None:
        """**排除必须带理由** —— 事后才知道是没测过还是不在候选里。"""
        matrix = CapabilityMatrix.of([_cell("claude-code", "m1")])
        selection = MatrixCapabilitySelection(matrix)
        with pytest.raises(CapabilitySelectionError) as info:
            await selection.select(_state(), ("codex",))
        assert info.value.rejected
        assert "候选" in info.value.rejected[0][1]

    async def test_without_a_plan_it_raises(self) -> None:
        """**没有计划就没有任务类型** —— 选了执行器也没有配套模型。"""
        matrix = CapabilityMatrix.of([_cell("claude-code", "m1")])
        selection = MatrixCapabilitySelection(matrix)
        state = LoopAState(task_id="t1", project_id="p1")
        with pytest.raises(CapabilitySelectionError):
            await selection.select(state, ("claude-code",))

    async def test_task_type_comes_from_the_plan(self) -> None:
        """**状态上那个 task_type 没人写** —— 用它会挑错格。"""
        matrix = CapabilityMatrix.of(
            [
                _cell("claude-code", "m1", succeeded=9),
                _cell("codex", "m2", succeeded=9),
            ]
        )
        selection = MatrixCapabilitySelection(matrix)
        state = _state(TaskType.BUGFIX)
        await selection.select(state, ("claude-code", "codex"))
        # 键是按计划里的任务类型记的 —— 与 resolve 问的那个一致。
        assert ("p1", TaskType.BUGFIX.value) in selection.decisions
        assert ("p1", TaskType.FEATURE.value) not in selection.decisions


class TestResolve:
    """问模型 —— **从上一次的决定里出**。"""

    async def test_resolve_before_select_raises(self) -> None:
        """**缓存里没有就是没有** —— 不猜一个模型顶上。"""
        matrix = CapabilityMatrix.of([_cell("claude-code", "m1")])
        selection = MatrixCapabilitySelection(matrix)
        with pytest.raises(CapabilitySelectionError):
            await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)

    async def test_resolve_with_another_task_type_raises(self) -> None:
        """**另一个任务类型没选过** —— 那就没有配套模型。"""
        matrix = CapabilityMatrix.of([_cell("claude-code", "m1")])
        selection = MatrixCapabilitySelection(matrix)
        await selection.select(_state(TaskType.FEATURE), ("claude-code",))
        with pytest.raises(CapabilitySelectionError):
            await selection.resolve(project_id="p1", task_type=TaskType.BUGFIX)

    async def test_resolve_with_another_project_raises(self) -> None:
        matrix = CapabilityMatrix.of([_cell("claude-code", "m1")])
        selection = MatrixCapabilitySelection(matrix)
        await selection.select(_state(project_id="p1"), ("claude-code",))
        with pytest.raises(CapabilitySelectionError):
            await selection.resolve(project_id="p2", task_type=TaskType.FEATURE)

    async def test_the_same_decision_is_reused_on_a_repair_round(self) -> None:
        """**返修那一轮不重选** —— 同一个组合从头跑到尾。"""
        matrix = CapabilityMatrix.of([_cell("claude-code", "m1")])
        selection = MatrixCapabilitySelection(matrix)
        await selection.select(_state(), ("claude-code",))
        first = await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)
        second = await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)
        assert first == second == "m1"


class TestCostCeiling:
    """成本上限 —— **硬闸**。"""

    async def test_ceiling_rejects_the_expensive_cell(self) -> None:
        matrix = CapabilityMatrix.of(
            [
                _cell("claude-code", "m1", cost="9"),
                _cell("codex", "m2", cost="1"),
            ]
        )
        selection = MatrixCapabilitySelection(
            matrix,
            ceiling=lambda _project, _task: Decimal("5"),
        )
        executor = await selection.select(_state(), ("claude-code", "codex"))
        model = await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)
        assert (executor, model) == ("codex", "m2")

    async def test_ceiling_is_per_project(self) -> None:
        """**上限来自项目余额** —— 甲项目有钱、乙项目没有，处置就该不同。"""
        matrix = CapabilityMatrix.of(
            [
                _cell("claude-code", "m1", succeeded=9, cost="9"),
                _cell("codex", "m2", succeeded=8, cost="1"),
            ]
        )

        def ceiling(project_id: str, _task: TaskType) -> Decimal | None:
            return Decimal("5") if project_id == "poor" else None

        selection = MatrixCapabilitySelection(matrix, ceiling=ceiling)
        assert (
            await selection.select(_state(project_id="poor"), ("claude-code", "codex")) == "codex"
        )
        assert await selection.select(_state(project_id="rich"), ("claude-code", "codex")) == (
            "claude-code"
        )

    async def test_without_a_ceiling_cost_does_not_filter(self) -> None:
        matrix = CapabilityMatrix.of([_cell("claude-code", "m1", cost="999")])
        selection = MatrixCapabilitySelection(matrix)
        assert await selection.select(_state(), ("claude-code",)) == "claude-code"


class TestRequest:
    """请求里的候选模型也管用。"""

    async def test_model_candidates_are_respected(self) -> None:
        matrix = CapabilityMatrix.of(
            [
                _cell("claude-code", "m1", succeeded=9),
                _cell("claude-code", "m2", succeeded=9),
            ]
        )
        selection = MatrixCapabilitySelection(matrix, request=SelectionRequest(models=("m2",)))
        await selection.select(_state(), ("claude-code",))
        model = await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)
        assert model == "m2"


class TestLoading:
    """读矩阵 —— **读坏了就说读坏了**。"""

    def test_round_trip(self, tmp_path: Path) -> None:
        matrix = CapabilityMatrix.of(
            [
                CapabilityCell(
                    executor="claude-code",
                    model="m1",
                    runs=9,
                    executed=9,
                    succeeded=9,
                    measured_runs=9,
                    regressed=0,
                    cost_median=Decimal("1.5"),
                    duration_median=Decimal("76"),
                    tokens_median=1000,
                    currency="USD",
                    capability_group="g1",
                )
            ]
        )
        path = tmp_path / "matrix.json"
        path.write_text(
            json.dumps(
                {
                    "cells": [
                        {
                            "executor": "claude-code",
                            "model": "m1",
                            "runs": 9,
                            "executed": 9,
                            "succeeded": 9,
                            "measured_runs": 9,
                            "regressed": 0,
                            "cost_median": "1.5",
                            "duration_median": "76",
                            "tokens_median": 1000,
                            "currency": "USD",
                            "capability_group": "g1",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        restored = load_capability_matrix(path)
        assert restored.cells == matrix.cells

    def test_broken_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text('{"cells": 42}', encoding="utf-8")
        with pytest.raises(ValueError, match="cells"):
            load_capability_matrix(path)


class TestWiring:
    """接进循环 A —— **图里跑出来的执行器与模型要来自同一格**。"""

    async def test_executor_and_model_come_from_the_same_cell(self) -> None:
        matrix = CapabilityMatrix.of(
            [
                _cell("stub-executor", "m-strong", succeeded=9),
                _cell("stub-executor", "m-weak", succeeded=3),
            ]
        )
        selection = MatrixCapabilitySelection(matrix)
        stub = StubHandlers()
        handlers = replace(stub.handlers, selector=selection, model_resolver=selection)
        graph = build_loop_a_graph(handlers)
        state = await run_loop_a(graph, ready_state())
        assert state.executor == "stub-executor"
        # **模型是矩阵挑出来的那一个** —— 不是执行器自己惯用的那个。
        assert stub.executor.session_configs[0].model == "m-strong"

    async def test_without_evidence_the_task_does_not_run(self) -> None:
        """**挑不出来就不跑** —— 不退回"随便挑一个执行器"。"""
        matrix = CapabilityMatrix.of([_cell("other-executor", "m1")])
        selection = MatrixCapabilitySelection(matrix)
        stub = StubHandlers()
        handlers = replace(stub.handlers, selector=selection, model_resolver=selection)
        graph = build_loop_a_graph(handlers)
        with pytest.raises(CapabilitySelectionError):
            await run_loop_a(graph, ready_state())
        assert stub.executor.session_configs == []
