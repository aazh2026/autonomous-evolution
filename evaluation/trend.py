"""跨周期趋势看板 —— 四层指标在时间上的曲线。

设计依据：docs/design.md §10（四级指标体系）、§12（可验证的改善）；
docs/tasks.md TASK-P3-017（交付物"跨周期趋势看板（达成度、质量、成本）"；
验收"能导出 ≥7 天连续运行的四层指标趋势"；验证 T2）、R-117（不得用任务数 /
Token 数 / 提交量作核心指标）、R-118（四级指标体系）。

**看板不是"把今天的数字画成曲线"。** 它回答的是另一个问题：**这些天下来，
系统在变好还是变坏？** 这个问题有三个陷阱，绕过去，看板就只是一张好看的
废纸。

- **缺口伪装成零。** 某天没有数据，不是"那天指标是 0"，而是"那天算不出来"。
  两者在图上长得一模一样，含义却相反 —— 所以每个点都带状态
  （``computed`` / ``no_samples`` / ``baseline_missing``），算不出来时
  ``value`` 是 ``None`` 而不是 0。
- **窗口悄悄缩水。** 要 7 天，给了 3 天，看板看上去仍然"正常"。所以窗口的
  每一天都必须有一个点 —— **少一天就是少一天**，看板自己报出
  ``days``，而每条曲线都恰好有那么多点。
- **指标被看板优化。** R-117 禁的那三个量（任务数 / Token 数 / 提交量）
  恰恰是最容易画成曲线的三个量 —— **曲线本身就是一种"成绩"的外观**。所以
  看板在构造时**主动拒绝**它们，而不只是"初始集合里没有它们"。

**每一条曲线都是累计的。** 第 D 天的点 = "截至第 D 天为止" 的指标值。
理由是**近因偏差**：按天算的话，最后一天的任务还没跑完，那一天的成功率
必然低 —— 曲线一路向下，而那不是系统在变坏，是口径在骗人。累计口径没有
这个问题，而且与注册表里那些指标本身的定义一致（分母是"到目前为止的全部
样本"）。

**这一层不碰数据库。** 输入是若干条记录（:class:`TrendInput`），输出是一
块看板（:class:`TrendBoard`）—— 与其它指标一样是纯函数，所以每一格都能
被单独测：给它一组已知输入，断言它算出什么。
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from evaluation.common import MetricLayer, MetricResult, MetricState, rate
from evaluation.evolution_evaluation.decision import (
    DecisionRecord,
    apply_labels,
    decision_adoption_rate,
    false_positive_rate,
)
from evaluation.evolution_evaluation.discovery import (
    Discovery,
    duplicate_discovery_rate,
    valid_discovery_rate,
)
from evaluation.labels.pilot import LabelSet
from evaluation.project_evaluation.attainment import CycleAttainment, attainment_delta
from evaluation.registry import CORE_REGISTRY, ForbiddenMetricError, is_forbidden
from evaluation.task_evaluation.delivery import (
    TaskOutcome,
    regression_defect_rate,
    rework_rate,
    task_success_rate,
)

#: 验收要求的最小天数 —— "≥7 天连续运行"。
MIN_TREND_DAYS: Final[int] = 7

#: 质量维度：验收通过率。
#:
#: **它不是登记过的核心指标** —— 核心指标那一套的分母与口径都被注册表钉
#: 死（见 :mod:`evaluation.registry`），而验收通过率是这次看板为了回答
#: "交付出去的东西过没过得了验收"新加的诊断量。它可以进看板，但必须被标成
#: 诊断量 —— **一个量不能既是成绩又是背景**。
VERIFICATION_PASS_RATE: Final[str] = "verification_pass_rate"

#: 成本维度：窗口内的累计花费。
COST_CUMULATIVE: Final[str] = "cost_cumulative"

#: 四个指标层的人读名字。
LAYER_NAMES: Final[dict[MetricLayer, str]] = {
    MetricLayer.DISCOVERY: "自主发现",
    MetricLayer.DECISION: "决策质量",
    MetricLayer.DELIVERY: "工程交付",
    MetricLayer.EVOLUTION: "项目演进",
}

#: 两个诊断维度的人读名字。
QUALITY_DIMENSION: Final[str] = "质量"
COST_DIMENSION: Final[str] = "成本"

#: 验收结果里"跑过了"的三种。``skipped`` 不算 —— 没跑的东西没有结论。
_VERIFICATION_CONCLUDED: Final[frozenset[str]] = frozenset({"passed", "failed", "error"})

#: 判断走向至少要几个算得出的点 —— **一个点说明不了方向**。
_MIN_COMPUTED_POINTS: Final[int] = 2


class TrendError(ValueError):
    """看板里的错。"""


class SeriesKind(StrEnum):
    """一条曲线是核心指标还是诊断量。

    **两者必须分开，而且互斥。** 核心指标是"成绩"—— 读者会拿它判断系统好
    不好；诊断量是"背景"—— 它解释成绩，不被当成成绩。一个量同时是两者，
    读者迟早会把它当成绩看，于是它就被优化了 —— 那正是 R-117 要防的事。
    """

    CORE = "core"
    DIAGNOSTIC = "diagnostic"


class TrendDirection(StrEnum):
    """一条曲线的走向。"""

    RISING = "rising"
    FALLING = "falling"
    FLAT = "flat"
    #: 算得出的点不足两个 —— **不知道往哪儿走**。
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TrendWindow:
    """看板的时间窗 —— **含首尾两天**。

    **窗口是横轴的范围，不是数据的过滤器。** 累计口径下，第 D 天的点算的是
    "截至第 D 天为止"的全部记录 —— 所以窗口开始之前的历史也算在内。否则
    第一天与第二天的分母就不是一回事，那条曲线自己跟自己都没法比。
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise TrendError(f"窗口的结束日 {self.end} 不能早于开始日 {self.start}")

    @property
    def days(self) -> int:
        """窗口里的天数 —— 含首尾两天。"""
        return (self.end - self.start).days + 1

    def day_list(self) -> tuple[date, ...]:
        """窗口里的每一天，**连续无缺口**。"""
        return tuple(self.start + timedelta(days=offset) for offset in range(self.days))

    @classmethod
    def last_days(cls, today: date, count: int) -> TrendWindow:
        """最近 ``count`` 天（含今天）。"""
        if count < 1:
            raise TrendError(f"天数必须为正，收到 {count}")
        return cls(start=today - timedelta(days=count - 1), end=today)


@dataclass(frozen=True, slots=True)
class TrendPoint:
    """一条曲线上的一天。

    **状态与值必须一起读。** ``value`` 为 ``None`` 时 ``state`` 一定不是
    ``computed`` —— "算不出来"与"算出来是 0"在报表上长得一模一样，而这
    两个字段就是为了把它们分开。
    """

    day: date
    metric_id: str
    state: MetricState
    value: Decimal | None
    numerator: Decimal | None = None
    denominator: Decimal | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    @property
    def is_computed(self) -> bool:
        """这个点算出来了吗。"""
        return self.state is MetricState.COMPUTED and self.value is not None

    @property
    def sample_size(self) -> int | None:
        """样本量 —— 分母。**看比率必须看它**：1/1 与 300/400 不是一回事。"""
        if self.denominator is None:
            return None
        return int(self.denominator)

    @classmethod
    def from_result(
        cls,
        day: date,
        result: MetricResult,
        *,
        extra: Mapping[str, object] | None = None,
    ) -> TrendPoint:
        """由一次指标计算的结果造一个点。"""
        return cls(
            day=day,
            metric_id=result.metric_id,
            state=result.state,
            value=result.value,
            numerator=result.numerator,
            denominator=result.denominator,
            detail={**result.detail, **(extra or {})},
        )


@dataclass(frozen=True, slots=True)
class MetricTrend:
    """一条曲线 —— 一个指标在窗口里的逐日取值。

    **点数必须等于窗口的天数。** 少一个点就是少一天 —— 缺的那天不会自己
    消失，它只是不见了，而"不见了"在图上读起来与"那天是平的"完全一样。
    """

    metric_id: str
    layer: MetricLayer | None
    kind: SeriesKind
    unit: str
    name: str
    dimension: str
    points: tuple[TrendPoint, ...]

    @property
    def days(self) -> int:
        """点数 —— 覆盖的天数。"""
        return len(self.points)

    @property
    def computed_days(self) -> int:
        """算得出数的天数。"""
        return sum(1 for point in self.points if point.is_computed)

    @property
    def missing_days(self) -> tuple[date, ...]:
        """算不出数的那些天。"""
        return tuple(point.day for point in self.points if not point.is_computed)

    @property
    def values(self) -> tuple[Decimal | None, ...]:
        """逐日的值 —— 算不出来的是 ``None``，**不是 0**。"""
        return tuple(point.value for point in self.points)

    @property
    def direction(self) -> TrendDirection:
        """走向 —— **拿头尾两个算得出的点比**。

        **只算得出一个点时是 ``unknown``，不是 ``flat``。** 一个点说明不了
        方向；说成"持平"等于凭空造了一个结论。
        """
        computed = [point.value for point in self.points if point.is_computed]
        if len(computed) < _MIN_COMPUTED_POINTS:
            return TrendDirection.UNKNOWN
        first = computed[0]
        last = computed[-1]
        if last is None or first is None:  # pragma: no cover —— 上面已过滤
            return TrendDirection.UNKNOWN
        if last > first:
            return TrendDirection.RISING
        if last < first:
            return TrendDirection.FALLING
        return TrendDirection.FLAT

    def point_on(self, day: date) -> TrendPoint:
        """某一天的那个点。"""
        for point in self.points:
            if point.day == day:
                return point
        raise TrendError(f"曲线 {self.metric_id!r} 没有 {day} 这一天的点")


@dataclass(frozen=True, slots=True)
class TrendBoard:
    """一块看板 —— 一个项目在一个时间窗上的全部曲线。

    **构造时就是一次检查。** 核心指标必须登记过、诊断量必须没登记过、
    R-117 禁的量一律拒绝、每条曲线的点数必须等于窗口的天数、四层核心指标
    一层都不能少 —— 这些不是"建议"，是构造不出来的东西。

    **"四层一层都不能少"是验收条件。** 少了项目演进层，看板就答不了
    "项目是否真的变好"；少了任何一层，它就答不了那一层的问题。
    """

    project_id: str
    window: TrendWindow
    trends: tuple[MetricTrend, ...]

    def __post_init__(self) -> None:
        days = self.window.day_list()
        for trend in self.trends:
            if is_forbidden(trend.metric_id):
                raise ForbiddenMetricError(
                    f"看板不得绘制指标 {trend.metric_id!r}（R-117）："
                    f"任务数 / Token 数 / 提交量很容易被优化，却不代表项目真的变好了。"
                    f"它仍可作为诊断输入使用。"
                )
            if trend.kind is SeriesKind.CORE:
                if trend.metric_id not in CORE_REGISTRY:
                    raise TrendError(
                        f"核心指标 {trend.metric_id!r} 没有登记在注册表里 —— "
                        f"看板只能画登记过的核心指标"
                    )
            elif trend.metric_id in CORE_REGISTRY:
                raise TrendError(
                    f"诊断量 {trend.metric_id!r} 不能同时是核心指标 —— "
                    f"一个量既是成绩又是背景，读者迟早会把它当成绩看"
                )
            if len(trend.points) != len(days):
                raise TrendError(
                    f"曲线 {trend.metric_id!r} 有 {len(trend.points)} 个点，"
                    f"而窗口有 {len(days)} 天 —— 缺的那天不会自己消失"
                )
            if tuple(point.day for point in trend.points) != days:
                raise TrendError(
                    f"曲线 {trend.metric_id!r} 的日期与窗口对不上 —— "
                    f"每一天都必须有一个点，顺序也要一致"
                )
        covered = {trend.layer for trend in self.trends if trend.kind is SeriesKind.CORE}
        missing = [layer for layer in MetricLayer if layer not in covered]
        if missing:
            names = "、".join(LAYER_NAMES[layer] for layer in missing)
            raise TrendError(f"看板缺了这些指标层：{names} —— 四层缺一层就答不了那一层的问题")

    @property
    def days(self) -> int:
        """窗口的天数。"""
        return self.window.days

    def by_layer(self, layer: MetricLayer) -> tuple[MetricTrend, ...]:
        """某一层的核心曲线。"""
        return tuple(
            trend for trend in self.trends if trend.kind is SeriesKind.CORE and trend.layer is layer
        )

    def diagnostics(self) -> tuple[MetricTrend, ...]:
        """全部诊断曲线。"""
        return tuple(trend for trend in self.trends if trend.kind is SeriesKind.DIAGNOSTIC)

    def trend(self, metric_id: str) -> MetricTrend:
        """取一条曲线。"""
        for trend in self.trends:
            if trend.metric_id == metric_id:
                return trend
        known = "、".join(trend.metric_id for trend in self.trends)
        raise TrendError(f"看板上没有指标 {metric_id!r}；有的是：{known}")

    def to_dict(self) -> dict[str, object]:
        """导出成可机读的结构。"""
        return {
            "project_id": self.project_id,
            "window": {
                "start": self.window.start.isoformat(),
                "end": self.window.end.isoformat(),
                "days": self.window.days,
            },
            "trends": [_trend_to_dict(trend) for trend in self.trends],
        }

    def to_json(self) -> str:
        """导出成 JSON —— **键排好序，两次写出来能逐字节比较**。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def to_markdown(self) -> str:
        """导出成 Markdown —— 给不看 JSON 的人读。"""
        lines: list[str] = []
        lines.append(f"# 跨周期趋势看板 —— {self.project_id}")
        lines.append("")
        lines.append(
            f"窗口 {self.window.start.isoformat()} ~ {self.window.end.isoformat()}"
            f"，共 {self.window.days} 天，每天一个点。"
        )
        lines.append("")
        lines.append("**— 表示算不出来（基线未建立 / 无样本），不是 0。**")
        lines.append("")
        lines.append(
            "曲线是**累计**口径：第 D 天的点 = 截至第 D 天为止的指标值。"
        )
        lines.append("")
        for dimension, trends in self._grouped():
            if not trends:
                continue
            lines.append(f"## {dimension}")
            lines.append("")
            lines.append(self._table(trends))
            lines.append("")
        return "\n".join(lines)

    def _grouped(self) -> tuple[tuple[str, tuple[MetricTrend, ...]], ...]:
        """按维度分组 —— 核心层在前，诊断维度在后。"""
        groups: list[tuple[str, tuple[MetricTrend, ...]]] = []
        for layer in MetricLayer:
            trends = self.by_layer(layer)
            if trends:
                groups.append((LAYER_NAMES[layer], trends))
        diagnostics = self.diagnostics()
        if diagnostics:
            groups.append(("诊断量（不是成绩）", diagnostics))
        return tuple(groups)

    def _table(self, trends: Sequence[MetricTrend]) -> str:
        """一张表 —— 行是指标，列是天。"""
        header = ["指标", *[day.isoformat() for day in self.window.day_list()]]
        rows = [header]
        for trend in trends:
            row = [trend.name]
            for point in trend.points:
                row.append(_format_point(point, trend))
            rows.append(row)
        widths = [
            max(_display_width(row[index]) for row in rows) for index in range(len(header))
        ]
        lines = [
            _markdown_row(rows[0], widths),
            "| " + " | ".join("-" * width for width in widths) + " |",
        ]
        for row in rows[1:]:
            lines.append(_markdown_row(row, widths))
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CycleSample:
    """一个**已闭合**周期 —— 看板的一格。

    **周期是这一层的时间单位。** 候选、决策、任务都挂在周期上，而周期闭合
    的那一刻，它下面的任务已经有了最终状态 —— 于是"这一格"里的一切都是
    定论，没有"还在跑"的悬案。按任务创建日归组反而会留下悬案：任务还在
    跑，那一天的成功率就必然低，而那不是系统在变坏。
    """

    cycle_id: str
    closed_on: date
    attainment_before: Decimal | None = None
    attainment_after: Decimal | None = None
    outcome: str = ""
    discoveries: tuple[Discovery, ...] = ()
    decisions: tuple[DecisionRecord, ...] = ()
    outcomes: tuple[TaskOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationSample:
    """一次验收 —— 质量维度的原料。"""

    run_id: str
    day: date
    #: passed / failed / error / skipped。
    result: str


@dataclass(frozen=True, slots=True)
class CostSample:
    """一笔成本 —— 成本维度的原料。"""

    day: date
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class TrendInput:
    """看板的全部原料 —— 与数据库无关。"""

    cycles: tuple[CycleSample, ...] = ()
    verifications: tuple[VerificationSample, ...] = ()
    costs: tuple[CostSample, ...] = ()


def build_board(
    project_id: str,
    window: TrendWindow,
    data: TrendInput,
    *,
    labels: LabelSet | None = None,
) -> TrendBoard:
    """造一块看板。

    **纯函数** —— 给它同样的原料，它每次给出同样的看板。所以每一格都能被
    单独测。

    :param labels: 人工标注集。**给了才标** —— 没标过的候选与决策一律
        报"基线未建立"，而不是"有效率 0%"。
    """
    days = window.day_list()
    cycles = sorted(data.cycles, key=lambda item: (item.closed_on, item.cycle_id))
    verifications = sorted(data.verifications, key=lambda item: (item.day, item.run_id))
    costs = sorted(data.costs, key=lambda item: (item.day, item.currency))

    points: dict[str, list[TrendPoint]] = {}
    for day in days:
        upto = [item for item in cycles if item.closed_on <= day]
        today = [item for item in cycles if item.closed_on == day]
        discoveries = _discoveries(upto, labels)
        decisions = _decisions(upto, labels)
        outcomes = [outcome for item in upto for outcome in item.outcomes]
        attainments = [
            CycleAttainment(
                cycle_id=item.cycle_id,
                attainment_before=item.attainment_before,
                attainment_after=item.attainment_after,
                outcome=item.outcome,
            )
            for item in upto
        ]
        verifications_upto = [item for item in verifications if item.day <= day]
        costs_upto = [item for item in costs if item.day <= day]

        context = {
            "cycles_closed": len(upto),
            "cycles_closed_today": len(today),
            "candidates_total": len(discoveries),
            "tasks_total": len(outcomes),
            "verifications_today": sum(1 for item in verifications if item.day == day),
        }
        results: dict[str, MetricResult] = {
            "valid_discovery_rate": valid_discovery_rate(discoveries),
            "duplicate_discovery_rate": duplicate_discovery_rate(discoveries),
            "decision_adoption_rate": decision_adoption_rate(decisions),
            "false_positive_rate": false_positive_rate(decisions),
            "task_success_rate": task_success_rate(outcomes),
            "rework_rate": rework_rate(outcomes),
            "regression_defect_rate": regression_defect_rate(outcomes),
            "attainment_delta": attainment_delta(attainments),
        }
        for metric_id, result in results.items():
            points.setdefault(metric_id, []).append(
                TrendPoint.from_result(day, result, extra=context)
            )

        points.setdefault(VERIFICATION_PASS_RATE, []).append(
            TrendPoint.from_result(
                day,
                _verification_pass_rate(verifications_upto),
                extra=context,
            )
        )
        points.setdefault(COST_CUMULATIVE, []).append(
            TrendPoint.from_result(
                day,
                _cumulative_cost(costs_upto, day),
                extra=context,
            )
        )

    trends: list[MetricTrend] = []
    for metric_id, series in points.items():
        trends.append(_to_trend(metric_id, tuple(series)))
    return TrendBoard(project_id=project_id, window=window, trends=tuple(trends))


def _discoveries(cycles: Sequence[CycleSample], labels: LabelSet | None) -> list[Discovery]:
    """把周期里的候选摊平，并按标注集打标。"""
    items = [item for cycle in cycles for item in cycle.discoveries]
    if labels is None:
        return items
    return [
        replace(item, label=labels.verdict_of(item.candidate_id)) for item in items
    ]


def _decisions(cycles: Sequence[CycleSample], labels: LabelSet | None) -> list[DecisionRecord]:
    """把周期里的决策摊平，并按标注集打标。

    **标注只从标注集来。** 决策自己带的标注会被丢掉 —— 让被评价的一方决定
    分母，那个分母就不可信。
    """
    items = [item for cycle in cycles for item in cycle.decisions]
    if labels is None:
        return items
    return list(apply_labels(items, labels))


def _verification_pass_rate(samples: Sequence[VerificationSample]) -> MetricResult:
    """验收通过率 = 通过的次数 / 跑过的次数。

    **``skipped`` 不进分母** —— 没跑的东西没有结论，把它算成"没通过"等于
    因为**没验**而惩罚系统。

    **只看跑过的那部分**，所以分母是"截至今天跑过几次验收"。
    """
    considered = [item for item in samples if item.result in _VERIFICATION_CONCLUDED]
    if not considered:
        return rate(
            VERIFICATION_PASS_RATE,
            0,
            0,
            empty_state=MetricState.NO_SAMPLES,
            detail={"reason": "还没有跑过验收"},
        )
    passed = sum(1 for item in considered if item.result == "passed")
    return rate(
        VERIFICATION_PASS_RATE,
        passed,
        len(considered),
        detail={
            "passed": passed,
            "failed": sum(1 for item in considered if item.result == "failed"),
            "errored": sum(1 for item in considered if item.result == "error"),
            "skipped": sum(1 for item in samples if item.result == "skipped"),
        },
    )


def _cumulative_cost(samples: Sequence[CostSample], day: date) -> MetricResult:
    """窗口内的累计花费。

    **没有流水就报"取不到"，不报 0。** "没记账"与"一文未花"在报表上长得
    一样，而后者是一个**假的安全信号**。

    **币种必须只有一个。** 两个币种的数不能相加 —— 那算出来的数什么都不是。
    """
    if not samples:
        return rate(
            COST_CUMULATIVE,
            0,
            0,
            empty_state=MetricState.NO_SAMPLES,
            detail={"reason": "账本里没有这个项目的成本流水"},
        )
    currencies = {item.currency for item in samples}
    if len(currencies) > 1:
        known = "、".join(sorted(currencies))
        raise ValueError(f"成本流水里有多个币种（{known}）—— 它们的数不能相加")
    total = sum((item.amount for item in samples), Decimal(0))
    day_total = sum((item.amount for item in samples if item.day == day), Decimal(0))
    currency = samples[0].currency
    return MetricResult(
        metric_id=COST_CUMULATIVE,
        state=MetricState.COMPUTED,
        value=total,
        numerator=total,
        denominator=Decimal(len(samples)),
        detail={
            "amount": str(total),
            "currency": currency,
            "entries": len(samples),
            "day_amount": str(day_total),
        },
    )


def _to_trend(metric_id: str, series: tuple[TrendPoint, ...]) -> MetricTrend:
    """把一串点变成一条曲线 —— 核心指标的元信息取自注册表。"""
    if metric_id in CORE_REGISTRY:
        definition = CORE_REGISTRY.get(metric_id)
        return MetricTrend(
            metric_id=metric_id,
            layer=definition.layer,
            kind=SeriesKind.CORE,
            unit=definition.unit,
            name=definition.name,
            dimension=LAYER_NAMES[definition.layer],
            points=series,
        )
    if metric_id == VERIFICATION_PASS_RATE:
        return MetricTrend(
            metric_id=metric_id,
            layer=None,
            kind=SeriesKind.DIAGNOSTIC,
            unit="ratio",
            name="验收通过率",
            dimension=QUALITY_DIMENSION,
            points=series,
        )
    return MetricTrend(
        metric_id=metric_id,
        layer=None,
        kind=SeriesKind.DIAGNOSTIC,
        unit="money",
        name="累计花费",
        dimension=COST_DIMENSION,
        points=series,
    )


def _trend_to_dict(trend: MetricTrend) -> dict[str, object]:
    """一条曲线的机读形态。"""
    return {
        "metric_id": trend.metric_id,
        "layer": trend.layer.value if trend.layer is not None else None,
        "kind": trend.kind.value,
        "name": trend.name,
        "unit": trend.unit,
        "dimension": trend.dimension,
        "direction": trend.direction.value,
        "days": trend.days,
        "computed_days": trend.computed_days,
        "points": [
            {
                "day": point.day.isoformat(),
                "state": point.state.value,
                "value": str(point.value) if point.value is not None else None,
                "numerator": str(point.numerator) if point.numerator is not None else None,
                "denominator": str(point.denominator) if point.denominator is not None else None,
                "sample_size": point.sample_size,
                "detail": _jsonable(point.detail),
            }
            for point in trend.points
        ],
    }


def _markdown_row(row: Sequence[str], widths: Sequence[int]) -> str:
    """一行 Markdown —— 单元格按列宽补齐。

    **按显示宽度补，不按字符数补。** 中文字符占两列，按字符数补齐的表在
    终端里是歪的 —— 而歪掉的表格读起来像坏了。
    """
    cells = [cell + " " * (widths[index] - _display_width(cell)) for index, cell in enumerate(row)]
    return "| " + " | ".join(cells) + " |"


def _display_width(text: str) -> int:
    """一段文字占几列。

    **全角字符占两列。** 东亚宽字符（``W`` 与 ``F``）按两列算，其余按一列。
    """
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _jsonable(value: object) -> object:
    """把细节里的值变成能进 JSON 的形态。

    **十进制数转成字符串，不转成浮点。** 浮点会把 ``0.1 + 0.2`` 变成
    ``0.30000000000000004`` —— 报表上的数对不上，读者会以为指标算错了。
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _format_point(point: TrendPoint, trend: MetricTrend) -> str:
    """一个点怎么写进表里。"""
    if not point.is_computed or point.value is None:
        return "—"
    if trend.unit in {"ratio", "ratio_delta"}:
        return f"{point.value * 100:.1f}%"
    if trend.unit == "money":
        currency = point.detail.get("currency", "")
        return f"{point.value} {currency}".strip()
    return str(point.value)


__all__ = [
    "COST_CUMULATIVE",
    "COST_DIMENSION",
    "LAYER_NAMES",
    "MIN_TREND_DAYS",
    "QUALITY_DIMENSION",
    "VERIFICATION_PASS_RATE",
    "CostSample",
    "CycleSample",
    "MetricTrend",
    "SeriesKind",
    "TrendBoard",
    "TrendDirection",
    "TrendError",
    "TrendInput",
    "TrendPoint",
    "TrendWindow",
    "VerificationSample",
    "build_board",
]
