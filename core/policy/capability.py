"""能力矩阵 —— 执行器 × 模型的二维实测结论，以及按它的选型。

设计依据：docs/tasks.md TASK-P4-030（交付物"执行器 × 模型的二维能力矩阵"；验收
条件"**能力矩阵由实测数据得出，且能指导自动选型**"；验证 T2）、R-111（阶段二验收：
结果可比较可评估）、I-17（多执行器结果可比性口径需前置设计）、``TASK-P2-018``
（一维的执行器横向评测 —— 本模块是它的二维版本）、``TASK-P4-020``（多模型路由 ——
路由按"这个角色要多强的模型"挑，能力矩阵按"实测下来这个组合有多强"给证据）。

**为什么是二维的。** 一维评测会让人以为"执行器好"与"模型好"是两件可以分开谈
的事，于是"最好的执行器 + 最好的模型"成了默认做法。但那**从来没有人测过** ——
某个执行器配上某个模型可能根本没跑过；而另一些组合之间可能有明显的交互（同一
个模型在 A 执行器上要 10 轮才做对，在 B 上 3 轮就够）。

**所以选型看格子，不看边际。** 边际（一维平均）会把这两种情况都抹平：

- **交互存在时**，边际最好 ≠ 格子最好 —— 按边际选，选出的是一个没测过的组合；
- **交互不存在时**，边际与格子一致 —— 那就明说"可以分开选"，不必假装矩阵给了
  新的信息。

**"没有实测值"不是 0。** 一格没跑过就是**没有证据**，不是"成功率 0"。这里用
``None`` 表示"没测"，它与 0 在报表上必须长得不一样 —— 所以**没有证据的格子一律
不进候选**，而不是"排在最后"。

**统计上的分不开，要算成"分不开"。** 两个格子成功率差一次运行，是噪声，不是能
据此排序的差别。所以离线评测会把**统计上分不开的格子归成同一"能力档"**
（:attr:`CapabilityCell.capability_group`），档内按**成本**排 —— 既然做对的程
度分不出高下，那就选便宜的。这是"用实测数据指导选型"里最容易做错的一步：
拿噪声当差别，选出来的组合在下一轮就不成立了。

**依赖方向。** 这一层是纯数据与纯函数：不碰数据库、不碰 IO、不认识执行器适配
器。**它不认识统计检验** —— 谁与谁分得开，是评测层（``evaluation/agents/
capability_matrix.py``）算出来的结论，作为**数据**带进来。这样运行时不必重算
统计量，而"哪两格分不开"这件事仍然有据可查。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

#: 排序里"无穷大"用的值 —— 成本或耗时没测出来时排在最后，而不是当成 0。
#:
#: **没测出来 ≠ 0。** 一次运行的费用未知，用 0 代替会让这一格看起来"最便宜"。
INFINITY: Decimal = Decimal("Infinity")

#: 选型默认要求的最小样本量 —— **一格至少要跑过这么多次才算有证据**。
#:
#: 一次运行得出的成功率是 0% 或 100%，拿它去选型等于没选。默认 1 是"至少跑过
#: 一次"；真要比较两格，用评测层给出的能力档，别自己拿一次运行当结论。
DEFAULT_MIN_RUNS: int = 1


@dataclass(frozen=True, slots=True)
class CapabilityCell:
    """能力矩阵的一格 —— 一个（执行器 × 模型）组合的实测成绩。

    **这一格只装实测出来的东西。** 没有的量一律是 ``None``：没跑过就没有可用
    率，没测出成本就没有成本中位数。**"没测"与"是 0"在这里长得不一样** ——
    而选型时这两件事的处置完全相反。
    """

    #: 执行器名字。
    executor: str
    #: 模型名字。
    model: str
    #: 该跑几次（跑了多少个任务 × 多少次尝试）。
    runs: int = 0
    #: 其中真跑起来几次。
    executed: int = 0
    #: 其中通过验收几次。
    succeeded: int = 0
    #: 其中验过回归套件几次。
    measured_runs: int = 0
    #: 其中引入回归几次。
    regressed: int = 0
    #: 成本中位数 —— **统一价目表折算出来的**，不是各 CLI 自报的。
    cost_median: Decimal | None = None
    #: 耗时中位数（秒）。
    duration_median: Decimal | None = None
    #: token 用量中位数。
    tokens_median: int | None = None
    #: 成本的币种。
    currency: str | None = None
    #: 能力档 —— **统计上分不开的格子共用一个档**。
    #:
    #: 空串表示"没分档"：还没做过区分性检验（或者做过但没结论）。**空档不表示
    #: "和别人一样"，也不表示"和别人不一样"** —— 它表示"没比过"。
    capability_group: str = ""

    def __post_init__(self) -> None:
        if not self.executor.strip():
            raise ValueError("executor 不能为空")
        if not self.model.strip():
            raise ValueError("model 不能为空")
        if min(self.runs, self.executed, self.succeeded, self.measured_runs, self.regressed) < 0:
            raise ValueError("计数不能为负")
        if self.executed > self.runs:
            raise ValueError(f"跑成的次数不能超过总次数：{self.executed} > {self.runs}")
        if self.succeeded > self.executed:
            raise ValueError(f"成功的次数不能超过跑成的次数：{self.succeeded} > {self.executed}")
        if self.regressed > self.measured_runs:
            raise ValueError(
                f"引入回归的次数不能超过验过套件的次数：{self.regressed} > {self.measured_runs}"
            )

    @property
    def measured(self) -> bool:
        """这一格有没有实测值。"""
        return self.runs > 0

    @property
    def availability(self) -> Decimal | None:
        """可用率 —— 跑成了几次 / 该跑几次。

        ``None`` 表示"一次都没跑"（不是 0 —— 是没测）。
        """
        if self.runs == 0:
            return None
        return Decimal(self.executed) / Decimal(self.runs)

    @property
    def success_rate(self) -> Decimal | None:
        """成功率 —— 做对了几次 / 跑成了几次。

        **"没跑成"不进分母。** 把"执行器没配好"算成"它能力差"，是把环境问题说
        成品格问题。
        """
        if self.executed == 0:
            return None
        return Decimal(self.succeeded) / Decimal(self.executed)

    @property
    def overall_rate(self) -> Decimal | None:
        """**该跑几次里做对了几次** —— 选型真正看的数。

        **为什么把可用率与成功率乘起来。** 归因上，跑不起来（环境）与做不对
        （能力）是两件事，所以报表上要分开报；但**选型上是同一件事** —— 这个组
        合用不了就是用不了。一个"三次里跑起来两次、做对两次"的格子，比一个"跑
        起来十次、做对六次"的格子更值得选。

        ``None`` 表示"没跑过"。
        """
        if self.runs == 0:
            return None
        return Decimal(self.succeeded) / Decimal(self.runs)

    @property
    def regression_rate(self) -> Decimal | None:
        """回归率 —— 引入回归的运行占"验过套件的那些运行"的比例。"""
        if self.measured_runs == 0:
            return None
        return Decimal(self.regressed) / Decimal(self.measured_runs)

    @property
    def key(self) -> tuple[str, str]:
        """这一格的坐标 —— (执行器, 模型)。"""
        return (self.executor, self.model)


def cell_to_dict(cell: CapabilityCell) -> dict[str, object]:
    """把一格转成可 JSON 化的字典 —— **给存档用**。

    **为什么转换住在这里。** 这一格是"评测层产出、运行时消费"的东西：谁写盘、
    谁读盘是两边的事，但**"一格长什么样"只有一处定义** —— 两边各写一份转换，
    迟早会对不上，而对不上的那几列会静悄悄地变成 0。
    """
    return {
        "executor": cell.executor,
        "model": cell.model,
        "runs": cell.runs,
        "executed": cell.executed,
        "succeeded": cell.succeeded,
        "measured_runs": cell.measured_runs,
        "regressed": cell.regressed,
        "cost_median": str(cell.cost_median) if cell.cost_median is not None else None,
        "duration_median": (
            str(cell.duration_median) if cell.duration_median is not None else None
        ),
        "tokens_median": cell.tokens_median,
        "currency": cell.currency,
        "capability_group": cell.capability_group,
    }


def cell_from_dict(item: Mapping[str, object]) -> CapabilityCell:
    """把字典还原成一格 —— **字段不对就当没那个数**。

    **"没测出来"与"是 0"在这里必须长得不一样。** 少一列就少一列，不拿 0 顶上 ——
    拿 0 顶上会让这一格在选型里变成"最便宜"，那是在用记账口径的差别冒充能力差
    别。
    """
    return CapabilityCell(
        executor=str(item["executor"]),
        model=str(item["model"]),
        runs=_count(item, "runs"),
        executed=_count(item, "executed"),
        succeeded=_count(item, "succeeded"),
        measured_runs=_count(item, "measured_runs"),
        regressed=_count(item, "regressed"),
        cost_median=_optional_decimal(item.get("cost_median")),
        duration_median=_optional_decimal(item.get("duration_median")),
        tokens_median=_optional_int(item.get("tokens_median")),
        currency=str(item["currency"]) if item.get("currency") else None,
        capability_group=str(item.get("capability_group", "")),
    )


def _count(item: Mapping[str, object], key: str) -> int:
    """字典里的一个计数 —— **读不出来就当 0**（计数没有"不知道"这个状态）。

    **为什么计数可以当 0，而度量不行。** "跑了 27 次"和"跑了 0 次"是可以分的，
    字段缺失本身就是"没这条记录"；而"成本中位数"缺失是"没测出来"，两者的处置
    完全相反 —— 所以计数走 0，度量走 ``None``。
    """
    value = item.get(key, 0)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _optional_decimal(value: object) -> Decimal | None:
    """字典里的一个十进制数 —— **没有就是没有**。"""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _optional_int(value: object) -> int | None:
    """字典里的一个整数 —— **没有就是没有**。"""
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class RejectedCell:
    """一格被排除的原因。

    **排除必须带理由。** 只说"没选它"，事后没人知道是它不行、是它没测过，还
    是它压根不在候选里 —— 而这三件事的处置完全不同。
    """

    #: 被排除的那一格。
    cell: CapabilityCell
    #: 排除理由。
    reason: str

    def __str__(self) -> str:
        return f"{self.cell.executor}/{self.cell.model}：{self.reason}"


@dataclass(frozen=True, slots=True)
class SelectionRequest:
    """一次选型请求。"""

    #: 候选执行器 —— 空表示"矩阵里都算"。
    executors: tuple[str, ...] = ()
    #: 候选模型 —— 空表示"矩阵里都算"。
    models: tuple[str, ...] = ()
    #: 单次成本上限 —— **硬闸**（预算是不许超过，不是尽量少花）。
    max_cost: Decimal | None = None
    #: 一格至少要跑过这么多次才算有证据。
    min_runs: int = DEFAULT_MIN_RUNS

    def __post_init__(self) -> None:
        if self.min_runs < 1:
            raise ValueError(f"min_runs 至少为 1，得到 {self.min_runs}")


@dataclass(frozen=True, slots=True)
class SelectionOutcome:
    """一次选型的结果。"""

    #: 选中的那一格 —— ``None`` 表示"没有能选的"。
    chosen: CapabilityCell | None
    #: 候选的排序 —— 从前到后。
    ranked: tuple[CapabilityCell, ...]
    #: 被排除的格子与理由。
    rejected: tuple[RejectedCell, ...]
    #: 选中的理由 —— 人话。
    reason: str = ""

    @property
    def found(self) -> bool:
        """有没有选出来。"""
        return self.chosen is not None


@dataclass(frozen=True, slots=True)
class CapabilityMatrix:
    """执行器 × 模型的二维能力矩阵。

    **矩阵是格子的集合，不是两列数。** 边际（"这个执行器总体多好"）从矩阵里能
    算出来，但它**不能替格子做决定** —— 交互存在时，边际最好不等于格子最好。
    """

    #: 矩阵里的格子 —— 按 (执行器, 模型) 排好序。
    cells: tuple[CapabilityCell, ...] = ()

    @classmethod
    def of(cls, cells: Iterable[CapabilityCell]) -> CapabilityMatrix:
        """建一个矩阵 —— **同一坐标只留一格**。

        **重复的坐标是数据错了，不是"多测了几次"。** 两格同名同坐标却带着不同
        的数，说明有两次测量被写进了同一格 —— 合并它们会把两次测量的差别抹掉。
        """
        seen: dict[tuple[str, str], CapabilityCell] = {}
        for cell in cells:
            existing = seen.get(cell.key)
            if existing is not None:
                raise ValueError(f"矩阵里出现了重复的坐标：{cell.key}")
            seen[cell.key] = cell
        return cls(cells=tuple(sorted(seen.values(), key=lambda item: item.key)))

    @property
    def executors(self) -> tuple[str, ...]:
        """矩阵里出现过的执行器。"""
        return tuple(sorted({cell.executor for cell in self.cells}))

    @property
    def models(self) -> tuple[str, ...]:
        """矩阵里出现过的模型。"""
        return tuple(sorted({cell.model for cell in self.cells}))

    def cell(self, executor: str, model: str) -> CapabilityCell | None:
        """按坐标取格子 —— 没有就是没有。"""
        for item in self.cells:
            if item.executor == executor and item.model == model:
                return item
        return None

    def row(self, executor: str) -> tuple[CapabilityCell, ...]:
        """一整行 —— 某个执行器在各模型上的格子。"""
        return tuple(cell for cell in self.cells if cell.executor == executor)

    def column(self, model: str) -> tuple[CapabilityCell, ...]:
        """一整列 —— 某个模型在各执行器上的格子。"""
        return tuple(cell for cell in self.cells if cell.model == model)

    def groups(self) -> Mapping[str, tuple[CapabilityCell, ...]]:
        """按能力档分组。

        **没分档的格子各自成一档。** 空档的意思是"没比过"，不是"和别人一样" ——
        把它们归进同一档等于替它们下了一个没做过的结论。
        """
        grouped: dict[str, list[CapabilityCell]] = {}
        for cell in self.cells:
            grouped.setdefault(self.group_of(cell), []).append(cell)
        return {key: tuple(value) for key, value in sorted(grouped.items())}

    def group_of(self, cell: CapabilityCell) -> str:
        """某一格属于哪一档。

        **没分档的格子用的是带坐标的独一份档名** —— 这样两格没分档的也不会被
        归到一起（那等于替它们下了"分不开"的结论）。
        """
        return cell.capability_group or f"ungrouped:{cell.executor}/{cell.model}"


def _group_rank(matrix: CapabilityMatrix, group: str) -> Decimal:
    """一档的排序值 —— **档里最好的一格的总体成功率**。

    **为什么用"档里最好的那一格"当整档的档位。** 档内各格在统计上分不开，所
    以整档对外表现出的水平就是其中最强的那一格 —— 用别的格子代表整档，会把整
    档的排序压低，冤枉了档里最好的那些组合。
    """
    cells = matrix.groups().get(group, ())
    rates = [cell.overall_rate for cell in cells if cell.overall_rate is not None]
    if not rates:
        return Decimal("-1")
    return max(rates)


def _sort_key(
    matrix: CapabilityMatrix, cell: CapabilityCell
) -> tuple[Decimal, Decimal, Decimal, str, str]:
    """候选的排序键 —— **全序**，同样的输入永远给出同样的顺序。

    **为什么成本排在成功率后面而不是前面。** 成功率是"能不能做对"，成本是"值
    多少钱"。**先做对，再便宜** —— 但"做对"的差别要是**统计上分不开**的，那就
    由成本说了算（分档正是干这件事的）。把成本排在最前面会买到一个便宜但做不
    对的组合；把成功率排在最前面而不分档，则会让一次运行的差别决定选型。

    **最后两格是名字** —— 兜底，保证全序：同样的输入永远给出同样的顺序，否则
    "这次为什么用这个组合"就永远答不上来。
    """
    group = matrix.group_of(cell)
    return (
        -_group_rank(matrix, group),
        cell.cost_median if cell.cost_median is not None else INFINITY,
        cell.duration_median if cell.duration_median is not None else INFINITY,
        cell.executor,
        cell.model,
    )


def recommend(
    matrix: CapabilityMatrix, request: SelectionRequest | None = None
) -> SelectionOutcome:
    """按实测矩阵选一格。

    **没有证据的格子一律不进候选。** 没跑过就是没跑过 —— 拿它当"成功率 0"排
    在最后，等于让一个没测过的组合去跟测过的组合比，那不是选型，是抽签。

    **成本上限是硬闸。** 预算说的是"不许超过"，不是"尽量少花"。

    **成本没测出来又设了上限的格子，也被排除** —— 不知道多少钱，就没法说它没超。

    **挑不出来就明说挑不出来**（:attr:`SelectionOutcome.chosen` 为 ``None``），
    不悄悄退回到"矩阵里随便挑一个"。退回一个没测过的组合，比不选更糟。
    """
    query = request or SelectionRequest()
    candidates: list[CapabilityCell] = []
    rejected: list[RejectedCell] = []

    for cell in matrix.cells:
        reason = _rejection_reason(cell, query)
        if reason is None:
            candidates.append(cell)
        else:
            rejected.append(RejectedCell(cell=cell, reason=reason))

    if not candidates:
        return SelectionOutcome(
            chosen=None,
            ranked=(),
            rejected=tuple(rejected),
            reason="没有一格既有实测值、又符合候选与成本上限",
        )

    ordered = tuple(sorted(candidates, key=lambda cell: _sort_key(matrix, cell)))
    chosen = ordered[0]
    return SelectionOutcome(
        chosen=chosen,
        ranked=ordered,
        rejected=tuple(rejected),
        reason=_choice_reason(matrix, chosen),
    )


def _rejection_reason(cell: CapabilityCell, request: SelectionRequest) -> str | None:
    """这一格为什么被排除 —— **没有理由就返回 ``None``**（即"可以进候选"）。"""
    reasons = _rejection_reasons(cell, request)
    return reasons[0] if reasons else None


def _rejection_reasons(cell: CapabilityCell, request: SelectionRequest) -> tuple[str, ...]:
    """排除理由 —— **按固定次序**，第一条就是报出来的那条。

    **只报最先卡住的那一条。** 一次报四条会让人以为"修好一条就能用了"，而实际
    上四条都得满足。
    """
    reasons: list[str] = []
    if request.executors and cell.executor not in request.executors:
        reasons.append("不在候选执行器里")
    if request.models and cell.model not in request.models:
        reasons.append("不在候选模型里")
    if not cell.measured:
        reasons.append("没有实测值")
    if cell.runs < request.min_runs:
        reasons.append(f"样本不足（{cell.runs} 次 < 要求的 {request.min_runs} 次）")
    if cell.measured and cell.executed == 0:
        reasons.append("一次都没跑起来")
    if (
        request.max_cost is not None
        and cell.cost_median is not None
        and cell.cost_median > request.max_cost
    ):
        reasons.append(f"超过成本上限（{cell.cost_median} > {request.max_cost}）")
    if request.max_cost is not None and cell.measured and cell.cost_median is None:
        reasons.append("成本没测出来，没法确认它在上限之内")
    return tuple(reasons)


def _choice_reason(matrix: CapabilityMatrix, cell: CapabilityCell) -> str:
    """说清楚为什么是这一格 —— **理由要能被复核**。"""
    group = matrix.group_of(cell)
    tied = [other for other in matrix.groups().get(group, ()) if other.key != cell.key]
    if tied:
        peers = "、".join(f"{other.executor}/{other.model}" for other in tied)
        return (
            f"{cell.executor}/{cell.model} 与 {peers} 在做对率上分不开，"
            f"所以按成本与耗时排（它最便宜或最快）"
        )
    return f"{cell.executor}/{cell.model} 的做对率最高，且没有同档的对手"


__all__ = [
    "DEFAULT_MIN_RUNS",
    "INFINITY",
    "CapabilityCell",
    "CapabilityMatrix",
    "RejectedCell",
    "SelectionOutcome",
    "SelectionRequest",
    "cell_from_dict",
    "cell_to_dict",
    "recommend",
]
