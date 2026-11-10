"""TASK-P3-017 —— 跨周期趋势看板的契约。

验证（T2）：看板能导出 ≥7 天连续运行的四层指标趋势；缺口不伪装成零；窗口
不悄悄缩水；R-117 禁的活动量进不来。

**这一层是纯函数。** 给它一组已知原料，它每次给出同样的看板 —— 所以这里
的每一格断言都是在证伪"看板的口径"，而不是在证伪"数据库连得上"。

**三个陷阱各有一组用例。**

- **缺口伪装成零** —— 算不出来时 ``value`` 是 ``None``，不是 0。
- **窗口悄悄缩水** —— 窗口的每一天都必须有一个点，少一天就是少一天。
- **指标被看板优化** —— R-117 禁的活动量在构造时就被拒绝。

**为什么口径是累计的。** 按天算的话，最后一天的任务还没跑完，那一天的成功
率必然低 —— 曲线一路向下，而那不是系统在变坏，是口径在骗人。所以第 D 天的
点是"截至第 D 天为止"。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from evaluation.common import MetricLayer, MetricState
from evaluation.evolution_evaluation.decision import DecisionRecord
from evaluation.evolution_evaluation.discovery import Discovery
from evaluation.labels.pilot import LabelSet, PilotLabel
from evaluation.registry import CORE_REGISTRY, ForbiddenMetricError
from evaluation.task_evaluation.delivery import TaskOutcome
from evaluation.trend import (
    COST_CUMULATIVE,
    MIN_TREND_DAYS,
    VERIFICATION_PASS_RATE,
    CostSample,
    CycleSample,
    MetricTrend,
    SeriesKind,
    TrendBoard,
    TrendDirection,
    TrendError,
    TrendInput,
    TrendPoint,
    TrendWindow,
    VerificationSample,
    build_board,
)

#: 看板的窗口 —— 验收要求"≥7 天连续运行"。
TODAY = date(2026, 11, 10)


def _window(days: int = MIN_TREND_DAYS) -> TrendWindow:
    return TrendWindow.last_days(TODAY, days)


def _cycle(cycle_id: str, closed_on: date) -> CycleSample:
    """一个周期样例 —— 默认有前后快照，判定为"变好了"。

    **要改的地方用 :func:`dataclasses.replace` 改** —— 少写一个函数，
    少一处漏改默认值的机会。
    """
    return CycleSample(
        cycle_id=cycle_id,
        closed_on=closed_on,
        attainment_before=Decimal("0.40"),
        attainment_after=Decimal("0.60"),
        outcome="improved",
    )


def _with_outcomes(cycle: CycleSample, *outcomes: TaskOutcome) -> CycleSample:
    """给周期配上任务结局。"""
    return replace(cycle, outcomes=tuple(outcomes))


def _points(window: TrendWindow, metric_id: str) -> tuple[TrendPoint, ...]:
    """一串"每一天都算得出"的点。"""
    return tuple(
        TrendPoint(
            day=day,
            metric_id=metric_id,
            state=MetricState.COMPUTED,
            value=Decimal("0.5"),
            numerator=Decimal(1),
            denominator=Decimal(2),
        )
        for day in window.day_list()
    )


def _trend(
    metric_id: str,
    window: TrendWindow,
    *,
    kind: SeriesKind = SeriesKind.CORE,
    layer: MetricLayer | None = None,
) -> MetricTrend:
    """造一条曲线 —— 手工的，用来试看板自己的拒绝。"""
    definition = CORE_REGISTRY.get(metric_id) if metric_id in CORE_REGISTRY else None
    return MetricTrend(
        metric_id=metric_id,
        layer=layer if layer is not None else (definition.layer if definition else None),
        kind=kind,
        unit=definition.unit if definition else "ratio",
        name=definition.name if definition else metric_id,
        dimension="测试",
        points=_points(window, metric_id),
    )


def _labels(refs: tuple[str, ...]) -> LabelSet:
    return LabelSet(
        labels=tuple(
            PilotLabel(
                issue_ref=ref,
                is_valid_finding=index % 2 == 0,
                annotated_at=datetime(2026, 11, 1, tzinfo=UTC),
                annotated_by="tester",
                category="quality",
            )
            for index, ref in enumerate(refs)
        )
    )


class TestTheWindow:
    def test_the_window_covers_every_day_without_a_gap(self) -> None:
        window = _window(7)

        days = window.day_list()

        assert window.days == 7
        assert len(days) == 7
        assert days[0] == TODAY - timedelta(days=6)
        assert days[-1] == TODAY
        assert days == tuple(sorted(days))

    def test_a_window_that_ends_before_it_starts_is_rejected(self) -> None:
        with pytest.raises(TrendError):
            TrendWindow(start=TODAY, end=TODAY - timedelta(days=1))

    def test_a_non_positive_day_count_is_rejected(self) -> None:
        with pytest.raises(TrendError):
            TrendWindow.last_days(TODAY, 0)


class TestEveryDayHasAPoint:
    def test_every_curve_has_one_point_per_window_day(self) -> None:
        window = _window(7)
        board = build_board("demo", window, TrendInput())

        for trend in board.trends:
            assert trend.days == window.days, f"{trend.metric_id} 的点数与窗口对不上"
            assert tuple(point.day for point in trend.points) == window.day_list()

    def test_a_day_without_a_cycle_still_has_a_point(self) -> None:
        """**没有周期的那天不是"那天是平的"** —— 它是一个点，值算不出来。"""
        window = _window(7)
        data = TrendInput(
            cycles=(
                _with_outcomes(
                    _cycle("c-1", TODAY - timedelta(days=2)),
                    TaskOutcome(task_id="t-1", completed=True),
                ),
            ),
        )

        board = build_board("demo", window, data)
        trend = board.trend("task_success_rate")

        assert trend.days == 7
        assert trend.point_on(TODAY - timedelta(days=2)).is_computed is True
        assert trend.point_on(TODAY - timedelta(days=3)).is_computed is False

    def test_the_board_reports_how_many_days_it_covers(self) -> None:
        board = build_board("demo", _window(30), TrendInput())

        assert board.days == 30


class TestTheBoardRefusesActivityMetrics:
    """R-117 —— 任务数 / Token 数 / 提交量进不了看板。"""

    def test_a_forbidden_metric_is_rejected(self) -> None:
        window = _window(7)
        trends = tuple(
            _trend(metric_id, window)
            for metric_id in CORE_REGISTRY.metric_ids()
        )
        trends = (*trends, _trend("task_count", window, layer=MetricLayer.DELIVERY))

        with pytest.raises(ForbiddenMetricError):
            TrendBoard(project_id="demo", window=window, trends=trends)

    def test_a_core_metric_must_be_registered(self) -> None:
        window = _window(7)
        trends = tuple(
            _trend(metric_id, window) for metric_id in CORE_REGISTRY.metric_ids()
        )
        trends = (*trends, _trend("made_up_metric", window, layer=MetricLayer.DELIVERY))

        with pytest.raises(TrendError):
            TrendBoard(project_id="demo", window=window, trends=trends)

    def test_a_diagnostic_metric_must_not_be_a_core_metric(self) -> None:
        """一个量既是成绩又是背景，读者迟早会把它当成绩看。"""
        window = _window(7)
        trends = tuple(
            _trend(metric_id, window) for metric_id in CORE_REGISTRY.metric_ids()
        )
        trends = (
            *trends,
            _trend("task_success_rate", window, kind=SeriesKind.DIAGNOSTIC, layer=None),
        )

        with pytest.raises(TrendError):
            TrendBoard(project_id="demo", window=window, trends=trends)

    def test_a_curve_with_too_few_points_is_rejected(self) -> None:
        """缺的那天不会自己消失 —— 少一个点就是少一天。"""
        window = _window(7)
        trends = tuple(
            _trend(metric_id, window) for metric_id in CORE_REGISTRY.metric_ids()
        )
        short = MetricTrend(
            metric_id="short_one",
            layer=MetricLayer.DELIVERY,
            kind=SeriesKind.CORE,
            unit="ratio",
            name="少一天",
            dimension="测试",
            points=_points(window, "short_one")[:-1],
        )

        with pytest.raises(TrendError):
            TrendBoard(project_id="demo", window=window, trends=(*trends, short))


class TestAllFourLayersMustBePresent:
    def test_a_board_missing_a_layer_is_rejected(self) -> None:
        """四层缺一层就答不了那一层的问题 —— 少一层不是"少一条曲线"。"""
        window = _window(7)
        kept = tuple(
            _trend(metric_id, window)
            for metric_id in CORE_REGISTRY.metric_ids()
            if CORE_REGISTRY.get(metric_id).layer is not MetricLayer.EVOLUTION
        )

        with pytest.raises(TrendError):
            TrendBoard(project_id="demo", window=window, trends=kept)

    def test_the_built_board_covers_all_four_layers(self) -> None:
        board = build_board("demo", _window(7), TrendInput())

        for layer in MetricLayer:
            assert board.by_layer(layer), f"看板缺了 {layer} 层"


class TestAGapIsNotZero:
    def test_no_samples_is_none_not_zero(self) -> None:
        """没有任务不是"成功率为 0" —— 那是两件相反的事。"""
        board = build_board("demo", _window(7), TrendInput())

        point = board.trend("task_success_rate").point_on(TODAY)

        assert point.value is None
        assert point.state is MetricState.NO_SAMPLES
        assert point.is_computed is False

    def test_a_missing_label_baseline_is_not_zero(self) -> None:
        """没有人工标注不是"有效发现率 0%" —— 那会让人以为系统很准。"""
        window = _window(7)
        discovery = Discovery(
            candidate_id="cand-1",
            cycle_id="c-1",
            title="一个候选",
            evidence_refs=("issue:1",),
        )
        data = TrendInput(
            cycles=(replace(_cycle("c-1", TODAY), discoveries=(discovery,)),),
        )

        board = build_board("demo", window, data)
        point = board.trend("valid_discovery_rate").point_on(TODAY)

        assert point.value is None
        assert point.state is MetricState.BASELINE_MISSING

    def test_the_regression_defect_rate_reports_a_missing_baseline(self) -> None:
        """缺陷数据尚不可得 —— 不是"没有缺陷"。"""
        window = _window(7)
        data = TrendInput(
            cycles=(
                _with_outcomes(
                    _cycle("c-1", TODAY),
                    TaskOutcome(task_id="t-1", completed=True),
                ),
            ),
        )

        board = build_board("demo", window, data)
        point = board.trend("regression_defect_rate").point_on(TODAY)

        assert point.value is None
        assert point.state is MetricState.BASELINE_MISSING


class TestTheCurvesAreCumulative:
    def test_a_cycle_closed_today_does_not_affect_earlier_days(self) -> None:
        """**窗口是横轴的范围，不是数据的过滤器。**"""
        window = _window(7)
        data = TrendInput(
            cycles=(
                _with_outcomes(
                    _cycle("c-1", TODAY),
                    TaskOutcome(task_id="t-1", completed=True),
                ),
            ),
        )

        board = build_board("demo", window, data)
        trend = board.trend("task_success_rate")

        assert trend.point_on(TODAY - timedelta(days=1)).value is None
        assert trend.point_on(TODAY).value == Decimal(1)

    def test_the_denominator_grows_with_the_history(self) -> None:
        window = _window(7)
        data = TrendInput(
            cycles=(
                _with_outcomes(
                    _cycle("c-1", TODAY - timedelta(days=3)),
                    TaskOutcome(task_id="t-1", completed=True),
                ),
                _with_outcomes(
                    _cycle("c-2", TODAY - timedelta(days=1)),
                    TaskOutcome(task_id="t-2", completed=False),
                ),
            ),
        )

        board = build_board("demo", window, data)
        trend = board.trend("task_success_rate")

        assert trend.point_on(TODAY - timedelta(days=3)).value == Decimal(1)
        assert trend.point_on(TODAY - timedelta(days=1)).value == Decimal("0.5")
        assert trend.point_on(TODAY).value == Decimal("0.5")

    def test_the_sample_size_is_reported(self) -> None:
        """看比率必须看样本量 —— 1/1 与 300/400 不是一回事。"""
        window = _window(7)
        data = TrendInput(
            cycles=(
                _with_outcomes(
                    _cycle("c-1", TODAY),
                    TaskOutcome(task_id="t-1", completed=True),
                ),
            ),
        )

        board = build_board("demo", window, data)
        point = board.trend("task_success_rate").point_on(TODAY)

        assert point.sample_size == 1


class TestTheQualityAndCostDimensions:
    def test_skipped_verifications_stay_out_of_the_denominator(self) -> None:
        """没跑的东西没有结论 —— 因为没验而惩罚系统是不对的。"""
        window = _window(7)
        data = TrendInput(
            verifications=(
                VerificationSample(run_id="r-1", day=TODAY, result="passed"),
                VerificationSample(run_id="r-2", day=TODAY, result="skipped"),
                VerificationSample(run_id="r-3", day=TODAY, result="failed"),
            ),
        )

        board = build_board("demo", window, data)
        point = board.trend(VERIFICATION_PASS_RATE).point_on(TODAY)

        assert point.value == Decimal("0.5")
        assert point.denominator == Decimal(2)

    def test_without_any_verification_there_are_no_samples(self) -> None:
        board = build_board("demo", _window(7), TrendInput())

        point = board.trend(VERIFICATION_PASS_RATE).point_on(TODAY)

        assert point.value is None
        assert point.state is MetricState.NO_SAMPLES

    def test_the_cost_is_cumulative(self) -> None:
        window = _window(7)
        data = TrendInput(
            costs=(
                CostSample(day=TODAY - timedelta(days=2), amount=Decimal("1.5"), currency="USD"),
                CostSample(day=TODAY, amount=Decimal("2.5"), currency="USD"),
            ),
        )

        board = build_board("demo", window, data)
        trend = board.trend(COST_CUMULATIVE)

        assert trend.point_on(TODAY - timedelta(days=2)).value == Decimal("1.5")
        assert trend.point_on(TODAY - timedelta(days=1)).value == Decimal("1.5")
        assert trend.point_on(TODAY).value == Decimal("4.0")

    def test_without_any_cost_there_are_no_samples(self) -> None:
        """**没记账不是"一文未花"** —— 那是一个假的安全信号。"""
        board = build_board("demo", _window(7), TrendInput())

        point = board.trend(COST_CUMULATIVE).point_on(TODAY)

        assert point.value is None
        assert point.state is MetricState.NO_SAMPLES

    def test_two_currencies_cannot_be_added(self) -> None:
        """两个币种的数加起来什么都不是 —— 直接报错，别猜。"""
        window = _window(7)
        data = TrendInput(
            costs=(
                CostSample(day=TODAY, amount=Decimal("1.5"), currency="USD"),
                CostSample(day=TODAY, amount=Decimal("2.5"), currency="CNY"),
            ),
        )

        with pytest.raises(ValueError, match="币种"):
            build_board("demo", window, data)


class TestTheDirectionOfACurve:
    def test_one_computed_point_does_not_say_flat(self) -> None:
        """一个点说明不了方向 —— 说成"持平"等于凭空造了一个结论。"""
        window = _window(7)
        data = TrendInput(
            cycles=(_cycle("c-1", TODAY),),
        )

        board = build_board("demo", window, data)

        assert board.trend("attainment_delta").direction is TrendDirection.UNKNOWN

    def test_a_rising_curve_is_reported_as_rising(self) -> None:
        window = _window(7)
        data = TrendInput(
            cycles=(
                replace(
                    _cycle("c-1", TODAY - timedelta(days=3)),
                    attainment_before=Decimal("0.40"),
                    attainment_after=Decimal("0.50"),
                ),
                replace(
                    _cycle("c-2", TODAY - timedelta(days=1)),
                    attainment_before=Decimal("0.50"),
                    attainment_after=Decimal("0.80"),
                ),
            ),
        )

        board = build_board("demo", window, data)

        assert board.trend("attainment_delta").direction is TrendDirection.RISING

    def test_a_falling_curve_is_reported_as_falling(self) -> None:
        window = _window(7)
        data = TrendInput(
            cycles=(
                replace(
                    _cycle("c-1", TODAY - timedelta(days=3)),
                    attainment_before=Decimal("0.60"),
                    attainment_after=Decimal("0.50"),
                ),
                replace(
                    _cycle("c-2", TODAY - timedelta(days=1)),
                    attainment_before=Decimal("0.50"),
                    attainment_after=Decimal("0.20"),
                ),
            ),
        )

        board = build_board("demo", window, data)

        assert board.trend("attainment_delta").direction is TrendDirection.FALLING


class TestTheLabels:
    def test_labels_turn_the_baseline_missing_into_a_number(self) -> None:
        window = _window(7)
        discovery = Discovery(
            candidate_id="cand-1",
            cycle_id="c-1",
            title="一个候选",
            evidence_refs=("issue:1",),
        )
        data = TrendInput(
            cycles=(replace(_cycle("c-1", TODAY), discoveries=(discovery,)),),
        )

        board = build_board("demo", window, data, labels=_labels(("cand-1",)))

        point = board.trend("valid_discovery_rate").point_on(TODAY)

        assert point.state is MetricState.COMPUTED
        assert point.value == Decimal(1)

    def test_the_label_decides_the_denominator(self) -> None:
        """标注集里没有的候选不进分母 —— 分母不能由被评价的一方决定。"""
        window = _window(7)
        discovery = Discovery(
            candidate_id="cand-1",
            cycle_id="c-1",
            title="一个候选",
            evidence_refs=("issue:1",),
        )
        data = TrendInput(
            cycles=(replace(_cycle("c-1", TODAY), discoveries=(discovery,)),),
        )

        board = build_board("demo", window, data, labels=_labels(("cand-other",)))

        point = board.trend("valid_discovery_rate").point_on(TODAY)

        assert point.state is MetricState.BASELINE_MISSING


class TestTheExport:
    def test_the_json_export_is_deterministic(self) -> None:
        """两次写出来能逐字节比较 —— 报表要能进版本控制。"""
        board = build_board("demo", _window(7), TrendInput())

        assert board.to_json() == board.to_json()

    def test_the_json_export_carries_the_states(self) -> None:
        board = build_board("demo", _window(7), TrendInput())

        payload = json.loads(board.to_json())
        trend = next(item for item in payload["trends"] if item["metric_id"] == "task_success_rate")

        assert trend["kind"] == "core"
        assert len(trend["points"]) == 7
        assert trend["points"][0]["value"] is None
        assert trend["points"][0]["state"] == "no_samples"

    def test_the_markdown_export_marks_the_gaps(self) -> None:
        """表里的"—"表示"算不出来"，不是 0。"""
        board = build_board("demo", _window(7), TrendInput())

        text = board.to_markdown()

        assert "—" in text
        assert "不是 0" in text

    def test_the_markdown_export_has_one_column_per_day(self) -> None:
        window = _window(7)
        board = build_board("demo", window, TrendInput())

        header = board.to_markdown().splitlines()[0]
        table_header = next(
            line for line in board.to_markdown().splitlines() if line.startswith("| 指标")
        )

        assert table_header.count("2026-11") == 7
        assert header  # 标题行存在


class TestTheDecisionLayer:
    def test_a_recorded_decision_counts_towards_the_adoption_rate(self) -> None:
        window = _window(7)
        decision = DecisionRecord(
            decision_id="d-1",
            candidate_id="cand-1",
            decision="record",
        )
        data = TrendInput(
            cycles=(replace(_cycle("c-1", TODAY), decisions=(decision,)),),
        )

        board = build_board("demo", window, data)
        point = board.trend("decision_adoption_rate").point_on(TODAY)

        assert point.value == Decimal(1)
        assert point.denominator == Decimal(1)

    def test_an_overridden_decision_is_not_an_adopted_one(self) -> None:
        """**被推翻的建议不算被采纳** —— 人在替它做决定，那它就没在自主决策。"""
        window = _window(7)
        decision = DecisionRecord(
            decision_id="d-1",
            candidate_id="cand-1",
            decision="record",
            overridden_by="critic",
        )
        data = TrendInput(
            cycles=(replace(_cycle("c-1", TODAY), decisions=(decision,)),),
        )

        board = build_board("demo", window, data)
        point = board.trend("decision_adoption_rate").point_on(TODAY)

        assert point.value == Decimal(0)

    def test_a_rejected_decision_is_not_a_proposal(self) -> None:
        """reject 本来就不是"要执行它"的提议 —— 把它算进分母，指标就变成了
        "系统动作的激进程度"。"""
        window = _window(7)
        decision = DecisionRecord(
            decision_id="d-1",
            candidate_id="cand-1",
            decision="reject",
        )
        data = TrendInput(
            cycles=(replace(_cycle("c-1", TODAY), decisions=(decision,)),),
        )

        board = build_board("demo", window, data)
        point = board.trend("decision_adoption_rate").point_on(TODAY)

        assert point.value is None
        assert point.state is MetricState.NO_SAMPLES
