"""金额 —— 带币种的十进制数。

**金额没有币种就没法和别处比。** 这一模块刻意把币种捆在金额上：构造出来
的金额不可变，币种改不了。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: 金额的量化步长 —— 与账本那一列一样。
MONEY_QUANTUM = Decimal("0.01")


class Money:
    """一笔钱 —— 金额与币种缺一不可。"""

    __slots__ = ("amount", "currency")

    def __init__(self, amount, currency):
        if not isinstance(amount, Decimal):
            amount = Decimal(str(amount))
        if not currency or not str(currency).strip():
            raise ValueError("币种不能是空白")
        self.amount = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        self.currency = str(currency).strip().upper()

    def __repr__(self):
        return f"Money({self.amount}, {self.currency!r})"

    def __eq__(self, other):
        if not isinstance(other, Money):
            return NotImplemented
        return self.amount == other.amount and self.currency == other.currency

    def __hash__(self):
        return hash((self.amount, self.currency))

    def add(self, other):
        """相加。**币种不一样就报错** —— 换个币种记一笔账比记错数字更坏。"""
        self._assert_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def multiply(self, factor):
        """乘一个系数。"""
        return Money(self.amount * Decimal(str(factor)), self.currency)

    def is_zero(self):
        return self.amount == Decimal("0")

    def _assert_same_currency(self, other):
        if other.currency != self.currency:
            raise ValueError(
                f"币种不一样，不能相加：{self.currency} 与 {other.currency}"
            )


def total(amounts):
    """把若干笔钱加起来。"""
    if not amounts:
        raise ValueError("空列表没有合计")
    result = amounts[0]
    for amount in amounts[1:]:
        result = result.add(amount)
    return result


__all__ = ["MONEY_QUANTUM", "Money", "total"]
