"""基线 —— 成本、时延与覆盖率的"正常水位"。

设计依据：docs/tasks.md TASK-P1-072（T1 ① 基线表可被程序读取并用于阈值
比较；T1 ② 基线缺失时相关 T2 指标明确报"基线未建立"而非返回 0；T2 基线表
中位数为后续成本预算提供依据，阶段一用于 ``max_daily_cost`` 校准）；
docs/tasks.md §5 盲区 F（成本基线缺失）与盲区 G（性能退化判定缺基线）；
R-018（预算管 Token / 模型调用 / 计算资源 / 时间）、R-039（成本同源同币种）。

**基线回答的是"一次典型任务要花多少"。** 没有它，所有成本类 T2 指标都是
空转 —— design.md 给的 ``max_daily_cost: 50`` 是示意值，**没有来源的阈值
不是阈值，是许愿**（§5 盲区 F 说的就是这件事）。

**为什么用中位数而不是平均值。** 一次跑飞的任务会把平均值拽高，而预算是按
"正常水位"定的，不是按"最坏情况"定的 —— 按最坏情况定出来的预算大到没人愿
意用。反过来说，**中位数也不告诉你最坏情况有多坏**，那是 p95 的事；这里只
承诺"一半的任务比它便宜"。

**"基线未建立"是一种状态，不是 0。** 这条是 TASK-P1-014 的老规矩在基线
上的落地：还没采到基线时，报 0 会让看板显示"成本 0"，读的人会以为任务不要
钱。:attr:`MetricState.BASELINE_MISSING` 就是"还没建立"这个事实的载体，
而 :data:`BASELINE_MISSING_TEXT` 是它被说给人听的那句话。

**没测到的量是 ``None``，不是 0。** 一次执行没报覆盖率，那是"没测"；报了
0，那是"测了，覆盖率是 0"。两者在报表上长得一模一样，含义相反。

**表是数据，代码只负责口径。** 表本身落成一份 YAML（机读）与一份 Markdown
（人读）；这一层只负责把一批测量归拢成中位数、把测量跟基线比、把基线换算
成预算上限。**它不碰数据库、不起进程** —— 采集是别的事（``scripts/
collect_baselines.py``），这样每个口径都能被单独测。

**覆盖率按模块分开。** 一个仓库的总覆盖率会掩盖问题：新增一个没测的模块，
总覆盖率掉 0.3%，而那个模块其实是 0。模块级的基线是 TASK-P1-042
（``threshold = max(模块基线, 全局地板)``）的输入。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

import yaml

from core.metrics.cost import MONEY_QUANTUM, CostAmount, CurrencyMismatchError, TokenUsage
from core.policy.budget import Budget
from evaluation.common import MetricState

#: "基线未建立" —— T1 ② 要的那五个字。
BASELINE_MISSING_TEXT: Final[str] = "基线未建立"

#: 阈值比较的默认倍数：花销超过基线的这个倍数就算超出。
#:
#: **2 倍** 与 TASK-P1-081 的验收口径一致（"单轮闭环端到端耗时与成本落在
#: TASK-P1-072 基线的 2 倍以内"）。
DEFAULT_FACTOR: Final[Decimal] = Decimal("2")


class BaselineError(ValueError):
    """基线表里的错。"""


class BaselineMetric(StrEnum):
    """基线要盯的量。

    **刻意不含任务数、Token 数作为"核心指标"。** R-117 禁的是它们**冒充核心
    指标**（"做得多"不等于"项目变好了"），不是禁它们被采集 —— 基线表要的
    恰恰是这些诊断量。
    """

    #: 成本 —— 带币种。
    COST = "cost"
    #: token 总量（四类分开记，见 :class:`core.metrics.cost.TokenUsage`）。
    TOKENS = "tokens"
    #: 时长 —— 秒。
    DURATION = "duration"
    #: 工具调用次数。
    TOOL_CALLS = "tool_calls"
    #: 覆盖率 —— 比例。
    COVERAGE = "coverage"


#: 每个指标的单位。
UNITS: Final[Mapping[BaselineMetric, str]] = {
    BaselineMetric.COST: "金额",
    BaselineMetric.TOKENS: "token",
    BaselineMetric.DURATION: "秒",
    BaselineMetric.TOOL_CALLS: "次",
    BaselineMetric.COVERAGE: "比例",
}

#: 每个指标的量化步长 —— ``None`` 表示不量化。
#:
#: **金额按账本那一列量化**（``Numeric(20, 6)``），否则基线与账上的钱差最后
#: 几位，而"差最后几位"在钱上就是差钱。
QUANTA: Final[Mapping[BaselineMetric, Decimal | None]] = {
    BaselineMetric.COST: MONEY_QUANTUM,
    BaselineMetric.TOKENS: None,
    BaselineMetric.DURATION: Decimal("0.001"),
    BaselineMetric.TOOL_CALLS: None,
    BaselineMetric.COVERAGE: Decimal("0.000001"),
}


def _decimal_of(value: object, what: str) -> Decimal:
    """把外部读来的一个值读成十进制。

    **只认字符串与整数。** YAML 里裸写的 ``0.1`` 是浮点数，读进来是
    ``0.1000000000000000055511151231257827…`` —— 金额差最后几位就是差钱，
    覆盖率差最后几位就是差一个数量级的幻觉。写字符串，读进来就是十进制。
    """
    if isinstance(value, bool):
        raise BaselineError(f"{what}不能是布尔值")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as error:
            raise BaselineError(f"{what}不是十进制数：{value!r}") from error
    raise BaselineError(f"{what}的类型不对：{type(value).__name__}")


def median(values: Sequence[Decimal], *, quantum: Decimal | None = None) -> Decimal:
    """中位数。

    **偶数个样本时取中间两个的平均。** 取下中位数会让一半样本落在基线之上
    —— 那样"一半的任务比基线便宜"这句话就不成立了。
    """
    ordered = sorted(values)
    if not ordered:
        raise BaselineError("中位数不能对空样本算")
    count = len(ordered)
    middle = count // 2
    value = ordered[middle] if count % 2 == 1 else (ordered[middle - 1] + ordered[middle]) / 2
    if quantum is not None:
        value = value.quantize(quantum)
    return value


@dataclass(frozen=True, slots=True)
class TaskMeasurement:
    """一次代表性任务执行测到的量。

    **没测到的就是 ``None``，不填 0。** 一次执行没报覆盖率，那是"没测"；
    报了 0，那是"测了，覆盖率是 0"。

    :attr:`cost` 带币种 —— 金额没有币种就没法和别处比，而"能不能比"取决于
    两边是不是同一个币种（**换个币种记一笔账，比记错数字更坏**）。
    """

    #: 任务类别 —— "代表性任务"的那一类。基线是**按类别**给的：一个改文档
    #: 的任务与一个改核心逻辑的任务不该共用一个基线。
    task_kind: str
    #: 时长 —— 秒。
    duration_seconds: Decimal
    #: 工具调用次数。
    tool_calls: int
    #: token 用量 —— 四类分开记。
    #:
    #: ``None`` 表示**这次没读到用量** —— 不是"用了 0 个"。结果事件里没有
    #: ``usage`` 字段时就是这个情形，而"没用 token"与"我们没读到"在报表上
    #: 长得一模一样，含义相反。
    usage: TokenUsage | None
    #: 成本 —— ``None`` 表示这次没有可用的金额。
    cost: CostAmount | None = None
    #: 整体覆盖率 —— ``None`` 表示没测。
    coverage: Decimal | None = None
    #: 按模块的覆盖率 —— 键是模块名。
    coverage_by_module: Mapping[str, Decimal] = field(default_factory=dict)
    #: 这条测量**从哪来** —— 进基线表时逐条留档，好让人复核。
    source: str = ""

    def __post_init__(self) -> None:
        if not self.task_kind.strip():
            raise BaselineError("任务类别不能是空白")
        if self.duration_seconds < 0:
            raise BaselineError(f"时长不能为负：{self.duration_seconds}")
        if self.tool_calls < 0:
            raise BaselineError(f"工具调用次数不能为负：{self.tool_calls}")
        if self.coverage is not None and not (Decimal(0) <= self.coverage <= Decimal(1)):
            raise BaselineError(f"覆盖率必须在 0 与 1 之间：{self.coverage}")
        for module, ratio in self.coverage_by_module.items():
            if not module.strip():
                raise BaselineError("模块名不能是空白")
            if not (Decimal(0) <= ratio <= Decimal(1)):
                raise BaselineError(f"模块 {module} 的覆盖率必须在 0 与 1 之间：{ratio}")

    def value_of(self, metric: BaselineMetric) -> Decimal | None:
        """这个指标在这条测量上的值 —— **没测到就是 ``None``**。"""
        match metric:
            case BaselineMetric.COST:
                return None if self.cost is None else self.cost.amount
            case BaselineMetric.TOKENS:
                return None if self.usage is None else Decimal(self.usage.total_tokens)
            case BaselineMetric.DURATION:
                return self.duration_seconds
            case BaselineMetric.TOOL_CALLS:
                return Decimal(self.tool_calls)
            case BaselineMetric.COVERAGE:
                return self.coverage


@dataclass(frozen=True, slots=True)
class Baseline:
    """某个任务类别在某个指标上的基线 —— **中位数**。"""

    task_kind: str
    metric: BaselineMetric
    median: Decimal
    sample_count: int
    #: 金额类才有 —— 别的指标与币种无关。
    currency: str | None = None
    #: 贡献了这些样本的来源 —— 逐条留档，好让人复核这个数是怎么来的。
    sources: tuple[str, ...] = ()

    @property
    def unit(self) -> str:
        return UNITS[self.metric]


@dataclass(frozen=True, slots=True)
class ModuleCoverageBaseline:
    """一个模块的覆盖率基线。"""

    module: str
    median: Decimal
    sample_count: int


@dataclass(frozen=True, slots=True)
class BaselineComparison:
    """一次测量跟基线的比较结果。

    **状态是三态。** :attr:`MetricState.COMPUTED` 是比出来了；
    :attr:`MetricState.BASELINE_MISSING` 是**还没建立基线**；
    :attr:`MetricState.NO_SAMPLES` 是**这次没测到**那个量。
    后两种情况下 :attr:`ratio` 与 :attr:`within_baseline` 都是 ``None`` ——
    **不是 0，也不是 False。** "没基线"与"没超出"在报表上长得一样，而前者
    说的是"这件事还没法判断"。
    """

    task_kind: str
    metric: BaselineMetric
    state: MetricState
    #: 这次测到的值 —— **没测到就是 ``None``**。
    observed: Decimal | None
    #: 基线 —— **没建立就是 ``None``**。
    baseline: Decimal | None
    #: 允许的倍数。
    factor: Decimal
    currency: str | None = None

    @property
    def ratio(self) -> Decimal | None:
        """这次的值是基线的几倍 —— **没有基线就没有倍数**。"""
        if self.baseline is None or self.baseline == 0 or self.observed is None:
            return None
        return self.observed / self.baseline

    @property
    def within_baseline(self) -> bool | None:
        """落在基线的 :attr:`factor` 倍以内吗。

        **没有基线时是 ``None``，不是 ``False``。** ``False`` 说的是"超了"，
        而"没法判断"是另一回事。
        """
        if self.state is not MetricState.COMPUTED:
            return None
        if self.baseline is None or self.observed is None:
            return None
        if self.baseline == 0:
            return self.observed == 0
        return self.observed <= self.baseline * self.factor

    @property
    def message(self) -> str:
        """说给人听的一句话 —— **基线缺失时那句话里必须有"基线未建立"**。"""
        name = UNITS[self.metric]
        if self.state is MetricState.BASELINE_MISSING:
            return f"{self.task_kind} 的{name}基线未建立 —— {BASELINE_MISSING_TEXT}"
        if self.state is MetricState.NO_SAMPLES:
            return f"{self.task_kind} 这次没有采到{name} —— 无法与基线比较"
        assert self.observed is not None
        assert self.baseline is not None
        if self.within_baseline:
            return f"{self.task_kind} 的{name}在基线 {self.factor} 倍以内"
        return f"{self.task_kind} 的{name}超出基线 {self.factor} 倍"


@dataclass(frozen=True, slots=True)
class DailyCostCalibration:
    """按基线校准出来的每日成本上限。

    **缺基线时 :attr:`max_daily_cost` 是 ``None``，不是 0。** 一个"每天最多
    允许花 0 元"的预算会让系统第一天就停摆，而看板上它显示的是"预算 0" ——
    读的人会以为"不花钱"是目标。
    """

    state: MetricState
    #: 校准出来的 ``max_daily_cost`` —— **缺基线就是 ``None``**。
    max_daily_cost: Decimal | None
    #: 记账币种。
    currency: str | None
    #: 一天里预计跑多少个任务。
    tasks_per_day: int
    #: 安全倍数。
    factor: Decimal
    #: 单个任务的中位成本 —— 校准的输入。
    median_cost_per_task: Decimal | None
    #: 参与计算的成本样本数。
    sample_count: int

    @property
    def message(self) -> str:
        if self.state is MetricState.BASELINE_MISSING:
            return f"成本基线未建立 —— {BASELINE_MISSING_TEXT}，无法校准 max_daily_cost"
        assert self.max_daily_cost is not None
        assert self.currency is not None
        return (
            f"单任务中位成本 {self.median_cost_per_task} {self.currency}，"
            f"按每天 {self.tasks_per_day} 个任务、{self.factor} 倍安全系数，"
            f"max_daily_cost = {self.max_daily_cost} {self.currency}"
        )

    def to_budget(self) -> Budget | None:
        """变成一个 :class:`~core.policy.budget.Budget` —— **缺基线就没有**。

        **没有基线就不给预算。** 给一个 0 元的预算看上去很安全，实际上会让
        系统第一天就拒绝任何执行 —— 而看板上它显示的是"预算正常"。
        """
        if self.state is not MetricState.COMPUTED:
            return None
        assert self.max_daily_cost is not None
        assert self.currency is not None
        return Budget(currency=self.currency, max_daily_cost=self.max_daily_cost)


class BaselineTable:
    """基线表 —— 一批测量按任务类别归拢后的中位数。

    **表是数据；这一层只负责口径。** 采集在 ``scripts/collect_baselines.py``，
    落盘成一份 YAML（机读）与一份 Markdown（人读）。分开的理由：采集要起
    进程、读文件，而口径必须能被单元测试单独证伪。

    **币种必须一致。** 混着两种币种的样本没法取中位数 —— 那是两个项目的钱
    加起来除以二。构造时直接报错，而不是等到用的时候才发现。
    """

    def __init__(self, measurements: Sequence[TaskMeasurement], *, pilot: str) -> None:
        if not pilot.strip():
            raise BaselineError("试点仓库名不能是空白")
        self._pilot = pilot
        self._measurements = tuple(measurements)
        currencies = {item.cost.currency for item in self._measurements if item.cost is not None}
        if len(currencies) > 1:
            known = "、".join(sorted(currencies))
            raise CurrencyMismatchError(
                f"基线表里的金额不是一个币种：{known} —— 中位数对两个项目的钱没有意义"
            )
        self._currency = next(iter(currencies), None) if currencies else None

    # ---------- 只读视图 ----------

    @property
    def pilot(self) -> str:
        """试点仓库。"""
        return self._pilot

    @property
    def measurements(self) -> tuple[TaskMeasurement, ...]:
        """全部原始测量 —— **逐条留档，好复核中位数是怎么来的**。"""
        return self._measurements

    @property
    def currency(self) -> str | None:
        """记账币种 —— 没有金额样本就是 ``None``。"""
        return self._currency

    def task_kinds(self) -> tuple[str, ...]:
        """表里有哪些任务类别。"""
        return tuple(sorted({item.task_kind for item in self._measurements}))

    # ---------- 基线 ----------

    def baselines(self) -> tuple[Baseline, ...]:
        """全部基线 —— 按任务类别与指标排好序。"""
        result: list[Baseline] = []
        for task_kind in self.task_kinds():
            for metric in BaselineMetric:
                baseline = self.baseline(task_kind, metric)
                if baseline is not None:
                    result.append(baseline)
        return tuple(result)

    def baseline(self, task_kind: str, metric: BaselineMetric) -> Baseline | None:
        """某个类别在某个指标上的基线 —— **没有样本就是 ``None``**。

        ``None`` 表示"基线未建立"。它与"基线是 0"是两回事：前者是还没采到
        数据，后者是采到了且结果就是 0。
        """
        values: list[Decimal] = []
        sources: list[str] = []
        for item in self._measurements:
            if item.task_kind != task_kind:
                continue
            value = item.value_of(metric)
            if value is None:
                continue
            values.append(value)
            if item.source:
                sources.append(item.source)
        if not values:
            return None
        quantum = QUANTA[metric]
        return Baseline(
            task_kind=task_kind,
            metric=metric,
            median=median(values, quantum=quantum),
            sample_count=len(values),
            currency=self._currency if metric is BaselineMetric.COST else None,
            sources=tuple(sorted(set(sources))),
        )

    def median(self, task_kind: str, metric: BaselineMetric) -> Decimal | None:
        """基线的中位数 —— 便捷入口。"""
        baseline = self.baseline(task_kind, metric)
        return None if baseline is None else baseline.median

    def module_coverage(self) -> tuple[ModuleCoverageBaseline, ...]:
        """按模块的覆盖率基线。

        **只算报了这个模块的样本。** 没报就是"这个模块这次没测"，不是"这个
        模块覆盖率为 0"。
        """
        collected: dict[str, list[Decimal]] = {}
        for item in self._measurements:
            for module, ratio in item.coverage_by_module.items():
                collected.setdefault(module, []).append(ratio)
        return tuple(
            ModuleCoverageBaseline(
                module=module,
                median=median(ratios, quantum=QUANTA[BaselineMetric.COVERAGE]),
                sample_count=len(ratios),
            )
            for module, ratios in sorted(collected.items())
        )

    # ---------- 比较 ----------

    def compare(
        self,
        measurement: TaskMeasurement,
        metric: BaselineMetric,
        *,
        factor: Decimal = DEFAULT_FACTOR,
    ) -> BaselineComparison:
        """把一次测量跟基线比。

        **缺基线时报"基线未建立"，不报 0。** 这是 T1 ② 的落点：报 0 会让
        看板显示"这项在基线的 0 倍"——读的人以为"远低于基线"，而真相是
        "还没法比"。

        :raises CurrencyMismatchError: 这次的钱与基线的钱不是一个币种 ——
            拿两笔不同的钱互相比，比出来的倍数没有意义。
        """
        baseline = self.baseline(measurement.task_kind, metric)
        observed = measurement.value_of(metric)
        currency = self._currency if metric is BaselineMetric.COST else None
        if baseline is None:
            return BaselineComparison(
                task_kind=measurement.task_kind,
                metric=metric,
                state=MetricState.BASELINE_MISSING,
                observed=observed,
                baseline=None,
                factor=factor,
                currency=currency,
            )
        if observed is None:
            return BaselineComparison(
                task_kind=measurement.task_kind,
                metric=metric,
                state=MetricState.NO_SAMPLES,
                observed=None,
                baseline=baseline.median,
                factor=factor,
                currency=currency,
            )
        if metric is BaselineMetric.COST:
            assert measurement.cost is not None
            assert baseline.currency is not None
            if measurement.cost.currency != baseline.currency:
                raise CurrencyMismatchError(
                    f"比不了：这次记的是 {measurement.cost.currency}，基线是 {baseline.currency}"
                )
        return BaselineComparison(
            task_kind=measurement.task_kind,
            metric=metric,
            state=MetricState.COMPUTED,
            observed=observed,
            baseline=baseline.median,
            factor=factor,
            currency=currency,
        )

    # ---------- 落盘 ----------

    def to_yaml(self) -> str:
        """机读版 —— **所有十进制量写成字符串**。

        YAML 里裸写的 ``0.1`` 是浮点数，读回来是
        ``0.1000000000000000055511151231257827…``。金额差最后几位就是差钱。
        """
        data: dict[str, object] = {
            "pilot": self._pilot,
            "currency": self._currency,
            "measurements": [_measurement_to_mapping(item) for item in self._measurements],
        }
        return yaml.safe_dump(
            data,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )

    @classmethod
    def from_yaml(cls, text: str) -> BaselineTable:
        """从机读版读回来 —— **读回来的表必须和写出去的一样**。

        **不认识的键直接报错。** 少写一个字段（``coverage_by_module`` 拼错
        了）会让那一列静默消失，而消失的列看起来跟"没测过"一模一样。
        """
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise BaselineError(f"基线表不是合法的 YAML：{error}") from error
        if not isinstance(raw, Mapping):
            raise BaselineError("基线表必须是一个映射")
        unknown = set(raw) - {"pilot", "currency", "measurements"}
        if unknown:
            raise BaselineError(f"基线表里有不认识的键：{'、'.join(sorted(unknown))}")
        pilot = raw.get("pilot")
        if not isinstance(pilot, str):
            raise BaselineError("基线表缺 pilot")
        items = raw.get("measurements")
        if not isinstance(items, list):
            raise BaselineError("基线表缺 measurements")
        measurements = [_measurement_from_mapping(entry) for entry in items]
        return cls(measurements, pilot=pilot)

    def render_markdown(self) -> str:
        """人读版 —— ``docs/evaluation/baselines.md`` 的内容。

        **缺失的项写"基线未建立"，不留空。** 表格里一个空格看起来像"这项
        没有要求"，而"基线未建立"说的是"这件事还没做"。
        """
        lines: list[str] = []
        lines.append("# 基线表 —— 成本、时延与覆盖率")
        lines.append("")
        lines.append("> 本文件由 `scripts/collect_baselines.py` 生成，**请勿手工编辑**。")
        lines.append("> 机读版：[`baselines.yaml`](baselines.yaml)。")
        lines.append("")
        lines.append("## 试点仓库")
        lines.append("")
        lines.append(f"- **试点**：`{self._pilot}`")
        lines.append(f"- **记账币种**：{self._currency or '（本次没有金额样本）'}")
        lines.append(f"- **样本数**：{len(self._measurements)}")
        lines.append(f"- **任务类别数**：{len(self.task_kinds())}")
        lines.append("")
        lines.append("## 各类任务的基线（中位数）")
        lines.append("")
        if not self.baselines():
            lines.append(f"**{BASELINE_MISSING_TEXT}** —— 表里还没有任何样本。")
        else:
            lines.append("| 任务类别 | 指标 | 中位数 | 单位 | 样本数 | 来源 |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for task_kind in self.task_kinds():
                for metric in BaselineMetric:
                    baseline = self.baseline(task_kind, metric)
                    if baseline is None:
                        # **缺的那一行要写出来，不能不写。** 少了这一行，读的人
                        # 只会以为"这一类任务没有金额要求" —— 而真相是"这一类
                        # 任务的金额还没采到"。
                        lines.append(
                            f"| {task_kind} | {UNITS[metric]} | "
                            f"{BASELINE_MISSING_TEXT} | {UNITS[metric]} | 0 | — |"
                        )
                        continue
                    value = baseline.median
                    if baseline.metric is BaselineMetric.COST and baseline.currency:
                        value_text = f"{value} {baseline.currency}"
                    else:
                        value_text = str(value)
                    sources = "、".join(baseline.sources) or "—"
                    lines.append(
                        f"| {baseline.task_kind} | {UNITS[baseline.metric]} | "
                        f"{value_text} | {baseline.unit} | {baseline.sample_count} | {sources} |"
                    )
        lines.append("")

        modules = self.module_coverage()
        lines.append("## 覆盖率分布（按模块）")
        lines.append("")
        if not modules:
            lines.append(f"**{BASELINE_MISSING_TEXT}** —— 没有任何模块的覆盖率样本。")
        else:
            lines.append("| 模块 | 覆盖率中位数 | 样本数 |")
            lines.append("| --- | --- | --- |")
            for item in modules:
                lines.append(f"| {item.module} | {item.median} | {item.sample_count} |")
        lines.append("")

        calibration = calibrate_daily_cost(self, tasks_per_day=DEFAULT_TASKS_PER_DAY)
        lines.append("## 成本预算校准")
        lines.append("")
        lines.append(f"- 每日任务数假设：{DEFAULT_TASKS_PER_DAY}")
        lines.append(f"- 安全系数：{DEFAULT_FACTOR}")
        lines.append(f"- 结论：{calibration.message}")
        lines.append("")
        return "\n".join(lines)


#: 校准每日成本时默认假设的一天任务数。
#:
#: **这是个显式假设，不是事实。** 写在这里而不是藏在调用处，是为了让人
#: 看见"这个数字是我假设的"。
DEFAULT_TASKS_PER_DAY: Final[int] = 20


def calibrate_daily_cost(
    table: BaselineTable,
    *,
    tasks_per_day: int,
    factor: Decimal = DEFAULT_FACTOR,
) -> DailyCostCalibration:
    """按基线校准 ``max_daily_cost`` —— T2 的落点。

    **口径：中位成本 × 每日任务数 × 安全系数。** 用中位数而不是平均值，
    理由与基线本身一样 —— 一次跑飞的任务不该把预算拽高。

    **缺成本基线时报"基线未建立"，不给一个 0 元的预算。**

    :raises BaselineError: ``tasks_per_day`` 不是正数。
    """
    if tasks_per_day <= 0:
        raise BaselineError(f"每日任务数必须是正数：{tasks_per_day}")
    if factor <= 0:
        raise BaselineError(f"安全系数必须是正数：{factor}")
    costs = [item.cost.amount for item in table.measurements if item.cost is not None]
    if not costs:
        return DailyCostCalibration(
            state=MetricState.BASELINE_MISSING,
            max_daily_cost=None,
            currency=table.currency,
            tasks_per_day=tasks_per_day,
            factor=factor,
            median_cost_per_task=None,
            sample_count=0,
        )
    per_task = median(costs, quantum=MONEY_QUANTUM)
    daily = (per_task * Decimal(tasks_per_day) * factor).quantize(MONEY_QUANTUM)
    return DailyCostCalibration(
        state=MetricState.COMPUTED,
        max_daily_cost=daily,
        currency=table.currency,
        tasks_per_day=tasks_per_day,
        factor=factor,
        median_cost_per_task=per_task,
        sample_count=len(costs),
    )


def _measurement_to_mapping(item: TaskMeasurement) -> dict[str, object]:
    """一条测量 → 一张可落盘的表。"""
    return {
        "task_kind": item.task_kind,
        "source": item.source,
        "duration_seconds": str(item.duration_seconds),
        "tool_calls": item.tool_calls,
        "tokens": (
            None
            if item.usage is None
            else {
                "input_tokens": item.usage.input_tokens,
                "output_tokens": item.usage.output_tokens,
                "cache_read_tokens": item.usage.cache_read_tokens,
                "cache_write_tokens": item.usage.cache_write_tokens,
            }
        ),
        "cost": None if item.cost is None else str(item.cost.amount),
        "currency": None if item.cost is None else item.cost.currency,
        "coverage": None if item.coverage is None else str(item.coverage),
        "coverage_by_module": (
            None
            if not item.coverage_by_module
            else {key: str(value) for key, value in sorted(item.coverage_by_module.items())}
        ),
    }


_MEASUREMENT_KEYS = frozenset(
    {
        "task_kind",
        "source",
        "duration_seconds",
        "tool_calls",
        "tokens",
        "cost",
        "currency",
        "coverage",
        "coverage_by_module",
    }
)

_TOKEN_KEYS = frozenset(
    {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"}
)


def _measurement_from_mapping(entry: object) -> TaskMeasurement:
    """一张落盘的表 → 一条测量。"""
    if not isinstance(entry, Mapping):
        raise BaselineError("一条测量必须是一个映射")
    unknown = set(entry) - _MEASUREMENT_KEYS
    if unknown:
        raise BaselineError(f"一条测量里有不认识的键：{'、'.join(sorted(unknown))}")
    kind = entry.get("task_kind")
    if not isinstance(kind, str):
        raise BaselineError("一条测量缺 task_kind")
    raw_tokens = entry.get("tokens", {})
    usage: TokenUsage | None = None
    if raw_tokens is not None:
        if not isinstance(raw_tokens, Mapping):
            raise BaselineError("tokens 必须是一个映射")
        unknown_tokens = set(raw_tokens) - _TOKEN_KEYS
        if unknown_tokens:
            raise BaselineError(f"tokens 里有不认识的键：{'、'.join(sorted(unknown_tokens))}")
        tokens = {
            key: _decimal_of(raw_tokens.get(key, 0), f"token {key}").to_integral_value()
            for key in _TOKEN_KEYS
        }
        usage = TokenUsage(
            input_tokens=int(tokens["input_tokens"]),
            output_tokens=int(tokens["output_tokens"]),
            cache_read_tokens=int(tokens["cache_read_tokens"]),
            cache_write_tokens=int(tokens["cache_write_tokens"]),
        )
    raw_cost = entry.get("cost")
    currency = entry.get("currency")
    cost: CostAmount | None = None
    if raw_cost is not None:
        if not isinstance(currency, str):
            raise BaselineError("有金额就必须有币种")
        cost = CostAmount(_decimal_of(raw_cost, "金额"), currency)
    raw_coverage = entry.get("coverage")
    coverage = None if raw_coverage is None else _decimal_of(raw_coverage, "覆盖率")
    raw_modules = entry.get("coverage_by_module")
    modules: dict[str, Decimal] = {}
    if raw_modules is not None:
        if not isinstance(raw_modules, Mapping):
            raise BaselineError("coverage_by_module 必须是一个映射")
        modules = {
            str(name): _decimal_of(value, f"模块 {name} 的覆盖率")
            for name, value in raw_modules.items()
        }
    source = entry.get("source", "")
    if not isinstance(source, str):
        raise BaselineError("source 必须是字符串")
    return TaskMeasurement(
        task_kind=kind,
        duration_seconds=_decimal_of(entry.get("duration_seconds", 0), "时长"),
        tool_calls=int(_decimal_of(entry.get("tool_calls", 0), "工具调用次数")),
        usage=usage,
        cost=cost,
        coverage=coverage,
        coverage_by_module=modules,
        source=source,
    )


__all__ = [
    "BASELINE_MISSING_TEXT",
    "DEFAULT_FACTOR",
    "DEFAULT_TASKS_PER_DAY",
    "QUANTA",
    "UNITS",
    "Baseline",
    "BaselineComparison",
    "BaselineError",
    "BaselineMetric",
    "BaselineTable",
    "DailyCostCalibration",
    "ModuleCoverageBaseline",
    "TaskMeasurement",
    "calibrate_daily_cost",
    "median",
]
