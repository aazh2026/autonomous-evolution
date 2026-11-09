"""基线采集的端到端 —— **表可被程序读取，缺基线时报"基线未建立"**。

设计依据：docs/tasks.md TASK-P1-072 的三条验收：

- **T1 ①** 基线表可被程序读取并用于阈值比较；
- **T1 ②** 基线缺失时相关 T2 指标明确报"基线未建立"而非返回 0；
- **T2** 基线表中位数为后续成本预算提供依据（阶段一用于 ``max_daily_cost``
  校准）。

**这里跑的是真的采集流程。** 采集器真的复制一份试点仓库、真的起一个子进程
（打桩 CLI 吐 NDJSON）、真的跑一遍覆盖率、真的落盘一份 YAML 与一份
Markdown。**不联网、不花钱、不碰真仓库。**

**为什么端到端地验。** 每一段单独看都对：口径算得准、YAML 写得对、文件
落得下。可"基线表能被用来比阈值"不是任何一段各自的性质 —— 它是**表落盘
之后还能被读回来**才有的性质。而落盘那一段最容易出事的地方是十进制量
（YAML 里的浮点数会悄悄改掉钱的最后几位）。

**断言的是"读回来的表还能用"，不是"文件存在"。**
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from core.metrics.cost import CostAmount, TokenUsage
from evaluation.baselines import (
    BASELINE_MISSING_TEXT,
    BaselineMetric,
    BaselineTable,
    TaskMeasurement,
    calibrate_daily_cost,
)
from evaluation.common import MetricState
from scripts.collect_baselines import REPRESENTATIVE_TASKS, main
from tests.unit.executors.stub_cli import claude_events, write_stub

#: 试点仓库 —— 就是仓库里那一份。
PILOT = Path(__file__).resolve().parents[2] / "samples" / "pilot_repo"

#: 打桩 CLI 报出来的成本。
CLI_COST = 0.42

#: 打桩 CLI 报出来的用量。
CLI_USAGE: dict[str, int] = {
    "input_tokens": 12_000,
    "output_tokens": 3_500,
    "cache_read_input_tokens": 20_000,
    "cache_creation_input_tokens": 1_200,
}


def _fingerprint(root: Path) -> dict[str, str]:
    """把一棵目录树的内容摘要下来 —— **用来证明采集没碰真仓库**。"""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def stub_cli(tmp_path: Path) -> Path:
    """一个会吐"真实形状"事件的打桩 CLI。

    **事件形状照着真实 Claude Code 写** —— 改这里要连带改
    :mod:`executors.claude_code.events` 的映射表，两边一起改。
    """
    script = write_stub(
        tmp_path / "cli",
        events=claude_events(
            cost_usd=CLI_COST,
            usage=CLI_USAGE,
            duration_ms=2_500,
            tool_calls=4,
        ),
    )
    return script


def _collect(tmp_path: Path, stub: Path, *, tasks: int = 2) -> Path:
    """跑一次采集，返回输出目录。"""
    output = tmp_path / "out"
    exit_code = main(
        [
            "--pilot",
            str(PILOT),
            "--output",
            str(output),
            "--binary",
            str(stub),
            "--tasks",
            str(tasks),
        ]
    )
    assert exit_code == 0
    return output


class TestTheCollection:
    """采集本身。"""

    def test_the_collector_writes_both_forms(self, tmp_path: Path, stub_cli: Path) -> None:
        """一份机读（YAML）、一份人读（Markdown）—— **两种读者都要有**。"""
        output = _collect(tmp_path, stub_cli)
        assert (output / "baselines.yaml").exists()
        assert (output / "baselines.md").exists()

    def test_the_machine_readable_table_parses_back(self, tmp_path: Path, stub_cli: Path) -> None:
        """T1 ① 的第一步 —— **读回来的表必须还是那张表**。"""
        output = _collect(tmp_path, stub_cli)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        assert table.pilot == PILOT.name
        assert len(table.measurements) == 2

    def test_the_cost_is_the_one_the_executor_reported(
        self, tmp_path: Path, stub_cli: Path
    ) -> None:
        """表里的钱是**执行器报上来的那个数** —— 不是我们算的。"""
        output = _collect(tmp_path, stub_cli)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        for measurement in table.measurements:
            assert measurement.cost is not None
            assert measurement.cost.amount == Decimal("0.42")
            assert measurement.cost.currency == "USD"

    def test_the_token_usage_is_read_from_the_result_event(
        self, tmp_path: Path, stub_cli: Path
    ) -> None:
        """四类 token 分开记 —— 并进输入里的那笔账会按错的价算。"""
        output = _collect(tmp_path, stub_cli)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        measurement = table.measurements[0]
        assert measurement.usage is not None
        assert measurement.usage.input_tokens == 12_000
        assert measurement.usage.output_tokens == 3_500
        assert measurement.usage.cache_read_tokens == 20_000
        assert measurement.usage.cache_write_tokens == 1_200

    def test_a_run_without_a_usage_is_not_recorded_as_zero_tokens(self, tmp_path: Path) -> None:
        """**没读到用量不是"用了 0 个 token"。**

        结果事件里没有 ``usage`` 字段时，那一笔用量就是"我们没读到"——
        记成 0 会让 token 基线偏低，而基线偏低意味着**预算偏松**。
        """
        script = write_stub(tmp_path / "cli", events=claude_events(cost_usd=CLI_COST))
        output = _collect(tmp_path, script, tasks=1)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        measurement = table.measurements[0]
        assert measurement.usage is None
        assert measurement.value_of(BaselineMetric.TOKENS) is None

    def test_a_usage_without_a_recognised_field_is_not_recorded_as_zero_tokens(
        self, tmp_path: Path
    ) -> None:
        """**用量字段在、但一个数都认不出来 —— 那还是"没读到"。**

        这是与"完全没有 ``usage`` 字段"不同的另一条路：字段在，认不出。
        两种都得记成"没读到"—— 记成四个 0 会让 token 基线偏低，而基线偏低
        意味着**预算偏松**。
        """
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(cost_usd=CLI_COST, usage={"some_other_field": 5}),
        )
        output = _collect(tmp_path, script, tasks=1)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        measurement = table.measurements[0]
        assert measurement.usage is None
        assert measurement.value_of(BaselineMetric.TOKENS) is None

    def test_the_tool_calls_are_counted_from_the_events(
        self, tmp_path: Path, stub_cli: Path
    ) -> None:
        output = _collect(tmp_path, stub_cli)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        assert table.measurements[0].tool_calls == 4

    def test_the_duration_comes_from_the_executor(self, tmp_path: Path, stub_cli: Path) -> None:
        """2500 毫秒 = 2.5 秒 —— 来自结果事件，不是我们掐表掐的。"""
        output = _collect(tmp_path, stub_cli)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        assert table.measurements[0].duration_seconds == Decimal("2.500")

    def test_the_coverage_is_measured_on_the_copy(self, tmp_path: Path, stub_cli: Path) -> None:
        """覆盖率是**真的跑出来的** —— 不是抄来的。"""
        output = _collect(tmp_path, stub_cli)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        measurement = table.measurements[0]
        assert measurement.coverage is not None
        assert Decimal(0) < measurement.coverage <= Decimal(1)
        assert "tinylib.stats" in measurement.coverage_by_module

    def test_the_source_records_the_executor_and_the_model(
        self, tmp_path: Path, stub_cli: Path
    ) -> None:
        """逐条留档 —— 好让人复核这个中位数是怎么来的。"""
        output = _collect(tmp_path, stub_cli)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        assert table.measurements[0].source.startswith("claude-code:")
        assert "test-model" in table.measurements[0].source

    def test_each_task_gets_its_own_kind(self, tmp_path: Path, stub_cli: Path) -> None:
        """基线是**按类别**给的 —— 十个任务十种活，不该混成一锅。"""
        output = _collect(tmp_path, stub_cli, tasks=3)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        assert [item.task_kind for item in table.measurements] == [
            task.kind for task in REPRESENTATIVE_TASKS[:3]
        ]

    def test_the_pilot_repository_is_never_touched(self, tmp_path: Path, stub_cli: Path) -> None:
        """**采集在副本上做，真仓库一个字节都不动。**

        真仓库被改了，基线就不可复现了 —— 而基线的全部价值就在于"下次还能
        再采一遍，拿到同一个数"。
        """
        before = _fingerprint(PILOT)
        _collect(tmp_path, stub_cli)
        assert _fingerprint(PILOT) == before

    def test_a_failing_task_is_still_recorded(self, tmp_path: Path) -> None:
        """**失败的任务照样记。** 不记失败等于把最贵的那种情况从预算里抹掉。"""
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(cost_usd=CLI_COST, is_error=True, usage=CLI_USAGE),
            exit_code=1,
        )
        output = _collect(tmp_path, script, tasks=1)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        measurement = table.measurements[0]
        assert measurement.cost is not None
        assert "failed" in measurement.source


class TestTheThresholdComparison:
    """T1 ① —— **基线表能被程序读取并用于阈值比较**。"""

    def test_the_collected_table_compares_a_new_measurement(
        self, tmp_path: Path, stub_cli: Path
    ) -> None:
        output = _collect(tmp_path, stub_cli)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        kind = table.measurements[0].task_kind
        measurement = TaskMeasurement(
            task_kind=kind,
            duration_seconds=Decimal("3"),
            tool_calls=4,
            usage=TokenUsage(input_tokens=12_000, output_tokens=3_500),
            cost=CostAmount(Decimal("0.42"), "USD"),
        )
        comparison = table.compare(measurement, BaselineMetric.COST)
        assert comparison.state is MetricState.COMPUTED
        assert comparison.ratio == Decimal(1)
        assert comparison.within_baseline is True

    def test_an_expensive_run_is_beyond_the_baseline(self, tmp_path: Path, stub_cli: Path) -> None:
        output = _collect(tmp_path, stub_cli)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        kind = table.measurements[0].task_kind
        measurement = TaskMeasurement(
            task_kind=kind,
            duration_seconds=Decimal("30"),
            tool_calls=40,
            usage=TokenUsage(input_tokens=120_000, output_tokens=35_000),
            cost=CostAmount(Decimal("4.2"), "USD"),
        )
        comparison = table.compare(measurement, BaselineMetric.COST)
        assert comparison.within_baseline is False
        assert comparison.ratio == Decimal(10)

    def test_the_calibration_uses_the_collected_median(
        self, tmp_path: Path, stub_cli: Path
    ) -> None:
        """T2 —— **基线表的中位数给 ``max_daily_cost`` 提供依据**。"""
        output = _collect(tmp_path, stub_cli)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        calibration = calibrate_daily_cost(table, tasks_per_day=20)
        assert calibration.state is MetricState.COMPUTED
        assert calibration.median_cost_per_task == Decimal("0.42")
        assert calibration.max_daily_cost == Decimal("16.800000")
        assert calibration.to_budget() is not None


class TestTheMissingBaseline:
    """T1 ② —— **基线缺失时报"基线未建立"，不报 0**。"""

    def test_a_kind_that_was_not_collected_has_no_baseline(
        self, tmp_path: Path, stub_cli: Path
    ) -> None:
        output = _collect(tmp_path, stub_cli, tasks=1)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        measurement = TaskMeasurement(
            task_kind="a_kind_that_was_never_collected",
            duration_seconds=Decimal("10"),
            tool_calls=1,
            usage=TokenUsage(input_tokens=1),
            cost=CostAmount(Decimal("1"), "USD"),
        )
        comparison = table.compare(measurement, BaselineMetric.COST)
        assert comparison.state is MetricState.BASELINE_MISSING

    def test_the_missing_baseline_says_so(self, tmp_path: Path, stub_cli: Path) -> None:
        """**那句话里必须有"基线未建立"** —— 看板要能照着显示。"""
        output = _collect(tmp_path, stub_cli, tasks=1)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        measurement = TaskMeasurement(
            task_kind="a_kind_that_was_never_collected",
            duration_seconds=Decimal("10"),
            tool_calls=1,
            usage=TokenUsage(input_tokens=1),
            cost=CostAmount(Decimal("1"), "USD"),
        )
        comparison = table.compare(measurement, BaselineMetric.COST)
        assert BASELINE_MISSING_TEXT in comparison.message

    def test_the_missing_baseline_is_not_reported_as_zero(
        self, tmp_path: Path, stub_cli: Path
    ) -> None:
        """报 0 会让看板显示"成本 0" —— 读的人会以为任务不要钱。"""
        output = _collect(tmp_path, stub_cli, tasks=1)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        measurement = TaskMeasurement(
            task_kind="a_kind_that_was_never_collected",
            duration_seconds=Decimal("10"),
            tool_calls=1,
            usage=TokenUsage(input_tokens=1),
            cost=CostAmount(Decimal("1"), "USD"),
        )
        comparison = table.compare(measurement, BaselineMetric.COST)
        assert comparison.baseline is None
        assert comparison.ratio is None

    def test_the_missing_baseline_is_not_declared_within(
        self, tmp_path: Path, stub_cli: Path
    ) -> None:
        """**是 None，不是 False。** ``False`` 说的是"超了"，而"没法判断"是另一回事。"""
        output = _collect(tmp_path, stub_cli, tasks=1)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        measurement = TaskMeasurement(
            task_kind="a_kind_that_was_never_collected",
            duration_seconds=Decimal("10"),
            tool_calls=1,
            usage=TokenUsage(input_tokens=1),
            cost=CostAmount(Decimal("1"), "USD"),
        )
        comparison = table.compare(measurement, BaselineMetric.COST)
        assert comparison.within_baseline is None


class TestTheMarkdown:
    """人读版 —— **缺失的项写"基线未建立"，不留空**。"""

    def test_the_markdown_states_the_pilot_and_the_sample_count(
        self, tmp_path: Path, stub_cli: Path
    ) -> None:
        output = _collect(tmp_path, stub_cli)
        markdown = (output / "baselines.md").read_text(encoding="utf-8")
        assert PILOT.name in markdown
        assert "样本数" in markdown

    def test_the_markdown_gives_the_calibration(self, tmp_path: Path, stub_cli: Path) -> None:
        output = _collect(tmp_path, stub_cli)
        markdown = (output / "baselines.md").read_text(encoding="utf-8")
        assert "max_daily_cost" in markdown
        assert "16.800000" in markdown


class TestTheUnreportedCost:
    """**没读到金额不是"这次没花钱"。**

    一次跑飞的执行：CLI 被掐了，金额没吐出来 —— 而那次执行**明明花了钱**。
    记成 0 等于把最贵的一类任务说成"免费"，而报表上那一栏看上去跟"真的
    不花钱"一模一样。
    """

    def test_a_run_without_a_cost_is_not_recorded_as_free(self, tmp_path: Path) -> None:
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(cost_usd=None, usage=CLI_USAGE),
        )
        output = _collect(tmp_path, script, tasks=1)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))

        measurement = table.measurements[0]

        assert measurement.cost is None
        assert measurement.value_of(BaselineMetric.COST) is None

    def test_a_kind_without_a_cost_has_no_cost_baseline(self, tmp_path: Path) -> None:
        """**没有金额样本就是"基线未建立"** —— 不是"基线是 0"。"""
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(cost_usd=None, usage=CLI_USAGE),
        )
        output = _collect(tmp_path, script, tasks=1)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))

        assert table.median(table.measurements[0].task_kind, BaselineMetric.COST) is None

    def test_the_markdown_says_the_cost_baseline_is_missing(self, tmp_path: Path) -> None:
        """**缺的那一行要写出来** —— 少了它，读的人只会以为"这一类任务没有
        金额要求"，而真相是"这一类任务的金额还没采到"。"""
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(cost_usd=None, usage=CLI_USAGE),
        )
        output = _collect(tmp_path, script, tasks=1)
        markdown = (output / "baselines.md").read_text(encoding="utf-8")

        assert BASELINE_MISSING_TEXT in markdown

    def test_the_other_metrics_are_still_recorded(self, tmp_path: Path) -> None:
        """**没读到金额只影响金额那一栏** —— 时长、工具调用、覆盖率照记。"""
        script = write_stub(
            tmp_path / "cli",
            events=claude_events(cost_usd=None, usage=CLI_USAGE),
        )
        output = _collect(tmp_path, script, tasks=1)
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        kind = table.measurements[0].task_kind

        assert table.median(kind, BaselineMetric.DURATION) is not None
        assert table.median(kind, BaselineMetric.TOOL_CALLS) is not None
        assert table.median(kind, BaselineMetric.COVERAGE) is not None


class TestTheResume:
    """**量过的不用再量 —— 但没量全的要重跑。**

    采集一次要花钱、要时间。跑飞了一个任务就重来一遍，那钱就白花了；而
    把没量到的那一次原样留在表里，下一个人会以为这一类任务"便宜"。
    """

    def test_a_complete_measurement_is_reused(self, tmp_path: Path, stub_cli: Path) -> None:
        output = tmp_path / "out"
        assert (
            main(
                [
                    "--pilot",
                    str(PILOT),
                    "--output",
                    str(output),
                    "--binary",
                    str(stub_cli),
                    "--tasks",
                    "1",
                ]
            )
            == 0
        )
        first = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))

        # 第二个打桩 CLI 报一个**不一样的**金额 —— 复用就意味着表里还是
        # 第一次那个数；如果它又跑了一遍，金额就变了。
        other = write_stub(
            tmp_path / "other",
            events=claude_events(cost_usd=9.99, usage=CLI_USAGE),
        )
        assert (
            main(
                [
                    "--pilot",
                    str(PILOT),
                    "--output",
                    str(output),
                    "--binary",
                    str(other),
                    "--tasks",
                    "1",
                    "--resume",
                ]
            )
            == 0
        )
        second = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))

        assert second.measurements[0].cost == first.measurements[0].cost
        assert second.measurements[0].cost is not None
        assert second.measurements[0].cost.amount == Decimal("0.42")

    def test_an_incomplete_measurement_is_measured_again(self, tmp_path: Path) -> None:
        """**没读到金额的那一次要重跑** —— 它留下的不是事实，是"没量到"。"""
        output = tmp_path / "out"
        silent = write_stub(
            tmp_path / "silent",
            events=claude_events(cost_usd=None, usage=CLI_USAGE),
        )
        assert (
            main(
                [
                    "--pilot",
                    str(PILOT),
                    "--output",
                    str(output),
                    "--binary",
                    str(silent),
                    "--tasks",
                    "1",
                ]
            )
            == 0
        )
        first = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))
        assert first.measurements[0].cost is None

        loud = write_stub(
            tmp_path / "loud",
            events=claude_events(cost_usd=CLI_COST, usage=CLI_USAGE),
        )
        assert (
            main(
                [
                    "--pilot",
                    str(PILOT),
                    "--output",
                    str(output),
                    "--binary",
                    str(loud),
                    "--tasks",
                    "1",
                    "--resume",
                ]
            )
            == 0
        )
        second = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))

        assert second.measurements[0].cost is not None
        assert second.measurements[0].cost.amount == Decimal("0.42")

    def test_another_pilot_is_not_reused(self, tmp_path: Path, stub_cli: Path) -> None:
        """**换一个仓库重采，量的是另一堆代码** —— 拿上一堆的数字顶上来，
        那张表就答非所问，而表上没有任何地方写着这一点。"""
        output = tmp_path / "out"
        assert (
            main(
                [
                    "--pilot",
                    str(PILOT),
                    "--output",
                    str(output),
                    "--binary",
                    str(stub_cli),
                    "--tasks",
                    "1",
                ]
            )
            == 0
        )

        other_pilot = tmp_path / "other_pilot"
        other_pilot.mkdir()
        assert (
            main(
                [
                    "--pilot",
                    str(other_pilot),
                    "--output",
                    str(output),
                    "--binary",
                    str(stub_cli),
                    "--tasks",
                    "1",
                    "--resume",
                ]
            )
            == 0
        )
        table = BaselineTable.from_yaml((output / "baselines.yaml").read_text(encoding="utf-8"))

        assert table.pilot == other_pilot.name
