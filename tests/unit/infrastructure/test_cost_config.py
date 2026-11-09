"""单价表的外部配置 —— **换价不改代码**。

设计依据：docs/tasks.md TASK-P1-035（"币种与单价表外部可配"）。

**为什么单价表不能写死在代码里。** 模型的单价会变，汇率会变 —— 而写死的
单价表**第一次过期就开始算错**。算错的是钱，而且它不会报错：单价翻倍，算出来
的金额同样是"一个合理的金额"，只是大了两倍。月底对账才会发现。

**没配单价表就不换算**，而不是按一张内置的默认价记账。没配的意思通常是
"还没想清楚按什么价算" —— 那时候报一个错，比按一个错的价记账好。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from core.metrics.cost import CostError, TokenUsage, UnknownRateError
from infrastructure.config.cost import load_cost_converter, load_rate_table

REPO_ROOT = Path(__file__).resolve().parents[3]

VALID_TABLE = """
base_currency: USD
model_rates:
  claude-sonnet-4-6:
    currency: USD
    input_price_per_million: "3.00"
    output_price_per_million: "15.00"
exchange_rates:
  USD: "1"
  CNY: "0.14"
"""


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "rates.yaml"
    path.write_text(content, encoding="utf-8")
    return path


class TestTheRateTableFile:
    """从 YAML 读单价表。"""

    def test_the_table_is_loaded(self, tmp_path: Path) -> None:
        table = load_rate_table(_write(tmp_path, VALID_TABLE))

        assert table.base_currency == "USD"
        assert table.rate_of("claude-sonnet-4-6").input_price_per_million == Decimal("3.00")
        assert table.exchange_rate("CNY") == Decimal("0.14")

    def test_the_amounts_keep_their_precision(self, tmp_path: Path) -> None:
        """裸写的 0.1 是浮点数 —— 金额要写成字符串。"""
        table = load_rate_table(
            _write(
                tmp_path,
                """
base_currency: USD
model_rates:
  m:
    currency: USD
    input_price_per_million: "0.10"
    output_price_per_million: "0"
""",
            )
        )
        assert str(table.rate_of("m").input_price_per_million) == "0.10"

    def test_a_missing_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(CostError):
            load_rate_table(tmp_path / "nope.yaml")

    def test_a_file_that_is_not_a_mapping_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(CostError):
            load_rate_table(_write(tmp_path, "- 一\n- 二\n"))

    def test_broken_yaml_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(CostError):
            load_rate_table(_write(tmp_path, "base_currency: [未闭合\n"))

    def test_a_table_without_a_base_currency_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(CostError):
            load_rate_table(_write(tmp_path, "model_rates: {}\n"))


class TestTheConverter:
    """按路径读单价表并做成换算器。"""

    def test_no_path_means_no_converter(self) -> None:
        """**没配就不换算** —— 不猜价。"""
        assert load_cost_converter(None) is None

    def test_the_converter_uses_the_table(self, tmp_path: Path) -> None:
        converter = load_cost_converter(_write(tmp_path, VALID_TABLE))

        assert converter is not None
        assert converter.currency == "USD"

    def test_the_converter_prices_a_model(self, tmp_path: Path) -> None:
        converter = load_cost_converter(_write(tmp_path, VALID_TABLE))
        assert converter is not None

        with pytest.raises(UnknownRateError):
            converter.price(TokenUsage(input_tokens=1000, output_tokens=1000), model="gpt-9")


class TestTheShippedExample:
    """仓库里那份示例 —— **它得是真的能读的**。"""

    def test_the_example_file_loads(self) -> None:
        table = load_rate_table(REPO_ROOT / "docs" / "examples" / "cost_rates.yaml")

        assert table.base_currency == "USD"
        assert table.rate_of("claude-sonnet-4-6").output_price_per_million == 15
