"""TASK-P4-040 —— 自适应调度：按历史表现为每个任务挑组合。

验证五件事：

1. **一次成功不算证据** —— 样本少的时候达成度下界低，骗不过去。
2. **先保达成度，再压成本** —— 底线之外出局，底线之内按成本排。
3. **冷启动要有话说** —— 没有历史就按显式声明的偏好来，并如实标成冷启动。
4. **失败关闭** —— 挑不出来就明说挑不出来，不悄悄退回一个默认组合。
5. **自适应** —— 历史变长，结论会变。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from core.policy.scheduling import (
    AdaptiveScheduler,
    Observation,
    PerformanceLedger,
    SchedulingPolicy,
    observation_from_dict,
    observation_to_dict,
    wilson_lower_bound,
)


@dataclass(frozen=True, slots=True)
class _Extra:
    """一次观测里那些**不常变**的部分。"""

    task_id: str = "t1"
    succeeded: bool = True
    executed: bool = True
    cost: str | None = "1.0"
    duration: str | None = "10"


#: 默认的那一份 —— **模块级单例当默认值**。
_DEFAULT_EXTRA: _Extra = _Extra()


def _obs(executor: str, model: str, extra: _Extra = _DEFAULT_EXTRA) -> Observation:
    """造一次观测。"""
    return Observation(
        executor=executor,
        model=model,
        task_id=extra.task_id,
        executed=extra.executed,
        succeeded=extra.succeeded,
        cost=Decimal(extra.cost) if extra.cost is not None else None,
        duration=Decimal(extra.duration) if extra.duration is not None else None,
    )


def _history(
    executor: str,
    model: str,
    count: int,
    *,
    succeeded: bool = True,
    cost: str | None = "1.0",
) -> list[Observation]:
    """造一串观测 —— **同一格、同一个任务**。"""
    return [
        _obs(
            executor,
            model,
            _Extra(succeeded=succeeded, cost=cost),
        )
        for _ in range(count)
    ]


class TestWilson:
    """达成度下界 —— **样本少的时候要保守**。"""

    def test_no_trials_is_zero(self) -> None:
        """**没跑过不是 0，是没有证据** —— 调用方要把这种情况单独挑出来。"""
        assert wilson_lower_bound(0, 0) == Decimal(0)

    def test_one_success_is_not_evidence(self) -> None:
        """**一次成功骗不过去** —— 1/1 的下界只有 0.21 出头。"""
        assert wilson_lower_bound(1, 1) < Decimal("0.25")

    def test_many_successes_raise_the_bound(self) -> None:
        assert wilson_lower_bound(9, 9) > Decimal("0.70")
        assert wilson_lower_bound(27, 27) > Decimal("0.87")

    def test_the_bound_grows_with_evidence(self) -> None:
        """**样本越多，下界越实** —— 同样的成功率，早期说得保守。"""
        assert wilson_lower_bound(9, 9) > wilson_lower_bound(1, 1)

    def test_negative_counts_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            wilson_lower_bound(-1, 3)
        with pytest.raises(ValueError):
            wilson_lower_bound(1, -3)

    def test_successes_cannot_exceed_trials(self) -> None:
        with pytest.raises(ValueError):
            wilson_lower_bound(4, 3)


class TestObservation:
    """一次观测 —— **三态与它的不变量**。"""

    def test_empty_names_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            Observation(executor=" ", model="m", task_id="t")
        with pytest.raises(ValueError):
            Observation(executor="e", model="", task_id="t")
        with pytest.raises(ValueError):
            Observation(executor="e", model="m", task_id=" ")

    def test_not_run_cannot_have_succeeded(self) -> None:
        """**没跑起来就不可能做对了** —— 两个布尔必须自洽。"""
        with pytest.raises(ValueError):
            Observation(executor="e", model="m", task_id="t", executed=False, succeeded=True)

    def test_negative_cost_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            Observation(executor="e", model="m", task_id="t", cost=Decimal("-1"))

    def test_dict_round_trip(self) -> None:
        """**一次观测长什么样只有一处定义** —— 存盘与读回要对得上。"""
        original = _obs("claude-code", "m1", _Extra(cost="1.25", duration="76"))
        restored = observation_from_dict(observation_to_dict(original))
        assert restored == original

    def test_unknown_cost_stays_unknown(self) -> None:
        """**没测出来不许拿 0 顶上** —— 0 会被当成"免费"。"""
        restored = observation_from_dict(observation_to_dict(_obs("e", "m", _Extra(cost=None))))
        assert restored.cost is None


class TestLedger:
    """账本 —— **按格子汇总**。"""

    def test_cells_are_grouped_by_executor_and_model(self) -> None:
        ledger = PerformanceLedger.of(
            [
                *_history("claude-code", "m1", 3),
                *_history("codex", "m1", 2),
                *_history("claude-code", "m1", 1),
            ]
        )
        assert len(ledger.cells()) == 2
        cell = ledger.cell("claude-code", "m1")
        assert cell is not None
        assert cell.runs == 4

    def test_missing_cell_is_none(self) -> None:
        assert PerformanceLedger.of([]).cell("e", "m") is None

    def test_not_run_is_not_a_failure(self) -> None:
        """**跑不成与做不对是两回事** —— 成功率的分母里没有"跑不成"。"""
        ledger = PerformanceLedger.of(
            [
                *_history("e", "m", 4),
                _obs("e", "m", _Extra(succeeded=False, executed=False)),
            ]
        )
        cell = ledger.cell("e", "m")
        assert cell is not None
        assert cell.runs == 5
        assert cell.executed == 4
        assert cell.succeeded == 4
        assert cell.success_rate == Decimal(1)
        assert cell.overall_rate == Decimal(4) / Decimal(5)

    def test_cost_averages_only_measured_runs(self) -> None:
        """**没测到成本的运行不进平均** —— 否则平均会被拉低。"""
        ledger = PerformanceLedger.of(
            [
                _obs("e", "m", _Extra(cost="2")),
                _obs("e", "m", _Extra(cost=None)),
            ]
        )
        cell = ledger.cell("e", "m")
        assert cell is not None
        assert cell.mean_cost == Decimal(2)
        assert cell.cost_samples == 1

    def test_unknown_cost_has_no_mean(self) -> None:
        ledger = PerformanceLedger.of([_obs("e", "m", _Extra(cost=None))])
        cell = ledger.cell("e", "m")
        assert cell is not None
        assert cell.mean_cost is None

    def test_observations_before_is_a_strict_prefix(self) -> None:
        """**回放要靠它"不许看未来"。**"""
        ledger = PerformanceLedger.of(_history("e", "m", 3))
        assert len(ledger.observations_before(2)) == 2


class TestColdStart:
    """冷启动 —— **没有历史也得能开工**。"""

    def test_without_history_and_preference_it_refuses(self) -> None:
        """**挑不出来就明说挑不出来** —— 不悄悄退回一个默认组合。"""
        decision = AdaptiveScheduler().decide("t1")
        assert decision.chosen is None
        assert "没有历史" in decision.reason

    def test_preference_is_used(self) -> None:
        """**冷启动按显式声明的偏好来。**"""
        scheduler = AdaptiveScheduler(
            SchedulingPolicy(preference=("codex/m2", "claude-code/m1")),
        )
        decision = scheduler.decide("t1")
        assert decision.chosen is not None
        assert decision.chosen.executor == "codex"
        assert decision.chosen.model == "m2"

    def test_cold_start_is_marked(self) -> None:
        """**没证据的选择要如实标出来** —— 不混进"有据可依"的那一堆。"""
        scheduler = AdaptiveScheduler(SchedulingPolicy(preference=("codex/m2",)))
        assert scheduler.decide("t1").cold_start is True

    def test_with_a_ceiling_cold_start_refuses(self) -> None:
        """**不知道要花多少钱，就没法说它没超上限。**"""
        scheduler = AdaptiveScheduler(
            SchedulingPolicy(preference=("codex/m2",), maximum_cost=Decimal("5")),
        )
        decision = scheduler.decide("t1")
        assert decision.chosen is None
        assert "上限" in decision.reason

    def test_a_malformed_preference_is_rejected(self) -> None:
        """**偏好要写成 '执行器/模型'** —— 含糊的偏好会在运行时变成含糊的选择。"""
        with pytest.raises(ValueError):
            SchedulingPolicy(preference=("codex",))


class TestQualityFloor:
    """底线 —— **先保达成度**。"""

    def test_one_success_does_not_win(self) -> None:
        """**一次成功不算证据** —— 它进不了"分不开"的那一档。"""
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("lucky", "m1", 1))
        scheduler.record_all(_history("solid", "m1", 9))
        decision = scheduler.decide("t1")
        assert decision.chosen is not None
        assert decision.chosen.executor == "solid"

    def test_the_weaker_cell_is_rejected_with_a_reason(self) -> None:
        """**排除必须带理由** —— 事后才知道是没测过还是不行。"""
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("solid", "m1", 9))
        scheduler.record_all(_history("weak", "m1", 9, succeeded=False))
        decision = scheduler.decide("t1")
        assert decision.chosen is not None
        assert decision.chosen.executor == "solid"
        assert any("weak" in str(item) for item in decision.rejected)
        assert any("底线" in item.reason for item in decision.rejected)

    def test_the_absolute_floor_is_honoured(self) -> None:
        """**容差是相对的，底线是绝对的** —— 再差也不能低于这个数。"""
        policy = SchedulingPolicy(tolerance=Decimal("0.9"), minimum_rate=Decimal("0.6"))
        scheduler = AdaptiveScheduler(policy)
        scheduler.record_all(_history("solid", "m1", 9))
        scheduler.record_all(_history("weak", "m1", 9, succeeded=False))
        decision = scheduler.decide("t1")
        assert decision.chosen is not None
        assert decision.chosen.executor == "solid"

    def test_a_tolerance_of_zero_keeps_only_the_best(self) -> None:
        """**容差为 0 就是"只认最好的那一档"** —— 可复现的严格口径。"""
        policy = SchedulingPolicy(tolerance=Decimal("0"))
        scheduler = AdaptiveScheduler(policy)
        scheduler.record_all(_history("a", "m1", 9))
        scheduler.record_all(_history("b", "m1", 9, succeeded=False))
        decision = scheduler.decide("t1")
        assert decision.chosen is not None
        assert decision.chosen.executor == "a"


class TestCost:
    """成本 —— **底线之内按成本排**。"""

    def test_the_cheaper_cell_wins_inside_one_tier(self) -> None:
        """**做对率分不开，那就选便宜的** —— 与能力矩阵同一套口径。"""
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("expensive", "m1", 9, cost="9"))
        scheduler.record_all(_history("cheap", "m1", 9, cost="1"))
        decision = scheduler.decide("t1")
        assert decision.chosen is not None
        assert decision.chosen.executor == "cheap"

    def test_the_ceiling_is_a_hard_gate(self) -> None:
        """**预算说的是"不许超过"，不是"尽量少花"。**"""
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("expensive", "m1", 9, cost="9"))
        scheduler.record_all(_history("cheap", "m1", 9, cost="1"))
        decision = scheduler.decide("t1", maximum_cost=Decimal("5"))
        assert decision.chosen is not None
        assert decision.chosen.executor == "cheap"

    def test_unknown_cost_is_excluded_under_a_ceiling(self) -> None:
        """**成本没测出来就没法确认它在上限之内** —— 宁可空着。"""
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("expensive", "m1", 9, cost="9"))
        scheduler.record_all(_history("unknown", "m1", 9, cost=None))
        decision = scheduler.decide("t1", maximum_cost=Decimal("5"))
        assert decision.chosen is None
        assert any("成本没测出来" in item.reason for item in decision.rejected)

    def test_without_a_ceiling_unknown_cost_is_not_excluded(self) -> None:
        """**没有上限时，成本没测出来只是排最后，不是出局。**"""
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("known", "m1", 9, cost="9"))
        scheduler.record_all(_history("unknown", "m1", 9, cost=None))
        decision = scheduler.decide("t1")
        assert decision.chosen is not None
        assert decision.chosen.executor == "known"

    def test_the_ceiling_comes_from_the_policy_too(self) -> None:
        scheduler = AdaptiveScheduler(SchedulingPolicy(maximum_cost=Decimal("5")))
        scheduler.record_all(_history("expensive", "m1", 9, cost="9"))
        scheduler.record_all(_history("cheap", "m1", 9, cost="1"))
        decision = scheduler.decide("t1")
        assert decision.chosen is not None
        assert decision.chosen.executor == "cheap"


class TestCandidates:
    """候选 —— **编排层给的清单**。"""

    def test_executor_candidates_are_respected(self) -> None:
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("a", "m1", 9, cost="1"))
        scheduler.record_all(_history("b", "m1", 9, cost="2"))
        decision = scheduler.decide("t1", candidates=("b",))
        assert decision.chosen is not None
        assert decision.chosen.executor == "b"

    def test_model_candidates_are_respected(self) -> None:
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("a", "m1", 9, cost="1"))
        scheduler.record_all(_history("a", "m2", 9, cost="2"))
        decision = scheduler.decide("t1", models=("m2",))
        assert decision.chosen is not None
        assert decision.chosen.model == "m2"

    def test_out_of_candidates_is_rejected_with_a_reason(self) -> None:
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("a", "m1", 9))
        decision = scheduler.decide("t1", candidates=("b",))
        assert decision.chosen is None
        assert any("候选" in item.reason for item in decision.rejected)


class TestAdaptivity:
    """自适应 —— **历史变长，结论会变**。"""

    def test_the_choice_changes_when_the_history_grows(self) -> None:
        """**这是"调度"与"查表"的分别。**"""
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("a", "m1", 9, cost="9"))
        first = scheduler.decide("t1")
        assert first.chosen is not None
        assert first.chosen.executor == "a"
        scheduler.record_all(_history("b", "m1", 9, cost="1"))
        second = scheduler.decide("t1")
        assert second.chosen is not None
        assert second.chosen.executor == "b"

    def test_the_same_history_gives_the_same_choice(self) -> None:
        """**可复现** —— 否则"这次为什么用这个组合"永远答不上来。"""
        first = AdaptiveScheduler()
        first.record_all(_history("a", "m1", 9, cost="9"))
        first.record_all(_history("b", "m1", 9, cost="1"))
        second = AdaptiveScheduler()
        second.record_all(_history("a", "m1", 9, cost="9"))
        second.record_all(_history("b", "m1", 9, cost="1"))
        assert first.decide("t1").chosen == second.decide("t1").chosen

    def test_the_reason_is_human_readable(self) -> None:
        """**理由要能被复核。**"""
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("cheap", "m1", 9, cost="1"))
        decision = scheduler.decide("t1")
        assert "cheap" in decision.reason
        assert "底线" in decision.reason

    def test_the_task_level_evidence_breaks_cost_ties(self) -> None:
        """**同一个任务上做过一次，比在别的任务上做过更贴近这次要做的事。**"""
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("a", "m1", 9, cost="1"))
        scheduler.record_all(_history("b", "m1", 9, cost="1"))
        scheduler.record(
            _obs("a", "m1", _Extra(task_id="t2", succeeded=False, cost="1")),
        )
        scheduler.record(
            _obs("b", "m1", _Extra(task_id="t2", succeeded=True, cost="1")),
        )
        decision = scheduler.decide("t2")
        assert decision.chosen is not None
        assert decision.chosen.executor == "b"


class TestExploration:
    """探索 —— **花掉的成本，要留痕**。"""

    def test_exploration_is_off_by_default(self) -> None:
        scheduler = AdaptiveScheduler()
        scheduler.record_all(_history("a", "m1", 9, cost="1"))
        scheduler.record_all(_history("b", "m1", 1, cost="1"))
        assert scheduler.decide("t1").exploration is False

    def test_exploration_probes_the_least_observed_cell(self) -> None:
        """**没怎么试过的格子要先试一次** —— 不试就永远不知道它行不行。"""
        policy = SchedulingPolicy(explore_every=2)
        scheduler = AdaptiveScheduler(policy)
        scheduler.record_all(_history("a", "m1", 9, cost="1"))
        scheduler.record_all(_history("b", "m1", 1, cost="1"))
        scheduler.decide("t1")
        decision = scheduler.decide("t1")
        assert decision.exploration is True
        assert decision.chosen is not None
        assert decision.chosen.executor == "b"

    def test_exploration_stops_when_everything_is_tried(self) -> None:
        """**全都试过很多次了，探索就是白花钱。**"""
        policy = SchedulingPolicy(explore_every=2)
        scheduler = AdaptiveScheduler(policy)
        scheduler.record_all(_history("a", "m1", 9, cost="1"))
        scheduler.record_all(_history("b", "m1", 9, cost="1"))
        scheduler.decide("t1")
        assert scheduler.decide("t1").exploration is False
