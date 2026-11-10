"""跨周期资源闸门 —— 预算、成本归集与并发限流。

设计依据：docs/tasks.md TASK-P3-015（交付物：跨周期预算、成本归集、并发限流；
验收条件"连续运行期间每日成本不超过 ``max_daily_cost``；超限时优雅停止而非
崩溃"；验证 T1 + T2）、R-013（Budget 含日成本与并发上限，可配）、R-018
（Budget 管 Token / 调用 / 资源 / 时间）、docs/design.md §3.1（Budget Manager）。

**这一层回答的是"预算到底约束了谁"。**

:mod:`core.policy.budget` 给了预算、账本与原子扣减 —— 但**账本是按作用域记
账的**。于是"每个周期各建一个 :class:`~core.policy.budget.BudgetManager`"这件事，
看起来像无害的写法，实际效果是**每个周期都有一份日预算**：跑十轮，账面上的
日成本是十份上限。**上限还能不能被叫做上限，取决于一共有多少本账。**

所以第一件事是**一本账**：一个项目一个闸门，所有周期往里记。

**第二件事是"撞过墙就别再开新周期"。** 账本的原子扣减只保证"记进去的总额不
超上限"，它管不了"这一轮该不该开跑"。撞过上限的那一笔**没有记进累计额**
（累计额只增不减，拦下的钱不算花出去）—— 于是"余额还剩三块"是可以发生的，
而系统会再开一轮、再撞一次、再撞一次。**准入门**管这个：开跑之前先问一句，
撞过墙就优雅停止。

**第三件事是"并发只有一个数"。** 上限写在 :class:`~core.policy.budget.Budget`
里，但每个并发引擎各建各的信号量 —— 两批任务同时跑，limit 就翻倍了，限流限
了个寂寞。**闸门持有那个信号量**，谁要用谁从这儿拿。

**"优雅停止"不是抛异常。** 超预算时抛
:class:`~core.policy.budget.BudgetExhausted` 会把整个进程掀掉 —— 正在跑的
东西没了，跑完的东西也没了。**正确的做法是停下来，并把原因说清楚**：准入不
通过就不要再开新周期，这一轮撞墙的那一笔留下"被拒绝"的流水，账上还剩多少写
进结论里。

**钱只从闸门进。** 同一笔钱记两遍，审计时就得先分辨哪一遍才是真的 —— 所以
跨周期这条路上，记账走 :meth:`ResourceGate.charge`；执行器那一层的单次记账
（:mod:`orchestration.cost_accounting`）记的是它自己的账，**两条路别同时用
同一笔钱**。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from core.metrics.cost import CostAmount, CostObserver
from core.metrics.cycle import CycleStatus
from core.policy.budget import (
    Budget,
    BudgetExhausted,
    BudgetLedger,
    BudgetManager,
    BudgetMetric,
    period_key,
)


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    """一次资源使用 —— 同一件事在各个维度上的量。

    **一次调用记多个维度，因为它们是同一笔开销的不同侧面。** 一次模型调用既
    是钱（``cost``），也是 token（``tokens``），也是一次调用（``model_calls``）。
    分开记就会漏 —— 而漏掉的那一维，恰恰是没人盯着的那一维。

    :param cost: 这次花了多少钱。**已经换算好的** —— 换算是
        :mod:`core.metrics.cost` 的事，闸门不换算（理由与
        :meth:`core.policy.budget.BudgetManager.consume_cost` 相同）。
    :param tokens: 这次用掉多少 token。
    :param model_calls: 这次调了几次模型。
    :param compute_minutes: 这次占了多少计算时间。
    :param wall_clock_minutes: 这次占了多少墙钟时间。
    """

    cost: CostAmount | None = None
    tokens: int | None = None
    model_calls: int | None = None
    compute_minutes: int | None = None
    wall_clock_minutes: int | None = None

    def counters(self) -> tuple[tuple[BudgetMetric, int], ...]:
        """计数类的维度 —— token、调用次数、分钟数。

        **钱不在这里。** 钱要带币种，而币种对不上是要报错的（混算出来的数字不
        再代表任何东西），计数类维度没有这个问题。
        """
        pairs = (
            (BudgetMetric.TOKEN_USAGE, self.tokens),
            (BudgetMetric.MODEL_CALLS, self.model_calls),
            (BudgetMetric.COMPUTE_MINUTES, self.compute_minutes),
            (BudgetMetric.WALL_CLOCK_MINUTES, self.wall_clock_minutes),
        )
        return tuple((metric, value) for metric, value in pairs if value is not None)


@dataclass(frozen=True, slots=True)
class DimensionCharge:
    """一个维度上的记账结果。

    :param metric: 记在哪个维度。
    :param amount: 记了多少。
    :param recorded: 记进去了吗。**没记进去也是一个结果** —— 撞了上限这件事
        要能被问出来，不能只留下一个"总量没变"。
    :param reason: 没记进去的原因。
    """

    metric: BudgetMetric
    amount: Decimal
    recorded: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ChargeOutcome:
    """一次记账的结果 —— **各维度各算各的**。

    **一个维度被拦下，不意味着别的维度没记上。** 钱撞了上限，token 照样用掉
    了 —— 把整笔记成"失败"会让人以为 token 也没用上；把整笔记成"成功"又会
    让人以为钱没超。

    :param charges: 各维度的结果。
    :param cycle_id: 记在哪个周期名下 —— **成本归集靠它**。
    """

    charges: tuple[DimensionCharge, ...] = ()
    cycle_id: str = ""

    @property
    def recorded(self) -> bool:
        """全部维度都记进去了吗。"""
        return all(charge.recorded for charge in self.charges)

    @property
    def blocked(self) -> bool:
        """有维度被预算拦下了吗。"""
        return any(not charge.recorded for charge in self.charges)

    def blocked_charges(self) -> tuple[DimensionCharge, ...]:
        """被拦下的那些维度。"""
        return tuple(charge for charge in self.charges if not charge.recorded)


@dataclass(frozen=True, slots=True)
class ResourceAdmission:
    """能不能开跑 —— 不能的时候，把原因带上。

    **"不能"不是一个布尔值，是一个结论。** 只说"不行"，调用方不知道该停
    多久、该报什么、该去改哪一项配置。**理由是必需品，不是装饰。**

    :param allowed: 能不能开跑。
    :param reason: 为什么不能 —— **说给人听的一句话**。
    :param metric: 挡住它的是哪个维度。
    :param spent: 那个维度已经花了多少。
    :param limit: 那个维度的上限是多少。
    """

    allowed: bool
    reason: str = ""
    metric: BudgetMetric | None = None
    spent: Decimal | None = None
    limit: Decimal | None = None

    @property
    def stopped(self) -> bool:
        """该停了吗。"""
        return not self.allowed

    @property
    def cycle_status(self) -> CycleStatus:
        """周期状态 —— **词汇表里早有这一档**。

        被预算掐断的周期"跑完了但没变好"：它的 status 是
        ``budget_exhausted``，outcome 是 ``stalled``
        （见 :func:`core.metrics.cycle.classify_cycle`）。
        """
        if self.allowed:
            return CycleStatus.COMPLETED
        return CycleStatus.BUDGET_EXHAUSTED


class ConcurrencyLimiter:
    """跨周期共享的并发闸门。

    **上限只有一个出处。** 上限写在 :class:`~core.policy.budget.Budget` 里，
    谁都不许自己再抄一份 —— 两处各有一份，就会有两个答案，而"实际同时跑了
    几个"没人能对得上。

    **闸门是共享的，所以并发数跨批次、跨周期都不超。** 每批任务各建各的信
    号量，两批同时跑，上限就翻倍了。

    :param limit: 同时最多跑几个。**至少 1** —— 上限为 0 等于"什么都不许
        做"，那不是限流，那是停机。
    """

    __slots__ = ("_limit", "_live", "_lock", "_peak", "_semaphore")

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError(f"并发上限至少是 1 —— 收到 {limit}")
        self._limit = limit
        self._semaphore = asyncio.Semaphore(limit)
        self._lock = asyncio.Lock()
        self._live = 0
        self._peak = 0

    @property
    def limit(self) -> int:
        """并发上限。"""
        return self._limit

    @property
    def live(self) -> int:
        """现在同时跑着几个。"""
        return self._live

    @property
    def peak(self) -> int:
        """同时跑着几个的**历史最高**。

        **跨批次也记着。** 单批的峰值会被下一批冲掉，而"整个系统在某个时刻
        同时跑过几个"才是限流真正要回答的问题。
        """
        return self._peak

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """占一个号牌 —— **占了才开始干活**。

        **不是"先干活再检查"。** 那样的话，超限的那一瞬间已经有东西在跑
        了，检查只能拦下后面的，拦不下已经发生的。
        """
        async with self._semaphore:
            async with self._lock:
                self._live += 1
                self._peak = max(self._peak, self._live)
            try:
                yield
            finally:
                async with self._lock:
                    self._live -= 1


class ResourceGate:
    """跨周期资源闸门 —— **一本账、一道准入门、一个信号量**。

    :param budget: 预算配置。**上限只有一个出处**（R-013）。
    :param scope: 记在哪个名下。**一个项目一个闸门** —— 这是"跨周期"三个字
        的落点：换了 scope 就等于换了一本账，跨周期预算也就不存在了。
    :param ledger: 记账的持久化口。默认内存账本（同进程内原子）。跨进程要
        用数据库账本（``infrastructure/database/budget.py``）。
    :param clock: 时钟。测试里用它推进日切。
    :param observer: 观测口。给了就顺便把成本记进观测系统 —— **一次事件观察
        一次**，不然"三处同源"就成了"两处同源"。

    **为什么这个类存在。** :class:`core.policy.budget.BudgetManager` 管的是
    "一个作用域的账"。**但演进是一个接一个的周期** —— 每个周期各建一个
    BudgetManager，就等于"每个周期都有一份日预算"。**上限还能不能被叫做上
    限，取决于一共有多少本账。**
    """

    __slots__ = (
        "_budget",
        "_clock",
        "_cycle_spend",
        "_limiter",
        "_manager",
        "_observer",
        "_scope",
    )

    def __init__(
        self,
        budget: Budget,
        *,
        scope: str = "default",
        ledger: BudgetLedger | None = None,
        clock: Callable[[], datetime] | None = None,
        observer: CostObserver | None = None,
    ) -> None:
        self._budget = budget
        self._scope = scope
        self._clock = clock or (lambda: datetime.now(UTC))
        self._manager = BudgetManager(
            budget,
            scope=scope,
            ledger=ledger,
            clock=self._clock,
        )
        self._limiter = ConcurrencyLimiter(budget.max_parallel_agents)
        self._observer = observer
        self._cycle_spend: dict[str, Decimal] = {}

    @property
    def budget(self) -> Budget:
        """预算配置。"""
        return self._budget

    @property
    def scope(self) -> str:
        """记在哪个名下。"""
        return self._scope

    @property
    def manager(self) -> BudgetManager:
        """底层账本管理器。

        **要跟别的组件共用同一本账，就用它** —— 另起一个 manager 另起一本账，
        跨周期预算立刻失效。
        """
        return self._manager

    @property
    def limiter(self) -> ConcurrencyLimiter:
        """共享的并发闸门。"""
        return self._limiter

    def _now(self) -> datetime:
        return self._clock()

    async def admit(self) -> ResourceAdmission:
        """这一轮能不能开跑。

        **既看余额，也看今天撞过没有。** 撞墙的那一笔**没有记进累计额** ——
        累计额只增不减，拦下来的钱不算花出去 —— 于是"余额还剩三块"是可以
        发生的，而系统会再开一轮、再撞一次、再撞一次。**只看余额是不够的。**

        :returns: 能开跑就返回 ``allowed=True``；不能就带上理由、维度、已花
            与上限。
        """
        now = self._now()
        for metric in BudgetMetric:
            limit = self._budget.limit_for(metric)
            if limit is None:
                continue
            spent = await self._manager.spent(metric)
            if spent >= limit:
                return ResourceAdmission(
                    allowed=False,
                    reason=f"{metric.value} 已经用满（{spent} / {limit}）—— 不再开新周期",
                    metric=metric,
                    spent=spent,
                    limit=limit,
                )
            period = period_key(metric, now)
            if await self._hit_the_wall_today(metric, period):
                return ResourceAdmission(
                    allowed=False,
                    reason=(
                        f"{metric.value} 今天已经撞过一次上限"
                        f"（{spent} / {limit}）—— 今天不再开新周期"
                    ),
                    metric=metric,
                    spent=spent,
                    limit=limit,
                )
        return ResourceAdmission(allowed=True)

    async def _hit_the_wall_today(self, metric: BudgetMetric, period: str) -> bool:
        """今天这个维度撞过墙吗。

        **只查这一个维度的流水。** 一次查全部维度，流水越多拉回来的越多 ——
        而这个判断每轮都要做一次。

        **账本没有"按周期查"的接口**，所以周期这一层过滤在这里做 —— 拉回来的
        仍然是这个维度在这个作用域下的全部流水。**流水长到几万条之后这一步会
        变慢**，那时要往账本上加"按周期查"的口子，而不是在这里硬扛。
        """
        entries = await self._manager.ledger.entries(self._manager.scope, metric)
        return any(entry.period == period and entry.is_blocked for entry in entries)

    async def charge(self, usage: ResourceUsage, *, cycle_id: str) -> ChargeOutcome:
        """把一次资源使用记到账上。

        :param usage: 这次用了什么。
        :param cycle_id: 记在哪个周期名下 —— **成本归集靠它**。

        **一个维度被拦下，不影响别的维度。** 钱撞了上限，token 照样用掉了；
        把整笔记成"失败"会让人以为 token 也没用上。
        """
        charges: list[DimensionCharge] = []
        if usage.cost is not None:
            charges.extend(await self._charge_money(usage.cost, cycle_id=cycle_id))
        for metric, value in usage.counters():
            charges.append(await self._charge_counter(metric, value))
        return ChargeOutcome(charges=tuple(charges), cycle_id=cycle_id)

    async def _charge_money(
        self,
        cost: CostAmount,
        *,
        cycle_id: str,
    ) -> list[DimensionCharge]:
        """钱记进每一个"有人管上限"的窗口。

        **被拦下的那笔不记进累计额，但流水留着** —— 那条"被拒绝"的流水就是
        撞过墙的证据，而"撞过墙"这件事正是准入门要看的。
        """
        charges: list[DimensionCharge] = []
        for metric in self._money_metrics():
            try:
                record = await self._manager.consume_cost(metric, cost)
            except BudgetExhausted as exhausted:
                charges.append(
                    DimensionCharge(
                        metric=metric,
                        amount=cost.amount,
                        recorded=False,
                        reason=(
                            f"{metric.value} 撞上限了"
                            f"（请求 {exhausted.requested}，上限 {exhausted.limit}）"
                        ),
                    )
                )
                continue
            charges.append(
                DimensionCharge(
                    metric=metric,
                    amount=record.amount,
                    recorded=True,
                )
            )
        if any(charge.recorded for charge in charges):
            self._attribute(cycle_id, cost.amount)
        return charges

    async def _charge_counter(self, metric: BudgetMetric, value: int) -> DimensionCharge:
        """计数类的维度 —— token、调用次数、分钟数。"""
        amount = Decimal(value)
        try:
            record = await self._manager.consume(metric, amount)
        except BudgetExhausted as exhausted:
            return DimensionCharge(
                metric=metric,
                amount=amount,
                recorded=False,
                reason=(
                    f"{metric.value} 撞上限了（请求 {exhausted.requested}，上限 {exhausted.limit}）"
                ),
            )
        return DimensionCharge(metric=metric, amount=record.amount, recorded=True)

    def _money_metrics(self) -> tuple[BudgetMetric, ...]:
        """钱记到哪些窗口。

        **每天那本账一定有** —— 它是"今天花了多少"的归集，也是日上限落脚
        的地方。**生命周期那本账只在有人要这条上限时才有** —— 没有上限要守
        的窗口不必开账，而同一笔钱记两遍，审计时得先分辨哪一遍才是真的。
        """
        metrics = [BudgetMetric.DAILY_COST]
        if self._budget.max_total_cost is not None:
            metrics.append(BudgetMetric.TOTAL_COST)
        return tuple(metrics)

    def _attribute(self, cycle_id: str, amount: Decimal) -> None:
        """把这一笔归到周期名下 —— **成本归集的那一步**。

        **中间不让出控制权。** 读、算、写三步之间没有 ``await``，所以不会
        有两个协程同时改这一行而丢掉一次累加。
        """
        self._cycle_spend[cycle_id] = self._cycle_spend.get(cycle_id, Decimal(0)) + amount

    async def cycle_spend(self, cycle_id: str) -> Decimal:
        """这一轮花了多少钱（按周期归集）。

        **账本里的钱按窗口记着，周期归属记在闸门手里** —— 归属是"这轮花了多
        少"这个问题的答案，而账本要回答的是"今天/一共花了多少"。
        """
        return self._cycle_spend.get(cycle_id, Decimal(0))

    async def daily_spend(self) -> Decimal:
        """今天花了多少钱。"""
        return await self._manager.spent(BudgetMetric.DAILY_COST)

    async def lifetime_spend(self) -> Decimal:
        """一共花了多少钱。

        **从流水求和，而不是另开一本累计账。** 同一笔钱记两遍，审计时就得
        先分辨哪一遍才是真的。
        """
        entries = await self._manager.ledger_entries()
        return sum(
            (entry.amount for entry in entries if entry.metric is BudgetMetric.DAILY_COST),
            Decimal(0),
        )

    async def blocked_attempts(self) -> tuple[DimensionCharge, ...]:
        """被预算拦下的那些次尝试 —— **证据**。

        **"我们想花超了"这件事要被问出来。** 只看累计额，看不出系统是不是在
        反复撞墙；而反复撞墙是"该停"或者"该改配置"的信号。
        """
        entries = await self._manager.ledger_entries()
        return tuple(
            DimensionCharge(
                metric=entry.metric,
                amount=entry.amount,
                recorded=False,
                reason="被预算拦下",
            )
            for entry in entries
            if entry.is_blocked
        )


__all__ = [
    "ChargeOutcome",
    "ConcurrencyLimiter",
    "DimensionCharge",
    "ResourceAdmission",
    "ResourceGate",
    "ResourceUsage",
]
