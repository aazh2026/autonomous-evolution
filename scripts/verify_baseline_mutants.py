"""TASK-P1-072 的反向验证 —— 逐个改坏基线口径，证明测试抓得住。

做法跟仓库里其它任务的反向验证一样：**改生产代码，然后跑测试，看它红。**
改完一定还原（``finally`` 里）。

**为什么这次改坏的是两个不同的文件。** TASK-P1-072 的交付物不是一段代码，
而是**一条口径分两截**：口径本身（``evaluation/baselines.py``）与采集
（``scripts/collect_baselines.py``）。**只改一处而测试全绿，就说明那条口径
没被守住。**

**每一条变异都对着一条具体的设计要求。** 抓不住就说明那条要求没被验到。

**为什么这么多变异改的是"看起来无害"的地方。** ③ 把"没有样本"说成"基线是
0"、④ 把"没法判断"说成"超了"、⑦ 给一个 0 元的预算、⑬ 把"没读到用量"说成
"用了 0 个 token" —— 这些改动都不会让任何东西报错，只会让"基线未建立"这句
话悄悄变成假的。**这类错才是这套口径真正要防的。**
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
        "evaluation/baselines.py",
        "空样本的中位数返回 0 —— “还没采到数据”与“采到了，是 0”混成一件",
        '        raise BaselineError("中位数不能对空样本算")',
        "        return Decimal(0)",
    ),
    (
        "②",
        "evaluation/baselines.py",
        "偶数个样本取下中位数 —— 一半样本落在基线之上，“一半比基线便宜”成了假的",
        "    value = ordered[middle] if count % 2 == 1 "
        "else (ordered[middle - 1] + ordered[middle]) / 2",
        "    value = ordered[middle]",
    ),
    (
        "③",
        "evaluation/baselines.py",
        "没有样本也说有基线（值 0）—— “基线未建立”变成了“基线是 0”",
        "        if not values:\n            return None\n        quantum = QUANTA[metric]",
        "        if not values:\n"
        "            return Baseline(\n"
        "                task_kind=task_kind,\n"
        "                metric=metric,\n"
        "                median=Decimal(0),\n"
        "                sample_count=0,\n"
        "                currency=self._currency if metric is BaselineMetric.COST else None,\n"
        "            )\n"
        "        quantum = QUANTA[metric]",
    ),
    (
        "④",
        "evaluation/baselines.py",
        "没有基线时判成“超了” —— “没法判断”被说成了“超了”",
        "        if self.state is not MetricState.COMPUTED:\n"
        "            return None\n"
        "        if self.baseline is None or self.observed is None:",
        "        if self.state is not MetricState.COMPUTED:\n"
        "            return False\n"
        "        if self.baseline is None or self.observed is None:",
    ),
    (
        "⑤",
        "evaluation/baselines.py",
        "没有基线时报倍数 0 —— 看板上显示“在基线的 0 倍”",
        "        if self.baseline is None or self.baseline == 0 or self.observed is None:\n"
        "            return None\n"
        "        return self.observed / self.baseline",
        "        if self.baseline is None or self.baseline == 0 or self.observed is None:\n"
        "            return Decimal(0)\n"
        "        return self.observed / self.baseline",
    ),
    (
        "⑥",
        "evaluation/baselines.py",
        "话里没有“基线未建立”五个字 —— 看板照着显示就没了",
        '            return f"{self.task_kind} 的{name}基线未建立 —— {BASELINE_MISSING_TEXT}"',
        '            return f"{self.task_kind} 的{name}是 0"',
    ),
    (
        "⑦",
        "evaluation/baselines.py",
        "缺基线时给一个 0 元的每日上限 —— 系统第一天就停摆，看板显示“预算正常”",
        "            state=MetricState.BASELINE_MISSING,\n            max_daily_cost=None,",
        "            state=MetricState.BASELINE_MISSING,\n            max_daily_cost=Decimal(0),",
    ),
    (
        "⑧",
        "evaluation/baselines.py",
        "缺基线时也给出预算 —— 0 元的预算看上去很安全",
        "        if self.state is not MetricState.COMPUTED:\n"
        "            return None\n"
        "        assert self.max_daily_cost is not None",
        "        if self.state is not MetricState.COMPUTED:\n"
        '            return Budget(currency=self.currency or "USD", max_daily_cost=Decimal(0))\n'
        "        assert self.max_daily_cost is not None",
    ),
    (
        "⑨",
        "evaluation/baselines.py",
        "两种币种混在一张表里也不报错 —— 中位数对两个项目的钱没有意义",
        "        if len(currencies) > 1:\n"
        '            known = "、".join(sorted(currencies))\n'
        "            raise CurrencyMismatchError(\n"
        '                f"基线表里的金额不是一个币种：{known} —— 中位数对两个项目的钱没有意义"\n'
        "            )",
        "        if False:\n            pass",
    ),
    (
        "⑩",
        "evaluation/baselines.py",
        "比阈值时不核币种 —— 拿两笔不同的钱互相比",
        "            if measurement.cost.currency != baseline.currency:",
        "            if False:",
    ),
    (
        "⑪",
        "evaluation/baselines.py",
        "金额写成浮点数落盘 —— 钱差最后几位就是差钱",
        '        "cost": None if item.cost is None else str(item.cost.amount),',
        '        "cost": None if item.cost is None else float(item.cost.amount),',
    ),
    (
        "⑫",
        "evaluation/baselines.py",
        "不认识的键不报错 —— 拼错一个字段，那一列静默消失，而消失看起来像“没测过”",
        '        unknown = set(raw) - {"pilot", "currency", "measurements"}\n'
        "        if unknown:\n"
        "            raise BaselineError(f\"基线表里有不认识的键：{'、'.join(sorted(unknown))}\")",
        '        unknown = set(raw) - {"pilot", "currency", "measurements"}\n        if False:\n'
        "            raise BaselineError(f\"基线表里有不认识的键：{'、'.join(sorted(unknown))}\")",
    ),
    (
        "⑬",
        "evaluation/baselines.py",
        "没读到用量就说用了 0 个 token —— token 基线偏低，预算偏松",
        "                return None if self.usage is None else Decimal(self.usage.total_tokens)",
        "                return Decimal(self.usage.total_tokens if self.usage else 0)",
    ),
    (
        "⑭",
        "evaluation/baselines.py",
        "没测到覆盖率就报 0 —— 与“测出来是 0”混成一件",
        "            case BaselineMetric.COVERAGE:\n                return self.coverage",
        "            case BaselineMetric.COVERAGE:\n"
        "                return self.coverage if self.coverage is not None else Decimal(0)",
    ),
    (
        "⑮",
        "evaluation/baselines.py",
        "模块覆盖率取最小值而不是中位数 —— 一个坏样本就把基线压到底",
        "                median=median(ratios, quantum=QUANTA[BaselineMetric.COVERAGE]),",
        "                median=min(ratios, default=Decimal(0)),",
    ),
    (
        "⑯",
        "evaluation/baselines.py",
        "预算按平均值算而不是中位数 —— 一次跑飞的任务就把预算拽高一倍",
        "    per_task = median(costs, quantum=MONEY_QUANTUM)",
        "    per_task = (sum(costs) / len(costs)).quantize(MONEY_QUANTUM)",
    ),
    (
        "⑰",
        "scripts/collect_baselines.py",
        "没读到用量就记成四个 0 —— “没用 token”与“我们没读到”混成一件",
        "    if all(value is None for value in counts.values()):\n        return None",
        "    if all(value is None for value in counts.values()):\n        return TokenUsage()",
    ),
    (
        "⑱",
        "scripts/collect_baselines.py",
        "缓存 token 并进输入 token —— 按用量推算出来的钱偏小，而多算出来的没人喊",
        '        input_tokens=counts["input_tokens"] or 0,\n'
        '        output_tokens=counts["output_tokens"] or 0,\n'
        '        cache_read_tokens=counts["cache_read_input_tokens"] or 0,\n'
        '        cache_write_tokens=counts["cache_creation_input_tokens"] or 0,',
        '        input_tokens=(counts["input_tokens"] or 0)\n'
        '        + (counts["cache_read_input_tokens"] or 0)\n'
        '        + (counts["cache_creation_input_tokens"] or 0),\n'
        '        output_tokens=counts["output_tokens"] or 0,\n'
        "        cache_read_tokens=0,\n"
        "        cache_write_tokens=0,",
    ),
    (
        "⑲",
        "scripts/collect_baselines.py",
        "只留整体覆盖率、丢掉按模块的那份 —— 总覆盖率会掩盖“新增模块覆盖率为 0”",
        "            modules[_module_name(str(path))] = _fraction(percent)",
        "            modules = {}",
    ),
)

#: 跑测试的命令。这一批全是单元与系统测试 —— **不需要数据库、不花钱**。
CMD = [
    str(ROOT / ".venv/bin/python"),
    "-m",
    "pytest",
    "tests/unit/evaluation/test_baselines.py",
    "tests/system/test_baseline_collection.py",
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
