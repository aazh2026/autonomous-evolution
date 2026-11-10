"""TASK-P4-030 —— 能力矩阵的纯领域：格子、矩阵与选型。

验证三件最容易做错的事：

1. **"没测出来"不是 0** —— 没跑过的格子不进候选，没测到的成本不是 0。
2. **统计上分不开的格子不排序** —— 同档内按成本排，不拿噪声当差别。
3. **排除必须带理由** —— 挑不出来就明说挑不出来，不退回"随便挑一个"。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.policy.capability import (
    CapabilityCell,
    CapabilityMatrix,
    SelectionRequest,
    cell_from_dict,
    cell_to_dict,
    recommend,
)


def _cell(executor: str, model: str, **kwargs: object) -> CapabilityCell:
    """造一格 —— **默认是"跑过 9 次、成过 9 次"的那种好格子**。"""
    defaults: dict[str, object] = {
        "runs": 9,
        "executed": 9,
        "succeeded": 9,
        "measured_runs": 9,
        "regressed": 0,
    }
    defaults.update(kwargs)
    return CapabilityCell(executor=executor, model=model, **defaults)  # type: ignore[arg-type]


class TestCellInvariants:
    """格子自己拦下说不通的数。"""

    def test_empty_executor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="executor"):
            CapabilityCell(executor="  ", model="m")

    def test_empty_model_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="model"):
            CapabilityCell(executor="e", model="")

    def test_negative_counts_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="计数"):
            _cell("e", "m", runs=-1)

    def test_executed_cannot_exceed_runs(self) -> None:
        with pytest.raises(ValueError, match="跑成的次数"):
            _cell("e", "m", runs=1, executed=2, succeeded=1)

    def test_succeeded_cannot_exceed_executed(self) -> None:
        with pytest.raises(ValueError, match="成功的次数"):
            _cell("e", "m", runs=3, executed=2, succeeded=3)

    def test_regressed_cannot_exceed_measured(self) -> None:
        with pytest.raises(ValueError, match="回归"):
            _cell("e", "m", measured_runs=1, regressed=2)


class TestThreeStates:
    """**"没测出来"与"是 0"长得不一样。**"""

    def test_unmeasured_cell_has_no_rates(self) -> None:
        cell = CapabilityCell(executor="e", model="m")
        assert cell.measured is False
        assert cell.availability is None
        assert cell.success_rate is None
        assert cell.overall_rate is None
        assert cell.regression_rate is None

    def test_zero_success_is_not_missing(self) -> None:
        cell = _cell("e", "m", runs=5, executed=5, succeeded=0)
        assert cell.measured is True
        assert cell.success_rate == Decimal(0)
        assert cell.overall_rate == Decimal(0)

    def test_not_run_does_not_enter_success_denominator(self) -> None:
        """**跑不成不算做不对** —— 那是环境问题，不是能力问题。"""
        cell = _cell("e", "m", runs=3, executed=2, succeeded=2)
        assert cell.availability == Decimal(2) / Decimal(3)
        assert cell.success_rate == Decimal(1)
        assert cell.overall_rate == Decimal(2) / Decimal(3)

    def test_never_executed_has_no_success_rate(self) -> None:
        cell = _cell("e", "m", runs=3, executed=0, succeeded=0)
        assert cell.availability == Decimal(0)
        assert cell.success_rate is None
        assert cell.overall_rate == Decimal(0)

    def test_regression_rate_needs_measured_runs(self) -> None:
        cell = _cell("e", "m", measured_runs=0, regressed=0)
        assert cell.regression_rate is None


class TestMatrix:
    """矩阵是格子的集合。"""

    def test_duplicate_coordinates_are_rejected(self) -> None:
        """**重复坐标是数据错了** —— 不是"多测了几次"。"""
        with pytest.raises(ValueError, match="重复的坐标"):
            CapabilityMatrix.of([_cell("e", "m"), _cell("e", "m", runs=3, executed=3, succeeded=3)])

    def test_cells_are_sorted(self) -> None:
        matrix = CapabilityMatrix.of([_cell("b", "z"), _cell("a", "y")])
        assert [cell.key for cell in matrix.cells] == [("a", "y"), ("b", "z")]

    def test_row_and_column(self) -> None:
        matrix = CapabilityMatrix.of(
            [_cell("a", "x"), _cell("a", "y"), _cell("b", "x")],
        )
        assert [cell.model for cell in matrix.row("a")] == ["x", "y"]
        assert [cell.executor for cell in matrix.column("x")] == ["a", "b"]

    def test_missing_cell_is_none(self) -> None:
        matrix = CapabilityMatrix.of([_cell("a", "x")])
        assert matrix.cell("a", "y") is None

    def test_ungrouped_cells_each_get_their_own_group(self) -> None:
        """**没分档不表示"和别人一样"** —— 那是"没比过"。"""
        matrix = CapabilityMatrix.of([_cell("a", "x"), _cell("b", "y")])
        groups = matrix.groups()
        assert len(groups) == 2
        assert matrix.group_of(matrix.cells[0]) == "ungrouped:a/x"

    def test_grouped_cells_share_one_group(self) -> None:
        matrix = CapabilityMatrix.of(
            [_cell("a", "x", capability_group="g1"), _cell("b", "y", capability_group="g1")],
        )
        assert len(matrix.groups()) == 1


class TestRecommend:
    """选型 —— **有据才选，选不出就明说**。"""

    def test_cell_without_measurements_never_wins(self) -> None:
        matrix = CapabilityMatrix.of([CapabilityCell(executor="a", model="x")])
        outcome = recommend(matrix)
        assert outcome.chosen is None
        assert outcome.found is False
        assert "没有实测值" in outcome.rejected[0].reason

    def test_executor_candidates_are_respected(self) -> None:
        matrix = CapabilityMatrix.of([_cell("a", "x"), _cell("b", "y")])
        outcome = recommend(matrix, SelectionRequest(executors=("b",)))
        assert outcome.chosen is not None
        assert outcome.chosen.executor == "b"

    def test_model_candidates_are_respected(self) -> None:
        matrix = CapabilityMatrix.of([_cell("a", "x"), _cell("a", "y")])
        outcome = recommend(matrix, SelectionRequest(models=("y",)))
        assert outcome.chosen is not None
        assert outcome.chosen.model == "y"

    def test_min_runs_rejects_thin_samples(self) -> None:
        matrix = CapabilityMatrix.of([_cell("a", "x", runs=1, executed=1, succeeded=1)])
        outcome = recommend(matrix, SelectionRequest(min_runs=3))
        assert outcome.chosen is None
        assert "样本不足" in outcome.rejected[0].reason

    def test_cost_ceiling_rejects_expensive_cell(self) -> None:
        matrix = CapabilityMatrix.of(
            [_cell("a", "x", cost_median=Decimal("10")), _cell("b", "y", cost_median=Decimal("1"))],
        )
        outcome = recommend(matrix, SelectionRequest(max_cost=Decimal("5")))
        assert outcome.chosen is not None
        assert outcome.chosen.executor == "b"
        reasons = {item.cell.executor: item.reason for item in outcome.rejected}
        assert "超过成本上限" in reasons["a"]

    def test_cell_with_unknown_cost_is_rejected_under_a_ceiling(self) -> None:
        """**不知道多少钱，就没法说它没超。**"""
        matrix = CapabilityMatrix.of([_cell("a", "x", cost_median=None)])
        outcome = recommend(matrix, SelectionRequest(max_cost=Decimal("5")))
        assert outcome.chosen is None
        assert "成本没测出来" in outcome.rejected[0].reason

    def test_cell_that_never_ran_is_rejected(self) -> None:
        matrix = CapabilityMatrix.of([_cell("a", "x", runs=3, executed=0, succeeded=0)])
        outcome = recommend(matrix)
        assert outcome.chosen is None
        assert "一次都没跑起来" in outcome.rejected[0].reason

    def test_no_candidates_means_no_choice(self) -> None:
        """**挑不出来就明说挑不出来** —— 不退回"随便挑一个"。"""
        matrix = CapabilityMatrix.of([CapabilityCell(executor="a", model="x")])
        outcome = recommend(matrix)
        assert outcome.chosen is None
        assert outcome.ranked == ()
        assert outcome.reason != ""

    def test_higher_success_rate_wins_across_groups(self) -> None:
        matrix = CapabilityMatrix.of(
            [
                _cell("a", "x", capability_group="g1", cost_median=Decimal("1"), succeeded=6),
                _cell("b", "y", capability_group="g2", cost_median=Decimal("99")),
            ],
        )
        outcome = recommend(matrix)
        assert outcome.chosen is not None
        assert outcome.chosen.executor == "b"

    def test_cheaper_wins_inside_one_group(self) -> None:
        """**同一档里分不出高下** —— 那就选便宜的。"""
        matrix = CapabilityMatrix.of(
            [
                _cell("a", "x", capability_group="g1", cost_median=Decimal("9")),
                _cell("b", "y", capability_group="g1", cost_median=Decimal("2")),
            ],
        )
        outcome = recommend(matrix)
        assert outcome.chosen is not None
        assert outcome.chosen.executor == "b"
        assert "分不开" in outcome.reason

    def test_missing_cost_sorts_last_inside_a_group(self) -> None:
        """**没测出成本不等于免费** —— 未知排在已知后面。"""
        matrix = CapabilityMatrix.of(
            [
                _cell("a", "x", capability_group="g1", cost_median=None),
                _cell("b", "y", capability_group="g1", cost_median=Decimal("2")),
            ],
        )
        outcome = recommend(matrix)
        assert outcome.chosen is not None
        assert outcome.chosen.executor == "b"

    def test_ordering_is_total_and_stable(self) -> None:
        """**同样的输入永远给出同样的顺序** —— 否则"为什么是它"答不上来。"""
        matrix = CapabilityMatrix.of(
            [
                _cell("a", "x", capability_group="g1", cost_median=Decimal("2")),
                _cell("b", "y", capability_group="g1", cost_median=Decimal("2")),
            ],
        )
        first = recommend(matrix)
        second = recommend(matrix)
        assert [cell.key for cell in first.ranked] == [cell.key for cell in second.ranked]
        assert first.chosen is not None
        assert first.chosen.executor == "a"

    def test_rejected_cells_carry_reasons(self) -> None:
        matrix = CapabilityMatrix.of([_cell("a", "x"), _cell("b", "y")])
        outcome = recommend(matrix, SelectionRequest(executors=("a",)))
        assert len(outcome.rejected) == 1
        assert outcome.rejected[0].cell.executor == "b"
        assert "候选" in outcome.rejected[0].reason


class TestCellSerialization:
    """**一格长什么样只有一处定义。**"""

    def test_round_trip_keeps_every_field(self) -> None:
        cell = _cell(
            "a",
            "x",
            cost_median=Decimal("1.25"),
            duration_median=Decimal("76.5"),
            tokens_median=1234,
            currency="USD",
            capability_group="g1",
        )
        restored = cell_from_dict(cell_to_dict(cell))
        assert restored == cell

    def test_missing_cost_stays_missing(self) -> None:
        """**少一列就少一列** —— 不拿 0 顶上。"""
        restored = cell_from_dict({"executor": "a", "model": "x", "runs": 3})
        assert restored.cost_median is None
        assert restored.duration_median is None
        assert restored.tokens_median is None
        assert restored.runs == 3

    def test_zero_cost_survives_the_round_trip(self) -> None:
        """**0 与没有是两回事** —— 0 得保住。"""
        restored = cell_from_dict(
            {"executor": "a", "model": "x", "runs": 1, "cost_median": "0"},
        )
        assert restored.cost_median == Decimal(0)
