"""TASK-P3-018 —— 连续 10 周期长跑的**驱动器**。

跑法::

    uv run python -m scripts.run_long_run
    # 或
    python -m scripts.run_long_run --rounds 10 --out docs/evaluation/long-run.json

**这一份脚本做四件事。**

1. **检查数据库连不连得上** —— 连不上就直说，而不是跑到一半炸在一个看不懂的
   堆栈里。
2. **连着跑十轮** —— 每一轮的输入是**上一轮合入之后**的那份仓库（见
   :mod:`apps.director.long_run`）。
3. **按事先定好的判据判定** —— 判据在
   :mod:`evaluation.long_run` 里，**跑之前就定死了**（三条：一个有证据的候
   选、一个被采纳的决策、一条可验证的改善记录）。
4. **把长跑记录写下来** —— JSON（机器可读，逐字节稳定）+ Markdown（人读）。

**为什么记录要落成文件。** 验收条件是"连续 10 轮无人工派发任务；每轮产出可
验证证据；达成度呈非负趋势" —— 这句话要能被第三方核对，就得有一份**不依赖运
行者记性**的东西。屏幕上滚过去的输出不算记录。

**退出码就是验收结论。** 0 = 通过验收；1 = 没通过，且打印出**哪几条没过**。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.director.long_run import LongRunConfig, LongRunOutcome, run_long_run
from evaluation.long_run import REQUIRED_ROUNDS

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 默认数据库 —— 与 docker-compose.yml 里的服务一致。
DEFAULT_DATABASE_URL = "postgresql+asyncpg://ases:ases@localhost:5432/ases"

#: 记录默认落在哪儿 —— 与文档同目录，便于一起看。
DEFAULT_RECORD = REPO_ROOT / "docs" / "evaluation" / "long-run.json"


def _banner(step: str, title: str) -> None:
    """一节的分隔线。"""
    print(f"\n{'─' * 78}\n{step}  {title}\n{'─' * 78}")


def _fail(message: str) -> int:
    """说清楚哪里不行，然后返回一个非零退出码。"""
    print(f"\n❌ {message}")
    return 1


def _text(value: Decimal | None) -> str:
    """把达成度渲染成一句话里的样子 —— **测不出来是 ``—``，不是 0**。"""
    if value is None:
        return "—"
    return str(value)


def _cycle_facts(outcome: LongRunOutcome) -> list[dict[str, object]]:
    """每一轮的可核对事实 —— **每一行都能回到库里的某一行**。"""
    facts: list[dict[str, object]] = []
    for index, (result, measurement) in enumerate(
        zip(outcome.rounds, outcome.measurements, strict=True), start=1
    ):
        facts.append(
            {
                "round": index,
                "cycle_id": result.cycle_id,
                "cycle_ref": result.cycle_ref,
                "candidate_id": result.candidate_id,
                "decision_id": result.decision_id,
                "task_key": result.task_key,
                "task_id": result.task_id,
                "approval_request_id": result.approval_request_id,
                "verdict": str(result.verdict),
                "accepted": result.accepted,
                "merged": result.merged,
                "merge_commit": result.merge_commit,
                "changed_files": result.changed_files,
                "elapsed_seconds": str(result.elapsed_seconds),
                "cost": str(result.cost),
                "currency": result.currency,
                "cycle_outcome": result.cycle_outcome,
                "attainment_before": _text(result.attainment_before),
                "attainment_after": _text(result.attainment_after),
                "attainment_delta": _text(result.attainment_delta),
                "visited_nodes": list(result.visited_nodes),
                "critic_outcomes": [list(item) for item in result.critic_outcomes],
                "candidates": [list(item) for item in result.candidates],
                "decisions": [list(item) for item in result.decisions],
                "records": dict(result.records),
                "measurement": {
                    "line_coverage": str(measurement.line_coverage),
                    "cli_coverage": str(measurement.cli_coverage),
                    "covered_lines": measurement.covered_lines,
                    "total_lines": measurement.total_lines,
                    "test_count": measurement.test_count,
                    "defects_fixed": measurement.defects_fixed,
                    "failing_repro": list(measurement.failing_repro),
                    "money_paths_covered": measurement.money_paths_covered,
                    "boundary_covered": measurement.boundary_covered,
                },
            }
        )
    return facts


def _record(outcome: LongRunOutcome) -> dict[str, object]:
    """长跑记录 —— **JSON 键排好序，两次导出逐字节相同**。"""
    return {
        "report": json.loads(outcome.report.to_json()),
        "rounds": _cycle_facts(outcome),
    }


def _print_rounds(outcome: LongRunOutcome) -> None:
    """逐轮打印 —— **达成度、实测值、合并提交**。"""
    print(f"  {'轮次':<4} {'候选':<52} {'达成度（前→后）':<26} {'合并':<10} {'覆盖率':<12}")
    for index, (result, measurement) in enumerate(
        zip(outcome.rounds, outcome.measurements, strict=True), start=1
    ):
        title = result.candidates[0][1] if result.candidates else "—"
        before = _text(result.attainment_before)
        after = _text(result.attainment_after)
        commit = result.merge_commit[:8] if result.merge_commit else "—"
        print(
            f"  {index:<4} {title[:50]:<52} {before + ' → ' + after:<26} "
            f"{commit:<10} {measurement.line_coverage!s:<12}"
        )


async def _run(
    *,
    database_url: str,
    pilot: Path,
    work_root: Path,
    rounds: int,
    record_path: Path,
) -> int:
    """跑一次长跑，并把记录写下来。"""
    project_file = pilot / "project.yaml"
    if not project_file.exists():
        return _fail(f"试点仓库缺少 project.yaml：{project_file}")

    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        _banner("[1/4]", "检查依赖：PostgreSQL 连通性")
        try:
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
        except Exception as exc:
            return _fail(
                f"连不上 PostgreSQL（{database_url}）：{exc}\n"
                "提示：如果是本地开发，请先执行 make up 启动数据库"
            )
        print("  PostgreSQL 连通正常")

        _banner("[2/4]", f"连着跑 {rounds} 轮（每一轮的输入是上一轮合入后的仓库）")
        print(f"  试点仓库源码：{pilot}")
        print(f"  工作根目录  ：{work_root}")
        config = LongRunConfig(
            project_file=project_file,
            pilot_source=pilot,
            work_root=work_root,
            python_executable=sys.executable,
            rounds=rounds,
        )
        outcome = await run_long_run(config, session_factory)

        _banner("[3/4]", "逐轮事实")
        _print_rounds(outcome)

        _banner("[4/4]", "按事先定好的判据判定")
        print(outcome.report.to_markdown())

        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(_record(outcome), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"\n长跑记录已写入：{record_path}")

        if not outcome.report.accepted:
            return _fail("长跑没有通过验收 —— 没过的都写在上面")
        print("\n🎉 长跑通过验收：每一轮都是有效演进周期，且达成度呈非负趋势。")
        return 0
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="连续 10 周期长跑")
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL, help="PostgreSQL 连接串")
    parser.add_argument("--pilot", type=Path, default=None, help="试点仓库源码目录")
    parser.add_argument("--work-root", type=Path, default=None, help="工作根目录")
    parser.add_argument(
        "--rounds",
        type=int,
        default=REQUIRED_ROUNDS,
        help=f"跑多少轮（验收要求 {REQUIRED_ROUNDS} 轮）",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_RECORD, help="长跑记录写到哪里")
    parser.add_argument("--keep", action="store_true", help="保留工作根目录")

    args = parser.parse_args(argv)
    pilot = args.pilot or (REPO_ROOT / "samples" / "pilot_repo")

    temp_ctx = None
    if args.work_root is None:
        temp_ctx = tempfile.TemporaryDirectory(prefix="ases-long-run-")
        work = Path(temp_ctx.name)
    else:
        work = args.work_root
        work.mkdir(parents=True, exist_ok=True)

    try:
        return asyncio.run(
            _run(
                database_url=args.database_url,
                pilot=pilot,
                work_root=work,
                rounds=args.rounds,
                record_path=args.out,
            )
        )
    finally:
        if temp_ctx is not None and not args.keep:
            temp_ctx.cleanup()


if __name__ == "__main__":
    sys.exit(main())
