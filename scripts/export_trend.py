"""导出跨周期趋势看板 —— 四层指标在时间上的曲线。

设计依据：docs/tasks.md TASK-P3-017（交付物"跨周期趋势看板（达成度、质量、
成本）"；验收"能导出 ≥7 天连续运行的四层指标趋势"；验证 T2）、R-117、R-118。

**为什么这是一个脚本而不是一个接口。** 看板的读者是人 —— 值班的人想在早上
看一眼"这些天下来系统在变好还是变坏"。给他一个 JSON 端点，他会去写脚本；
给他一份 Markdown，他直接读。

**天数下限是硬的。** 验收条件是"≥7 天连续运行"，所以 ``--days`` 小于 7 就
直接拒绝 —— 少于七天的窗口**看起来照样正常**，而那正是看板要防的那种
"看起来正常"。

**连接串只从环境来。** ``ASES_DATABASE_URL``（或 ``.env``）—— 命令行参数在
``ps`` 里是全局可见的，而连接串里有口令（``infrastructure/config/settings.py``）。

**用法**::

    python -m scripts.export_trend --project demo
    python -m scripts.export_trend --project demo --days 30 --format json
    python -m scripts.export_trend --project demo --output docs/observability/trend.md
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from core.policy.budget import BudgetMetric
from evaluation.labels.pilot import pilot_label_store
from evaluation.trend import MIN_TREND_DAYS, TrendWindow
from infrastructure.database.engine import build_engine, build_session_factory, dispose_engine
from infrastructure.database.trend import PostgresTrendReader
from orchestration.dashboard import TrendDashboard


def _parse_day(text: str) -> date:
    """解析一个 ISO 日期。"""
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"不是合法的 ISO 日期：{text}") from exc


def _parse_metric(text: str) -> BudgetMetric:
    """解析一个预算计量维度。"""
    try:
        return BudgetMetric(text)
    except ValueError as exc:
        known = "、".join(metric.value for metric in BudgetMetric)
        raise argparse.ArgumentTypeError(f"不是合法的计量维度：{text}（有的是 {known}）") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出跨周期趋势看板")
    parser.add_argument("--project", required=True, help="项目 id")
    parser.add_argument(
        "--days",
        type=int,
        default=MIN_TREND_DAYS,
        help=f"窗口天数（含首尾两天）。**不得少于 {MIN_TREND_DAYS} 天** —— "
        "少于七天的窗口看起来照样正常，而那正是看板要防的",
    )
    parser.add_argument(
        "--until",
        type=_parse_day,
        default=None,
        help="窗口的最后一天（ISO 日期）。默认是今天（UTC）",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="导出格式",
    )
    parser.add_argument("--output", type=Path, default=None, help="写到哪（默认打到标准输出）")
    parser.add_argument(
        "--scope",
        default=None,
        help="成本归到哪个预算作用域。默认是项目 id",
    )
    parser.add_argument(
        "--cost-metric",
        type=_parse_metric,
        default=BudgetMetric.TOTAL_COST,
        help="成本取自哪个预算维度",
    )
    parser.add_argument(
        "--no-labels",
        action="store_true",
        help="不接人工标注基线（依赖标注的两项指标会报「基线未建立」）",
    )
    return parser


async def _export(args: argparse.Namespace) -> str:
    """取数、算指标、导出。"""
    until = args.until if args.until is not None else datetime_utc_today()
    window = TrendWindow.last_days(until, args.days)

    engine = build_engine()
    try:
        factory = build_session_factory(engine)
        reader = PostgresTrendReader(factory, cost_metric=args.cost_metric)
        dashboard = TrendDashboard(
            source=reader,
            label_store=None if args.no_labels else pilot_label_store(),
        )
        export = await dashboard.build(
            project_id=args.project,
            window=window,
            scope=args.scope,
        )
    finally:
        await dispose_engine(engine)

    if args.format == "json":
        return export.to_json()
    return export.to_markdown()


def datetime_utc_today() -> date:
    """今天 —— **按 UTC**。

    全库的时间都是 UTC，按本地时区切日会让同一条记录在两台机器上落到不同
    的格子里。
    """
    return datetime.now(UTC).date()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.days < MIN_TREND_DAYS:
        parser.error(
            f"--days 不得少于 {MIN_TREND_DAYS} 天 —— "
            "验收要求「≥7 天连续运行」，而少于七天的窗口看起来照样正常"
        )

    text = asyncio.run(_export(args))
    if args.output is None:
        print(text)
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text + "\n", encoding="utf-8")
    print(f"看板写到了 {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
