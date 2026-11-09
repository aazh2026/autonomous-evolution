"""``tinylib.money`` 的测试。"""

from decimal import Decimal

from tinylib.money import Money, total


def test_amount_is_quantized_to_cents():
    assert Money(Decimal("1.005"), "usd").amount == Decimal("1.01")


def test_currency_is_normalized():
    assert Money(1, "usd").currency == "USD"


def test_add_two_amounts():
    assert Money(1, "USD").add(Money(2, "USD")) == Money(3, "USD")


def test_add_rejects_a_different_currency():
    try:
        Money(1, "USD").add(Money(2, "CNY"))
    except ValueError:
        return
    raise AssertionError("币种不一样应当报错")


def test_multiply_by_a_factor():
    assert Money(2, "USD").multiply(3) == Money(6, "USD")


def test_total_of_three_amounts():
    assert total([Money(1, "USD"), Money(2, "USD"), Money(3, "USD")]) == Money(6, "USD")


def test_total_of_nothing_raises():
    try:
        total([])
    except ValueError:
        return
    raise AssertionError("空列表应当报错")
