"""自适应调度回放 —— 用真跑出来的记录，看"按历史调度"能不能打过静态分配。

设计依据：docs/tasks.md TASK-P4-040（交付物"执行器动态调度 —— 基于历史表现的
自适应调度"；验收条件"**调度器在实测中优于静态分配的对照组**"；验证 T2）、
R-038（调度要基于实测而不是预设）、``TASK-P4-030``（二维能力矩阵 —— 调度器读的
就是那张矩阵）、``TASK-P2-018``（一维评测 —— 口径不许两套）。

**回放是什么。** 把真实评测跑出来的每一条记录按时间排成"决策位"，让不同的调度
策略在每个位上挑一个格子。**挑完之后发生的事，用那一格在同一位上的真实记录来
算** —— 所以每一个数都是实测出来的，只有"挑哪个"这件事是反事实的。

**不许看未来。** 第 i 个位上做决定时，账本里只有第 i 个位**之前**的记录。少了
这一条，回放就变成"拿结果挑结果" —— 那什么结论都能证明。

**历史有两种口径，差别是结论级的。**

- ``shared`` —— 账本里装的是"这一位之前**所有格子**跑过的"。这正是生产上的
  样子：**历史来自离线评测（能力矩阵），调度器读的是那张在长的表。**
- ``chosen`` —— 账本里只有"**自己跑过**的那几格"。这是"边跑边学"的样子。

这两种口径的差别不是措辞：**只用自己的历史，就永远发现不了没试过的组合** ——
因为没试过的格子根本没有观测，调度器连它存在都不知道。回放会把这件事摆出来。

**回放不能替代真跑。** 回放回答的是"**同样这批真实运行，如果当初按这个策略调
度，结果会是什么**"，不是"这个策略在线上会怎样"。它是**便宜的筛选**，不是
**验收** —— 验收要另跑一次对照实验。

**口径不许两套。** 成功率是"跑成了几次里做对几次"，做对率是"该跑几次里做对几
次"，没跑成不进成功率的分母 —— 与 :mod:`evaluation.executors.benchmark` 和
:mod:`evaluation.agents.capability_matrix` 完全一致。

**不做"每次成功多少钱"的比值。** 那个比值在成功率高的时候反而显得差，而两个
策略的差别往往只值那几个百分点。**"同样的钱能做对几件事"**（预算内做对数）才
是能拿去做决定的那个数。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from core.policy.scheduling import AdaptiveScheduler, Observation, SchedulingPolicy
from evaluation.executors.benchmark import PriceTable, RunOutcome, RunRecord
from evaluation.executors.statistics import (
    DEFAULT_OPTIONS,
    StatisticsOptions,
    TestResult,
    mcnemar_exact,
    paired_permutation_test,
)

#: 自适应策略线在报表上的名字。
ADAPTIVE_NAME = "adaptive"

#: 历史口径 —— 账本里装的是"这一位之前所有格子跑过的"（像一张在长的矩阵）。
HISTORY_SHARED = "shared"

#: 历史口径 —— 账本里只有"自己跑过的那几格"。
HISTORY_CHOSEN = "chosen"

#: 成功率的指标名。
METRIC_SUCCESS = "success"

#: 成本的指标名。
METRIC_COST = "cost"


#: 一条策略线在一个决策位上的结果。
@dataclass(frozen=True, slots=True)
class SlotOutcome:
    """一个位上的结果 —— 挑了哪个格子、做没做对、花了多少。

    **位号是配对的依据。** 两条策略线要逐位配对，配对靠的是位号，不是顺序 ——
    少一位两位就配错了，而配错的对检验出来的 p 值是假的。
    """

    #: 位号 —— 与 :class:`ReplaySlot` 的位号一一对应。
    index: int
    #: 挑中的格子 —— ``"执行器/模型"``。
    cell: str
    #: 做对了没有。
    succeeded: bool
    #: 跑起来没有。
    executed: bool
    #: 这一次花了多少 —— **没量到就是 ``None``**，不是 0。
    cost: Decimal | None = None
    #: 这一次跑了多久。
    duration: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RecordedRun:
    """一次真实运行 —— 回放里的事实。

    **"没跑成"与"做不对"是两个字。** 没跑成的那次 :attr:`executed` 是 ``False``，
    它不进成功率的分母 —— 把环境问题说成能力问题，是这一层最不该犯的错。
    """

    #: 执行器名字。
    executor: str
    #: 模型名字。
    model: str
    #: 任务编号。
    task_id: str
    #: 第几次尝试。
    attempt: int
    #: 做对了没有。
    succeeded: bool
    #: 跑起来没有。
    executed: bool
    #: 折算出来的成本 —— **没有价目表就是 ``None``**。
    cost: Decimal | None = None
    #: 耗时（秒）。
    duration: Decimal | None = None

    @property
    def cell(self) -> str:
        """格子坐标 —— ``"执行器/模型"``。"""
        return f"{self.executor}/{self.model}"

    def observation(self) -> Observation:
        """转成调度器看得懂的观测 —— **只有这一处转换**。

        **为什么不让调度器直接吃 :class:`RunRecord`。** 调度器在 ``core`` 层，
        而 :class:`RunRecord` 是评测层的记录（带 token、原始输出那些）。让下层
        认识上层的记录，依赖方向就反了。
        """
        return Observation(
            executor=self.executor,
            model=self.model,
            task_id=self.task_id,
            executed=self.executed,
            succeeded=self.succeeded,
            cost=self.cost,
            duration=self.duration,
        )


@dataclass(frozen=True, slots=True)
class ReplaySlot:
    """一个决策位 —— 一次任务的一次尝试。"""

    #: 位号 —— 从 0 起。
    index: int
    #: 第几次尝试。
    attempt: int
    #: 任务编号。
    task_id: str
    #: 这一位上跑过的格子 —— ``"执行器/模型"`` → 记录。
    runs: Mapping[str, RecordedRun]

    @property
    def cells(self) -> tuple[str, ...]:
        """这一位上跑过的格子。"""
        return tuple(sorted(self.runs))


def build_slots(runs: Sequence[RecordedRun]) -> tuple[ReplaySlot, ...]:
    """把记录排成决策位 —— **按 (次数, 任务) 分组，次序按第一次出现。**

    **次序必须与真实评测的次序一致。** 回放的因果性靠这个次序建立：第 i 位上的
    决定只准看第 i 位之前的记录。次序一乱，"不许看未来"就是一句空话。
    """
    order: list[tuple[int, str]] = []
    grouped: dict[tuple[int, str], dict[str, RecordedRun]] = {}
    for run in runs:
        key = (run.attempt, run.task_id)
        if key not in grouped:
            grouped[key] = {}
            order.append(key)
        grouped[key][run.cell] = run
    return tuple(
        ReplaySlot(index=index, attempt=key[0], task_id=key[1], runs=dict(grouped[key]))
        for index, key in enumerate(order)
    )


@dataclass(frozen=True, slots=True)
class PolicyLine:
    """一条策略线在一次回放里的结果。

    **数都在结果里，结论从结果里算。** 不另存一份"成功了几次" —— 两份数迟早会
    对不上，而对不上的那几列会静悄悄地变成错的。
    """

    #: 策略名。
    name: str
    #: 一句话说明 —— 这条线代表什么。
    note: str = ""
    #: 各位上的结果。
    outcomes: tuple[SlotOutcome, ...] = ()
    #: 冷启动的次数 —— **没证据的选择要如实标出来**。
    cold_starts: int = 0
    #: 探索的次数 —— **探索是花掉的成本**。
    explorations: int = 0
    #: 没有决策的位数 —— **挑不出来就是挑不出来**。
    undecided: int = 0
    #: 挑了但这一位上没有实测记录的位数。
    missing: int = 0

    @property
    def slots(self) -> int:
        """参与统计的位数。"""
        return len(self.outcomes)

    @property
    def executed(self) -> int:
        """跑成了几次。"""
        return sum(1 for outcome in self.outcomes if outcome.executed)

    @property
    def successes(self) -> int:
        """做对了几次。"""
        return sum(1 for outcome in self.outcomes if outcome.succeeded)

    @property
    def success_rate(self) -> Decimal | None:
        """成功率 —— **跑成了几次里做对几次**。没跑成的不进分母。"""
        if self.executed == 0:
            return None
        return Decimal(self.successes) / Decimal(self.executed)

    @property
    def overall_rate(self) -> Decimal | None:
        """做对率 —— **该跑几次里做对几次**。这是选型真正看的那个数。"""
        if self.slots == 0:
            return None
        return Decimal(self.successes) / Decimal(self.slots)

    @property
    def total_cost(self) -> Decimal:
        """总成本 —— **只算量到成本的运行**。"""
        return sum(
            (outcome.cost for outcome in self.outcomes if outcome.cost is not None),
            Decimal(0),
        )

    @property
    def cost_samples(self) -> int:
        """量到成本的次数。"""
        return sum(1 for outcome in self.outcomes if outcome.cost is not None)

    @property
    def mean_cost(self) -> Decimal | None:
        """平均成本 —— 没量到成本时是 ``None``，不是 0。"""
        if self.cost_samples == 0:
            return None
        return self.total_cost / Decimal(self.cost_samples)

    @property
    def median_duration(self) -> Decimal | None:
        """耗时中位数。"""
        values = sorted(
            outcome.duration for outcome in self.outcomes if outcome.duration is not None
        )
        if not values:
            return None
        middle = len(values) // 2
        if len(values) % 2 == 1:
            return values[middle]
        return (values[middle - 1] + values[middle]) / Decimal(2)

    def within_budget(self, ceiling: Decimal) -> tuple[int, int]:
        """预算内做对了几件事 —— **返回 (预算内做对, 预算内总数)**。

        **"同样的钱能做对几件事"才是能拿去做决定的数。** 成功率与总成本分开
        看，中间那句"每次成功多少钱"是把两个不同量纲的东西除在一起 —— 成功率高
        的策略在那个比值上反而显得差。

        **没量到成本的那一位不进预算内。** 不知道花了多少，就不能说它没超预算。
        """
        within: list[SlotOutcome] = []
        for outcome in self.outcomes:
            if outcome.cost is not None and outcome.cost <= ceiling:
                within.append(outcome)
        return sum(1 for outcome in within if outcome.succeeded), len(within)


class BaselinePort(Protocol):
    """静态对照线 —— 只回答"这一步用哪个格子"。

    **对照线不许看历史。** 它代表的是"事先定死"的分配方式 —— 它的价值就在于
    它不看数据，所以它到底行不行只能靠实测说。
    """

    @property
    def name(self) -> str:
        """这条线的名字。"""

    def choose(self, slot: ReplaySlot) -> str | None:
        """这一步用哪个格子 —— **返回 ``None`` 表示这一步不参与**。"""


@dataclass(frozen=True, slots=True)
class StaticBaseline:
    """一条静态对照线 —— 始终用同一个格子。"""

    #: 名字。
    name: str
    #: 始终用的那个格子。
    cell: str
    #: 一句话说明。
    note: str = ""

    def choose(self, _slot: ReplaySlot) -> str | None:
        """这一步用哪个格子 —— **不看这一位上的任何东西**。"""
        return self.cell


@dataclass(frozen=True, slots=True)
class RoundRobinBaseline:
    """轮转分配 —— **静态分配里最讲道理的一种**。

    **它不是随机。** 随机分配每次的结果都不可复现，而"每个格子轮着来"是**事先
    说得清**的分配方式 —— 它是生产上真会被采用的静态方案，所以拿它当对照才公
    平。
    """

    #: 名字。
    name: str
    #: 轮转的格子顺序。
    cells: tuple[str, ...]
    #: 一句话说明。
    note: str = ""

    def choose(self, slot: ReplaySlot) -> str | None:
        """这一步用哪个格子 —— **按位号轮转**。"""
        if not self.cells:
            return None
        return self.cells[slot.index % len(self.cells)]


@dataclass(frozen=True, slots=True)
class Decision:
    """一个位上的决定 —— **挑了什么、为什么**。

    **理由要跟挑的结果一起留着。** 只记"挑了 claude-code"，事后没人答得上来
    "那是它最便宜，还是它是唯一一个跑通过的"。**理由是调度器自己写的**，所以
    它跟决策是同一份事实。
    """

    #: 位号。
    index: int
    #: 第几次尝试。
    attempt: int
    #: 任务编号。
    task_id: str
    #: 挑中的格子 —— 空串表示"没挑出来"。
    cell: str
    #: 理由 —— 人话。
    reason: str = ""
    #: 是不是冷启动。
    cold_start: bool = False
    #: 是不是探索。
    exploration: bool = False


@dataclass(frozen=True, slots=True)
class PolicyComparison:
    """自适应策略与一条静态对照线的配对比较。

    **两个检验都按"自适应在前"记账。** 成功率的统计量是"自适应赢的对数减基线
    赢的对数"，正数意味着自适应更强；成本的统计量是"基线的钱减自适应的钱"，
    正数意味着自适应更省。
    """

    #: 基线的名字。
    baseline: str
    #: 成功率的配对检验 —— McNemar 精确检验。
    success: TestResult | None
    #: 成本的配对检验 —— 符号翻转置换检验。
    cost: TestResult | None
    #: 成功率的配对数。
    success_pairs: int
    #: 成本的配对数。
    cost_pairs: int


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """一次回放的完整结论。"""

    #: 决策位的个数。
    slots: int
    #: 历史口径。
    history: str
    #: 调度的规矩 —— **看板要写明这次是按哪套规矩回的**。
    policy: SchedulingPolicy
    #: 每一条策略线。
    lines: tuple[PolicyLine, ...]
    #: 自适应策略与各条静态对照线的配对比较。
    comparisons: tuple[PolicyComparison, ...]
    #: 自适应策略每个位上的决定 —— **让人能复核**。
    decisions: tuple[Decision, ...]

    def line(self, name: str) -> PolicyLine | None:
        """按名字取一条策略线。"""
        for line in self.lines:
            if line.name == name:
                return line
        return None

    @property
    def adaptive(self) -> PolicyLine | None:
        """自适应策略那条线。"""
        return self.line(ADAPTIVE_NAME)

    def render(self, ceilings: Sequence[Decimal] = ()) -> str:
        """渲染成 Markdown 看板。"""
        lines = self._head()
        lines.extend(self._policy_table())
        lines.extend(self._budget_table(ceilings))
        lines.extend(self._comparison_table())
        lines.extend(self._decision_table())
        return "\n".join(lines)

    def _head(self) -> list[str]:
        """开头 —— **口径先说清楚**。"""
        history_text = (
            "账本里装的是**这一位之前所有格子跑过的**（像一张在长的能力矩阵）"
            if self.history == HISTORY_SHARED
            else "账本里只有**自己跑过的那几格**（边跑边学）"
        )
        return [
            "# 自适应调度回放 —— 调度 vs 静态分配",
            "",
            f"- 决策位：**{self.slots}** 个（一次任务的一次尝试算一个位）",
            f"- 历史口径：{history_text}",
            f"- 调度规矩：容差 {self.policy.tolerance}、底线 {self.policy.minimum_rate}、"
            f"冷启动偏好 {'、'.join(self.policy.preference) or '（无）'}、"
            f"每 {self.policy.explore_every} 次探一次",
            "- 结果口径：**做对了就用那一格在同一位的真实记录来算** —— 每一个数都是"
            "实测出来的，只有'挑哪个'是反事实的。",
            "- 成功率 = 跑成了几次里做对几次；**没跑成的不进分母**。",
        ]

    def _policy_table(self) -> list[str]:
        """策略线对照表。"""
        lines = [
            "",
            "## 各条策略线",
            "",
            "| 策略 | 位数 | 做对 | 跑成 | 成功率 | 做对率 | 总成本 | 平均成本 | 冷启动 | 探索 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for line in self.lines:
            lines.append(
                f"| {line.name} | {line.slots} | {line.successes} | {line.executed}"
                f" | {_format_rate(line.success_rate)} | {_format_rate(line.overall_rate)}"
                f" | {line.total_cost:.6f} | {_format_cost(line.mean_cost)}"
                f" | {line.cold_starts} | {line.explorations} |"
            )
        lines.append("")
        for line in self.lines:
            if line.note:
                lines.append(f"- **{line.name}**：{line.note}")
        return lines

    def _budget_table(self, ceilings: Sequence[Decimal]) -> list[str]:
        """预算内做对数 —— **同样的钱能做对几件事**。"""
        if not ceilings:
            return []
        lines = [
            "",
            "## 同样的钱能做对几件事",
            "",
            "每格是 **预算内做对 / 预算内**（没量到成本的那一位不进预算内）。",
            "",
            "| 策略 | " + " | ".join(f"≤ {ceiling:.4f}" for ceiling in ceilings) + " |",
            "| --- | " + " | ".join("---" for _ in ceilings) + " |",
        ]
        for line in self.lines:
            cells = []
            for ceiling in ceilings:
                succeeded, total = line.within_budget(ceiling)
                cells.append(f"{succeeded}/{total}")
            lines.append(f"| {line.name} | " + " | ".join(cells) + " |")
        return lines

    def _comparison_table(self) -> list[str]:
        """配对检验表。"""
        if not self.comparisons:
            return ["", "（没有可比的静态对照线）"]
        lines = [
            "",
            "## 配对检验（自适应 vs 静态对照）",
            "",
            "| 对照 | 指标 | 统计量 | p 值 | 方法 | 配对数 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for comparison in self.comparisons:
            for metric, result, pairs in (
                (METRIC_SUCCESS, comparison.success, comparison.success_pairs),
                (METRIC_COST, comparison.cost, comparison.cost_pairs),
            ):
                if result is None:
                    lines.append(f"| {comparison.baseline} | {metric} | 无样本 | — | — | 0 |")
                    continue
                lines.append(
                    f"| {comparison.baseline} | {metric} | {result.statistic:.6f}"
                    f" | {result.p_value:.4f} | {result.method} | {pairs} |"
                )
        lines.append("")
        lines.append(
            "成功率的统计量 = **自适应赢的对数减基线赢的对数**（正数 = 自适应更强）；"
            "成本的统计量 = **基线的钱减自适应的钱**（正数 = 自适应更省）。"
        )
        return lines

    def _decision_table(self) -> list[str]:
        """自适应每次挑了什么 —— **让人能复核**。"""
        if not self.decisions:
            return []
        lines = [
            "",
            "## 自适应策略每次挑了什么",
            "",
            "| 位 | 次数 | 任务 | 挑的格子 | 冷启动 | 探索 | 理由 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        adaptive = self.adaptive
        for decision in self.decisions:
            lines.append(
                f"| {decision.index} | {decision.attempt} | {decision.task_id}"
                f" | {decision.cell or '（没挑出来）'}"
                f" | {'是' if decision.cold_start else '否'}"
                f" | {'是' if decision.exploration else '否'} | {decision.reason} |"
            )
        if adaptive is not None:
            lines.append("")
            lines.append(
                f"冷启动 {adaptive.cold_starts} 次、探索 {adaptive.explorations} 次、"
                f"没挑出来 {adaptive.undecided} 次、没有实测记录 {adaptive.missing} 次。"
            )
        return lines


def _format_rate(value: Decimal | None) -> str:
    """排版一个比率 —— 没有样本时写"无样本"。"""
    if value is None:
        return "无样本"
    return f"{value:.4f}"


def _format_cost(value: Decimal | None) -> str:
    """排版一个成本 —— 没量到时写"没测出来"，不是 0。"""
    if value is None:
        return "没测出来"
    return f"{value:.6f}"


def _outcome(cell: str, run: RecordedRun, index: int) -> SlotOutcome:
    """把一次真实运行变成一个位上的结果。"""
    return SlotOutcome(
        index=index,
        cell=cell,
        succeeded=run.succeeded,
        executed=run.executed,
        cost=run.cost,
        duration=run.duration,
    )


def replay(
    slots: Sequence[ReplaySlot],
    *,
    policy: SchedulingPolicy,
    baselines: Sequence[BaselinePort],
    options: StatisticsOptions = DEFAULT_OPTIONS,
    history: str = HISTORY_SHARED,
) -> ReplayReport:
    """回放一遍 —— **每一位的决定都只能看这一位之前的记录**。

    :param slots: 决策位 —— 顺序就是因果顺序。
    :param policy: 调度的规矩 —— 容差、底线、冷启动偏好、探索间隔都在这儿。
    :param baselines: 静态对照线。
    :param options: 统计检验的设置。
    :param history: 历史口径 —— :data:`HISTORY_SHARED`（所有格子的历史）或
        :data:`HISTORY_CHOSEN`（只有自己跑过的）。
    """
    if history not in (HISTORY_SHARED, HISTORY_CHOSEN):
        raise ValueError(f"历史口径只能是 {HISTORY_SHARED} 或 {HISTORY_CHOSEN}，得到：{history}")

    adaptive_line, decisions = _replay_adaptive(slots, policy=policy, history=history)
    lines = [adaptive_line]
    for baseline in baselines:
        lines.append(_replay_baseline(slots, baseline))
    comparisons = _compare(adaptive_line, lines[1:], options=options)
    return ReplayReport(
        slots=len(slots),
        history=history,
        policy=policy,
        lines=tuple(lines),
        comparisons=comparisons,
        decisions=decisions,
    )


def _replay_adaptive(
    slots: Sequence[ReplaySlot],
    *,
    policy: SchedulingPolicy,
    history: str,
) -> tuple[PolicyLine, tuple[Decision, ...]]:
    """回放自适应策略 —— **决定在前，记历史在后**。

    **次序反了就作弊了。** 先把这一位的记录记进账本再做决定，等于让它偷看这一位
    的结果 —— 那它挑的格子就一定是"事后最好的那个"，回放也就没意义了。
    """
    scheduler = AdaptiveScheduler(policy)
    outcomes: list[SlotOutcome] = []
    decisions: list[Decision] = []
    cold_starts = 0
    explorations = 0
    undecided = 0
    missing = 0

    for slot in slots:
        decision = scheduler.decide(slot.task_id)
        cell = str(decision.chosen) if decision.chosen is not None else ""
        if decision.cold_start:
            cold_starts += 1
        if decision.exploration:
            explorations += 1
        decisions.append(
            Decision(
                index=slot.index,
                attempt=slot.attempt,
                task_id=slot.task_id,
                cell=cell,
                reason=decision.reason,
                cold_start=decision.cold_start,
                exploration=decision.exploration,
            )
        )
        if not cell:
            undecided += 1
        else:
            run = slot.runs.get(cell)
            if run is None:
                missing += 1
            else:
                outcomes.append(_outcome(cell, run, slot.index))
        if history == HISTORY_SHARED:
            for run in slot.runs.values():
                scheduler.record(run.observation())
        elif cell and cell in slot.runs:
            scheduler.record(slot.runs[cell].observation())

    line = PolicyLine(
        name=ADAPTIVE_NAME,
        note="按历史调度：先保达成度，底线之内按成本排",
        outcomes=tuple(outcomes),
        cold_starts=cold_starts,
        explorations=explorations,
        undecided=undecided,
        missing=missing,
    )
    return line, tuple(decisions)


def _replay_baseline(slots: Sequence[ReplaySlot], baseline: BaselinePort) -> PolicyLine:
    """回放一条静态对照线 —— **它不看历史，只按事先说好的来**。"""
    outcomes: list[SlotOutcome] = []
    missing = 0
    for slot in slots:
        cell = baseline.choose(slot)
        if not cell:
            continue
        run = slot.runs.get(cell)
        if run is None:
            missing += 1
            continue
        outcomes.append(_outcome(cell, run, slot.index))
    note = getattr(baseline, "note", "")
    return PolicyLine(
        name=baseline.name,
        note=note,
        outcomes=tuple(outcomes),
        missing=missing,
    )


def _compare(
    adaptive: PolicyLine,
    baselines: Sequence[PolicyLine],
    *,
    options: StatisticsOptions,
) -> tuple[PolicyComparison, ...]:
    """自适应与各条对照线逐位配对比较。

    **配对靠位号，不靠顺序。** 少一位两位就配错了 —— 而配错的对检验出来的 p 值
    是假的，还看不出来。

    **两边的数都得有。** 一边"没跑成"或"没量到成本"，这一位就不进检验 —— 拿不
    齐的一对去比，比出来的是编的。
    """
    comparisons: list[PolicyComparison] = []
    for baseline in baselines:
        adaptive_by_index = {outcome.index: outcome for outcome in adaptive.outcomes}
        baseline_by_index = {outcome.index: outcome for outcome in baseline.outcomes}
        shared = sorted(set(adaptive_by_index) & set(baseline_by_index))

        success_pairs: list[tuple[bool, bool]] = []
        cost_pairs: list[tuple[Decimal, Decimal]] = []
        for index in shared:
            left = adaptive_by_index[index]
            right = baseline_by_index[index]
            if left.executed and right.executed:
                success_pairs.append((left.succeeded, right.succeeded))
            if left.cost is not None and right.cost is not None:
                cost_pairs.append((left.cost, right.cost))

        success_test = (
            mcnemar_exact(
                [pair[0] for pair in success_pairs],
                [pair[1] for pair in success_pairs],
                StatisticsOptions(alpha=options.alpha, label=METRIC_SUCCESS),
            )
            if success_pairs
            else None
        )
        cost_test = (
            paired_permutation_test(
                [pair[0] for pair in cost_pairs],
                [pair[1] for pair in cost_pairs],
                StatisticsOptions(alpha=options.alpha, label=METRIC_COST),
            )
            if cost_pairs
            else None
        )
        comparisons.append(
            PolicyComparison(
                baseline=baseline.name,
                success=success_test,
                cost=cost_test,
                success_pairs=len(success_pairs),
                cost_pairs=len(cost_pairs),
            )
        )
    return tuple(comparisons)


def recorded_runs(
    records: Sequence[RunRecord],
    prices: Mapping[str, PriceTable] | None = None,
) -> tuple[RecordedRun, ...]:
    """把评测记录转成回放认得的运行 —— **成本按统一价目表折算**。

    **没有价目表的模型，成本是 ``None``。** 拿不到价目表就记"没测出来"，不记 0
    —— 0 会被当成免费，于是那条线会凭空赢下"最省"。

    **没记模型的运行切不进任何格子** —— 与能力矩阵同一套规矩。
    """
    runs: list[RecordedRun] = []
    for record in records:
        if not record.model:
            continue
        table = (prices or {}).get(record.model)
        cost = table.cost_of(record.tokens) if table is not None and record.tokens else None
        runs.append(
            RecordedRun(
                executor=record.executor,
                model=record.model,
                task_id=record.task_id,
                attempt=record.attempt,
                succeeded=record.outcome is RunOutcome.SUCCEEDED,
                executed=record.outcome is not RunOutcome.NOT_RUN,
                cost=cost,
                duration=record.duration_seconds,
            )
        )
    return tuple(runs)


__all__ = [
    "ADAPTIVE_NAME",
    "HISTORY_CHOSEN",
    "HISTORY_SHARED",
    "METRIC_COST",
    "METRIC_SUCCESS",
    "BaselinePort",
    "Decision",
    "PolicyComparison",
    "PolicyLine",
    "RecordedRun",
    "ReplayReport",
    "ReplaySlot",
    "RoundRobinBaseline",
    "SlotOutcome",
    "StaticBaseline",
    "build_slots",
    "recorded_runs",
    "replay",
]
