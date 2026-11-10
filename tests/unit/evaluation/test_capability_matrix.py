"""TASK-P4-030 —— 二维能力矩阵：从实测记录长出一张能据此选型的表。

验证四件事：

1. **口径与一维评测完全一致** —— 同一套 :func:`evaluation.executors.benchmark.
   evaluate_executor`，两张表才可比。
2. **没记模型的运行切不进任何格子** —— 宁可少一格，不要有一格是凭空的。
3. **统计上分不开的格子归同一档** —— 拿噪声当差别，选出来的组合下一轮就不成立。
4. **交互项能回答"能不能把执行器与模型分开选"**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from core.policy.capability import CapabilityCell, CapabilityMatrix
from evaluation.agents.capability_matrix import (
    build_board,
    build_cell,
    build_matrix,
    capability_groups,
    compare_cells,
    dump_matrix,
    interaction_test,
    load_matrix,
    unclassified_runs,
)
from evaluation.executors.benchmark import (
    PriceTable,
    RunOutcome,
    RunRecord,
    TokenUsage,
)

#: 一张假的价目表 —— **每百万输入 0.30，每百万输出 1.20**。
PRICES = PriceTable(
    input=Decimal("0.30"),
    output=Decimal("1.20"),
    cache_read=Decimal("0.06"),
    cache_write=Decimal("0.30"),
    currency="USD",
    source="测试用的假价目表",
)


@dataclass(frozen=True, slots=True)
class _Extra:
    """一条运行记录里那些**不常变**的部分。"""

    regressions: int | None = 0
    suite_ran: bool = True
    duration: str | None = "76"
    tokens: TokenUsage | None = None


#: 默认的那一份 —— **模块级单例当默认值**。
_DEFAULT_EXTRA: _Extra = _Extra()


def _run(
    executor: str,
    model: str,
    outcome: RunOutcome = RunOutcome.SUCCEEDED,
    *,
    task_id: str = "pilot-001",
    extra: _Extra = _DEFAULT_EXTRA,
) -> RunRecord:
    """造一条运行记录。"""
    return RunRecord(
        task_id=task_id,
        executor=executor,
        attempt=1,
        outcome=outcome,
        regressions=extra.regressions,
        suite_ran=extra.suite_ran,
        model=model,
        duration_seconds=(Decimal(extra.duration) if extra.duration is not None else None),
        tokens=extra.tokens,
    )


def _runs(
    executor: str,
    model: str,
    outcomes: Sequence[RunOutcome],
    *,
    prefix: str = "pilot",
) -> list[RunRecord]:
    """造一组运行记录 —— **结局按给的顺序排**。

    **"该跑几次里做对几次"就是这样数出来的** —— 成功、失败、没跑成混在一起，
    分母才是真的分母。
    """
    return [
        _run(executor, model, outcome, task_id=f"{prefix}-{index:03d}")
        for index, outcome in enumerate(outcomes, start=1)
    ]


def _ok(count: int) -> list[RunOutcome]:
    """成功的若干次。"""
    return [RunOutcome.SUCCEEDED] * count


def _bad(count: int) -> list[RunOutcome]:
    """失败的若干次。"""
    return [RunOutcome.FAILED] * count


def _missed(count: int) -> list[RunOutcome]:
    """没跑成的若干次。"""
    return [RunOutcome.NOT_RUN] * count


class TestBuildCell:
    """一格的算法 —— **口径与一维评测一致**。"""

    def test_runs_are_counted(self) -> None:
        cell = build_cell("claude-code", "m", _runs("claude-code", "m", _ok(5) + _bad(2)))
        assert cell.runs == 7
        assert cell.executed == 7
        assert cell.succeeded == 5

    def test_not_run_is_not_a_failure(self) -> None:
        """**跑不成与做不对是两回事** —— 归因不同，报表也不同。"""
        cell = build_cell("claude-code", "m", _runs("claude-code", "m", _ok(4) + _missed(3)))
        assert cell.runs == 7
        assert cell.executed == 4
        assert cell.succeeded == 4
        assert cell.success_rate == Decimal(1)
        assert cell.overall_rate == Decimal(4) / Decimal(7)

    def test_cost_needs_a_price_table(self) -> None:
        """**没有价目表就没有成本这一列** —— 宁可空着，也不拿自报数比。"""
        runs = [
            _run("claude-code", "m", extra=_Extra(tokens=TokenUsage(input=1_000_000, output=0))),
        ]
        assert build_cell("claude-code", "m", runs).cost_median is None
        assert build_cell("claude-code", "m", runs, prices=PRICES).cost_median == Decimal("0.30")

    def test_duration_and_tokens_are_medians(self) -> None:
        runs = [
            _run("claude-code", "m", extra=_Extra(duration="10", tokens=TokenUsage(input=100))),
            _run("claude-code", "m", extra=_Extra(duration="20", tokens=TokenUsage(input=300))),
        ]
        cell = build_cell("claude-code", "m", runs)
        assert cell.duration_median == Decimal("15")
        assert cell.tokens_median == 200

    def test_regressions_are_counted_only_where_the_suite_ran(self) -> None:
        runs = [
            _run("claude-code", "m", extra=_Extra(regressions=2)),
            _run("claude-code", "m", extra=_Extra(regressions=0, suite_ran=False)),
        ]
        cell = build_cell("claude-code", "m", runs)
        assert cell.measured_runs == 1
        assert cell.regressed == 1
        assert cell.regression_rate == Decimal(1)


class TestBuildMatrix:
    """切片 —— **没记模型的运行切不进任何格子**。"""

    def test_cells_are_grouped_by_executor_and_model(self) -> None:
        matrix = build_matrix(
            [
                *_runs("claude-code", "m1", _ok(3), prefix="a"),
                *_runs("claude-code", "m2", _ok(2), prefix="b"),
                *_runs("codex", "m1", _ok(1), prefix="c"),
            ]
        )
        assert len(matrix.cells) == 3
        assert matrix.executors == ("claude-code", "codex")
        assert matrix.models == ("m1", "m2")

    def test_runs_without_a_model_are_dropped(self) -> None:
        """**宁可少一格** —— 不要有一格是凭空的。"""
        runs = [
            *_runs("claude-code", "m1", _ok(2)),
            _run("codex", "", outcome=RunOutcome.SUCCEEDED),
        ]
        matrix = build_matrix(runs)
        assert len(matrix.cells) == 1
        assert matrix.cells[0].key == ("claude-code", "m1")

    def test_unclassified_runs_are_reported(self) -> None:
        runs = [_run("codex", "", outcome=RunOutcome.SUCCEEDED)]
        assert len(unclassified_runs(runs)) == 1

    def test_prices_are_looked_up_per_model(self) -> None:
        runs = [
            _run("claude-code", "m1", extra=_Extra(tokens=TokenUsage(input=1_000_000, output=0))),
        ]
        matrix = build_matrix(runs, prices={"m1": PRICES})
        cell = matrix.cell("claude-code", "m1")
        assert cell is not None
        assert cell.cost_median == Decimal("0.30")


class TestComparisons:
    """两两比较 —— **哪些格子分得开**。"""

    def test_identical_cells_are_not_distinguishable(self) -> None:
        matrix = build_matrix(
            [
                *_runs("a", "m", _ok(9), prefix="x"),
                *_runs("b", "m", _ok(9), prefix="y"),
            ]
        )
        comparisons = compare_cells(matrix)
        assert len(comparisons) == 1
        assert comparisons[0].significant is False
        assert comparisons[0].distinguishable is False

    def test_clearly_different_cells_are_distinguishable(self) -> None:
        matrix = build_matrix(
            [
                *_runs("a", "m", _ok(18), prefix="x"),
                *_runs("b", "m", _ok(0) + _bad(18), prefix="y"),
            ]
        )
        comparisons = compare_cells(matrix)
        assert comparisons[0].significant is True

    def test_unmeasured_cells_are_not_compared(self) -> None:
        """**没跑过的格子没有分母** —— 比不了就不比。"""
        measured = build_matrix([*_runs("a", "m", _ok(3))])
        matrix = CapabilityMatrix.of(
            [*measured.cells, CapabilityCell(executor="b", model="m")],
        )
        assert compare_cells(matrix) == ()

    def test_adjusted_p_is_not_smaller_than_raw(self) -> None:
        """**Holm 校正只会把 p 变大** —— 多比几对，证据就得更硬。"""
        matrix = build_matrix(
            [
                *_runs("a", "m", _ok(9), prefix="x"),
                *_runs("b", "m", _ok(9), prefix="y"),
                *_runs("c", "m", _ok(9), prefix="z"),
            ]
        )
        for comparison in compare_cells(matrix):
            assert comparison.adjusted_p_value >= comparison.test.p_value


class TestCapabilityGroups:
    """分档 —— **分不开的归一档**。"""

    def test_indistinguishable_cells_share_a_group(self) -> None:
        matrix = build_matrix(
            [
                *_runs("a", "m", _ok(9), prefix="x"),
                *_runs("b", "m", _ok(9), prefix="y"),
            ]
        )
        groups = capability_groups(matrix, compare_cells(matrix))
        assert groups[("a", "m")] == groups[("b", "m")]

    def test_distinguishable_cells_land_in_different_groups(self) -> None:
        matrix = build_matrix(
            [
                *_runs("a", "m", _ok(18), prefix="x"),
                *_runs("b", "m", _ok(0) + _bad(18), prefix="y"),
            ]
        )
        groups = capability_groups(matrix, compare_cells(matrix))
        assert groups[("a", "m")] != groups[("b", "m")]

    def test_group_names_are_prefixed(self) -> None:
        matrix = build_matrix([*_runs("a", "m", _ok(3))])
        groups = capability_groups(matrix, compare_cells(matrix))
        assert all(name.startswith("g") for name in groups.values())


class TestInteraction:
    """交互项 —— **模型的效果是不是因执行器而异**。"""

    def test_one_executor_has_no_interaction(self) -> None:
        """**一个执行器上没有"交互"这回事**。"""
        runs = [*_runs("a", "m1", _ok(4), prefix="x"), *_runs("a", "m2", _ok(4))]
        assert interaction_test(runs, model_first="m1", model_second="m2") is None

    def test_no_interaction_when_effects_match(self) -> None:
        """**两个执行器上模型的效果一样** —— 那就可以分开选。"""
        runs = [
            *_runs("a", "m1", _ok(4), prefix="x"),
            *_runs("a", "m2", _ok(0) + _bad(4), prefix="y"),
            *_runs("b", "m1", _ok(4), prefix="z"),
            *_runs("b", "m2", _ok(0) + _bad(4), prefix="w"),
        ]
        result = interaction_test(runs, model_first="m1", model_second="m2")
        assert result is not None
        assert result.significant is False
        assert "可以分开选" in result.verdict

    def test_interaction_is_detected(self) -> None:
        """**同一个模型在两个执行器上一个好一个坏** —— 那就是交互。"""
        runs = [
            *_runs("a", "m1", _ok(4), prefix="x"),
            *_runs("a", "m2", _ok(0) + _bad(4), prefix="y"),
            *_runs("b", "m1", _ok(0) + _bad(4), prefix="z"),
            *_runs("b", "m2", _ok(4), prefix="w"),
        ]
        result = interaction_test(runs, model_first="m1", model_second="m2")
        assert result is not None
        assert result.significant is True
        assert "不能分开选" in result.verdict

    def test_result_reports_which_models_were_compared(self) -> None:
        runs = [
            *_runs("a", "m1", _ok(4), prefix="x"),
            *_runs("a", "m2", _ok(4), prefix="y"),
            *_runs("b", "m1", _ok(4), prefix="z"),
            *_runs("b", "m2", _ok(4), prefix="w"),
        ]
        result = interaction_test(runs, model_first="m1", model_second="m2")
        assert result is not None
        assert result.models == ("m1", "m2")


class TestBoard:
    """看板 —— **给人看的**。"""

    def test_board_renders_matrix_and_selection(self) -> None:
        runs = [
            *_runs("claude-code", "m1", _ok(9), prefix="x"),
            *_runs("claude-code", "m2", _ok(4), prefix="y"),
            *_runs("codex", "m1", _ok(9), prefix="z"),
        ]
        board = build_board(runs)
        text = board.render()
        assert "claude-code" in text
        assert "m1" in text
        assert "交互项" in text

    def test_board_counts_unclassified_runs(self) -> None:
        runs = [*_runs("a", "m", _ok(2)), _run("b", "", outcome=RunOutcome.SUCCEEDED)]
        board = build_board(runs)
        assert board.unclassified == 1
        assert "没记模型" in board.render()

    def test_board_reports_no_samples_as_missing(self) -> None:
        """**"无样本"与"成功率 0"在报表上必须长得不一样**。"""
        runs = [*_runs("a", "m", _ok(2))]
        board = build_board(runs)
        assert "无样本" in board.render()


class TestSerialization:
    """存盘与读回 —— **一格长什么样只有一处定义**。"""

    def test_round_trip(self, tmp_path: Path) -> None:
        runs = [
            *_runs("claude-code", "m1", _ok(9), prefix="x"),
            *_runs("codex", "m2", _ok(3), prefix="y"),
        ]
        matrix = build_matrix(runs, prices={"m1": PRICES, "m2": PRICES})
        path = tmp_path / "matrix.json"
        dump_matrix(matrix, path)
        restored = load_matrix(path)
        assert restored.cells == matrix.cells

    def test_round_trip_keeps_capability_groups(self, tmp_path: Path) -> None:
        """**能力档要一起写进去** —— 运行时不该重算统计量，但得知道谁跟谁分不开。"""
        matrix = build_matrix([*_runs("a", "m", _ok(3))], capability_groups={("a", "m"): "g1"})
        path = tmp_path / "matrix.json"
        dump_matrix(matrix, path)
        restored = load_matrix(path)
        cell = restored.cell("a", "m")
        assert cell is not None
        assert cell.capability_group == "g1"

    def test_broken_file_is_not_silently_empty(self, tmp_path: Path) -> None:
        """**读坏了就说读坏了** —— 空矩阵会让选型悄悄退化成"随便挑一个"。"""
        path = tmp_path / "matrix.json"
        path.write_text('{"cells": "not-a-list"}', encoding="utf-8")
        try:
            load_matrix(path)
        except ValueError as error:
            assert "cells" in str(error)
        else:
            raise AssertionError("读坏的矩阵文件必须报错，不能读成空矩阵")
