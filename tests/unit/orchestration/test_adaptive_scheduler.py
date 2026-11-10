"""TASK-P4-040 —— 按历史自适应调度：运行时那一层。

验证编排层最要紧的几件事：

- **一次选型，两个端口** —— 问执行器与问模型必须给出**同一格**里的两个值。分开问
  两次就会挑出一个**没测过**的组合，那正是二维能力矩阵要防的事。
- **失败关闭** —— 挑不出来就炸，不退回"随便挑一个"。
- **历史会变** —— 跑完一次记回历史，下一次的决定就该不一样；这就是"自适应"与
  "查表"的分别。
- **上限是硬闸** —— 规矩里的上限与项目给的上限，**取更紧的那个**。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from core.planning.roadmap import TaskType
from core.policy.scheduling import (
    AdaptiveScheduler,
    Observation,
    SchedulingPolicy,
    observation_to_dict,
)
from core.task.execution_loop import ExecutionPlan, LoopAState
from orchestration.adaptive_scheduler import (
    AdaptiveSelection,
    AdaptiveSelectionError,
    load_observations,
)


def _state(task_type: TaskType = TaskType.FEATURE, *, project_id: str = "p1") -> LoopAState:
    """造一个**有计划**的状态 —— 规划器已经跑过。"""
    return LoopAState(
        task_id="t1",
        project_id=project_id,
        plan=ExecutionPlan(task_id="t1", instruction="做点事", task_type=task_type),
    )


def _observation(
    executor: str,
    model: str,
    *,
    task_id: str = "t1",
    cost: str | None = None,
) -> Observation:
    """造一次观测 —— **历史就是这些**（默认跑成了、做对了）。"""
    return Observation(
        executor=executor,
        model=model,
        task_id=task_id,
        executed=True,
        succeeded=True,
        cost=Decimal(cost) if cost is not None else None,
        duration=Decimal("10"),
    )


class TestSelection:
    """选型 —— **有据才选**。"""

    async def test_the_executor_and_the_model_come_from_the_same_cell(self) -> None:
        """**执行器与模型必须来自同一格。**

        两格都跑过、都做对过，那么它们"分不开" —— 分不开的那一档里按成本排，
        于是挑中便宜的那格。**执行器与模型都是从这一格出来的**。
        """
        selection = AdaptiveSelection(
            scheduler=_scheduler(),
        )
        selection.record(_observation("claude-code", "m1", cost="10"))
        selection.record(_observation("codex", "m2", cost="1"))

        executor = await selection.select(_state(), ("claude-code", "codex"))
        model = await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)

        assert (executor, model) == ("codex", "m2")

    async def test_the_candidates_are_respected(self) -> None:
        """**候选是编排层给的** —— 选择器只能在候选里挑。"""
        selection = AdaptiveSelection(scheduler=_scheduler())
        selection.record(_observation("claude-code", "m1", cost="10"))
        selection.record(_observation("codex", "m2", cost="1"))

        assert await selection.select(_state(), ("claude-code",)) == "claude-code"

    async def test_without_evidence_it_raises(self) -> None:
        """**没有历史也没有冷启动偏好 —— 挑不出来就不跑。**"""
        selection = AdaptiveSelection(scheduler=_scheduler())
        with pytest.raises(AdaptiveSelectionError):
            await selection.select(_state(), ("claude-code",))

    async def test_the_cold_start_uses_the_declared_preference(self) -> None:
        """**冷启动是一次没有证据的选择** —— 但它必须显式声明过。"""
        selection = AdaptiveSelection(
            scheduler=_scheduler(preference=("claude-code/m1",)),
        )
        executor = await selection.select(_state(), ("claude-code", "codex"))
        model = await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)

        assert (executor, model) == ("claude-code", "m1")
        assert "冷启动" in selection.reason_for("p1", TaskType.FEATURE)

    async def test_without_a_plan_it_raises(self) -> None:
        """**没有计划就没有任务类型** —— 选了执行器也没有配套模型。"""
        selection = AdaptiveSelection(scheduler=_scheduler(preference=("claude-code/m1",)))
        state = LoopAState(task_id="t1", project_id="p1")
        with pytest.raises(AdaptiveSelectionError):
            await selection.select(state, ("claude-code",))

    async def test_the_task_type_comes_from_the_plan(self) -> None:
        """**状态上那个 task_type 没人写** —— 用它会挑错格、也会让模型对不上。"""
        selection = AdaptiveSelection(scheduler=_scheduler(preference=("claude-code/m1",)))
        await selection.select(_state(TaskType.BUGFIX), ("claude-code",))
        assert ("p1", TaskType.BUGFIX.value) in selection.decisions
        assert ("p1", TaskType.FEATURE.value) not in selection.decisions

    async def test_the_rejection_reasons_are_reported(self) -> None:
        """**排除必须带理由** —— 事后才知道是没测过还是不在候选里。"""
        selection = AdaptiveSelection(scheduler=_scheduler())
        selection.record(_observation("claude-code", "m1", cost="1"))
        with pytest.raises(AdaptiveSelectionError) as info:
            await selection.select(_state(), ("codex",))
        assert info.value.rejected
        assert "候选" in info.value.rejected[0][1]


class TestResolve:
    """问模型 —— **从上一次的决定里出**。"""

    async def test_resolve_before_select_raises(self) -> None:
        """**缓存里没有就是没有** —— 不猜一个模型顶上。"""
        selection = AdaptiveSelection(scheduler=_scheduler())
        with pytest.raises(AdaptiveSelectionError):
            await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)

    async def test_resolve_with_another_task_type_raises(self) -> None:
        """**另一个任务类型没选过** —— 那就没有配套模型。"""
        selection = AdaptiveSelection(scheduler=_scheduler(preference=("claude-code/m1",)))
        await selection.select(_state(TaskType.FEATURE), ("claude-code",))
        with pytest.raises(AdaptiveSelectionError):
            await selection.resolve(project_id="p1", task_type=TaskType.BUGFIX)

    async def test_resolve_with_another_project_raises(self) -> None:
        selection = AdaptiveSelection(scheduler=_scheduler(preference=("claude-code/m1",)))
        await selection.select(_state(project_id="p1"), ("claude-code",))
        with pytest.raises(AdaptiveSelectionError):
            await selection.resolve(project_id="p2", task_type=TaskType.FEATURE)

    async def test_the_same_decision_is_reused_on_a_repair_round(self) -> None:
        """**返修那一轮不重选** —— 同一个组合从头跑到尾。"""
        selection = AdaptiveSelection(scheduler=_scheduler(preference=("claude-code/m1",)))
        await selection.select(_state(), ("claude-code",))
        first = await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)
        second = await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)
        assert first == second == "m1"


class TestHistory:
    """历史 —— **自适应就长在这儿**。"""

    async def test_a_recorded_observation_changes_the_next_decision(self) -> None:
        """**记回历史之后，下一次的决定就该换人。**

        先只有贵的那一格有证据 —— 只能用它；后来便宜的那一格也跑过了，于是下一次
        改用便宜的那格。**这就是"自适应"与"查表"的分别。**
        """
        selection = AdaptiveSelection(scheduler=_scheduler())
        selection.record(_observation("claude-code", "m1", cost="10"))
        first = await selection.select(_state(), ("claude-code", "codex"))
        assert first == "claude-code"

        selection.record(_observation("codex", "m2", cost="1"))
        second = await selection.select(_state(), ("claude-code", "codex"))
        assert second == "codex"

    async def test_the_decisions_are_kept(self) -> None:
        """**留痕是为了事后能回答"这次为什么用这个组合"。**"""
        selection = AdaptiveSelection(scheduler=_scheduler(preference=("claude-code/m1",)))
        await selection.select(_state(), ("claude-code",))
        assert ("p1", TaskType.FEATURE.value) in selection.decisions


class TestCostCeiling:
    """成本上限 —— **硬闸**。"""

    async def test_the_ceiling_rejects_the_expensive_cell(self) -> None:
        selection = AdaptiveSelection(
            scheduler=_scheduler(),
            ceiling=lambda _project, _task: Decimal("5"),
        )
        selection.record(_observation("claude-code", "m1", cost="9"))
        selection.record(_observation("codex", "m2", cost="1"))

        executor = await selection.select(_state(), ("claude-code", "codex"))
        model = await selection.resolve(project_id="p1", task_type=TaskType.FEATURE)
        assert (executor, model) == ("codex", "m2")

    async def test_the_tighter_of_the_two_ceilings_wins(self) -> None:
        """**规矩里的上限与项目给的上限，取更紧的那个。**

        只认其中一个，等于另一个形同虚设 —— 而形同虚设的闸门挡不住超支。
        """
        tight_policy = AdaptiveSelection(
            scheduler=_scheduler(maximum_cost=Decimal("0.5")),
            ceiling=lambda _project, _task: Decimal("100"),
        )
        tight_policy.record(_observation("claude-code", "m1", cost="10"))
        with pytest.raises(AdaptiveSelectionError):
            await tight_policy.select(_state(), ("claude-code",))

        tight_project = AdaptiveSelection(
            scheduler=_scheduler(maximum_cost=Decimal("100")),
            ceiling=lambda _project, _task: Decimal("0.5"),
        )
        tight_project.record(_observation("claude-code", "m1", cost="10"))
        with pytest.raises(AdaptiveSelectionError):
            await tight_project.select(_state(), ("claude-code",))

    async def test_without_a_ceiling_the_cost_does_not_filter(self) -> None:
        selection = AdaptiveSelection(scheduler=_scheduler())
        selection.record(_observation("claude-code", "m1", cost="999"))
        assert await selection.select(_state(), ("claude-code",)) == "claude-code"

    async def test_a_cell_without_a_measured_cost_is_rejected(self) -> None:
        """**没量到成本就没法确认它在上限之内** —— 失败关闭。"""
        selection = AdaptiveSelection(
            scheduler=_scheduler(),
            ceiling=lambda _project, _task: Decimal("5"),
        )
        selection.record(_observation("claude-code", "m1", cost=None))
        with pytest.raises(AdaptiveSelectionError):
            await selection.select(_state(), ("claude-code",))


class TestLoadObservations:
    """读历史 —— **运行时调度器的历史就是这份文件**。"""

    def test_it_reads_a_jsonl_file(self, tmp_path: Path) -> None:
        path = tmp_path / "observations.jsonl"
        path.write_text(
            json.dumps(observation_to_dict(_observation("claude-code", "m1", cost="1")))
            + "\n"
            + json.dumps(observation_to_dict(_observation("codex", "m2", cost="2")))
            + "\n",
            encoding="utf-8",
        )

        observations = load_observations(path)

        assert len(observations) == 2
        assert observations[0].executor == "claude-code"
        assert observations[1].cost == Decimal("2")

    def test_a_broken_line_is_refused(self, tmp_path: Path) -> None:
        """**读坏了就说坏了。** 把坏文件读成"没有历史"，调度器会退化成冷启动。"""
        path = tmp_path / "observations.jsonl"
        path.write_text("{不是 JSON}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="不是 JSON"):
            load_observations(path)

    def test_a_line_that_is_not_an_object_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "observations.jsonl"
        path.write_text("[1, 2, 3]\n", encoding="utf-8")
        with pytest.raises(ValueError, match="必须是对象"):
            load_observations(path)

    def test_an_empty_file_gives_no_history(self, tmp_path: Path) -> None:
        path = tmp_path / "observations.jsonl"
        path.write_text("", encoding="utf-8")
        assert load_observations(path) == ()


def _scheduler(
    *,
    preference: tuple[str, ...] = (),
    maximum_cost: Decimal | None = None,
) -> AdaptiveScheduler:
    """造一个调度器 —— **规矩由调用方给**。"""
    return AdaptiveScheduler(SchedulingPolicy(preference=preference, maximum_cost=maximum_cost))
