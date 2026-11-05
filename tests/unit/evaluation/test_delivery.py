"""TASK-P1-071 —— 工程交付层指标。

验证（T1）：任务成功率、返工率、回归缺陷率各有**已知输入的期望值断言**。

三个指标的分母各不相同，而且**这个不同是有意的**：

- 任务成功率：全部任务 —— 只拿"跑完的任务"当分母，会让一个跑不完就放弃的
  系统显示出很高的成功率。
- 返工率：全部任务 —— 返工是交付过程的一部分，没跑完的任务也可能返过工。
- 回归缺陷率：**已完成**任务 —— 一个没完成的任务不构成交付，谈不上
  "引入了回归"。
"""

from __future__ import annotations

from decimal import Decimal

from evaluation.common import MetricState
from evaluation.task_evaluation.delivery import (
    TaskOutcome,
    regression_defect_rate,
    rework_rate,
    task_success_rate,
)


def _outcome(
    task_id: str,
    *,
    completed: bool = True,
    repair_count: int = 0,
    introduced_defects: int | None = None,
) -> TaskOutcome:
    return TaskOutcome(
        task_id=task_id,
        completed=completed,
        repair_count=repair_count,
        introduced_defects=introduced_defects,
    )


#: 一组固定的交付结果：4 个任务，3 个完成，其中 2 个返过工，
#: 已完成的任务里有 1 个带上了新缺陷。
_KNOWN_OUTCOMES = (
    _outcome("T-1", completed=True, repair_count=0, introduced_defects=0),
    _outcome("T-2", completed=True, repair_count=1, introduced_defects=2),
    _outcome("T-3", completed=True, repair_count=0, introduced_defects=0),
    _outcome("T-4", completed=False, repair_count=3, introduced_defects=5),
)


class TestTaskSuccessRate:
    def test_a_known_set_of_outcomes_gives_a_known_rate(self) -> None:
        result = task_success_rate(_KNOWN_OUTCOMES)

        assert result.metric_id == "task_success_rate"
        assert result.state is MetricState.COMPUTED
        assert result.numerator == Decimal(3)
        assert result.denominator == Decimal(4)
        assert result.value == Decimal("0.75")
        assert result.detail["total_tasks"] == 4

    def test_unfinished_tasks_stay_in_the_denominator(self) -> None:
        """跑不完就放弃的系统不该显示出很高的成功率。"""
        outcomes = [
            _outcome("T-1", completed=True),
            _outcome("T-2", completed=False),
            _outcome("T-3", completed=False),
        ]

        result = task_success_rate(outcomes)

        assert result.denominator == Decimal(3)
        assert result.value == Decimal("0.3333333333333333333333333333")

    def test_with_no_tasks_there_are_no_samples(self) -> None:
        result = task_success_rate([])

        assert result.state is MetricState.NO_SAMPLES
        assert result.value is None


class TestReworkRate:
    def test_a_known_set_of_outcomes_gives_a_known_rate(self) -> None:
        """T-2 与 T-4 返过工；T-4 虽然没完成，但返过工就是返过工。"""
        result = rework_rate(_KNOWN_OUTCOMES)

        assert result.metric_id == "rework_rate"
        assert result.state is MetricState.COMPUTED
        assert result.numerator == Decimal(2)
        assert result.denominator == Decimal(4)
        assert result.value == Decimal("0.5")

    def test_the_number_of_repairs_is_kept_alongside_the_rate(self) -> None:
        """ "有多少任务返过工"是率，"返了多少次"是量 —— 两个都要。"""
        result = rework_rate(_KNOWN_OUTCOMES)

        assert result.detail["total_repairs"] == 4
        assert result.detail["total_tasks"] == 4

    def test_many_repairs_on_one_task_count_as_one_reworked_task(self) -> None:
        """返工率按**任务**算，不是按次数算 —— 率与量的分工在这里。"""
        outcomes = [_outcome("T-1", repair_count=7)]

        result = rework_rate(outcomes)

        assert result.numerator == Decimal(1)
        assert result.denominator == Decimal(1)
        assert result.value == Decimal(1)
        assert result.detail["total_repairs"] == 7

    def test_with_no_tasks_there_are_no_samples(self) -> None:
        result = rework_rate([])

        assert result.state is MetricState.NO_SAMPLES
        assert result.value is None


class TestRegressionDefectRate:
    def test_a_known_set_of_outcomes_gives_a_known_rate(self) -> None:
        """已完成的三个任务里，只有 T-2 带上了新缺陷。"""
        result = regression_defect_rate(_KNOWN_OUTCOMES)

        assert result.metric_id == "regression_defect_rate"
        assert result.state is MetricState.COMPUTED
        assert result.numerator == Decimal(1)
        assert result.denominator == Decimal(3)
        assert result.detail["completed_tasks"] == 3

    def test_the_denominator_is_the_completed_tasks(self) -> None:
        """没完成的任务不构成交付 —— 它不进分母。"""
        outcomes = [
            _outcome("T-1", completed=True, introduced_defects=0),
            _outcome("T-2", completed=True, introduced_defects=3),
            _outcome("T-3", completed=False, introduced_defects=9),
        ]

        result = regression_defect_rate(outcomes)

        assert result.denominator == Decimal(2)
        assert result.numerator == Decimal(1)
        assert result.value == Decimal("0.5")

    def test_the_defect_count_of_unfinished_tasks_is_excluded(self) -> None:
        """缺陷个数只统计已完成任务 —— 否则会去数没交付出来的东西。"""
        result = regression_defect_rate(_KNOWN_OUTCOMES)

        assert result.detail["introduced_defects"] == 2

    def test_without_any_completed_task_there_are_no_samples(self) -> None:
        """一个都没交付 —— 无从谈回归，而不是"回归率 0%"。"""
        outcomes = [
            _outcome("T-1", completed=False, introduced_defects=4),
            _outcome("T-2", completed=False),
        ]

        result = regression_defect_rate(outcomes)

        assert result.state is MetricState.NO_SAMPLES
        assert result.value is None

    def test_a_rate_of_zero_is_a_computed_result(self) -> None:
        """交付了，而且查过没查出新缺陷 —— 这是"试过了，确实是零"。"""
        outcomes = [
            _outcome("T-1", completed=True, introduced_defects=0),
            _outcome("T-2", completed=True, introduced_defects=0),
        ]

        result = regression_defect_rate(outcomes)

        assert result.state is MetricState.COMPUTED
        assert result.value == Decimal(0)
        assert result.is_computed is True

    def test_missing_defect_data_is_not_reported_as_zero(self) -> None:
        """缺陷数据尚不可得 —— 报"算不出来"，不报 0。

        阶段一的库里还没有缺陷表。若把"没查过"折成 0，任何一次交付都会
        报出"回归缺陷率 0%" —— 一个**假的安全信号**，比"缺数据"危险得多。
        """
        outcomes = [
            _outcome("T-1", completed=True),
            _outcome("T-2", completed=True),
        ]

        result = regression_defect_rate(outcomes)

        assert result.state is MetricState.BASELINE_MISSING
        assert result.value is None
        assert result.is_computed is False
        assert result.detail["tasks_with_defect_data"] == 0

    def test_tasks_without_defect_data_leave_the_denominator(self) -> None:
        """ "查过了没查出缺陷"是知识，"没查过"不是 —— 后者不进分母。"""
        outcomes = [
            _outcome("T-1", completed=True, introduced_defects=0),
            _outcome("T-2", completed=True, introduced_defects=2),
            _outcome("T-3", completed=True),
        ]

        result = regression_defect_rate(outcomes)

        assert result.denominator == Decimal(2)
        assert result.numerator == Decimal(1)
        assert result.value == Decimal("0.5")
        assert result.detail["completed_tasks"] == 3
        assert result.detail["tasks_with_defect_data"] == 2
