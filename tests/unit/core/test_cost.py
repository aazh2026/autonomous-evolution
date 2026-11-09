"""统一成本换算器 —— **换一次，三处共用同一个数**。

设计依据：docs/tasks.md TASK-P1-035（T1 ① 同一次执行在 Adapter 报告、预算
账本、OTel span 属性三处的成本数值相等；T1 ② 换算单价变更后，三处同步变化）。

**这里测的是"数对不对"，不是"三处接没接上"。** 三处相等由
``tests/system/test_cost_reporting.py`` 从两头夹着验：这里保证换算器算出来
的数是唯一且准的，那里保证这个数原样出现在三个地方。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from core.metrics.cost import (
    COST_DEVIATION_TOLERANCE,
    MONEY_QUANTUM,
    CostAmount,
    CostConverter,
    CostError,
    CurrencyMismatchError,
    ModelRate,
    RateTable,
    TokenUsage,
    UnknownRateError,
    reconcile_costs,
)


def _table(**overrides: object) -> RateTable:
    """一张默认的单价表：记账币种 USD，另有 CNY 汇率。"""
    data: dict[str, object] = {
        "base_currency": "USD",
        "model_rates": {
            "claude-sonnet-4-6": {
                "currency": "USD",
                "input_price_per_million": "3.00",
                "output_price_per_million": "15.00",
            },
            "gpt-5": {
                "currency": "CNY",
                "input_price_per_million": "20.00",
                "output_price_per_million": "80.00",
            },
        },
        "exchange_rates": {"USD": "1", "CNY": "0.14"},
    }
    data.update(overrides)
    return RateTable.from_mapping(data)


class TestTheAmount:
    """一笔钱 —— 金额与币种缺一不可。"""

    def test_the_currency_is_normalized(self) -> None:
        """``"usd"`` 与 ``"USD"`` 是同一个币种。"""
        assert CostAmount(Decimal("1"), "usd").currency == "USD"

    def test_the_amount_is_quantized(self) -> None:
        """量化到六位 —— 与账本那一列一样。"""
        amount = CostAmount(Decimal("0.1234567"), "USD")
        assert amount.amount == Decimal("0.123457")
        assert amount.amount.as_tuple().exponent == -6

    def test_the_amount_must_not_be_negative(self) -> None:
        with pytest.raises(CostError):
            CostAmount(Decimal("-0.01"), "USD")

    def test_the_currency_must_not_be_blank(self) -> None:
        with pytest.raises(CostError):
            CostAmount(Decimal("1"), "   ")

    def test_the_amount_is_immutable(self) -> None:
        """共用一个可变对象 = 让某一处能在别人记账之后偷偷改数字。"""
        amount = CostAmount(Decimal("1"), "USD")
        with pytest.raises(FrozenInstanceError):
            amount.amount = Decimal("999")  # type: ignore[misc]

    def test_the_str_shows_both_the_amount_and_the_currency(self) -> None:
        assert str(CostAmount(Decimal("1.5"), "USD")) == "1.500000 USD"

    def test_the_quantum_is_the_one_the_ledger_uses(self) -> None:
        """账本那一列是 ``Numeric(20, 6)`` —— 量化步长必须一样。"""
        assert Decimal("0.000001") == MONEY_QUANTUM


class TestTheUsage:
    """token 用量。"""

    def test_the_total_is_the_sum(self) -> None:
        assert TokenUsage(input_tokens=100, output_tokens=50).total_tokens == 150

    def test_the_usage_must_not_be_negative(self) -> None:
        with pytest.raises(CostError):
            TokenUsage(input_tokens=-1)

    def test_the_cache_tokens_are_counted_separately(self) -> None:
        """缓存 token 各算各的 —— 并进 input 的话，这笔账就按错的价算。"""
        usage = TokenUsage(
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=900,
            cache_write_tokens=25,
        )
        assert usage.total_tokens == 1075


class TestTheModelRate:
    """模型单价 —— 按每百万 token 计价。"""

    def test_the_price_is_computed_per_million(self) -> None:
        rate = ModelRate(
            model="m",
            currency="USD",
            input_price_per_million=Decimal("3.00"),
            output_price_per_million=Decimal("15.00"),
        )
        price = rate.price_of(TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000))
        assert price == Decimal("18.00")

    def test_a_free_model_costs_nothing(self) -> None:
        rate = ModelRate("m", "USD", Decimal(0), Decimal(0))
        assert rate.price_of(TokenUsage(input_tokens=999_999)) == Decimal(0)

    def test_the_price_must_not_be_negative(self) -> None:
        with pytest.raises(CostError):
            ModelRate("m", "USD", Decimal("-1"), Decimal(0))

    def test_the_model_name_must_not_be_blank(self) -> None:
        with pytest.raises(CostError):
            ModelRate("  ", "USD", Decimal(0), Decimal(0))

    def test_the_cache_tokens_have_their_own_prices(self) -> None:
        """缓存读与缓存写各按各的价 —— 它们与输入价不是一个比例。"""
        rate = ModelRate(
            model="m",
            currency="USD",
            input_price_per_million=Decimal("3.00"),
            output_price_per_million=Decimal("15.00"),
            cache_read_price_per_million=Decimal("0.30"),
            cache_write_price_per_million=Decimal("3.75"),
        )
        price = rate.price_of(
            TokenUsage(
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                cache_read_tokens=1_000_000,
                cache_write_tokens=1_000_000,
            )
        )
        assert price == Decimal("22.05")

    def test_the_cache_prices_default_to_nothing(self) -> None:
        """不写缓存价就是 0 —— **那是一个明说的选择**，不是蒙出来的默认值。"""
        rate = ModelRate("m", "USD", Decimal("3.00"), Decimal("15.00"))
        price = rate.price_of(TokenUsage(cache_read_tokens=1_000_000))
        assert price == Decimal(0)


class TestTheRateTable:
    """单价表 —— **外部可配的一份数据**。"""

    def test_it_is_loaded_from_a_mapping(self) -> None:
        table = _table()
        assert table.base_currency == "USD"
        assert table.rate_of("claude-sonnet-4-6").input_price_per_million == Decimal("3.00")

    def test_an_unknown_model_is_an_error(self) -> None:
        """**不猜。** 猜出来的单价会算出看着合理的错账。"""
        with pytest.raises(UnknownRateError) as caught:
            _table().rate_of("gpt-9")
        assert "gpt-9" in str(caught.value)

    def test_the_error_says_what_the_table_does_have(self) -> None:
        with pytest.raises(UnknownRateError) as caught:
            _table().rate_of("gpt-9")
        assert "claude-sonnet-4-6" in str(caught.value)

    def test_an_unknown_currency_is_an_error(self) -> None:
        with pytest.raises(UnknownRateError):
            _table().exchange_rate("EUR")

    def test_the_base_currency_exchange_rate_is_one(self) -> None:
        assert _table().exchange_rate("USD") == Decimal(1)

    def test_the_base_currency_must_not_have_a_rate_other_than_one(self) -> None:
        """自相矛盾的表会算出"换算两次回到原点却不一样"的数。"""
        with pytest.raises(UnknownRateError):
            _table(exchange_rates={"USD": "2", "CNY": "0.14"})

    def test_the_exchange_rate_must_be_positive(self) -> None:
        with pytest.raises(UnknownRateError):
            _table(exchange_rates={"USD": "1", "CNY": "0"})

    def test_a_missing_base_currency_is_an_error(self) -> None:
        with pytest.raises(CostError):
            RateTable.from_mapping({"model_rates": {}})

    def test_the_prices_may_be_written_as_strings(self) -> None:
        """YAML 里裸写的 0.1 是浮点数 —— 金额要写成字符串。"""
        table = RateTable.from_mapping(
            {
                "base_currency": "USD",
                "model_rates": {
                    "m": {
                        "currency": "USD",
                        "input_price_per_million": "0.10",
                        "output_price_per_million": "0",
                    }
                },
            }
        )
        assert table.rate_of("m").input_price_per_million == Decimal("0.10")

    def test_the_cache_prices_are_read_from_the_mapping(self) -> None:
        table = RateTable.from_mapping(
            {
                "base_currency": "USD",
                "model_rates": {
                    "m": {
                        "currency": "USD",
                        "input_price_per_million": "3.00",
                        "output_price_per_million": "15.00",
                        "cache_read_price_per_million": "0.30",
                        "cache_write_price_per_million": "3.75",
                    }
                },
            }
        )
        rate = table.rate_of("m")
        assert rate.cache_read_price_per_million == Decimal("0.30")
        assert rate.cache_write_price_per_million == Decimal("3.75")

    def test_the_cache_prices_may_be_left_out(self) -> None:
        """不写就是 0 —— **那是一个明说的选择**，不是蒙出来的默认值。"""
        table = RateTable.from_mapping(
            {
                "base_currency": "USD",
                "model_rates": {
                    "m": {
                        "currency": "USD",
                        "input_price_per_million": "3.00",
                        "output_price_per_million": "15.00",
                    }
                },
            }
        )
        rate = table.rate_of("m")
        assert rate.cache_read_price_per_million == Decimal(0)
        assert rate.cache_write_price_per_million == Decimal(0)


class TestTheConverter:
    """换算器 —— **三处走的都是这一个**。"""

    def test_the_same_currency_is_left_alone(self) -> None:
        converter = CostConverter(_table())
        converted = converter.convert(CostAmount(Decimal("1.25"), "USD"))
        assert converted == CostAmount(Decimal("1.25"), "USD")

    def test_another_currency_is_converted(self) -> None:
        """1 CNY = 0.14 USD。"""
        converter = CostConverter(_table())
        converted = converter.convert(CostAmount(Decimal("1"), "CNY"))
        assert converted.amount == Decimal("0.140000")
        assert converted.currency == "USD"

    def test_the_conversion_is_precise(self) -> None:
        """金额差最后几位就是差钱 —— 不能用浮点数。"""
        converter = CostConverter(_table())
        converted = converter.convert(CostAmount(Decimal("1"), "CNY"))
        assert str(converted.amount) == "0.140000"

    def test_the_usage_is_priced_and_converted(self) -> None:
        """用量 → 金额 → 记账币种，一步到位。"""
        converter = CostConverter(_table())
        priced = converter.price(
            TokenUsage(input_tokens=1_000_000, output_tokens=0),
            model="claude-sonnet-4-6",
        )
        assert priced.amount == Decimal("3.000000")
        assert priced.currency == "USD"

    def test_a_model_priced_in_another_currency_is_converted(self) -> None:
        """计价币种与记账币种不同时，换算在最后做。"""
        converter = CostConverter(_table())
        priced = converter.price(
            TokenUsage(input_tokens=1_000_000, output_tokens=0),
            model="gpt-5",
        )
        # 20 CNY × 0.14 = 2.8 USD
        assert priced.amount == Decimal("2.800000")
        assert priced.currency == "USD"

    def test_the_price_of_an_unknown_model_is_an_error(self) -> None:
        converter = CostConverter(_table())
        with pytest.raises(UnknownRateError):
            converter.price(TokenUsage(), model="gpt-9")

    def test_the_target_currency_can_be_given(self) -> None:
        converter = CostConverter(_table())
        converted = converter.convert(CostAmount(Decimal("1"), "CNY"), to_currency="CNY")
        assert converted == CostAmount(Decimal("1"), "CNY")

    def test_the_base_currency_is_the_accounting_currency(self) -> None:
        assert CostConverter(_table()).currency == "USD"


class TestTheCurrencyMismatch:
    """币种对不上是错 —— **换个币种记一笔账比记错数字更坏**。"""

    def test_the_mismatch_error_is_a_cost_error(self) -> None:
        assert issubclass(CurrencyMismatchError, CostError)

    def test_the_converter_reports_the_mismatch(self) -> None:
        table = RateTable.from_mapping({"base_currency": "USD", "exchange_rates": {"USD": "1"}})
        with pytest.raises(UnknownRateError):
            CostConverter(table).convert(CostAmount(Decimal("1"), "CNY"))


class TestTheReconciliation:
    """核对 —— **记账的数 vs. 实际调用量推算的数**（T2：偏差 < 2%）。"""

    def test_two_equal_amounts_have_no_deviation(self) -> None:
        result = reconcile_costs(
            recorded=CostAmount(Decimal("1.00"), "USD"),
            expected=CostAmount(Decimal("1.00"), "USD"),
        )
        assert result.deviation == Decimal(0)
        assert result.within_tolerance is True

    def test_the_deviation_is_relative(self) -> None:
        """**相对**偏差 —— 同样差一毛钱，在两笔不同规模的账上不是一回事。"""
        small = reconcile_costs(
            recorded=CostAmount(Decimal("1.01"), "USD"),
            expected=CostAmount(Decimal("1.00"), "USD"),
        )
        large = reconcile_costs(
            recorded=CostAmount(Decimal("101.00"), "USD"),
            expected=CostAmount(Decimal("100.00"), "USD"),
        )
        assert small.deviation == Decimal("0.01")
        assert large.deviation == Decimal("0.01")

    def test_the_deviation_does_not_care_which_side_is_bigger(self) -> None:
        over = reconcile_costs(
            recorded=CostAmount(Decimal("1.05"), "USD"),
            expected=CostAmount(Decimal("1.00"), "USD"),
        )
        under = reconcile_costs(
            recorded=CostAmount(Decimal("0.95"), "USD"),
            expected=CostAmount(Decimal("1.00"), "USD"),
        )
        assert over.deviation == under.deviation

    def test_a_stale_rate_table_shows_up(self) -> None:
        """单价表过期了 —— 偏差会**超过容忍线**，这就是它该有的样子。"""
        result = reconcile_costs(
            recorded=CostAmount(Decimal("1.20"), "USD"),
            expected=CostAmount(Decimal("1.00"), "USD"),
        )
        assert result.deviation == Decimal("0.20")
        assert result.within_tolerance is False

    def test_the_tolerance_is_two_percent(self) -> None:
        """任务书 T2 给的数 —— 2%。"""
        assert Decimal("0.02") == COST_DEVIATION_TOLERANCE

    def test_two_percent_is_inside_and_above_is_outside(self) -> None:
        inside = reconcile_costs(
            recorded=CostAmount(Decimal("1.02"), "USD"),
            expected=CostAmount(Decimal("1.00"), "USD"),
        )
        outside = reconcile_costs(
            recorded=CostAmount(Decimal("1.021"), "USD"),
            expected=CostAmount(Decimal("1.00"), "USD"),
        )
        assert inside.within_tolerance is True
        assert outside.within_tolerance is False

    def test_a_different_tolerance_can_be_given(self) -> None:
        result = reconcile_costs(
            recorded=CostAmount(Decimal("1.05"), "USD"),
            expected=CostAmount(Decimal("1.00"), "USD"),
            tolerance=Decimal("0.10"),
        )
        assert result.within_tolerance is True

    def test_cannot_reconcile_two_currencies(self) -> None:
        """拿两笔不同的钱互相比 —— 比出来的偏差没有意义。"""
        with pytest.raises(CurrencyMismatchError):
            reconcile_costs(
                recorded=CostAmount(Decimal("1.00"), "USD"),
                expected=CostAmount(Decimal("7.00"), "CNY"),
            )

    def test_two_zeroes_have_no_deviation(self) -> None:
        result = reconcile_costs(
            recorded=CostAmount(Decimal("0"), "USD"),
            expected=CostAmount(Decimal("0"), "USD"),
        )
        assert result.deviation == Decimal(0)
        assert result.within_tolerance is True

    def test_a_recorded_cost_against_nothing_is_a_hundred_percent(self) -> None:
        """期望是 0 而记账不是 0 —— 相对偏差在数学上不存在，按 100% 记。"""
        result = reconcile_costs(
            recorded=CostAmount(Decimal("3.00"), "USD"),
            expected=CostAmount(Decimal("0"), "USD"),
        )
        assert result.deviation == Decimal(1)
        assert result.within_tolerance is False

    def test_the_reconciliation_keeps_both_amounts(self) -> None:
        """结论要带着证据 —— 事后能核对是哪两个数对不上。"""
        recorded = CostAmount(Decimal("1.05"), "USD")
        expected = CostAmount(Decimal("1.00"), "USD")
        result = reconcile_costs(recorded=recorded, expected=expected)
        assert result.recorded == recorded
        assert result.expected == expected
