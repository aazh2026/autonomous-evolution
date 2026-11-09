"""T2 的账 —— 这一轮落在 ``TASK-P1-072`` 基线的几倍。

设计依据：docs/tasks.md TASK-P1-081（"验证（T2）：单轮闭环端到端耗时与成本落在
``TASK-P1-072`` 基线的 2 倍以内"）。

**基线从文件里读，不写死在代码里。** 基线是**量出来的**（:mod:`scripts.
collect_baselines` 跑出来的 ``docs/evaluation/baselines.yaml``），把那个数抄进
代码，基线一更新代码就跟着过期 —— 而过期的基线会安静地给出"通过"。所以这里
只负责**读**它、**比**它，判定口径写在 :func:`check_m1_budget` 的文档里。

**比的是中位数，不是最大值。** 基线表给的是**按类别**的中位数，而 T2 要答的
是"一个任务典型要花多少" —— 那是全体样本的中位数，与基线报告里那句
"单任务中位成本 0.631445 USD"是同一个数。取最大值等于拿最贵的那一类当门槛，
那是另一件事。

**口径只有一份。** 演示脚本与集成测试都调 :func:`check_m1_budget` —— 两处各
写一遍判定，早晚写出两个不一样的答案，而"哪一份对"没人说得清。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

from evaluation.baselines import (
    QUANTA,
    BaselineMetric,
    BaselineTable,
    median,
)

#: 基线表在仓库里的相对位置。
BASELINE_RELATIVE_PATH: Final[str] = "docs/evaluation/baselines.yaml"

#: T2 允许的倍数 —— 基线的两倍。
DEFAULT_FACTOR: Final[Decimal] = Decimal("2")

#: 成本基线的单位 —— 金额带币种，没有币种的金额比不了。
COST_UNIT: Final[str] = "USD"

#: 时长基线的单位。
DURATION_UNIT: Final[str] = "秒"


@dataclass(frozen=True, slots=True)
class M1BudgetCheck:
    """一项量的基线比较。

    **"没基线"不是一个取值。** 基线缺失时这一项根本不会被造出来 ——
    造出来一个 :attr:`baseline` 是 ``None`` 的比较对象，读的人就得分辨
    "基线是 0"与"没有基线"，而这两件事在报表上长得一模一样（TASK-P1-072 的
    同一条规矩）。缺基线时 :func:`check_m1_budget` 直接报错。
    """

    #: 这一项叫什么 —— "成本" / "耗时"。
    metric: str
    #: 单位 —— "USD" / "秒"。
    unit: str
    #: 基线（中位数）。
    baseline: Decimal
    #: 这一轮真的花了多少。
    observed: Decimal
    #: 允许的倍数。
    factor: Decimal

    @property
    def ceiling(self) -> Decimal:
        """允许的上限 —— 基线的 :attr:`factor` 倍。"""
        return self.baseline * self.factor

    @property
    def ratio(self) -> Decimal:
        """这一轮是基线的几倍。"""
        if self.baseline == 0:
            return Decimal(0)
        return self.observed / self.baseline

    @property
    def within(self) -> bool:
        """落在上限以内吗。"""
        return self.observed <= self.ceiling

    def message(self) -> str:
        """说给人听的一句话。"""
        verdict = "在基线 2 倍以内" if self.within else "超出基线 2 倍"
        return (
            f"{self.metric}：{self.observed} {self.unit}，基线 {self.baseline} "
            f"{self.unit}（{self.ratio:.2f} 倍），上限 {self.ceiling} {self.unit} —— {verdict}"
        )


class M1BudgetError(RuntimeError):
    """基线比不了 —— 缺基线，或者两边不是同一个币种。"""


def load_baseline(repo_root: Path) -> BaselineTable:
    """读基线表。

    :param repo_root: 仓库根目录 —— 基线表按相对路径放在它下面。
    :raises M1BudgetError: 基线表不在或读不了。
    """
    path = repo_root / BASELINE_RELATIVE_PATH
    if not path.exists():
        raise M1BudgetError(f"基线表不在：{path}")
    try:
        return BaselineTable.from_yaml(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise M1BudgetError(f"基线表读不了：{path} —— {exc}") from exc


def check_m1_budget(
    *,
    repo_root: Path,
    cost: Decimal,
    elapsed_seconds: Decimal,
    factor: Decimal = DEFAULT_FACTOR,
) -> tuple[M1BudgetCheck, ...]:
    """把这一轮的成本与耗时跟基线比。

    **基线取全体样本的中位数。** 基线表按任务类别给了中位数，而 T2 问的是
    "一个任务典型要花多少" —— 那是跨类别的中位数，与基线报告里那句
    "单任务中位成本 0.631445 USD"是同一个数。

    **币种必须一致。** 两笔不同的钱互相比，比出来的倍数没有意义 —— 所以
    币种对不上直接报错，而不是给出一个看着合理的倍数。

    :param repo_root: 仓库根目录。
    :param cost: 这一轮的成本（带币种的金额）。
    :param elapsed_seconds: 这一轮的端到端耗时（秒）。
    :param factor: 允许的倍数 —— 默认两倍。
    :raises M1BudgetError: 缺基线，或币种对不上。
    """
    table = load_baseline(repo_root)
    baseline_currency = table.currency
    if baseline_currency is None:
        raise M1BudgetError("基线表里没有金额样本 —— 成本基线未建立")
    if baseline_currency != COST_UNIT:
        raise M1BudgetError(
            f"基线表的币种是 {baseline_currency!r}，这一轮记的是 {COST_UNIT!r} —— 两者不可比"
        )

    costs = [
        value
        for value in (item.value_of(BaselineMetric.COST) for item in table.measurements)
        if value is not None
    ]
    durations = [item.duration_seconds for item in table.measurements if item.duration_seconds >= 0]
    if not costs:
        raise M1BudgetError("基线表里没有采到金额 —— 成本基线未建立")
    if not durations:
        raise M1BudgetError("基线表里没有采到时长 —— 时长基线未建立")

    return (
        M1BudgetCheck(
            metric="成本",
            unit=COST_UNIT,
            baseline=median(costs, quantum=QUANTA[BaselineMetric.COST]),
            observed=cost,
            factor=factor,
        ),
        M1BudgetCheck(
            metric="耗时",
            unit=DURATION_UNIT,
            baseline=median(durations, quantum=QUANTA[BaselineMetric.DURATION]),
            observed=elapsed_seconds,
            factor=factor,
        ),
    )


__all__ = [
    "BASELINE_RELATIVE_PATH",
    "COST_UNIT",
    "DEFAULT_FACTOR",
    "DURATION_UNIT",
    "M1BudgetCheck",
    "M1BudgetError",
    "check_m1_budget",
    "load_baseline",
]
