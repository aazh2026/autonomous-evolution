"""TASK-P1-035 的反向验证 —— 逐个改坏成本口径，证明测试抓得住。

做法跟仓库里其它任务的反向验证一样：**改生产代码，然后跑测试，看它红。**
改完一定还原（``finally`` 里）。

**为什么这次改坏的是九处不同的文件。** TASK-P1-035 的交付物不是一段代码，
而是**一条口径穿过六个环节**：换算器（``core/metrics/cost.py``）、适配器
（``executors/cli/adapter.py``）、事件词汇表（``executors/claude_code/
events.py``）、执行结果（``executors/base.py``）、编排层
（``orchestration/loop_a.py`` 与 ``orchestration/cost_accounting.py``）、账本
与观测（``core/policy/budget.py`` 与 ``infrastructure/observability/``）。

**只改一处而测试全绿，就说明这条口径没被守住。** 所以每条变异都改坏一个环
节，看有没有人喊。

**每一条变异都对着一条具体的设计要求。** 抓不住就说明那条要求没被验到。

**为什么有的变异改的是"看起来无害"的地方。** ③ 把量化步长从六位改成两位、
⑥ 把账本记的数乘以二、⑯ 把偏差恒为 0 —— 这些改动都不会让任何东西报错，只
会让"三处相等""偏差 < 2%"这两句话悄悄变成假的。**这类错才是这套口径真正
要防的。**
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: (编号, 目标文件, 说明, 原文, 改后)
MUTANTS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "①",
        "core/metrics/cost.py",
        "量化步长从六位改成两位 —— 账本那一列（Numeric(20, 6)）对不上了",
        'MONEY_QUANTUM: Decimal = Decimal("0.000001")',
        'MONEY_QUANTUM: Decimal = Decimal("0.01")',
    ),
    (
        "②",
        "core/metrics/cost.py",
        "币种不归一 —— “usd”与“USD”成了两个币种",
        "    normalized = currency.strip().upper()",
        "    normalized = currency.strip()",
    ),
    (
        "③",
        "core/metrics/cost.py",
        "换算时忽略汇率 —— 数字看着正常，钱算错了",
        "        return CostAmount(cost.amount * rate, target)",
        "        return CostAmount(cost.amount, target)",
    ),
    (
        "④",
        "core/metrics/cost.py",
        "单价表里没有的模型也给个价 —— 猜价",
        "        except KeyError as error:\n"
        '            known = "、".join(sorted(self.model_rates)) or "（空）"\n'
        "            raise UnknownRateError("
        'f"单价表里没有模型 {model} 的单价；表里有：{known}") from error',
        "        except KeyError:\n"
        "            return ModelRate(\n"
        "                model=model,\n"
        "                currency=self.base_currency,\n"
        "                input_price_per_million=Decimal(0),\n"
        "                output_price_per_million=Decimal(0),\n"
        "            )",
    ),
    (
        "⑤",
        "core/metrics/cost.py",
        "计价币种不换算 —— 20 CNY 的模型被当成 20 USD 记进账",
        "        amount = CostAmount(rate.price_of(usage), rate.currency)\n"
        "        return self.convert(amount)",
        "        amount = CostAmount(rate.price_of(usage), rate.currency)\n        return amount",
    ),
    (
        "⑥",
        "core/policy/budget.py",
        "账本不核币种 —— 换个币种记一笔账没人拦",
        "        if cost.currency != self._budget.currency:",
        "        if False:",
    ),
    (
        "⑦",
        "core/policy/budget.py",
        "账本记的数不是换算好的那一个 —— 三处悄悄不一样了",
        "            amount=cost.amount,",
        "            amount=cost.amount * 2,",
    ),
    (
        "⑧",
        "executors/cli/adapter.py",
        "适配器不换算 —— 三处虽然相等，换算没了",
        "        return self._cost_converter.convert(amount)",
        "        return amount",
    ),
    (
        "⑨",
        "executors/base.py",
        "从执行结果派生的金额不是同一个数 —— 账本与适配器对不上",
        "        return CostAmount(self.cost, self.currency)",
        "        return CostAmount(self.cost * 2, self.currency)",
    ),
    (
        "⑩",
        "orchestration/cost_accounting.py",
        "超预算不当回事 —— 已经做完的执行变成失败",
        "        except BudgetExhausted:",
        "        except ValueError:",
    ),
    (
        "⑪",
        "orchestration/loop_a.py",
        "编排层不记账 —— 执行完了账本还是空的",
        "    if handlers.cost_accountant is not None:",
        "    if False:",
    ),
    (
        "⑫",
        "infrastructure/observability/tracing.py",
        "span 属性写成浮点数 —— 精度掉在最后几位",
        "        self._span.set_attribute(COST_AMOUNT_ATTRIBUTE, str(cost.amount))",
        "        self._span.set_attribute(COST_AMOUNT_ATTRIBUTE, float(cost.amount))",
    ),
    (
        "⑬",
        "infrastructure/observability/tracing.py",
        "span 属性不记币种 —— “多少钱”这个问题没有答案",
        "        self._span.set_attribute(COST_CURRENCY_ATTRIBUTE, cost.currency)",
        '        self._span.set_attribute(COST_CURRENCY_ATTRIBUTE, "")',
    ),
    (
        "⑭",
        "infrastructure/config/cost.py",
        "没配单价表也按默认价换算 —— 猜价",
        "    if path is None:\n        return None",
        '    if path is None:\n        return CostConverter(RateTable(base_currency="USD"))',
    ),
    (
        "⑮",
        "core/metrics/cost.py",
        "缓存读不计价 —— 按用量推算出来的钱偏小，偏差核对看不出来",
        "            + usage.cache_read_tokens / TOKENS_PER_PRICE_UNIT "
        "* self.cache_read_price_per_million",
        "            + Decimal(0)",
    ),
    (
        "⑯",
        "core/metrics/cost.py",
        "偏差恒为 0 —— 核对成了自证",
        "        deviation = abs(recorded.amount - expected.amount) / expected.amount",
        "        deviation = Decimal(0)",
    ),
    (
        "⑰",
        "core/metrics/cost.py",
        "偏差算成绝对值 —— 差一毛钱在一分钱的一笔账上是 100%",
        "        deviation = abs(recorded.amount - expected.amount) / expected.amount",
        "        deviation = abs(recorded.amount - expected.amount)",
    ),
    (
        "⑱",
        "executors/claude_code/events.py",
        "缓存 token 不读 —— 实际调用量少了一半",
        "        cache_read_tokens=cache_read or 0,\n        cache_write_tokens=cache_write or 0,",
        "        cache_read_tokens=0,\n        cache_write_tokens=0,",
    ),
    (
        "⑲",
        "executors/claude_code/events.py",
        "模型名不读 —— 找不到单价，核对根本做不成",
        '                model = _text(raw.get("model")) or model',
        "                model = None",
    ),
    (
        "⑳",
        "executors/cli/adapter.py",
        "不核对 —— 偏差永远是 None",
        "        return reconcile_costs(recorded=booked, expected=expected).deviation",
        "        return None",
    ),
    (
        "㉑",
        "executors/cli/adapter.py",
        "单价表里没有这个模型就把整次执行掀翻",
        "        except UnknownRateError:\n            return None",
        "        except UnknownRateError:\n            raise",
    ),
)

#: 跑测试的命令。这一批全是单元与系统测试 —— **不需要数据库**。
CMD = [
    str(ROOT / ".venv/bin/python"),
    "-m",
    "pytest",
    "tests/unit/core/test_cost.py",
    "tests/unit/orchestration/test_cost_accounting.py",
    "tests/unit/orchestration/test_loop_a.py",
    "tests/unit/infrastructure/test_cost_observer.py",
    "tests/unit/infrastructure/test_cost_config.py",
    "tests/unit/executors/",
    "tests/system/test_cost_reporting.py",
    "-o",
    "addopts=--strict-markers --strict-config -p no:langsmith_plugin",
    "-q",
]


def _run() -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("ASES_TEST_DB_NAME", "ases_test_mine")
    done = subprocess.run(CMD, cwd=ROOT, capture_output=True, text=True, env=env, check=False)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _summary(output: str) -> str:
    """取测试输出最后一行有意义的。"""
    for line in reversed(output.strip().splitlines()):
        if line.strip():
            return line.strip()
    return ""


def main() -> int:
    # 每个目标文件读一次原文 —— 还原时用。
    originals: dict[str, str] = {}
    for _, target, *_ in MUTANTS:
        if target not in originals:
            originals[target] = (ROOT / target).read_text(encoding="utf-8")

    code, output = _run()
    if code != 0:
        print("基线就红了 —— 先修好再谈变异：")
        print(output[-3000:])
        return 1
    print(f"基线绿（{_summary(output)}）")

    caught = 0
    missed: list[str] = []
    try:
        for number, target, why, before, after in MUTANTS:
            original = originals[target]
            if before not in original:
                missed.append(f"{number}（原文没找到，变异没生效：{target}）")
                print(f"{number} 跳过 —— 原文没找到（脚本过期了）")
                continue
            if before == after:
                missed.append(f"{number}（变异与原文相同，等价变异）")
                print(f"{number} 等价变异 —— {why}")
                continue
            path = ROOT / target
            path.write_text(original.replace(before, after, 1), encoding="utf-8")
            try:
                code, output = _run()
            finally:
                path.write_text(original, encoding="utf-8")
            if code != 0:
                caught += 1
                tail = [line for line in output.splitlines() if line.startswith("FAILED")]
                print(f"{number} 抓住了 —— {why}（{len(tail)} 条用例转红）")
            else:
                missed.append(f"{number}（{why}）")
                print(f"{number} 没抓住 —— {why}  ← 这条要求没被验到")
    finally:
        for target, original in originals.items():
            (ROOT / target).write_text(original, encoding="utf-8")

    print()
    print(f"抓住 {caught}/{len(MUTANTS)}")
    for item in missed:
        print(f"  没抓住：{item}")
    return 0 if caught == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main())
