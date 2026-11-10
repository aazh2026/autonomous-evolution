"""能力矩阵 —— 从真跑出来的记录里，长出一张能据此选型的二维表。

设计依据：docs/tasks.md TASK-P4-030（交付物"执行器 × 模型的二维能力矩阵"；验收
条件"**能力矩阵由实测数据得出，且能指导自动选型**"；验证 T2）、R-111（阶段二验收：
结果可比较可评估）、I-17（多执行器结果可比性口径需前置设计）、``TASK-P2-018``
（一维的执行器横向评测）、``TASK-P4-020``（多模型路由）。

**这一层只做三件事。**

1. **把记录切片成格子** —— 按 (执行器, 模型) 归堆，每一格用与一维评测**完全
   同一套口径**算：可用率 / 成功率 / 回归率 / 成本中位数 / token 中位数 / 耗时
   中位数。**口径不许两套** —— 两套口径的两张表没法比。
2. **算清楚哪些格子分得开、哪些分不开** —— Fisher 精确检验 + Holm 校正。分不开
   的归成同一"能力档"。
3. **回答"能不能把执行器与模型分开选"** —— 交互项检验。

**为什么要第 2 件事。** 一格与另一格的成功率差一次运行，是噪声，不是能据此排序
的差别。**拿噪声当差别，选出来的组合在下一轮就不成立了** —— 而矩阵是用来指导
自动选型的，指导错了比没有更糟。

**为什么要第 3 件事。** "最好的执行器"与"最好的模型"这两个边际结论，**只有在
没有交互时才成立**。有交互时，按边际选会选出一个**根本没测过**的组合。所以这
一层的结论不是"谁更好"，而是"**能不能分开选**"。

**三态，不是两态。** 一格没跑过就是**没有证据**，不是"成功率 0"。所以没有实测值
的格子不进候选（由 :mod:`core.policy.capability` 负责排除），报表上写"无样本"。
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from itertools import combinations
from pathlib import Path

from core.policy.capability import (
    CapabilityCell,
    CapabilityMatrix,
    SelectionRequest,
    cell_from_dict,
    cell_to_dict,
    recommend,
)
from evaluation.executors.benchmark import (
    METRIC_SUCCESS_RATE,
    PriceTable,
    RunOutcome,
    RunRecord,
    evaluate_executor,
)
from evaluation.executors.statistics import (
    ALPHA,
    Alternative,
    StatisticsOptions,
    TestResult,
    compare_success_rates,
    holm_adjust,
)

#: 矩阵上的能力档 —— **统计上分不开的格子共用一个档**。
CAPABILITY_GROUP_PREFIX = "g"

#: 交互项检验的统计量名。
METRIC_INTERACTION = "capability_interaction"

#: 做交互项检验至少要几个执行器 —— **一个执行器上没有"交互"这回事**。
MIN_EXECUTORS_FOR_INTERACTION = 2

#: 做交互项检验至少要几个模型 —— **一个模型上没有"交互"这回事**。
MIN_MODELS_FOR_INTERACTION = 2

#: 交互项检验的默认统计设置 —— **用模块级单例当默认值**，避免每次调用重建。
DEFAULT_INTERACTION_OPTIONS: StatisticsOptions = StatisticsOptions()


@dataclass(frozen=True, slots=True)
class CellComparison:
    """两格之间的比较。

    **比较的是"该跑几次里做对了几次"** —— 跑不起来与做不对，在选型上是同一件事。
    """

    #: 前一格。
    first: tuple[str, str]
    #: 后一格。
    second: tuple[str, str]
    #: 检验结论。
    test: TestResult
    #: 多重比较校正后的 p 值。
    adjusted_p_value: Decimal

    @property
    def significant(self) -> bool:
        """校正之后还显著吗。"""
        return self.adjusted_p_value <= self.test.alpha

    @property
    def distinguishable(self) -> bool:
        """两格**分得开**吗 —— 分不开就归同一档。"""
        return self.significant


@dataclass(frozen=True, slots=True)
class InteractionResult:
    """交互项 —— **模型的效果是不是因执行器而异**。

    **零假设**：模型标签在每个执行器内部是可交换的（也就是"没有交互"）。
    **统计量**：各执行器上"模型 A 减模型 B"这个差额的**离散程度** —— 差得越
    不齐，交互越强。
    """

    #: 参与比较的两个模型。
    models: tuple[str, str]
    #: 检验结论。
    test: TestResult

    @property
    def significant(self) -> bool:
        """交互项显著吗。"""
        return self.test.p_value <= self.test.alpha

    @property
    def verdict(self) -> str:
        """一句话结论 —— **给人看的**。"""
        if self.significant:
            return "有交互：同一个模型在不同执行器上不是一个效果，不能分开选"
        return "没有可检出的交互：执行器与模型可以分开选（按格子选也一样）"


@dataclass(frozen=True, slots=True)
class CapabilityBoard:
    """能力矩阵看板 —— 格子、比较、交互与选型建议。"""

    #: 矩阵。
    matrix: CapabilityMatrix
    #: 两两比较。
    comparisons: tuple[CellComparison, ...]
    #: 交互项结论 —— 没有可比的两个模型时为 ``None``。
    interaction: InteractionResult | None
    #: 模型不明的运行条数 —— **没记模型的运行切不进任何格子**。
    unclassified: int = 0
    #: 显著性水平。
    alpha: Decimal = ALPHA

    def cell(self, executor: str, model: str) -> CapabilityCell | None:
        """按坐标取格子。"""
        return self.matrix.cell(executor, model)

    def render(self) -> str:
        """渲染成 Markdown 看板。"""
        lines = self._matrix_table()
        lines.extend(self._marginal_tables())
        lines.extend(self._comparison_table())
        lines.extend(self._interaction_section())
        lines.extend(self._selection_section())
        return "\n".join(lines)

    def _matrix_table(self) -> list[str]:
        """二维的那张表 —— **行是执行器，列是模型**。"""
        models = self.matrix.models
        lines = [
            "| 执行器 \\ 模型 | " + " | ".join(models) + " |",
            "| --- | " + " | ".join("---" for _ in models) + " |",
        ]
        for executor in self.matrix.executors:
            cells = []
            for model in models:
                cell = self.matrix.cell(executor, model)
                cells.append(_format_cell(cell))
            lines.append(f"| {executor} | " + " | ".join(cells) + " |")
        lines.append("")
        lines.append("每格：**做对率（该跑几次里做对几次）** / 成本中位数 / 耗时中位数。")
        lines.append("无样本 = 这一格**没有实测值**，不是成功率 0。")
        if self.unclassified:
            lines.append(
                f"另有 **{self.unclassified} 条**运行没记模型 —— 它们切不进任何一格，不计入矩阵。"
            )
        return lines

    def _marginal_tables(self) -> list[str]:
        """边际视图 —— **看，但不能拿它做决定**。"""
        lines = ["", "## 边际（把模型合起来 / 把执行器合起来）", ""]
        lines.append("| 执行器 | 做对率 | 成本中位数 | 耗时中位数 |")
        lines.append("| --- | --- | --- | --- |")
        for executor in self.matrix.executors:
            row = self.matrix.row(executor)
            lines.append(
                f"| {executor} | {_format_rate(_pooled(row, 'succeeded'), _pooled(row, 'runs'))}"
                f" | {_format_median(_pooled_median(row, 'cost_median'))}"
                f" | {_format_median(_pooled_median(row, 'duration_median'))} |"
            )
        lines.append("")
        lines.append("| 模型 | 做对率 | 成本中位数 | 耗时中位数 |")
        lines.append("| --- | --- | --- | --- |")
        for model in self.matrix.models:
            column = self.matrix.column(model)
            lines.append(
                f"| {model} | {_format_rate(_pooled(column, 'succeeded'), _pooled(column, 'runs'))}"
                f" | {_format_median(_pooled_median(column, 'cost_median'))}"
                f" | {_format_median(_pooled_median(column, 'duration_median'))} |"
            )
        return lines

    def _comparison_table(self) -> list[str]:
        """两两比较 —— **哪些格子分得开**。"""
        if not self.comparisons:
            return ["", "（样本不足，没有可做的两两比较）"]
        lines = [
            "",
            "## 格子之间的比较（Fisher 精确检验 + Holm 校正）",
            "",
            "| 指标 | A | B | 统计量 | p | p（Holm 校正） | 分得开 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for comparison in self.comparisons:
            lines.append(
                f"| {METRIC_SUCCESS_RATE} | {comparison.first[0]}/{comparison.first[1]}"
                f" | {comparison.second[0]}/{comparison.second[1]}"
                f" | {comparison.test.statistic:.4f}"
                f" | {comparison.test.p_value:.4f}"
                f" | {comparison.adjusted_p_value:.4f}"
                f" | {'是' if comparison.significant else '否'} |"
            )
        return lines

    def _interaction_section(self) -> list[str]:
        """交互项 —— **能不能把执行器与模型分开选**。"""
        if self.interaction is None:
            return ["", "## 交互项", "", "（可比的两个模型不足，没法做交互检验）"]
        result = self.interaction
        return [
            "",
            "## 交互项（模型的效果是不是因执行器而异）",
            "",
            f"- 统计量：{result.test.statistic:.6f}（各执行器上模型差额的离散程度）",
            f"- p 值：{result.test.p_value:.4f}（方法 {result.test.method}）",
            f"- 结论：**{result.verdict}**",
        ]

    def _selection_section(self) -> list[str]:
        """选型建议 —— **矩阵要能指导选型，否则只是一张表**。"""
        outcome = recommend(self.matrix, SelectionRequest())
        lines = ["", "## 选型建议（按矩阵自动选）", ""]
        if not outcome.found:
            lines.append(f"**选不出来** —— {outcome.reason}。")
            for rejected in outcome.rejected:
                lines.append(f"- {rejected}")
            return lines
        chosen = outcome.chosen
        assert chosen is not None
        lines.append(f"- 选中：**{chosen.executor}/{chosen.model}** —— {outcome.reason}")
        lines.append("")
        lines.append("| 名次 | 执行器/模型 | 做对率 | 成本中位数 | 耗时中位数 | 档 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for rank, cell in enumerate(outcome.ranked, start=1):
            lines.append(
                f"| {rank} | {cell.executor}/{cell.model}"
                f" | {_format_rate(cell.succeeded, cell.runs)}"
                f" | {_format_median(cell.cost_median)}"
                f" | {_format_median(cell.duration_median)}"
                f" | {cell.capability_group or '—'} |"
            )
        if outcome.rejected:
            lines.append("")
            lines.append("被排除的格子：")
            for rejected in outcome.rejected:
                lines.append(f"- {rejected}")
        return lines


def _pooled(cells: Sequence[CapabilityCell], field: str) -> int:
    """把一列格子上的某个计数加起来。"""
    return sum(int(getattr(cell, field)) for cell in cells)


def _pooled_median(cells: Sequence[CapabilityCell], field: str) -> Decimal | None:
    """把一列格子上的中位数合起来 —— **取各格中位数的加权平均**。

    **为什么是加权平均而不是"把原始样本重新算一遍"。** 各格的中位数是从各自
    的样本算出来的，把中位数再取中位数没有任何统计意义。这里退一步：用样本量
    加权平均，**它只用来给边际视图一个大概的数** —— 选型看的是格子，不是边际。
    """
    values: list[tuple[Decimal, int]] = []
    for cell in cells:
        raw = getattr(cell, field)
        if not isinstance(raw, Decimal) or cell.runs <= 0:
            continue
        values.append((raw, cell.runs))
    if not values:
        return None
    total = sum(weight for _, weight in values)
    if total == 0:
        return None
    return sum(value * weight for value, weight in values) / Decimal(total)


def _format_cell(cell: CapabilityCell | None) -> str:
    """排版一格。"""
    if cell is None or not cell.measured:
        return "无样本"
    parts = [
        f"{_format_rate(cell.succeeded, cell.runs)}",
        _format_median(cell.cost_median),
        _format_median(cell.duration_median),
    ]
    return " / ".join(parts)


def _format_rate(numerator: int, denominator: int) -> str:
    """排版一个比率 —— 分母为 0 时写"无样本"。"""
    if denominator == 0:
        return "无样本"
    return f"{numerator}/{denominator} ({Decimal(numerator) / Decimal(denominator) * 100:.1f}%)"


def _format_median(value: Decimal | None) -> str:
    """排版一个中位数。"""
    if value is None:
        return "无样本"
    return f"{value:.4f}"


def build_cell(
    executor: str,
    model: str,
    runs: Sequence[RunRecord],
    *,
    prices: PriceTable | None = None,
    capability_group: str = "",
) -> CapabilityCell:
    """把一格的运行记录算成 :class:`~core.policy.capability.CapabilityCell`。

    **口径与一维评测完全一致**（:func:`evaluation.executors.benchmark.
    evaluate_executor`）—— 成本按统一价目表折算，耗时取我们自己量的墙钟时间。
    """
    executed = sum(1 for run in runs if run.executed)
    succeeded = sum(1 for run in runs if run.outcome is RunOutcome.SUCCEEDED)
    measured = sum(1 for run in runs if run.suite_ran)
    regressed = sum(1 for run in runs if run.suite_ran and (run.regressions or 0) > 0)
    report = evaluate_executor(runs, prices=prices)
    tokens_value = report.tokens.value if report.tokens.is_computed else None
    return CapabilityCell(
        executor=executor,
        model=model,
        runs=len(runs),
        executed=executed,
        succeeded=succeeded,
        measured_runs=measured,
        regressed=regressed,
        cost_median=report.cost.value if report.cost.is_computed else None,
        duration_median=report.duration.value if report.duration.is_computed else None,
        tokens_median=int(tokens_value) if tokens_value is not None else None,
        currency=report.currency,
        capability_group=capability_group,
    )


def build_matrix(
    runs: Sequence[RunRecord],
    *,
    prices: Mapping[str, PriceTable] | None = None,
    capability_groups: Mapping[tuple[str, str], str] | None = None,
) -> CapabilityMatrix:
    """把运行记录切片成矩阵。

    **没记模型的运行切不进任何一格** —— 它们既不计入某个模型的成绩，也不该被
    悄悄算进"全局"。宁可少一格，也不要有一格是凭空的。
    """
    grouped: dict[tuple[str, str], list[RunRecord]] = {}
    for run in runs:
        if not run.model:
            continue
        grouped.setdefault((run.executor, run.model), []).append(run)
    cells = [
        build_cell(
            executor,
            model,
            records,
            prices=(prices or {}).get(model),
            capability_group=(capability_groups or {}).get((executor, model), ""),
        )
        for (executor, model), records in sorted(grouped.items())
    ]
    return CapabilityMatrix.of(cells)


def unclassified_runs(runs: Iterable[RunRecord]) -> tuple[RunRecord, ...]:
    """没记模型的运行 —— **切不进任何格子**。"""
    return tuple(run for run in runs if not run.model)


def compare_cells(
    matrix: CapabilityMatrix,
    *,
    alpha: Decimal = ALPHA,
) -> tuple[CellComparison, ...]:
    """两两比较格子 —— **哪些分得开，哪些分不开**。

    **只在有实测值的格子之间比。** 没跑过的格子没有分母，比不了。
    """
    measured = [cell for cell in matrix.cells if cell.runs > 0]
    raw: list[tuple[tuple[str, str], tuple[str, str], TestResult]] = []
    for index, first in enumerate(measured):
        for second in measured[index + 1 :]:
            test = compare_success_rates(
                first.succeeded,
                first.runs,
                second.succeeded,
                second.runs,
                StatisticsOptions(alpha=alpha, label=METRIC_SUCCESS_RATE),
            )
            raw.append((first.key, second.key, test))
    adjusted = holm_adjust([test.p_value for _, _, test in raw])
    return tuple(
        CellComparison(
            first=first,
            second=second,
            test=test,
            adjusted_p_value=adjusted[index],
        )
        for index, (first, second, test) in enumerate(raw)
    )


def capability_groups(
    matrix: CapabilityMatrix,
    comparisons: Sequence[CellComparison],
    *,
    prefix: str = CAPABILITY_GROUP_PREFIX,
) -> Mapping[tuple[str, str], str]:
    """把**统计上分不开**的格子归成同一档。

    **分档用的是贪心的"完全连接"聚类** —— 按做对率从高到低排，每一格跟它已
    经落进的档比：跟档里**每一格**都分不开，才并进那一档。

    **为什么不按"连通的就算一档"。** 连通聚类会串：A 与 B 分不开、B 与 C 分不
    开，就把 A 与 C 也归成一档 —— 而 A 与 C 可能分得开，那一档就成了一句错话。
    """
    measured = sorted(
        (cell for cell in matrix.cells if cell.runs > 0),
        key=lambda cell: (-(cell.overall_rate or Decimal(0)), cell.cost_median or Decimal(0)),
    )
    groups: dict[tuple[str, str], str] = {}
    representatives: list[tuple[str, CapabilityCell]] = []
    for cell in measured:
        placed = False
        for name, representative in representatives:
            if _indistinguishable(comparisons, representative, cell):
                groups[cell.key] = name
                placed = True
                break
        if not placed:
            name = f"{prefix}{len(representatives) + 1}"
            groups[cell.key] = name
            representatives.append((name, cell))
    return groups


def _indistinguishable(
    comparisons: Sequence[CellComparison],
    first: CapabilityCell,
    second: CapabilityCell,
) -> bool:
    """两格分得开吗 —— **没比过的算分不开**。

    **没比过就当分不开，是为了不冤枉格子** —— 但也意味着"分不开"这个词在报表
    上要连着"没比过"一起说。分档只是排序用的分组，不是结论。
    """
    for comparison in comparisons:
        pair = {comparison.first, comparison.second}
        if pair == {first.key, second.key}:
            return not comparison.significant
    return True


def interaction_test(
    runs: Sequence[RunRecord],
    *,
    model_first: str,
    model_second: str,
    options: StatisticsOptions = DEFAULT_INTERACTION_OPTIONS,
) -> InteractionResult | None:
    """交互项检验 —— **模型的效果是不是因执行器而异**。

    **零假设**：在每个执行器内部，模型标签是可交换的（"没有交互"）。
    **统计量**：各执行器上"模型 A 的做对率减模型 B 的做对率"这个差额的**方差**
    —— 差得越不齐，交互越强。

    **为什么用置换检验。** 样本很小（每格几十次运行），而且"做对率"在边界附近
    并不服从正态分布。置换检验只问一个换问题：**如果其实没有交互，观测到的这个
    参差程度有多罕见？** 它对分布形状不作任何假设。

    **样本够小就精确枚举，够大就蒙特卡洛** —— 方法名会写明是哪一种。
    """
    by_executor: dict[str, list[tuple[bool, str]]] = {}
    for run in runs:
        if run.model not in (model_first, model_second):
            continue
        by_executor.setdefault(run.executor, []).append(
            (run.outcome is RunOutcome.SUCCEEDED, run.model)
        )
    executors = sorted(by_executor)
    if len(executors) < MIN_EXECUTORS_FOR_INTERACTION:
        return None
    strata = [by_executor[executor] for executor in executors]
    observed = _interaction_statistic(strata, model_first)
    assignment_count = 1
    for stratum in strata:
        count_first = sum(1 for _, model in stratum if model == model_first)
        assignment_count *= math.comb(len(stratum), count_first)
    if assignment_count <= options.max_exact:
        extreme = 0
        examined = 0
        for assignment in _exact_assignments(strata, model_first, model_second):
            examined += 1
            if _interaction_statistic(assignment, model_first) >= observed:
                extreme += 1
        p_value = Fraction(extreme, examined) if examined else Fraction(1)
        method = "permutation_exact"
    else:
        generator = random.Random(options.seed)
        extreme = 0
        examined = options.draws
        for _ in range(options.draws):
            drawn = [_shuffle(stratum, generator) for stratum in strata]
            if _interaction_statistic(drawn, model_first) >= observed:
                extreme += 1
        # +1 修正：蒙特卡洛不该报出 p=0 —— 那是"没抽到"，不是"不可能"。
        p_value = Fraction(extreme + 1, examined + 1)
        method = "permutation_monte_carlo"
    return InteractionResult(
        models=(model_first, model_second),
        test=TestResult(
            name=METRIC_INTERACTION,
            statistic=_to_decimal(observed),
            p_value=_to_decimal(p_value),
            method=method,
            alternative=Alternative.TWO_SIDED,
            alpha=options.alpha,
            sample_size=sum(len(stratum) for stratum in strata),
            detail={
                "executors": executors,
                "permutations": examined,
                "exact": method == "permutation_exact",
                "combinations": assignment_count,
            },
        ),
    )


def _shuffle(
    stratum: Sequence[tuple[bool, str]], generator: random.Random
) -> list[tuple[bool, str]]:
    """把一个执行器内部的模型标签重新洗一遍 —— **成功与否不动**。

    **只洗标签，不洗结果。** 这是随机化的全部内容：在"没有交互"的零假设下，
    哪个运行算模型 A、哪个算模型 B 本来就该是随意的。
    """
    labels = [model for _, model in stratum]
    generator.shuffle(labels)
    return [(outcome, label) for (outcome, _), label in zip(stratum, labels, strict=True)]


def _exact_assignments(
    strata: Sequence[Sequence[tuple[bool, str]]],
    model_first: str,
    model_second: str,
) -> list[list[list[tuple[bool, str]]]]:
    """枚举所有可能的标签分配 —— **只在组合数不大时用**。

    **返回结构是"分配 → 层 → 运行"。** 每次分配给出**每一个执行器层**上的标
    签安排 —— 少给一层就不是一次完整分配，统计量会算错。
    """
    per_stratum: list[list[list[tuple[bool, str]]]] = []
    for stratum in strata:
        count_first = sum(1 for _, model in stratum if model == model_first)
        options: list[list[tuple[bool, str]]] = []
        for chosen in combinations(range(len(stratum)), count_first):
            picked = set(chosen)
            options.append(
                [
                    (outcome, model_first if index in picked else model_second)
                    for index, (outcome, _) in enumerate(stratum)
                ]
            )
        per_stratum.append(options)
    assignments: list[list[list[tuple[bool, str]]]] = [[]]
    for options in per_stratum:
        assignments = [[*prefix, option] for prefix in assignments for option in options]
    return assignments


def _interaction_statistic(
    strata: Sequence[Sequence[tuple[bool, str]]],
    model_first: str,
) -> Fraction:
    """交互项的统计量 —— **各执行器上模型差额的方差**。"""
    effects: list[Fraction] = []
    for stratum in strata:
        first = [outcome for outcome, model in stratum if model == model_first]
        second = [outcome for outcome, model in stratum if model != model_first]
        if not first or not second:
            continue
        rate_first = Fraction(sum(first), len(first))
        rate_second = Fraction(sum(second), len(second))
        effects.append(rate_first - rate_second)
    if len(effects) < MIN_EXECUTORS_FOR_INTERACTION:
        return Fraction(0)
    mean = sum(effects, Fraction(0)) / len(effects)
    return sum(((effect - mean) ** 2 for effect in effects), Fraction(0)) / len(effects)


def _to_decimal(value: Fraction) -> Decimal:
    """精确有理数落成 :class:`~decimal.Decimal`。"""
    return Decimal(value.numerator) / Decimal(value.denominator)


def build_board(
    runs: Sequence[RunRecord],
    *,
    prices: Mapping[str, PriceTable] | None = None,
    alpha: Decimal = ALPHA,
    seed: int = 20261110,
) -> CapabilityBoard:
    """出看板 —— **格子、比较、交互、选型建议**。

    **顺序是死的。** 先切格子 → 再比格子 → 再分档 → 再算交互 → 最后按档选型。
    **分档必须在比较之后** —— 档是"分不开"这件事的产物，不是先验。
    """
    preliminary = build_matrix(runs, prices=prices)
    comparisons = compare_cells(preliminary, alpha=alpha)
    groups = capability_groups(preliminary, comparisons)
    matrix = build_matrix(runs, prices=prices, capability_groups=groups)
    models = matrix.models
    interaction = (
        interaction_test(
            runs,
            model_first=models[0],
            model_second=models[1],
            options=StatisticsOptions(alpha=alpha, seed=seed),
        )
        if len(models) >= MIN_MODELS_FOR_INTERACTION
        else None
    )
    return CapabilityBoard(
        matrix=matrix,
        comparisons=comparisons,
        interaction=interaction,
        unclassified=len(unclassified_runs(runs)),
        alpha=alpha,
    )


def dump_matrix(matrix: CapabilityMatrix, path: Path) -> None:
    """把矩阵写成 JSON —— **给运行时选型用的那份数据**。

    **能力档要一起写进去。** 运行时不该重算统计量（那要原始样本），但它必须
    知道"哪几格分不开" —— 否则它会把噪声当差别。

    **一格长什么样由 :mod:`core.policy.capability` 说了算。** 这里只管写盘 ——
    两边各写一份转换，迟早会对不上，而对不上的那几列会静悄悄地变成 0。
    """
    payload = {
        "cells": [cell_to_dict(cell) for cell in matrix.cells],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_matrix(path: Path) -> CapabilityMatrix:
    """读回一份矩阵 —— **运行时拿它做选型**。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    cells = [cell_from_dict(item) for item in _as_items(payload.get("cells", []))]
    return CapabilityMatrix.of(cells)


def _as_items(value: object) -> list[Mapping[str, object]]:
    """JSON 里的格子列表 —— **不是列表就没有格子**。

    **读坏了就说读坏了。** 少一列就少一列，但把整个文件读成"空矩阵"会让选型
    悄悄退化成"随便挑一个" —— 那比报错坏得多。
    """
    if not isinstance(value, list):
        raise ValueError("矩阵文件里的 cells 必须是列表")
    items: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("矩阵文件里的每一格必须是对象")
        items.append(item)
    return items


__all__ = [
    "CAPABILITY_GROUP_PREFIX",
    "METRIC_INTERACTION",
    "CapabilityBoard",
    "CellComparison",
    "InteractionResult",
    "build_board",
    "build_cell",
    "build_matrix",
    "capability_groups",
    "compare_cells",
    "dump_matrix",
    "interaction_test",
    "load_matrix",
    "unclassified_runs",
]
