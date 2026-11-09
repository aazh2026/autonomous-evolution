"""统一成本换算器 —— Adapter 上报、预算记账、观测三处同源同币种。

设计依据：docs/tasks.md TASK-P1-035（T1 ① 同一次执行在 Adapter 报告、
预算账本、OTel span 属性三处的成本数值相等；T1 ② 换算单价变更后，三处同步
变化）、I-13（三处成本必须同源同币种）、R-039（统一执行结果 schema）。

**"同源"不是"三处各算一遍，碰巧算出同一个数"。** 是**换一次，三处共用**：
适配器把 CLI 报来的金额换算成记账币种，换算器只被调用一次，换出来的那一个
:class:`CostAmount` 原样进了三处 —— 账本记的是它、观测写的是它、适配器报告
的也是它。**三处各算一遍的话，换算规则改一次就要改三处，而漏掉的那一处不会
报错** —— 它只会安静地记下另一个数，而两个数都是"看起来正常的钱"。

**币种要跟着金额一起走。** 成本没有币种就没法和预算比，而"能不能比"的
答案取决于两边是不是同一个币种。**换个币种记一笔账，比记错数字更坏** ——
数字错了能对账，币种错了对出来的是"另一个项目的钱"。

**单价表外部可配。** 模型的单价会变，汇率会变 —— 写死在代码里的单价表第一次
过期就开始算错，而**算错的是钱**。所以单价表是一份**数据**（外部配置的
映射），不是代码里的常量。

**两条换算路径。** 一条是**金额换金额**（CLI 报来的是钱，按汇率换到记账
币种）；另一条是**用量换金额**（知道 token 数，按模型单价算钱）。两条都走
同一个 :class:`CostConverter`，产出同一种 :class:`CostAmount`。

**金额一律量化到六位小数。** 账本那一列是 ``Numeric(20, 6)`` —— 库里存的
就是量化过的数。**不量化的话，适配器报的那个数与库里存的那个数会差最后
几位**，而"三处相等"这条断言就成了比运气。

**"记账的数"要与"实际调用量"对得上。** 任务书 T2 要求：成本记账与实际调用
量的偏差 < 2%。**这条只能靠两条独立的路径互相对账才成立** —— 一条是执行器
报上来的实际花费，另一条是按单价表对**实际发生的 token 数**算出来的钱。
拿同一条路径的两个数互相比，比出来的永远是 0，那是自证。
:func:`reconcile_costs` 就是做这个对账的。

**缓存 token 单独计价。** 缓存读比普通输入便宜一个数量级，缓存写比普通输入
贵。把两者并进 ``input_tokens``，这笔账就会按错的价算 —— 而**多算出来的钱
不会有人喊**。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

#: 金额的量化步长 —— **与账本那一列一样**（``Numeric(20, 6)``）。
#:
#: 量化发生在 :class:`CostAmount` 构造的那一刻，所以三处拿到的必然是同一个
#: 量化过的数。
MONEY_QUANTUM: Decimal = Decimal("0.000001")

#: 单价表里"每百万 token"的那个百万。
TOKENS_PER_PRICE_UNIT: Decimal = Decimal(1000000)

#: 记账与实际调用量之间允许的相对偏差 —— **任务书 T2 给的是 2%**。
#:
#: 这是**相对**偏差（不是绝对金额）：0.02 表示差 2%。
COST_DEVIATION_TOLERANCE: Decimal = Decimal("0.02")


class CostError(ValueError):
    """成本换算里的错。"""


class CurrencyMismatchError(CostError):
    """币种对不上。"""


class UnknownRateError(CostError):
    """单价表里没有这一项。"""


def _normalize_currency(currency: str) -> str:
    """币种归一 —— 去空白、转大写。

    **归一要在入口做一次。** ``"usd"`` 与 ``"USD"`` 是同一个币种，不归一
    它们就会被当成两种币种，于是"同币种"的判断在有的地方成立、在有的地方
    不成立。
    """
    normalized = currency.strip().upper()
    if not normalized:
        raise CurrencyMismatchError("币种不能是空白")
    return normalized


def _quantize(amount: Decimal) -> Decimal:
    """把金额量化到六位小数。"""
    if not amount.is_finite():
        raise CostError(f"金额必须是有限的：{amount}")
    try:
        return amount.quantize(MONEY_QUANTUM)
    except InvalidOperation as error:
        raise CostError(f"金额量化后超出精度：{amount}") from error


@dataclass(frozen=True, slots=True)
class CostAmount:
    """一笔钱 —— **金额与币种缺一不可**。

    **它不可变。** 同一个对象会被三处共用，而共用一个可变对象等于让某一处
    能在别人记账之后偷偷改掉数字 —— 那种错查不出来。
    """

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise CostError(f"金额不能为负：{self.amount}")
        object.__setattr__(self, "amount", _quantize(self.amount))
        object.__setattr__(self, "currency", _normalize_currency(self.currency))

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """一次调用的 token 用量。

    **缓存 token 单独记，不并进 ``input_tokens``。** 缓存读比普通输入便宜一个
    数量级、缓存写比普通输入贵 —— 并进去的话，这笔账就按错的价算，而**多算
    出来的钱不会有人喊**。四类 token 各有各的价，单价表里分开写。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
            )
            < 0
        ):
            raise CostError("token 数不能为负")

    @property
    def total_tokens(self) -> int:
        """一共用了多少 token。"""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True, slots=True)
class ModelRate:
    """一个模型的单价。

    **按每百万 token 计价**，而不是每个 token —— 定价表就是这么写的，写成
    每百万才与外部配置对得上。

    **缓存读与缓存写的单价是分开的两项。** 它们与输入价不是同一个比例，而且
    **各家不一样** —— 所以写进外部单价表，而不是在这里按某个倍数推出来。
    """

    model: str
    currency: str
    input_price_per_million: Decimal
    output_price_per_million: Decimal
    cache_read_price_per_million: Decimal = Decimal(0)
    cache_write_price_per_million: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise CostError("模型名不能是空白")
        if (
            min(
                self.input_price_per_million,
                self.output_price_per_million,
                self.cache_read_price_per_million,
                self.cache_write_price_per_million,
            )
            < 0
        ):
            raise CostError("单价不能为负")
        object.__setattr__(self, "currency", _normalize_currency(self.currency))

    def price_of(self, usage: TokenUsage) -> Decimal:
        """按这份单价算出这笔用量的金额（**币种是这份单价自己的**）。"""
        return (
            usage.input_tokens / TOKENS_PER_PRICE_UNIT * self.input_price_per_million
            + usage.output_tokens / TOKENS_PER_PRICE_UNIT * self.output_price_per_million
            + usage.cache_read_tokens / TOKENS_PER_PRICE_UNIT * self.cache_read_price_per_million
            + usage.cache_write_tokens / TOKENS_PER_PRICE_UNIT * self.cache_write_price_per_million
        )


@dataclass(frozen=True)
class RateTable:
    """单价表 —— **外部可配的一份数据**。

    :attr:`base_currency` 是记账币种：换算的目标。
    :attr:`model_rates` 是各模型的单价（按模型名索引）。
    :attr:`exchange_rates` 是汇率（``1 单位某币种 = N 单位记账币种``）。

    **记账币种自己的汇率必须是 1。** 不是 1 的话，这张表自相矛盾 —— 而一张
    自相矛盾的表会算出"换算两次回到原点却不一样"的数。宁可在这里报错。
    """

    base_currency: str
    model_rates: Mapping[str, ModelRate] = field(default_factory=dict)
    exchange_rates: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_currency", _normalize_currency(self.base_currency))
        normalized: dict[str, Decimal] = {}
        for currency, rate in self.exchange_rates.items():
            code = _normalize_currency(currency)
            if rate <= 0:
                raise UnknownRateError(f"汇率必须为正：{code} = {rate}")
            normalized[code] = rate
        object.__setattr__(self, "exchange_rates", normalized)
        own = normalized.get(self.base_currency)
        if own is not None and own != Decimal(1):
            raise UnknownRateError(
                f"记账币种 {self.base_currency} 的汇率必须是 1，表里写的是 {own}"
            )

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> RateTable:
        """从一份映射装配 —— **外部配置长什么样，这里就长什么样**。

        形状::

            base_currency: USD
            model_rates:
              claude-sonnet-4-6:
                currency: USD
                input_price_per_million: "3.00"
                output_price_per_million: "15.00"
                cache_read_price_per_million: "0.30"
                cache_write_price_per_million: "3.75"
            exchange_rates:
              USD: "1"
              CNY: "0.14"

        **缓存两项可以不写** —— 不写就是 0，也就是"这类 token 不计价"。
        那是一个**明说的选择**，不是默认值蒙出来的：漏写缓存价会让按用量
        推算出来的钱偏小，而偏差核对会把它喊出来。
        """
        base = data.get("base_currency")
        if not isinstance(base, str):
            raise CostError("单价表缺 base_currency")

        raw_rates = data.get("model_rates", {})
        if not isinstance(raw_rates, Mapping):
            raise CostError("model_rates 必须是一个映射")
        model_rates: dict[str, ModelRate] = {}
        for model, value in raw_rates.items():
            if not isinstance(value, Mapping):
                raise CostError(f"模型 {model} 的单价必须是一个映射")
            model_rates[str(model)] = _model_rate(str(model), value)

        raw_exchange = data.get("exchange_rates", {})
        if not isinstance(raw_exchange, Mapping):
            raise CostError("exchange_rates 必须是一个映射")
        exchange: dict[str, Decimal] = {}
        for currency, value in raw_exchange.items():
            exchange[str(currency)] = _decimal_of(value, f"汇率 {currency}")

        return cls(
            base_currency=base,
            model_rates=model_rates,
            exchange_rates=exchange,
        )

    def rate_of(self, model: str) -> ModelRate:
        """取一个模型的单价 —— **没有就报错，不猜**。

        猜出来一个单价会算出看着合理的错账，而那种错要等到月底对账才发现。
        """
        try:
            return self.model_rates[model]
        except KeyError as error:
            known = "、".join(sorted(self.model_rates)) or "（空）"
            raise UnknownRateError(f"单价表里没有模型 {model} 的单价；表里有：{known}") from error

    def exchange_rate(self, currency: str) -> Decimal:
        """取一个币种到记账币种的汇率。"""
        code = _normalize_currency(currency)
        if code == self.base_currency:
            return Decimal(1)
        try:
            return self.exchange_rates[code]
        except KeyError as error:
            known = "、".join(sorted(self.exchange_rates)) or "（空）"
            raise UnknownRateError(f"单价表里没有币种 {code} 的汇率；表里有：{known}") from error


def _decimal_of(value: object, what: str) -> Decimal:
    """把配置里的一个值读成十进制。"""
    if isinstance(value, bool):
        raise CostError(f"{what}不能是布尔值")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as error:
            raise CostError(f"{what} 不是一个十进制数：{value!r}") from error
    raise CostError(f"{what} 的类型不对：{type(value).__name__}")


def _model_rate(model: str, value: Mapping[str, object]) -> ModelRate:
    """从一份映射读出一个模型的单价。"""
    currency = value.get("currency")
    if not isinstance(currency, str):
        raise CostError(f"模型 {model} 的单价缺 currency")
    return ModelRate(
        model=model,
        currency=currency,
        input_price_per_million=_decimal_of(
            value.get("input_price_per_million", 0), f"模型 {model} 的输入单价"
        ),
        output_price_per_million=_decimal_of(
            value.get("output_price_per_million", 0), f"模型 {model} 的输出单价"
        ),
        cache_read_price_per_million=_decimal_of(
            value.get("cache_read_price_per_million", 0), f"模型 {model} 的缓存读单价"
        ),
        cache_write_price_per_million=_decimal_of(
            value.get("cache_write_price_per_million", 0), f"模型 {model} 的缓存写单价"
        ),
    )


@dataclass(frozen=True, slots=True)
class CostConverter:
    """换算器 —— **三处走的都是这一个**。

    **它只换算，不记账，也不观测。** 记账是账本的事，观测是观测系统的事；
    换算器只负责把"一个带币种的金额"变成"记账币种的金额"，让那两处记的是
    同一个数。
    """

    table: RateTable

    @property
    def currency(self) -> str:
        """记账币种。"""
        return self.table.base_currency

    def convert(self, cost: CostAmount, *, to_currency: str | None = None) -> CostAmount:
        """把一笔钱换到目标币种。

        **同币种原样返回** —— 换算一次不该改动数值（量化那一步除外，
        而量化在构造时已经做过了）。
        """
        target = _normalize_currency(to_currency or self.table.base_currency)
        if cost.currency == target:
            return CostAmount(cost.amount, target)
        rate = self.table.exchange_rate(cost.currency)
        if target != self.table.base_currency:
            # 经由记账币种中转 —— 两张表各说各的，直接相乘要先把它们
            # 对到同一个基准上。
            to_base = cost.amount * rate
            back = self.table.exchange_rate(target)
            return CostAmount(to_base / back, target)
        return CostAmount(cost.amount * rate, target)

    def price(self, usage: TokenUsage, *, model: str) -> CostAmount:
        """按模型单价给一份用量算钱。

        **换算只做一次。** 单价表的币种若不是记账币种，这里换过去 —— 换出来
        的那个 :class:`CostAmount` 就是三处共用的那一个。
        """
        rate = self.table.rate_of(model)
        amount = CostAmount(rate.price_of(usage), rate.currency)
        return self.convert(amount)


@dataclass(frozen=True, slots=True)
class CostReconciliation:
    """一次核对的结果 —— **记账的数与实际调用量推算的数差多少**。

    :attr:`deviation` 是**相对偏差**：``0.02`` 表示差 2%。
    :attr:`within_tolerance` 是结论 —— 差得在容忍线以内还是以外。
    """

    recorded: CostAmount
    expected: CostAmount
    deviation: Decimal
    tolerance: Decimal

    @property
    def within_tolerance(self) -> bool:
        """偏差在容忍线以内吗。"""
        return self.deviation <= self.tolerance


def reconcile_costs(
    recorded: CostAmount,
    expected: CostAmount,
    *,
    tolerance: Decimal = COST_DEVIATION_TOLERANCE,
) -> CostReconciliation:
    """核对两笔钱 —— **记账的数 vs. 实际调用量推算出来的数**。

    **两个数必须来自两条独立的路径。** 一条是执行器报上来的实际花费，另一条
    是按单价表对实际发生的 token 数算出来的钱。拿同一条路径的两个数互相比，
    比出来的永远是 0 —— 那是自证，不是核对。

    **偏差是相对的**（除以期望值），不是绝对的：同样差一毛钱，在一分钱的一笔
    账上是 100%，在一百块钱的一笔账上是 0.1%。任务书 T2 要的是后者那种口径。

    :raises CurrencyMismatchError: 两个数的币种不一样 —— 那不是"差了一点"，
        是**在拿两笔不同的钱互相比**，比出来的偏差没有意义。
    """
    if recorded.currency != expected.currency:
        raise CurrencyMismatchError(
            f"核对不了：记账的是 {recorded.currency}，期望的是 {expected.currency}"
        )
    if expected.amount == 0:
        # 期望是 0 而记账不是 0 —— 相对偏差在数学上不存在。按 100% 记：
        # 至少"差了一整个"。
        deviation = Decimal(0) if recorded.amount == 0 else Decimal(1)
    else:
        deviation = abs(recorded.amount - expected.amount) / expected.amount
    return CostReconciliation(
        recorded=recorded,
        expected=expected,
        deviation=deviation,
        tolerance=tolerance,
    )


@runtime_checkable
class CostObserver(Protocol):
    """把一次执行的成本记到观测系统。

    **端口在领域层，实现在基础设施层** —— 与 ``BudgetLedger`` 同一套做法：
    领域层要能回答"这次花了多少被记下来了吗"，但它不该知道 OTel 的存在。

    **记的是 :class:`CostAmount` 本身，不是两个散着的数。** 金额与币种
    分开传，就有机会只传一半 —— 而只传一半的那一半没人会发现。

    **返回值是"这次真的记下来了吗"。** 观测系统连不上时它返回 ``False`` ——
    **观测不可用不得阻塞主流程**，所以这里不抛异常；但"没记下来"这件事要
    能被问出来，否则缺的那段轨迹与"这段时间没发生事"分不开。
    """

    async def observe(self, cost: CostAmount, *, task_id: str, session_id: str) -> bool:
        """记下一次执行的成本。返回是否真的记下来了。"""
        ...


__all__ = [
    "COST_DEVIATION_TOLERANCE",
    "MONEY_QUANTUM",
    "TOKENS_PER_PRICE_UNIT",
    "CostAmount",
    "CostConverter",
    "CostError",
    "CostObserver",
    "CostReconciliation",
    "CurrencyMismatchError",
    "ModelRate",
    "RateTable",
    "TokenUsage",
    "UnknownRateError",
    "reconcile_costs",
]
