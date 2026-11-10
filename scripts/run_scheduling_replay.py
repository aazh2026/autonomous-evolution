"""自适应调度回放 —— 驱动脚本：把真跑出来的记录排成决策位，回放一遍。

设计依据：docs/tasks.md TASK-P4-040（验收条件"**调度器在实测中优于静态分配的
对照组**"；验证 T2）、``TASK-P4-030``（二维能力矩阵 —— 那些记录就是这一层的
原料）、``TASK-P2-018``（一维评测 —— 口径不许两套）。

**这个脚本不跑任何东西。** 它只读 ``docs/evaluation/capability-matrix-runs.jsonl``
—— 那份记录是真跑出来的 —— 然后按"不许看未来"的次序回放一遍调度策略，并把结
论画成看板。**回放里的每一个数都是实测的**，只有"这一步挑哪个格子"是反事实的。

**它不能替代真跑。** 回放回答的是"同样这批真实运行，如果当初按这个策略调度，
结果会是什么"。要验收"调度器在线上更好"，还得另跑一次对照实验。

**它顺手产出运行时调度器的历史。** 回放用的那些观测会写成
``docs/evaluation/scheduling-observations.jsonl`` —— **格式由
:func:`core.policy.scheduling.observation_to_dict` 说了算**，而运行时那一层用
:func:`orchestration.adaptive_scheduler.load_observations` 读回来。两边各写一份
转换，迟早会对不上，而对不上的那几列会静悄悄地变成 0。

**用法**::

    python -m scripts.run_scheduling_replay
    python -m scripts.run_scheduling_replay --history chosen
    python -m scripts.run_scheduling_replay --tolerance 0.1 --explore-every 3
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from core.policy.scheduling import DEFAULT_TOLERANCE, SchedulingPolicy, observation_to_dict
from evaluation.agents.scheduling_replay import (
    HISTORY_CHOSEN,
    HISTORY_SHARED,
    BaselinePort,
    RecordedRun,
    ReplayReport,
    RoundRobinBaseline,
    StaticBaseline,
    build_slots,
    recorded_runs,
    replay,
)
from evaluation.executors.benchmark import PriceTable, RunRecord
from scripts.run_capability_matrix import PRICE_TABLES
from scripts.run_executor_benchmark import record_from_dict

#: 原始记录（JSONL）—— **二维能力矩阵那一份**。
DEFAULT_LOG = Path("docs/evaluation/capability-matrix-runs.jsonl")

#: 看板（Markdown）。
DEFAULT_BOARD = Path("docs/evaluation/scheduling-replay-board.md")

#: 观测存档（JSONL）—— **运行时调度器的历史**。
DEFAULT_OBSERVATIONS = Path("docs/evaluation/scheduling-observations.jsonl")

#: 冷启动偏好 —— **事先声明的默认组合**。
DEFAULT_PREFERENCE = "claude-code/MiniMax-M3"

#: 静态对照线：始终用默认组合。
DEFAULT_BASELINE = "declared-default"

#: 静态对照线：价格表上最便宜的那个模型，配默认执行器。
CHEAPEST_BASELINE = "cheapest-a-priori"

#: 静态对照线：所有格子轮着来。
ROUND_ROBIN_BASELINE = "round-robin"

#: 容差曲线上的那几个点 —— **容差是 knob，要看它把达成度与成本推向哪儿**。
CURVE_TOLERANCES: tuple[Decimal, ...] = (
    Decimal("0"),
    Decimal("0.05"),
    Decimal("0.1"),
    Decimal("0.2"),
    Decimal("0.3"),
)


def read_log(path: Path) -> list[dict[str, object]]:
    """读原始记录 —— **解不出来的行当没有**。"""
    if not path.exists():
        return []
    entries: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def records_from_log(entries: Sequence[Mapping[str, object]]) -> list[RunRecord]:
    """把原始记录还原成运行记录 —— **模型一起还原**。

    **没记模型的运行切不进任何格子** —— 与能力矩阵同一套规矩，所以这里直接
    丢掉它们，而不是让它们混进"全局"。
    """
    records: list[RunRecord] = []
    for entry in entries:
        if not entry.get("model"):
            continue
        records.append(replace(record_from_dict(entry), model=str(entry["model"])))
    return records


def cheapest_cell(prices: Mapping[str, PriceTable], executor: str) -> str | None:
    """价格表上最便宜的那个组合 —— **事先就知道的那一个**。

    **为什么按价格表挑，而不是按实测的均摊成本挑。** 实测均摊成本要用整批数据
    才算得出来 —— 那就是**事后**挑的，对照线占了这个便宜就不公平了。价格表是
    公开的、事先就有的，所以"按价格表挑最便宜的"是生产上真能用的静态分配。
    """
    best: tuple[Decimal, str] | None = None
    for model, table in prices.items():
        weight = table.input + table.output
        if best is None or weight < best[0]:
            best = (weight, model)
    if best is None:
        return None
    return f"{executor}/{best[1]}"


def _ceilings(costs: Sequence[Decimal], reference: Decimal | None) -> list[Decimal]:
    """预算上限 —— **两个：全体的中位数，与静态默认的均值**。

    **为什么要有这两个。** 全体中位数是"这批运行的一般价位"；静态默认的均值是
    "照老办法做一件事要多少钱"。**在老办法的价位内，自适应能做对几件事** ——
    这才是能拿去跟人谈的那个数。
    """
    values = sorted(costs)
    ceilings: list[Decimal] = []
    if values:
        middle = len(values) // 2
        if len(values) % 2 == 1:
            ceilings.append(values[middle])
        else:
            ceilings.append((values[middle - 1] + values[middle]) / Decimal(2))
    if reference is not None and reference not in ceilings:
        ceilings.append(reference)
    return ceilings


def build_report(
    entries: Sequence[Mapping[str, object]],
    *,
    policy: SchedulingPolicy,
    history: str,
    prices: Mapping[str, PriceTable],
    baseline_cell: str,
) -> tuple[ReplayReport, list[Decimal]]:
    """回放一遍 —— **返回 (报告, 预算上限)**。"""
    records = records_from_log(entries)
    runs = recorded_runs(records, prices)
    slots = build_slots(runs)
    cells = tuple(sorted({run.cell for run in runs}))

    baselines: list[BaselinePort] = [
        StaticBaseline(
            name=DEFAULT_BASELINE,
            cell=baseline_cell,
            note="始终用事先声明的默认组合 —— 静态分配的原样",
        ),
    ]
    cheapest = cheapest_cell(prices, baseline_cell.split("/", maxsplit=1)[0])
    if cheapest is not None and cheapest != baseline_cell:
        baselines.append(
            StaticBaseline(
                name=CHEAPEST_BASELINE,
                cell=cheapest,
                note="按公开价目表挑最便宜的模型 —— 事先就知道的静态分配",
            )
        )
    if cells:
        baselines.append(
            RoundRobinBaseline(
                name=ROUND_ROBIN_BASELINE,
                cells=cells,
                note="所有格子轮着来 —— 静态分配里最讲道理的一种",
            )
        )

    report = replay(slots, policy=policy, baselines=baselines, history=history)

    costs = [run.cost for run in runs if run.cost is not None]
    reference = None
    baseline_line = report.line(DEFAULT_BASELINE)
    if baseline_line is not None:
        reference = baseline_line.mean_cost
    return report, _ceilings(costs, reference)


def build_parser() -> argparse.ArgumentParser:
    """命令行。"""
    parser = argparse.ArgumentParser(description="自适应调度回放")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="原始记录（JSONL）")
    parser.add_argument("--board", default=str(DEFAULT_BOARD), help="把看板写到这个 Markdown 文件")
    parser.add_argument(
        "--observations",
        default=str(DEFAULT_OBSERVATIONS),
        help="把观测存档写到这个 JSONL 文件（运行时调度器的历史）",
    )
    parser.add_argument(
        "--history",
        choices=[HISTORY_SHARED, HISTORY_CHOSEN],
        default=HISTORY_SHARED,
        help="历史口径：shared = 所有格子的历史；chosen = 只有自己跑过的",
    )
    parser.add_argument(
        "--preference",
        default=DEFAULT_PREFERENCE,
        help="冷启动偏好（形如 '执行器/模型'）",
    )
    parser.add_argument(
        "--baseline",
        default=DEFAULT_PREFERENCE,
        help="静态对照线：始终用哪个组合",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=float(DEFAULT_TOLERANCE),
        help="达成度容差 —— 差这么多以内算分不开",
    )
    parser.add_argument(
        "--explore-every",
        type=int,
        default=0,
        help="每多少次决策探一次没怎么试过的格子（0 = 不探索）",
    )
    parser.add_argument(
        "--minimum-rate",
        type=float,
        default=0.0,
        help="绝对达成度底线",
    )
    return parser


def write_observations(path: Path, runs: Sequence[RecordedRun]) -> None:
    """把观测存档写下来 —— **运行时调度器读的就是它**。

    **格式只有一处定义**（:func:`core.policy.scheduling.observation_to_dict`）——
    两边各写一份转换，迟早会对不上，而对不上的那几列会静悄悄地变成 0。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(observation_to_dict(run.observation()), ensure_ascii=False, sort_keys=True)
        for run in runs
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def build_curve(
    *,
    report_for: Callable[[SchedulingPolicy], tuple[ReplayReport, list[Decimal]]],
    policy: SchedulingPolicy,
    tolerances: Sequence[Decimal],
    ceilings: Sequence[Decimal],
) -> list[str]:
    """容差曲线 —— **容差是 knob，实测它把达成度与成本推向哪儿**。

    **为什么要有这张表。** 容差调大，进底线的那一档变宽，调度器更敢选便宜的；代
    价是达成度往下掉。**哪一个换法划算，只有实测说了算** —— 所以这张表是按同一批
    实测记录跑出来的，不是估的。
    """
    header = ["容差", "做对率", "成功率", "总成本", "平均成本", "冷启动", "探索"]
    header.extend(f"预算内做对 ≤ {ceiling:.4f}" for ceiling in ceilings)
    lines = [
        "",
        "## 容差曲线",
        "",
        "容差越大，进底线的那一档越宽，调度器越敢选便宜的 —— **代价是达成度往下掉**。",
        "每一行都是**同一批实测记录**的回放结果，只有容差不同。",
        "",
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for tolerance in tolerances:
        report, _ = report_for(replace(policy, tolerance=tolerance))
        adaptive = report.adaptive
        if adaptive is None:
            continue
        cells = [
            f"{tolerance}",
            _format_rate(adaptive.overall_rate),
            _format_rate(adaptive.success_rate),
            f"{adaptive.total_cost:.6f}",
            "没测出来" if adaptive.mean_cost is None else f"{adaptive.mean_cost:.6f}",
            str(adaptive.cold_starts),
            str(adaptive.explorations),
        ]
        cells.extend(
            f"{succeeded}/{total}"
            for succeeded, total in (adaptive.within_budget(ceiling) for ceiling in ceilings)
        )
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _format_rate(value: Decimal | None) -> str:
    """排版一个比率 —— 没有样本时写"无样本"。"""
    if value is None:
        return "无样本"
    return f"{value:.4f}"


def main() -> int:
    """入口。"""
    args = build_parser().parse_args()
    entries = read_log(Path(args.log))
    if not entries:
        print(f"原始记录是空的：{args.log}", file=sys.stderr)
        return 2
    policy = SchedulingPolicy(
        tolerance=Decimal(str(args.tolerance)),
        minimum_rate=Decimal(str(args.minimum_rate)),
        preference=(args.preference,),
        explore_every=args.explore_every,
    )

    def report_for(current: SchedulingPolicy) -> tuple[ReplayReport, list[Decimal]]:
        """按给定的容差回放一遍 —— **同一批记录，只换规矩**。"""
        return build_report(
            entries,
            policy=current,
            history=args.history,
            prices=PRICE_TABLES,
            baseline_cell=args.baseline,
        )

    report, ceilings = report_for(policy)
    board = report.render(ceilings)
    board += "\n" + "\n".join(
        build_curve(
            report_for=report_for,
            policy=policy,
            tolerances=CURVE_TOLERANCES,
            ceilings=ceilings,
        )
    )
    print(board)

    runs = recorded_runs(records_from_log(entries), PRICE_TABLES)
    write_observations(Path(args.observations), runs)

    target = Path(args.board)
    if str(args.board):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(board + "\n", encoding="utf-8")
        print(f"\n看板已写入：{target}")
    print(f"观测存档已写入：{args.observations}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
