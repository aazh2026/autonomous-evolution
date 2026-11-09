"""基线口径的单元测试 —— **"基线未建立"不许变成 0**。

设计依据：docs/tasks.md TASK-P1-072（T1 ① 基线表可被程序读取并用于阈值
比较；T1 ② 基线缺失时相关 T2 指标明确报"基线未建立"而非返回 0；T2 基线表
中位数为后续成本预算提供依据）。

**这一层只测口径，不测采集。** 采集要起进程、跑测试、读覆盖率（那是
``tests/system/test_baseline_collection.py`` 的事），而口径必须能被单独
证伪 —— 口径错了，采集得再准也是错的。

**每条断言都在盯一件"看起来差不多但含义相反"的事。** "基线未建立"与
"基线是 0"、"没测到"与"测出来是 0"、"超出基线"与"没法判断" —— 这些在
报表上常常长得一模一样，而这正是要守住的地方。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import pytest

from core.metrics.cost import CostAmount, CurrencyMismatchError, TokenUsage
from evaluation.baselines import (
    BASELINE_MISSING_TEXT,
    DEFAULT_FACTOR,
    BaselineError,
    BaselineMetric,
    BaselineTable,
    TaskMeasurement,
    calibrate_daily_cost,
    median,
)
from evaluation.common import MetricState

#: "没给用量"与"给了零用量"必须分得开 —— 用一个哨兵来区分。
_NO_USAGE: Final[object] = object()


def _measurement(
    kind: str = "bug_fix",
    *,
    duration: str = "10",
    tool_calls: int = 3,
    usage: TokenUsage | object | None = _NO_USAGE,
    cost: CostAmount | None = None,
    coverage: Decimal | None = None,
    coverage_by_module: dict[str, Decimal] | None = None,
    source: str = "claude-code:test:completed",
) -> TaskMeasurement:
    """造一条测量 —— 默认给全套数据，测"缺哪一样"时再拿掉。"""
    resolved = TokenUsage(input_tokens=100, output_tokens=50) if usage is _NO_USAGE else usage
    return TaskMeasurement(
        task_kind=kind,
        duration_seconds=Decimal(duration),
        tool_calls=tool_calls,
        usage=resolved,  # type: ignore[arg-type]
        cost=cost,
        coverage=coverage,
        coverage_by_module=coverage_by_module or {},
        source=source,
    )


def _table(*measurements: TaskMeasurement) -> BaselineTable:
    return BaselineTable(measurements, pilot="pilot_repo")


class TestTheMedian:
    """中位数 —— **偶数个样本取中间两个的平均**。"""

    def test_an_odd_number_of_samples_takes_the_middle_one(self) -> None:
        assert median([Decimal(3), Decimal(1), Decimal(2)]) == Decimal(2)

    def test_an_even_number_of_samples_averages_the_two_middle_ones(self) -> None:
        """取下中位数会让一半样本落在基线之上 —— 那"一半比基线便宜"就是假的。"""
        assert median([Decimal(1), Decimal(2), Decimal(3), Decimal(4)]) == Decimal("2.5")

    def test_the_result_is_ordered_independently_of_the_input_order(self) -> None:
        assert median([Decimal(4), Decimal(1), Decimal(3), Decimal(2)]) == Decimal("2.5")

    def test_the_quantum_is_applied(self) -> None:
        assert median([Decimal(1), Decimal(2)], quantum=Decimal("0.01")) == Decimal("1.50")

    def test_an_empty_sample_is_an_error(self) -> None:
        """空样本没有中位数 —— 不是 0。"""
        with pytest.raises(BaselineError):
            median([])


class TestTheMeasurement:
    """一条测量 —— **没测到的量不许变成 0**。"""

    def test_a_missing_usage_is_not_zero_tokens(self) -> None:
        """没读到用量与"用了 0 个 token"必须分得开。"""
        measurement = _measurement(usage=None)
        assert measurement.value_of(BaselineMetric.TOKENS) is None

    def test_a_missing_cost_is_not_zero_cost(self) -> None:
        measurement = _measurement(cost=None)
        assert measurement.value_of(BaselineMetric.COST) is None

    def test_a_missing_coverage_is_not_zero_coverage(self) -> None:
        measurement = _measurement(coverage=None)
        assert measurement.value_of(BaselineMetric.COVERAGE) is None

    def test_a_measured_coverage_of_zero_is_zero(self) -> None:
        """测出来是 0 就是 0 —— 与"没测"是两件事。"""
        measurement = _measurement(coverage=Decimal(0))
        assert measurement.value_of(BaselineMetric.COVERAGE) == Decimal(0)

    def test_a_blank_task_kind_is_rejected(self) -> None:
        with pytest.raises(BaselineError):
            _measurement(kind="  ")

    def test_a_negative_duration_is_rejected(self) -> None:
        with pytest.raises(BaselineError):
            _measurement(duration="-1")

    def test_a_negative_tool_call_count_is_rejected(self) -> None:
        with pytest.raises(BaselineError):
            _measurement(tool_calls=-1)

    def test_a_coverage_above_one_is_rejected(self) -> None:
        with pytest.raises(BaselineError):
            _measurement(coverage=Decimal("1.5"))

    def test_a_module_coverage_above_one_is_rejected(self) -> None:
        with pytest.raises(BaselineError):
            _measurement(coverage_by_module={"tinylib.stats": Decimal("2")})


class TestTheBaseline:
    """基线是**按任务类别**给的中位数。"""

    def test_the_baseline_is_the_median_of_that_kind(self) -> None:
        """**中位数**，不是平均值 —— 样本刻意不对称，两者算得不一样。"""
        table = _table(
            _measurement("bug_fix", duration="10"),
            _measurement("bug_fix", duration="20"),
            _measurement("bug_fix", duration="100"),
        )
        assert table.median("bug_fix", BaselineMetric.DURATION) == Decimal(20)

    def test_each_kind_gets_its_own_baseline(self) -> None:
        """改文档的任务与改核心逻辑的任务不该共用一个基线。"""
        table = _table(
            _measurement("bug_fix", duration="10"),
            _measurement("doc_update", duration="100"),
        )
        assert table.median("bug_fix", BaselineMetric.DURATION) == Decimal(10)
        assert table.median("doc_update", BaselineMetric.DURATION) == Decimal(100)

    def test_a_kind_without_samples_has_no_baseline(self) -> None:
        """没有样本 = 基线未建立，不是基线 0。"""
        table = _table(_measurement("bug_fix"))
        assert table.baseline("refactor", BaselineMetric.DURATION) is None

    def test_the_cost_baseline_carries_the_currency(self) -> None:
        table = _table(_measurement(cost=CostAmount(Decimal("1.5"), "usd")))
        baseline = table.baseline("bug_fix", BaselineMetric.COST)
        assert baseline is not None
        assert baseline.currency == "USD"
        assert baseline.median == Decimal("1.500000")

    def test_the_sources_are_kept_for_review(self) -> None:
        """逐条留档 —— 好让人复核这个中位数是怎么来的。"""
        table = _table(
            _measurement(duration="10", source="claude-code:a:completed"),
            _measurement(duration="20", source="claude-code:b:completed"),
        )
        baseline = table.baseline("bug_fix", BaselineMetric.DURATION)
        assert baseline is not None
        assert baseline.sources == ("claude-code:a:completed", "claude-code:b:completed")

    def test_two_currencies_in_one_table_are_rejected(self) -> None:
        """中位数对两个项目的钱没有意义。"""
        with pytest.raises(CurrencyMismatchError):
            _table(
                _measurement(cost=CostAmount(Decimal("1"), "USD")),
                _measurement(cost=CostAmount(Decimal("1"), "CNY")),
            )

    def test_a_table_without_cost_samples_has_no_currency(self) -> None:
        table = _table(_measurement())
        assert table.currency is None


class TestTheModuleCoverage:
    """模块级覆盖率 —— **只算报了这个模块的样本**。"""

    def test_only_the_reported_modules_are_counted(self) -> None:
        table = _table(
            _measurement(coverage_by_module={"tinylib.stats": Decimal("0.9")}),
            _measurement(coverage_by_module={"tinylib.money": Decimal("0.5")}),
        )
        modules = {item.module: item for item in table.module_coverage()}
        assert modules["tinylib.stats"].sample_count == 1
        assert modules["tinylib.money"].sample_count == 1

    def test_a_module_reported_twice_gets_the_median(self) -> None:
        table = _table(
            _measurement(coverage_by_module={"tinylib.stats": Decimal("0.4")}),
            _measurement(coverage_by_module={"tinylib.stats": Decimal("0.8")}),
        )
        modules = {item.module: item for item in table.module_coverage()}
        assert modules["tinylib.stats"].median == Decimal("0.600000")
        assert modules["tinylib.stats"].sample_count == 2

    def test_a_module_absent_from_a_sample_is_not_a_zero(self) -> None:
        """这个模块这次没测，不是这个模块覆盖率为 0。"""
        table = _table(
            _measurement(coverage_by_module={"tinylib.stats": Decimal("0.9")}),
            _measurement(coverage_by_module={"tinylib.money": Decimal("0.5")}),
        )
        modules = {item.module: item for item in table.module_coverage()}
        assert modules["tinylib.stats"].sample_count == 1


class TestTheComparison:
    """T1 ① 与 T1 ② —— **缺基线时报"基线未建立"，不报 0**。"""

    def test_a_computed_comparison_gives_the_ratio(self) -> None:
        table = _table(
            _measurement(duration="10"),
            _measurement(duration="20"),
            _measurement(duration="30"),
        )
        comparison = table.compare(_measurement(duration="40"), BaselineMetric.DURATION)
        assert comparison.state is MetricState.COMPUTED
        assert comparison.baseline == Decimal(20)
        assert comparison.ratio == Decimal(2)

    def test_a_missing_baseline_says_so(self) -> None:
        """T1 ② 的落点。"""
        table = _table(_measurement("bug_fix"))
        comparison = table.compare(_measurement("refactor"), BaselineMetric.DURATION)
        assert comparison.state is MetricState.BASELINE_MISSING
        assert BASELINE_MISSING_TEXT in comparison.message

    def test_a_missing_baseline_has_no_ratio(self) -> None:
        table = _table(_measurement("bug_fix"))
        comparison = table.compare(_measurement("refactor"), BaselineMetric.DURATION)
        assert comparison.ratio is None

    def test_a_missing_baseline_is_not_declared_within(self) -> None:
        """**是 None，不是 False。** ``False`` 说的是"超了"，而"没法判断"是另一回事。"""
        table = _table(_measurement("bug_fix"))
        comparison = table.compare(_measurement("refactor"), BaselineMetric.DURATION)
        assert comparison.within_baseline is None

    def test_a_missing_baseline_does_not_report_a_zero_baseline(self) -> None:
        table = _table(_measurement("bug_fix"))
        comparison = table.compare(_measurement("refactor"), BaselineMetric.DURATION)
        assert comparison.baseline is None

    def test_a_missing_measurement_is_a_different_state(self) -> None:
        """基线在、这次没测到 —— 与"基线没建立"是两回事。"""
        table = _table(_measurement(coverage=Decimal("0.5")))
        comparison = table.compare(_measurement(coverage=None), BaselineMetric.COVERAGE)
        assert comparison.state is MetricState.NO_SAMPLES
        assert comparison.within_baseline is None

    def test_an_observation_within_the_factor_is_within(self) -> None:
        table = _table(_measurement(duration="10"))
        comparison = table.compare(_measurement(duration="15"), BaselineMetric.DURATION)
        assert comparison.within_baseline is True

    def test_an_observation_beyond_the_factor_is_not(self) -> None:
        table = _table(_measurement(duration="10"))
        comparison = table.compare(_measurement(duration="25"), BaselineMetric.DURATION)
        assert comparison.within_baseline is False

    def test_the_factor_is_configurable(self) -> None:
        table = _table(_measurement(duration="10"))
        comparison = table.compare(
            _measurement(duration="15"), BaselineMetric.DURATION, factor=Decimal("1.5")
        )
        assert comparison.within_baseline is True

    def test_the_default_factor_is_two(self) -> None:
        """与 TASK-P1-081 的验收口径一致。"""
        assert Decimal(2) == DEFAULT_FACTOR

    def test_a_currency_mismatch_is_refused(self) -> None:
        """拿两笔不同的钱互相比，比出来的倍数没有意义。"""
        table = _table(_measurement(cost=CostAmount(Decimal("1"), "USD")))
        with pytest.raises(CurrencyMismatchError):
            table.compare(_measurement(cost=CostAmount(Decimal("1"), "CNY")), BaselineMetric.COST)


class TestTheCalibration:
    """T2 —— **中位数为 ``max_daily_cost`` 提供依据**。"""

    def test_the_daily_limit_is_the_median_times_the_task_count_times_the_factor(self) -> None:
        """**中位数**，不是平均值 —— 样本刻意不对称，两者算得不一样。

        ``[1, 2, 10]`` 的中位数是 2、平均值是 4.33。若哪天有人把中位数换成
        平均值，一次跑飞的任务就会把预算拽高一倍 —— 而预算本该按"正常水位"
        定。
        """
        table = _table(
            _measurement(cost=CostAmount(Decimal("1"), "USD")),
            _measurement(cost=CostAmount(Decimal("2"), "USD")),
            _measurement(cost=CostAmount(Decimal("10"), "USD")),
        )
        calibration = calibrate_daily_cost(table, tasks_per_day=20)
        assert calibration.state is MetricState.COMPUTED
        assert calibration.median_cost_per_task == Decimal("2.000000")
        assert calibration.max_daily_cost == Decimal("80.000000")

    def test_a_missing_cost_baseline_gives_no_limit(self) -> None:
        """缺基线时报"基线未建立" —— 不是给一个 0 元的预算。"""
        table = _table(_measurement())
        calibration = calibrate_daily_cost(table, tasks_per_day=20)
        assert calibration.state is MetricState.BASELINE_MISSING
        assert calibration.max_daily_cost is None
        assert BASELINE_MISSING_TEXT in calibration.message

    def test_a_missing_cost_baseline_gives_no_budget(self) -> None:
        """0 元的预算会让系统第一天就拒绝任何执行，而看板显示"预算正常"。"""
        table = _table(_measurement())
        calibration = calibrate_daily_cost(table, tasks_per_day=20)
        assert calibration.to_budget() is None

    def test_a_computed_calibration_gives_a_budget(self) -> None:
        table = _table(_measurement(cost=CostAmount(Decimal("1"), "USD")))
        calibration = calibrate_daily_cost(table, tasks_per_day=20)
        budget = calibration.to_budget()
        assert budget is not None
        assert budget.currency == "USD"
        assert budget.max_daily_cost == Decimal("40.000000")

    def test_a_non_positive_task_count_is_rejected(self) -> None:
        table = _table(_measurement(cost=CostAmount(Decimal("1"), "USD")))
        with pytest.raises(BaselineError):
            calibrate_daily_cost(table, tasks_per_day=0)

    def test_a_non_positive_factor_is_rejected(self) -> None:
        table = _table(_measurement(cost=CostAmount(Decimal("1"), "USD")))
        with pytest.raises(BaselineError):
            calibrate_daily_cost(table, tasks_per_day=20, factor=Decimal(0))


class TestTheTableOnDisk:
    """基线表是**数据** —— 写得出去就要读得回来。"""

    def test_the_yaml_round_trip_is_byte_identical(self) -> None:
        table = _table(
            _measurement(
                duration="12.345",
                usage=TokenUsage(
                    input_tokens=10,
                    output_tokens=20,
                    cache_read_tokens=30,
                    cache_write_tokens=40,
                ),
                cost=CostAmount(Decimal("1.25"), "USD"),
                coverage=Decimal("0.812345"),
                coverage_by_module={"tinylib.stats": Decimal("0.9")},
            )
        )
        text = table.to_yaml()
        assert BaselineTable.from_yaml(text).to_yaml() == text

    def test_the_amounts_are_written_as_strings(self) -> None:
        """YAML 里裸写的 ``0.1`` 是浮点数 —— 金额差最后几位就是差钱。"""
        table = _table(_measurement(cost=CostAmount(Decimal("0.1"), "USD")))
        assert "0.100000" in table.to_yaml()

    def test_an_unknown_key_is_refused(self) -> None:
        """少写一个字段会让那一列静默消失 —— 而消失的列看起来跟"没测过"一样。"""
        text = _table(_measurement()).to_yaml() + "unexpected: 1\n"
        with pytest.raises(BaselineError):
            BaselineTable.from_yaml(text)

    def test_a_broken_yaml_is_refused(self) -> None:
        with pytest.raises(BaselineError):
            BaselineTable.from_yaml("- 不是\n- 一个映射\n")

    def test_a_missing_pilot_is_refused(self) -> None:
        with pytest.raises(BaselineError):
            BaselineTable.from_yaml("measurements: []\n")

    def test_a_missing_usage_survives_the_round_trip(self) -> None:
        """没读到用量这件事，落盘之后也得看得出来。"""
        table = _table(_measurement(usage=None))
        restored = BaselineTable.from_yaml(table.to_yaml())
        measurement = restored.measurements[0]
        assert measurement.usage is None
        assert measurement.value_of(BaselineMetric.TOKENS) is None


class TestTheMarkdown:
    """人读版 —— **缺失的项写"基线未建立"，不留空**。"""

    def test_an_empty_table_says_the_baseline_is_missing(self) -> None:
        markdown = _table().render_markdown()
        assert BASELINE_MISSING_TEXT in markdown

    def test_the_table_lists_every_kind_and_metric(self) -> None:
        markdown = _table(_measurement("bug_fix", duration="10")).render_markdown()
        assert "bug_fix" in markdown
        assert "秒" in markdown

    def test_the_module_coverage_is_listed(self) -> None:
        markdown = _table(
            _measurement(coverage_by_module={"tinylib.stats": Decimal("0.9")})
        ).render_markdown()
        assert "tinylib.stats" in markdown

    def test_the_calibration_conclusion_is_stated(self) -> None:
        markdown = _table(_measurement(cost=CostAmount(Decimal("1"), "USD"))).render_markdown()
        assert "max_daily_cost" in markdown

    def test_a_missing_metric_is_written_as_missing(self) -> None:
        """**缺的那一行要写出来，不能不写。**

        少了这一行，读的人只会以为"这一类任务没有金额要求" —— 而真相是
        "这一类任务的金额还没采到"。两件事在报表上长得一模一样。
        """
        markdown = _table(_measurement(cost=None)).render_markdown()

        assert BASELINE_MISSING_TEXT in markdown

    def test_the_missing_row_names_the_metric(self) -> None:
        """**要说得清缺的是哪一项** —— 不然读者得自己猜。"""
        markdown = _table(_measurement(cost=None)).render_markdown()
        rows = [line for line in markdown.splitlines() if line.startswith("| bug_fix")]

        assert any("金额" in row and BASELINE_MISSING_TEXT in row for row in rows)

    def test_a_present_metric_is_not_written_as_missing(self) -> None:
        """**有的那一项照常写数** —— 别把"有"也写成"没有"。"""
        markdown = _table(_measurement(cost=CostAmount(Decimal("1"), "USD"))).render_markdown()
        rows = [line for line in markdown.splitlines() if line.startswith("| bug_fix")]

        assert any("金额" in row and "1" in row for row in rows)
