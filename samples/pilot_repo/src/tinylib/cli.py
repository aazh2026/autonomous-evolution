"""tinylib 的命令行入口。

刻意做得很薄 —— 它只是把库里的函数摆到命令行上。
"""

from __future__ import annotations

import argparse
import sys

from tinylib.stats import mean, median, percentile
from tinylib.strings import slugify, truncate


def _numbers(text):
    return [float(part) for part in text.split(",") if part.strip()]


def build_parser():
    parser = argparse.ArgumentParser(prog="tinylib", description="tinylib 的命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    stats = sub.add_parser("stats", help="统计几个数")
    stats.add_argument("values", help="逗号分隔的数")
    stats.add_argument("--percentile", type=float, default=None, help="算第几百分位")

    text = sub.add_parser("text", help="处理一段文本")
    text.add_argument("value", help="文本")
    text.add_argument("--slug", action="store_true", help="转成短横线形式")
    text.add_argument("--limit", type=int, default=None, help="截断长度")

    return parser


def main(argv=None):
    """跑一次。返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "stats":
        values = _numbers(args.values)
        if args.percentile is not None:
            print(percentile(values, args.percentile))
        else:
            print(f"mean={mean(values)}")
            print(f"median={median(values)}")
        return 0

    if args.command == "text":
        if args.slug:
            print(slugify(args.value))
        elif args.limit is not None:
            print(truncate(args.value, args.limit))
        else:
            print(args.value)
        return 0

    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
