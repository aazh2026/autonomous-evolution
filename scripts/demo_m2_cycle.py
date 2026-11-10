"""TASK-P1-082 的 T3 演示 —— M2 自主闭环演进（只给目标，自主完成）。

跑法::

    uv run python -m scripts.demo_m2_cycle
    # 或
    make demo-m2

**M2 演示的核心验证点**：
1. **自主发掘候选（T2 ①）**：系统根据目标 YAML 与仓库状态，自主发掘多条候选，全部带文件证据；
2. **不行动也是合法决策（T2 ②）**：确凿缺陷走向 record，而高风险设想走向 defer 并产出分析项；
3. **独立验证（T2 ③）**：改动在隔离沙箱中真跑 pytest + ruff，VerificationRun 存在且通过；
4. **达成度提升（T2 ④）**：前后测量真实达成度，Delta > 0，周期结局判定为 ``improved``。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.director.m2_budget import M2BudgetCheck, check_m2_budget
from apps.director.m2_cycle import M2Config, M2CycleResult, run_m2_cycle

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 默认数据库 —— 与 docker-compose.yml 里的服务一致。
DEFAULT_DATABASE_URL = "postgresql+asyncpg://ases:ases@localhost:5432/ases"

#: M2 验收的最少候选数（一条确凿缺陷，一条设想推迟）。
MIN_CANDIDATE_COUNT: Final[int] = 2


def _banner(step: str, title: str) -> None:
    """一节的分隔线。"""
    print(f"\n{'─' * 78}\n{step}  {title}\n{'─' * 78}")


def _fail(message: str) -> int:
    """说清楚哪里不行，然后返回一个非零退出码。"""
    print(f"\n❌ {message}")
    return 1


async def _rows(engine: AsyncEngine, sql: str, **params: object) -> list[dict[str, object]]:
    """跑一句查询，把行取回来。"""
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        return [dict(row) for row in result.mappings()]


async def _print_the_candidates(engine: AsyncEngine, result: M2CycleResult) -> None:
    """第 ① 步 · 候选落库 —— 验证自主发掘的多条候选。"""
    for cid, _title, _conf in result.candidates:
        row_id = result.candidate_rows.get(cid, "")
        rows = await _rows(
            engine,
            "select id::text, category, title, status, confidence, estimated_effort, evidence "
            "from evolution_candidate where id = :id",
            id=row_id,
        )
        for row in rows:
            print(f"  候选标识  ：{cid}")
            print(f"  行 id      ：{row['id']}")
            print(f"  类别      ：{row['category']}")
            print(f"  标题      ：{row['title']}")
            print(f"  状态      ：{row['status']}")
            print(f"  置信度    ：{row['confidence']}")
            print(f"  预计工作量：{row['estimated_effort']}")
            evidence_raw = row.get("evidence")
            evidence_count = len(evidence_raw) if isinstance(evidence_raw, Sequence) else 0
            print(f"  证据项数  ：{evidence_count}")
            print()


async def _print_the_decisions(engine: AsyncEngine, result: M2CycleResult) -> None:
    """第 ② 步 · 决策落库 —— 验证 record 与 defer 共存。"""
    for cid, _dec, _rationale in result.decisions:
        row_id = result.decision_rows.get(cid, "")
        rows = await _rows(
            engine,
            "select id::text, candidate_id::text, decision, rationale, requested_analysis "
            "from evolution_decision where id = :id",
            id=row_id,
        )
        for row in rows:
            print(f"  决策归属  ：候选 {cid}")
            print(f"  行 id      ：{row['id']}")
            print(f"  决策结论  ：{row['decision']}")
            print(f"  决策理由  ：{row['rationale']}")
            analysis_raw = row.get("requested_analysis")
            analysis = analysis_raw if isinstance(analysis_raw, Sequence) else []
            if analysis:
                print(f"  补充分析项：{len(analysis)} 项（不行动留下的后继工作）")
            print()


async def _print_the_task(engine: AsyncEngine, result: M2CycleResult) -> None:
    """第 ③ 步 · 任务落库 —— 验证只有 record 的候选派生了任务。"""
    rows = await _rows(
        engine,
        "select id::text, title, type, status, executor, branch, workspace_id, "
        "repair_count, started_at is not null as started, "
        "completed_at is not null as finished, acceptance_contract "
        "from task where id = :id",
        id=result.task_id,
    )
    for row in rows:
        print(f"  任务标识  ：{result.task_key}")
        print(f"  行 id      ：{row['id']}")
        print(f"  标题      ：{row['title']}")
        print(f"  状态      ：{row['status']}")
        print(f"  执行器    ：{row['executor']}")
        print(f"  分支      ：{row['branch']}")
        started_str = "已开工" if row["started"] else "未开工"
        finished_str = "已完成" if row["finished"] else "未完成"
        print(f"  开工/收尾 ：{started_str} / {finished_str}")
        print(f"  验收契约  ：{'已绑定' if row['acceptance_contract'] else '未绑定'}")


async def _print_the_verification(engine: AsyncEngine, result: M2CycleResult) -> None:
    """第 ④ 步 · 独立验证落库。"""
    rows = await _rows(
        engine,
        "select id::text, task_id::text, cycle_id::text, result, verified_by, "
        "duration_ms, checks from verification_run where task_id = :id",
        id=result.task_id,
    )
    for row in rows:
        print(f"  行 id      ：{row['id']}")
        print(f"  结论      ：{row['result']}")
        print(f"  验证者    ：{row['verified_by']}")
        print(f"  耗时      ：{row['duration_ms']} 毫秒")
        print(f"  检查项    ：{row['checks']}")
    rows = await _rows(
        engine,
        "select id::text, type, uri, hash, size_bytes from artifact "
        "where task_id = :id order by created_at",
        id=result.task_id,
    )
    for row in rows:
        print(f"  产物      ：{row['type']}  {row['uri']}  ({row['size_bytes']} 字节)")


async def _print_the_cycle_and_attainment(engine: AsyncEngine, result: M2CycleResult) -> None:
    """第 ⑤ 步 · 周期落库与目标达成度（T2 ④）。"""
    rows = await _rows(
        engine,
        "select id::text, cycle_id, trigger, status, outcome, "
        "started_at is not null as started, ended_at is not null as ended "
        "from evolution_cycle where id = :id",
        id=result.cycle_ref,
    )
    for row in rows:
        print(f"  周期号    ：{row['cycle_id']}")
        print(f"  状态      ：{row['status']}")
        print(f"  结局判定  ：{row['outcome']}")
    print(f"  达成度轮前：{result.attainment_before}")
    print(f"  达成度轮后：{result.attainment_after}")
    print(f"  达成度差值：+{result.attainment_delta}（可验证的改善）")
    print(f"  结局理由  ：{result.cycle_reason}")


def _print_overview(result: M2CycleResult) -> None:
    """打印全链总览信息。"""
    _banner("[3/6]", "全链执行总览")
    print(f"  周期 ID   ：{result.cycle_id}")
    print(f"  主线任务  ：{result.task_key}（ID: {result.task_id}）")
    print(f"  执行耗时  ：{result.elapsed_seconds:.3f} 秒")
    print(f"  名义成本  ：{result.cost} {result.currency}")
    print(f"  验证结论  ：{result.verdict}")
    print(f"  合并提交  ：{result.merge_commit}")
    print(f"  周期结局  ：{result.cycle_outcome}")


def _check_m2_acceptance(result: M2CycleResult, checks: tuple[M2BudgetCheck, ...]) -> int:
    """校验 M2 的关键验收指标。"""
    for check in checks:
        print(f"  {check.message()}")
    if any(not check.within for check in checks):
        return _fail("耗时或成本超出了 TASK-P1-072 基线的 2 倍")

    has_no_action = any(dec[1] in ("defer", "reject") for dec in result.decisions)
    has_positive_delta = result.attainment_delta is not None and result.attainment_delta > 0

    validations = [
        (len(result.candidates) >= MIN_CANDIDATE_COUNT, "自主发掘的候选数量少于 2 个"),
        (has_no_action, "没有候选进入 defer/reject，未证明不行动机制生效"),
        (result.accepted and result.merged, "任务未通过验收或未成功合并到主分支"),
        (has_positive_delta, f"目标达成度差值必须 > 0，当前为 {result.attainment_delta}"),
        (
            result.cycle_outcome == "improved",
            f"周期结局必须为 improved，当前为 {result.cycle_outcome}",
        ),
    ]
    for condition, msg in validations:
        if not condition:
            return _fail(msg)
    return 0


async def run_demo(
    *,
    database_url: str = DEFAULT_DATABASE_URL,
    pilot_dir: Path | None = None,
    work_root: Path | None = None,
    cycle_id: str = "",
    keep_work_root: bool = False,
) -> int:
    """跑一次完整的 M2 演示。"""
    pilot = pilot_dir or (REPO_ROOT / "samples" / "pilot_repo")
    project_file = pilot / "project.yaml"
    if not project_file.exists():
        return _fail(f"试点仓库缺少 project.yaml：{project_file}")

    temp_ctx = None
    if work_root is None:
        temp_ctx = tempfile.TemporaryDirectory(prefix="ases-demo-m2-")
        work = Path(temp_ctx.name)
    else:
        work = work_root
        work.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        _banner("[1/6]", "检查依赖：PostgreSQL 连通性")
        try:
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
        except Exception as exc:
            return _fail(
                f"连不上 PostgreSQL（{database_url}）：{exc}\n"
                "提示：如果是本地开发，请先执行 make up 启动数据库"
            )
        print("  PostgreSQL 连通正常")

        _banner("[2/6]", "启动 M2 自主闭环驱动")
        print(f"  试点仓库源码：{pilot}")
        print(f"  工作根目录  ：{work}")
        print("  正在自主观察项目、发掘候选、决策规划并执行验证...")

        config = M2Config(
            project_file=project_file,
            pilot_source=pilot,
            work_root=work,
            python_executable=sys.executable,
            cycle_id=cycle_id,
        )

        result = await run_m2_cycle(config, session_factory)
        _print_overview(result)

        _banner("[4/6]", "T2 ① & ②：自主候选发掘与不行动决策")
        await _print_the_candidates(engine, result)
        await _print_the_decisions(engine, result)

        _banner("[5/6]", "T2 ③ & ④：工程验证与目标达成度正向提升")
        await _print_the_task(engine, result)
        await _print_the_verification(engine, result)
        await _print_the_cycle_and_attainment(engine, result)

        _banner("[6/6]", "预算与耗时对比基线（T2 预算检查）")
        checks: tuple[M2BudgetCheck, ...] = check_m2_budget(
            repo_root=REPO_ROOT,
            cost=result.cost,
            elapsed_seconds=result.elapsed_seconds,
        )
        verdict = _check_m2_acceptance(result, checks)
        if verdict != 0:
            return verdict

        print("\n🎉 M2 自主闭环演进演示成功完成！全部判据已通过验证。")
        return 0

    finally:
        await engine.dispose()
        if temp_ctx is not None and not keep_work_root:
            temp_ctx.cleanup()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M2 自主闭环演进演示")
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL, help="PostgreSQL 连接串")
    parser.add_argument("--pilot", type=Path, default=None, help="试点仓库源码目录")
    parser.add_argument("--work-root", type=Path, default=None, help="工作根目录")
    parser.add_argument("--cycle-id", default="", help="指定周期 ID")
    parser.add_argument("--keep", action="store_true", help="保留工作根目录")

    args = parser.parse_args(argv)

    return asyncio.run(
        run_demo(
            database_url=args.database_url,
            pilot_dir=args.pilot,
            work_root=args.work_root,
            cycle_id=args.cycle_id,
            keep_work_root=args.keep,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
