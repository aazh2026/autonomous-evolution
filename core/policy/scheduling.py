"""自适应调度 —— 按历史表现，为**每一个任务**挑一个（执行器 × 模型）组合。

设计依据：docs/tasks.md TASK-P4-040（交付物"基于历史表现的自适应调度"；验收
条件"**调度器在实测中优于静态分配的对照组**"；验证 T2）、R-038（执行器能力、
超时、成本、幂等补齐）、TASK-P4-030（二维能力矩阵 —— 静态选型的对照与证据来
源）、TASK-P4-020（成本上限来自项目余额）。

**调度与选型不是一回事。** 选型问的是"这个项目该用哪个组合"，答案在整个项目
里**只有一份**；调度问的是"**这个任务**该用哪个组合"，答案**每个任务一份**，
而且**会随着历史积累而改变** —— 这就是"自适应"两个字的全部含义。

**为什么静态分配是个坏主意。** 静态分配把所有任务压在一个组合上：要么全用最
强的（贵得离谱），要么全用最便宜的（做不对的时候全军覆没）。**任务之间本来
就有难易之分**，用同一套配置打所有任务，等于把强配置用在简单任务上、把弱配置
用在该用强配置的地方。

**先保达成度，再压成本。** 调度器不比"成功率 / 成本"这种比值 —— 那种比值会让
一个"三次里对一次"的便宜格子排在"三次里对三次"的贵格子前面，因为 0.33 / 0.01
比 1.0 / 1.0 大得多。**比值不是质量的代理。** 所以这里的次序是：先用**达成度
下界**划出底线（达不到底线的出局），**底线之内**再按成本排序 —— 与
:mod:`core.policy.capability` 里"档内按成本排"是同一套口径。

**"分不开"要有依据。** 底线不是随手拍的数：每一格的**达成度下界**用 Wilson
区间算（样本越小下界越保守），下界与最好一格相差不超过 :attr:`SchedulingPolicy.
tolerance` 的算"分不开"。**样本少的时候下界低** —— 一格只跑过一次且做对了，
它的下界只有 0.2 出头，于是它进不了"分不开"的那一档：**一次成功不算证据**。

**三态，不是两态。** "没跑起来"与"做不对"必须分得开：前者是环境问题（不该算
进能力），后者是能力问题。所以一次观测记两个事实 —— 跑没跑起来、做没做对。

**冷启动必须有话说。** 一次都没跑过时没有历史可选，但**调度器不能因为没证据
就不开工** —— 那等于系统永远起不来。所以有一条**显式声明**的冷启动偏好顺序；
这一次选择是**没有证据的选择**，它在决定里被如实记成 ``cold_start=True``，
而不是混进"有据可依"的那一堆里。

**失败关闭。** 设了成本上限时，**成本没测出来的格子一律出局** —— 不知道多少钱，
就没法说它没超上限。上限是硬闸：宁可这次不跑，也不要花掉不属于这个项目的钱。

**依赖方向。** 这一层是纯数据与纯函数：不碰数据库、不碰 IO、不认识执行器适配
器，也**不认识评测记录** —— 观测是别人喂进来的数据。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from core.policy.capability import INFINITY

#: Wilson 区间用的 z 值 —— 95% 双侧区间。
#:
#: **为什么是 95%。** 这不是随手挑的置信水平，而是与评测层那套 Fisher 精确检
#: 验同一个尺度（``ALPHA = 0.05``）：两处若用不同的置信水平，"分不开"的定义
#: 就会在两份文档之间对不上。
CONFIDENCE_Z: Decimal = Decimal("1.959963984540054")

#: 默认的容差 —— 与最好一格的达成度下界相差这么多以内就算"分不开"。
#:
#: **这个数是 knob，不是定论。** 调大它，能进底线的格子变多、调度器更省钱，
#: 但达成度会往下掉；调小它更保守，也更接近静态分配。实测里那条曲线见
#: ``docs/architecture/adaptive-scheduling.md``。
DEFAULT_TOLERANCE: Decimal = Decimal("0.05")

#: 任务级估计用的先验 —— Beta(1, 1)。
#:
#: **为什么用后验均值而不是原始比率。** 一个格子在这个任务上跑过 0 次时，原始
#: 比率是"没定义"，后验均值是 0.5 —— **"没试过"与"试过一次且失败"必须排在
#: 不一样的位置**，而原始比率根本分不出这两者。
PRIOR_SUCCESSES: Decimal = Decimal("1")

#: Beta(1, 1) 先验的试次总数 —— 均值是 (成功 + 1) / (试次 + 2)。
PRIOR_TRIALS: Decimal = Decimal("2")

#: 零 —— **模块级单例**：Decimal 是不可变的，共用一个默认值没有风险。
ZERO: Decimal = Decimal("0")


def wilson_lower_bound(
    successes: int,
    trials: int,
    *,
    z: Decimal = CONFIDENCE_Z,
) -> Decimal:
    """二项比例的 Wilson 置信下界。

    **为什么用下界而不是点估计。** 决策要回答的是"这格到底有多强"，而下界答
    的是"**最坏情况下它也有多强**"。样本少的时候下界低得很快 —— 1 次成功的
    下界只有 0.21 出头，9 次全成功是 0.70 —— 于是**一次成功骗不过去**。

    **为什么不是正态近似。** 这里的样本量只有几十个计数，正态近似在这个量级
    上会把"分不开"说成"分得开"。Wilson 区间在小样本下也站得住。

    :param successes: 做对了几次。
    :param trials: 该跑几次。
    :param z: 正态分位数。
    :return: 下界；``trials`` 为 0 时是 0（**没跑过不是 0，是没有证据** —— 调用
        方要把这种情况单独挑出来，不许拿 0 当下界用）。
    """
    if successes < 0 or trials < 0:
        raise ValueError("计数不能为负")
    if successes > trials:
        raise ValueError(f"成功数不能超过总次数：{successes} > {trials}")
    if trials == 0:
        return ZERO
    count = Decimal(trials)
    rate = Decimal(successes) / count
    square = z * z
    denominator = Decimal(1) + square / count
    centre = (rate + square / (2 * count)) / denominator
    spread = (z / denominator) * (
        rate * (Decimal(1) - rate) / count + square / (4 * count * count)
    ).sqrt()
    lower = centre - spread
    return lower if lower > ZERO else ZERO


@dataclass(frozen=True, slots=True)
class Observation:
    """一次运行的结果 —— **调度器眼里的历史**。

    **只有两件事是原子的：跑没跑起来、做没做对。** "没跑成"与"做不对"归因不同
    （前者是环境，后者是能力），所以用两个布尔记，而不是一个三值枚举 —— core
    不认识评测层的枚举，两个布尔就把三态说全了：跑起来且做对、跑起来但做不对、
    压根没跑起来。
    """

    #: 执行器名字。
    executor: str
    #: 模型名字。
    model: str
    #: 任务名字 —— **同格的历史按任务分开记**，因为"这个任务上做过一次"与
    #: "别的任务上做过一次"不是一回事。
    task_id: str
    #: 跑起来了吗。
    executed: bool = True
    #: 做对了吗。
    succeeded: bool = False
    #: 这一次花了多少 —— **统一价目表折算出来的**，不是各 CLI 自报的。
    cost: Decimal | None = None
    #: 这一次跑了多久（秒）。
    duration: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.executor.strip():
            raise ValueError("executor 不能为空")
        if not self.model.strip():
            raise ValueError("model 不能为空")
        if not self.task_id.strip():
            raise ValueError("task_id 不能为空")
        if self.succeeded and not self.executed:
            raise ValueError("没跑起来就不可能做对了")
        if self.cost is not None and self.cost < ZERO:
            raise ValueError("成本不能为负")
        if self.duration is not None and self.duration < ZERO:
            raise ValueError("耗时不能为负")

    @property
    def key(self) -> tuple[str, str]:
        """这一格的坐标 —— (执行器, 模型)。"""
        return (self.executor, self.model)


def observation_to_dict(observation: Observation) -> dict[str, object]:
    """把一次观测转成可 JSON 化的字典 —— **给存档用**。

    **为什么转换住在这里。** 观测是"评测层产出、运行时消费"的东西：谁写盘、
    谁读盘是两边的事，但**"一次观测长什么样"只有一处定义** —— 两边各写一份
    转换，迟早会对不上，而对不上的那几列会静悄悄地变成 0。
    """
    return {
        "executor": observation.executor,
        "model": observation.model,
        "task_id": observation.task_id,
        "executed": observation.executed,
        "succeeded": observation.succeeded,
        "cost": str(observation.cost) if observation.cost is not None else None,
        "duration": str(observation.duration) if observation.duration is not None else None,
    }


def observation_from_dict(item: Mapping[str, object]) -> Observation:
    """把字典还原成一次观测 —— **没测出来的量不许拿 0 顶上**。"""
    return Observation(
        executor=str(item["executor"]),
        model=str(item["model"]),
        task_id=str(item["task_id"]),
        executed=bool(item.get("executed", True)),
        succeeded=bool(item.get("succeeded", False)),
        cost=_optional_decimal(item.get("cost")),
        duration=_optional_decimal(item.get("duration")),
    )


def _optional_decimal(value: object) -> Decimal | None:
    """字典里的一个十进制数 —— **没有就是没有**。"""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


@dataclass(frozen=True, slots=True)
class TaskPerformance:
    """一个任务在一个格子上的表现。"""

    #: 该跑几次。
    runs: int = 0
    #: 其中跑起来几次。
    executed: int = 0
    #: 其中做对几次。
    succeeded: int = 0

    @property
    def overall_rate(self) -> Decimal | None:
        """做对率 —— 做对了几次 / 该跑几次。"""
        if self.runs == 0:
            return None
        return Decimal(self.succeeded) / Decimal(self.runs)

    @property
    def posterior_mean(self) -> Decimal:
        """Beta(1, 1) 后验均值 —— **"没试过"也有一个数**。

        没用原始比率，是因为原始比率在 0 次观测时是"没定义"，而"没试过"与"试过
        一次且失败"在调度里必须排不同的位置。
        """
        return (Decimal(self.succeeded) + PRIOR_SUCCESSES) / (Decimal(self.runs) + PRIOR_TRIALS)


@dataclass(frozen=True, slots=True)
class CellPerformance:
    """一个（执行器 × 模型）格子的累计表现。

    **这是"历史"，不是"能力"。** 它只说"到现在为止，这一格做过多少次、做对过
    多少次、花了多少钱" —— 不替任何人下"这一格更强"的结论。
    """

    #: 执行器名字。
    executor: str
    #: 模型名字。
    model: str
    #: 该跑几次。
    runs: int = 0
    #: 其中跑起来几次。
    executed: int = 0
    #: 其中做对几次。
    succeeded: int = 0
    #: 成本合计（**只累加测到过成本的运行**）。
    cost_total: Decimal = ZERO
    #: 测到过成本的运行次数。
    cost_samples: int = 0
    #: 耗时合计。
    duration_total: Decimal = ZERO
    #: 测到过耗时的运行次数。
    duration_samples: int = 0
    #: 每个任务上的表现 —— **同格的历史按任务分开记**。
    tasks: Mapping[str, TaskPerformance] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        """这一格的坐标 —— (执行器, 模型)。"""
        return (self.executor, self.model)

    @property
    def measured(self) -> bool:
        """这一格有没有跑过。"""
        return self.runs > 0

    @property
    def overall_rate(self) -> Decimal | None:
        """做对率 —— 做对了几次 / 该跑几次。"""
        if self.runs == 0:
            return None
        return Decimal(self.succeeded) / Decimal(self.runs)

    @property
    def success_rate(self) -> Decimal | None:
        """成功率 —— 做对了几次 / 跑成了几次。

        **"没跑成"不进分母** —— 把环境问题说成能力问题。
        """
        if self.executed == 0:
            return None
        return Decimal(self.succeeded) / Decimal(self.executed)

    @property
    def mean_cost(self) -> Decimal | None:
        """平均成本 —— **只按测到过成本的运行算**。

        **没测到就是没有**（``None``），不是 0：0 会被当成"免费"，于是这一格
        在排序里冲到最前面。
        """
        if self.cost_samples == 0:
            return None
        return self.cost_total / Decimal(self.cost_samples)

    @property
    def mean_duration(self) -> Decimal | None:
        """平均耗时 —— 只按测到过耗时的运行算。"""
        if self.duration_samples == 0:
            return None
        return self.duration_total / Decimal(self.duration_samples)

    def lower_bound(self, *, z: Decimal = CONFIDENCE_Z) -> Decimal:
        """达成度下界 —— 这一格"最坏情况下也有多强"。"""
        return wilson_lower_bound(self.succeeded, self.runs, z=z)

    def task_performance(self, task_id: str) -> TaskPerformance:
        """某个任务上的表现 —— 没跑过就是空的。"""
        return self.tasks.get(task_id, TaskPerformance())


@dataclass(frozen=True, slots=True)
class PerformanceLedger:
    """全部历史观测 —— **不可变**。

    **不可变是有意的。** 一次调度决定要能被复现：同样的历史必须给出同样的决定。
    账本只追加（:meth:`of` 造新的），不改旧账。
    """

    #: 全部观测 —— 按加入的先后排。
    observations: tuple[Observation, ...] = ()

    @classmethod
    def of(cls, observations: Iterable[Observation]) -> PerformanceLedger:
        """造一个账本。"""
        return cls(observations=tuple(observations))

    def cells(self) -> tuple[CellPerformance, ...]:
        """按格子汇总 —— **每个格子一份**。"""
        grouped: dict[tuple[str, str], list[Observation]] = {}
        for observation in self.observations:
            grouped.setdefault(observation.key, []).append(observation)
        return tuple(
            sorted(
                (_summarize(key[0], key[1], items) for key, items in grouped.items()),
                key=lambda cell: cell.key,
            )
        )

    def cell(self, executor: str, model: str) -> CellPerformance | None:
        """按坐标取一个格子 —— 没有就是没有。"""
        for cell in self.cells():
            if cell.executor == executor and cell.model == model:
                return cell
        return None

    def observations_before(self, marker: int) -> tuple[Observation, ...]:
        """前 ``marker`` 条观测 —— **给"不许看未来"的回放用**。"""
        return self.observations[:marker]


def _summarize(executor: str, model: str, observations: Sequence[Observation]) -> CellPerformance:
    """把一批观测汇总成一格。"""
    runs = len(observations)
    executed = sum(1 for item in observations if item.executed)
    succeeded = sum(1 for item in observations if item.succeeded)
    costs = [item.cost for item in observations if item.cost is not None]
    durations = [item.duration for item in observations if item.duration is not None]
    tasks: dict[str, TaskPerformance] = {}
    for item in observations:
        current = tasks.get(item.task_id, TaskPerformance())
        tasks[item.task_id] = TaskPerformance(
            runs=current.runs + 1,
            executed=current.executed + (1 if item.executed else 0),
            succeeded=current.succeeded + (1 if item.succeeded else 0),
        )
    return CellPerformance(
        executor=executor,
        model=model,
        runs=runs,
        executed=executed,
        succeeded=succeeded,
        cost_total=sum(costs, ZERO),
        cost_samples=len(costs),
        duration_total=sum(durations, ZERO),
        duration_samples=len(durations),
        tasks=tasks,
    )


@dataclass(frozen=True, slots=True)
class CellChoice:
    """一个（执行器 × 模型）组合。"""

    #: 执行器名字。
    executor: str
    #: 模型名字。
    model: str

    @property
    def key(self) -> tuple[str, str]:
        """坐标 —— (执行器, 模型)。"""
        return (self.executor, self.model)

    def __str__(self) -> str:
        return f"{self.executor}/{self.model}"


@dataclass(frozen=True, slots=True)
class RejectedChoice:
    """一个组合被排除的原因。

    **排除必须带理由。** 只说"没选它"，事后没人知道是它不行、是它没测过，还
    是它压根不在候选里 —— 而这三件事的处置完全不同。
    """

    #: 被排除的那个组合。
    choice: CellChoice
    #: 排除理由。
    reason: str

    def __str__(self) -> str:
        return f"{self.choice}：{self.reason}"


@dataclass(frozen=True, slots=True)
class SchedulingPolicy:
    """调度的规矩。

    :param tolerance: 容差 —— 与最好一格的达成度下界相差这么多以内就算"分不
        开"，于是这一档里按成本排序。**调大它更省钱也更冒险**。
    :param z: Wilson 区间的 z 值。
    :param maximum_cost: 单次成本上限 —— **硬闸**（预算是不许超过，不是尽量少
        花）。``None`` 表示不限。
    :param minimum_rate: 绝对达成度底线 —— **在容差之外再加一道**。容差是相对
        的（"跟最好的差多少"），底线是绝对的（"再差也不能低于这个数"）。
    :param preference: 冷启动偏好顺序 —— **形如 "执行器/模型"**。**没有历史时
        按这个顺序选**，而且这一次的决定会被记成冷启动。
    :param explore_every: 每多少次决策探一次没怎么试过的格子 —— **0 表示不探
        索**。探索是花掉的成本，所以默认关闭。
    """

    #: 容差。
    tolerance: Decimal = DEFAULT_TOLERANCE
    #: Wilson 区间的 z 值。
    z: Decimal = CONFIDENCE_Z
    #: 单次成本上限。
    maximum_cost: Decimal | None = None
    #: 绝对达成度底线。
    minimum_rate: Decimal = ZERO
    #: 冷启动偏好顺序。
    preference: tuple[str, ...] = ()
    #: 每多少次决策探一次。
    explore_every: int = 0

    def __post_init__(self) -> None:
        if self.tolerance < ZERO:
            raise ValueError(f"容差不能为负：{self.tolerance}")
        if self.maximum_cost is not None and self.maximum_cost < ZERO:
            raise ValueError(f"成本上限不能为负：{self.maximum_cost}")
        if not Decimal("0") <= self.minimum_rate <= Decimal("1"):
            raise ValueError(f"达成度底线要落在 0~1 之间：{self.minimum_rate}")
        if self.explore_every < 0:
            raise ValueError(f"探索间隔不能为负：{self.explore_every}")
        for entry in self.preference:
            executor, separator, model = entry.partition("/")
            if not separator or not executor.strip() or not model.strip():
                raise ValueError(f"冷启动偏好要写成 '执行器/模型'，得到：{entry}")


#: 调度的默认规矩 —— **模块级单例**，因为 :class:`SchedulingPolicy` 不可变。
DEFAULT_POLICY: SchedulingPolicy = SchedulingPolicy()


@dataclass(frozen=True, slots=True)
class SchedulingDecision:
    """一次调度的结果。"""

    #: 选中的组合 —— ``None`` 表示"挑不出来"。
    chosen: CellChoice | None = None
    #: 理由 —— 人话。
    reason: str = ""
    #: 通过底线的候选排序 —— 从前到后。
    ranked: tuple[CellChoice, ...] = ()
    #: 被排除的组合与理由。
    rejected: tuple[RejectedChoice, ...] = ()
    #: 这一一是不是冷启动 —— **没证据的选择要如实标出来**。
    cold_start: bool = False
    #: 这一次是不是探索 —— **探索是花掉的成本，要留痕**。
    exploration: bool = False

    @property
    def found(self) -> bool:
        """有没有选出来。"""
        return self.chosen is not None

    @property
    def executor(self) -> str:
        """选中的执行器 —— 没选出来时是空串。"""
        return self.chosen.executor if self.chosen is not None else ""

    @property
    def model(self) -> str:
        """选中的模型 —— 没选出来时是空串。"""
        return self.chosen.model if self.chosen is not None else ""


@dataclass(frozen=True, slots=True)
class AdaptiveScheduler:
    """按历史表现做调度 —— **每一次决定都基于"到这一刻为止"的历史**。

    **它不是矩阵的复读机。** 矩阵是一张固定的表；调度器每做一个决定，历史就
    多一条，下一次决定就可能换人。这正是"自适应"与"查表"的分别。

    **失败关闭。** 挑不出来就明说挑不出来 —— 返回 :attr:`SchedulingDecision.
    chosen` 为 ``None`` 的决定，而不是悄悄退回一个默认组合。**由调用方决定
    炸不炸**（编排层炸，评测层记成"没有决策"）。

    :param policy: 调度的规矩。
    """

    #: 调度的规矩。
    policy: SchedulingPolicy = DEFAULT_POLICY

    #: 历史观测 —— **只增不改**。
    _observations: list[Observation] = field(
        default_factory=list, init=False, repr=False, compare=False
    )

    #: 已经做过多少次决定 —— **探索额度按它算**。
    _decisions: int = field(default=0, init=False, repr=False, compare=False)

    @property
    def decisions(self) -> int:
        """已经做过多少次决定。"""
        return self._decisions

    @property
    def ledger(self) -> PerformanceLedger:
        """到目前为止的历史。"""
        return PerformanceLedger.of(self._observations)

    def record(self, observation: Observation) -> None:
        """记一次运行结果 —— **这是历史变长的唯一入口**。"""
        self._observations.append(observation)

    def record_all(self, observations: Iterable[Observation]) -> None:
        """记一批运行结果。"""
        self._observations.extend(observations)

    def decide(
        self,
        task_id: str,
        *,
        candidates: Sequence[str] = (),
        models: Sequence[str] = (),
        maximum_cost: Decimal | None = None,
    ) -> SchedulingDecision:
        """给一个任务挑组合 —— **挑不出来就明说挑不出来**。

        :param task_id: 这次的任务 —— **同格的历史按任务分开记**，所以它参与
            决策。
        :param candidates: 候选执行器 —— 空表示"账本里有的都算"。
        :param models: 候选模型 —— 空表示"账本里有的都算"。
        :param maximum_cost: 单次成本上限 —— **给了就以它为准**，没给就用规矩
            里那个。
        """
        object.__setattr__(self, "_decisions", self._decisions + 1)
        limit = maximum_cost if maximum_cost is not None else self.policy.maximum_cost
        rejected: list[RejectedChoice] = []
        allowed: list[CellPerformance] = []

        for cell in self.ledger.cells():
            reason = _gate_reason(cell, candidates, models, limit)
            if reason is None:
                allowed.append(cell)
            else:
                rejected.append(RejectedChoice(choice=_choice_of(cell), reason=reason))

        if not allowed:
            return self._cold_start(candidates, models, limit, rejected)

        estimates = {cell.key: cell.lower_bound(z=self.policy.z) for cell in allowed}
        best = max(estimates.values())
        floor = max(best - self.policy.tolerance, self.policy.minimum_rate)

        viable: list[CellPerformance] = []
        for cell in allowed:
            estimate = estimates[cell.key]
            if estimate < floor:
                rejected.append(
                    RejectedChoice(
                        choice=_choice_of(cell),
                        reason=f"达成度下界 {estimate:.4f} 低于底线 {floor:.4f}",
                    )
                )
            else:
                viable.append(cell)

        if not viable:
            return SchedulingDecision(
                reason="没有一格达到达成度底线",
                rejected=tuple(rejected),
            )

        ordered = tuple(sorted(viable, key=lambda cell: _ranking_key(cell, task_id, self.policy)))
        ranked = tuple(_choice_of(cell) for cell in ordered)
        chosen = ordered[0]
        decision = SchedulingDecision(
            chosen=_choice_of(chosen),
            reason=self._reason(_choice_of(chosen), estimates[chosen.key], floor),
            ranked=ranked,
            rejected=tuple(rejected),
        )
        if self._should_explore(allowed, chosen.runs):
            probe = self._probe(allowed, task_id)
            if probe is not None:
                return SchedulingDecision(
                    chosen=probe,
                    reason=(
                        f"探索：{probe} 的观测次数最少（{_runs_of(allowed, probe)} 次），"
                        "先试它再谈择优"
                    ),
                    ranked=ranked,
                    rejected=tuple(rejected),
                    exploration=True,
                )
        return decision

    # -- 内部 ---------------------------------------------------------------

    def _cold_start(
        self,
        candidates: Sequence[str],
        models: Sequence[str],
        limit: Decimal | None,
        rejected: list[RejectedChoice],
    ) -> SchedulingDecision:
        """没有历史时的选择 —— **按显式声明的偏好顺序**。

        **设了成本上限时冷启动是不允许的。** 不知道要花多少钱，就没法说它没超
        上限 —— 这是与 :mod:`core.policy.capability` 一样的硬闸。
        """
        if limit is not None:
            return SchedulingDecision(
                reason="没有历史，且设了成本上限 —— 没有实测成本就没法确认它在上限之内",
                rejected=tuple(rejected),
            )
        for entry in self.policy.preference:
            executor, _, model = entry.partition("/")
            if candidates and executor not in candidates:
                continue
            if models and model not in models:
                continue
            choice = CellChoice(executor=executor, model=model)
            return SchedulingDecision(
                chosen=choice,
                reason=f"没有历史，按冷启动偏好选了 {choice}",
                rejected=tuple(rejected),
                cold_start=True,
            )
        return SchedulingDecision(
            reason="没有历史也没有冷启动偏好，挑不出组合",
            rejected=tuple(rejected),
        )

    def _should_explore(self, allowed: Sequence[CellPerformance], chosen_runs: int) -> bool:
        """这一次要不要探索。

        **探索额度按"每 N 次决策探一次"算** —— 而且**只在还有没怎么试过的格子
        时才探**。全都试过很多次了，探索就是白花钱。

        **探的是"允许集合"里的格子，不是"过了底线"的格子。** 探索的意义正在
        于给证据最少的那个格子一次机会 —— 而证据最少的格子，恰恰常常是**过不
        了底线**的那个（一次成功不算证据）。**只在过得了底线的格子里挑，等于
        永远探索不到新东西。**
        """
        every = self.policy.explore_every
        if every <= 0:
            return False
        if self._decisions % every != 0:
            return False
        return any(cell.runs < chosen_runs for cell in allowed)

    def _probe(self, allowed: Sequence[CellPerformance], task_id: str) -> CellChoice | None:
        """挑一个"观测最少"的格子去探 —— **平手按成本、再按名字**。"""
        if not allowed:
            return None
        least = min(cell.runs for cell in allowed)
        candidates = [cell for cell in allowed if cell.runs == least]
        return _choice_of(
            min(candidates, key=lambda cell: _ranking_key(cell, task_id, self.policy))
        )

    def _reason(self, chosen: CellChoice, estimate: Decimal, floor: Decimal) -> str:
        """说清楚为什么是这一格 —— **理由要能被复核**。"""
        cell = self.ledger.cell(chosen.executor, chosen.model)
        cost = cell.mean_cost if cell is not None else None
        cost_text = "成本没测出来" if cost is None else f"平均成本 {cost:.6f}"
        return (
            f"{chosen} 的达成度下界 {estimate:.4f} 在底线 {floor:.4f} 之内，"
            f"这一档里它最便宜（{cost_text}）"
        )


def _choice_of(cell: CellPerformance) -> CellChoice:
    """一格 → 一个组合。"""
    return CellChoice(executor=cell.executor, model=cell.model)


def _runs_of(cells: Sequence[CellPerformance], choice: CellChoice) -> int:
    """某个组合跑过几次 —— 找不到就当作 0。"""
    for cell in cells:
        if cell.key == choice.key:
            return cell.runs
    return 0


def _gate_reason(
    cell: CellPerformance,
    candidates: Sequence[str],
    models: Sequence[str],
    limit: Decimal | None,
) -> str | None:
    """这一格为什么被挡在门外 —— **没有理由就返回 ``None``**（即"可以进候选"）。

    **成本上限是硬闸。** 而且**成本没测出来又设了上限的，也被挡住** —— 不知
    道多少钱，就没法说它没超。
    """
    if candidates and cell.executor not in candidates:
        return "不在候选执行器里"
    if models and cell.model not in models:
        return "不在候选模型里"
    if limit is None:
        return None
    cost = cell.mean_cost
    if cost is None:
        return "成本没测出来，没法确认它在上限之内"
    if cost > limit:
        return f"超过成本上限（{cost} > {limit}）"
    return None


def _ranking_key(
    cell: CellPerformance, task_id: str, policy: SchedulingPolicy
) -> tuple[object, ...]:
    """候选的排序键 —— **全序**，同样的输入永远给出同样的顺序。

    **成本排在最前面。** 底线之内的一档在能力上分不开，所以由成本说了算 ——
    这与 :mod:`core.policy.capability` 里"档内按成本排"是同一套口径。

    **任务级证据排在成本后面。** 它只在成本打平时起作用 —— 一个格子在这个
    任务上做过一次，**比在别的任务上做过**更贴近这次要做的事，但**样本只有一
    次的证据不足以压过成本**。

    **最后两格是名字** —— 兜底，保证全序：同样的输入永远给出同样的顺序，否则
    "这次为什么用这个组合"就永远答不上来。
    """
    cost = cell.mean_cost
    duration = cell.mean_duration
    return (
        cost if cost is not None else INFINITY,
        -_task_score(cell, task_id),
        -cell.lower_bound(z=policy.z),
        duration if duration is not None else INFINITY,
        cell.executor,
        cell.model,
    )


def _task_score(cell: CellPerformance, task_id: str) -> Decimal:
    """这个格子在**这个任务上**的证据 —— Beta(1, 1) 后验均值。

    **没在这个任务上跑过就是先验（0.5）**，不是 0 —— 一个"没试过"的格子不该
    被当成"试过且失败"。
    """
    return cell.task_performance(task_id).posterior_mean


__all__ = [
    "CONFIDENCE_Z",
    "DEFAULT_POLICY",
    "DEFAULT_TOLERANCE",
    "PRIOR_SUCCESSES",
    "PRIOR_TRIALS",
    "ZERO",
    "AdaptiveScheduler",
    "CellChoice",
    "CellPerformance",
    "Observation",
    "PerformanceLedger",
    "RejectedChoice",
    "SchedulingDecision",
    "SchedulingPolicy",
    "TaskPerformance",
    "observation_from_dict",
    "observation_to_dict",
    "wilson_lower_bound",
]
