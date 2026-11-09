"""单价表的外部配置 —— **一份数据，不是一段代码**。

设计依据：docs/tasks.md TASK-P1-035（"币种与单价表外部可配"）。

**为什么单价表不能写死在代码里。** 模型的单价会变，汇率会变 —— 而写死的
单价表**第一次过期就开始算错**。算错的是钱，而且它不会报错：单价翻倍，
算出来的金额同样是"一个合理的金额"，只是大了两倍。月底对账才会发现，
那时候已经花出去好几个月了。

**所以单价表是外部的一份 YAML。** 换价不改代码，改配置。

**计价币种与记账币种是两回事。** 单价表里的 ``base_currency`` 是**记账
币种**（换算的目标），而各模型单价的 ``currency`` 是**计价币种**（那个
价格表的原币）。两者不同时，换算由
:class:`core.metrics.cost.CostConverter` 负责 —— 而换算只做一次。

**单价表缺失时不猜。** 没配单价表就**不换算**（:func:`load_cost_converter`
返回 ``None``），而不是用一张内置的默认表。内置表看起来省事，但"没配"的
意思通常是"还没想清楚按什么价算" —— 那时候报一个错，比按一个错的价记账好。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from core.metrics.cost import CostConverter, CostError, RateTable


def load_rate_table(path: Path) -> RateTable:
    """从一份 YAML 文件读单价表。

    文件格式::

        base_currency: USD
        model_rates:
          claude-sonnet-4-6:
            currency: USD
            input_price_per_million: "3.00"
            output_price_per_million: "15.00"
        exchange_rates:
          USD: "1"
          CNY: "0.14"

    **金额一律写成字符串。** YAML 里裸写的 ``0.1`` 是浮点数，读进来是
    ``0.1000000000000000055511151231257827…`` —— 而金额差最后几位就是
    差钱。写字符串，读进来就是十进制。
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CostError(f"单价表读不出来：{path}（{error}）") from error
    except yaml.YAMLError as error:
        raise CostError(f"单价表不是合法的 YAML：{path}（{error}）") from error

    if not isinstance(raw, dict):
        raise CostError(f"单价表必须是一个映射：{path}")
    return RateTable.from_mapping(raw)


def load_cost_converter(path: Path | None) -> CostConverter | None:
    """按路径读单价表并做成换算器。

    **路径为 ``None`` 就返回 ``None``** —— 没有单价表就没有换算器，调用方
    原样保留适配器报出来的原始金额与币种。**不猜价。**

    :raises CostError: 文件在，但内容不合法 —— 那是要有人来改的，不该
        悄悄按缺省价记账。
    """
    if path is None:
        return None
    return CostConverter(load_rate_table(path))


__all__ = [
    "load_cost_converter",
    "load_rate_table",
]
