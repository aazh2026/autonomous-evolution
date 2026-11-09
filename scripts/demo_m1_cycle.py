"""TASK-P1-081 的 T3 演示 —— 从一条候选走到"验证通过并合入主分支"。

跑法::

    uv run python -m scripts.demo_m1_cycle
    # 或
    make demo-m1

**这个演示里没有假的东西。** 真的建 git 仓库、真的跑 pytest、真的用独立的
验证引擎判定、真的走审批门、真的 ``git merge``、真的落库。演示里唯一被模拟的
是"人" —— 合并审批的那一按必须由人来按，而演示不可能真等一个人来按。

**唯一被换掉的是"写代码"那一段。** 执行器是一个本地补丁器
(:class:`apps.director.m1_executor.M1PatchExecutor`)：它按一份写死的补丁清单
改文件、提交、报成本。**它不联网、不调模型、不花钱。** 换掉它不改变被验证的
东西 —— 改完之后跑的仍是真 pytest、真的独立验证、真的验收契约。M1 要证明的是
**链路**，不是"模型能不能把活干完"。

**要一个真的 PostgreSQL。** T1 的验收口径是"每一步的 DB 记录可查" —— 那些记录
得**真的**在数据库里。本演示默认连 ``postgresql+asyncpg://ases:ases@localhost:5432/ases``，
可以用 ``ASES_DATABASE_URL`` 改。

**演示结束后库里留着这一轮的行。** 那是故意的 —— "每一步的 DB 记录可查"这句
话得有个东西可查。想清库就 ``make reset``。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.director.m1_budget import M1BudgetCheck, check_m1_budget
from apps.director.m1_cycle import M1Config, M1CycleResult, run_m1_cycle
from apps.director.m1_executor import EXECUTOR_NAME

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 默认数据库 —— 与 docker-compose.yml 里的服务一致。
DEFAULT_DATABASE_URL = "postgresql+asyncpg://ases:ases@localhost:5432/ases"


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


# --------------------------------------------------------------------------
# 把这一轮落下的库记录一条一条摊开
# --------------------------------------------------------------------------


async def _print_the_candidate(engine: AsyncEngine, result: M1CycleResult) -> None:
    """第 ① 步 · 候选落库。"""
    rows = await _rows(
        engine,
        "select id::text, category, title, status, confidence, estimated_effort "
        "from evolution_candidate where id = :id",
        id=result.records.get("evolution_candidate", ""),
    )
    for row in rows:
        print(f"  行 id      ：{row['id']}")
        print(f"  类别      ：{row['category']}")
        print(f"  标题      ：{row['title']}")
        print(f"  状态      ：{row['status']}")
        print(f"  置信度    ：{row['confidence']}")
        print(f"  预计工作量：{row['estimated_effort']}")


async def _print_the_decision(engine: AsyncEngine, result: M1CycleResult) -> None:
    """第 ② 步 · 决策落库。"""
    rows = await _rows(
        engine,
        "select id::text, candidate_id::text, decision, rationale, overridden_by "
        "from evolution_decision where id = :id",
        id=result.records.get("evolution_decision", ""),
    )
    for row in rows:
        print(f"  行 id      ：{row['id']}")
        print(f"  挂在候选上：{row['candidate_id']}")
        print(f"  决策      ：{row['decision']}（record = 记下来要做）")
        print(f"  理由      ：{row['rationale']}")
        print(f"  被推翻    ：{row['overridden_by'] or '（没有）'}")


async def _print_the_task(engine: AsyncEngine, result: M1CycleResult) -> None:
    """第 ③ 步 · 任务落库。"""
    rows = await _rows(
        engine,
        "select id::text, title, type, status, executor, branch, workspace_id, "
        "repair_count, started_at is not null as started, "
        "completed_at is not null as finished, acceptance_contract "
        "from task where id = :id",
        id=result.task_id,
    )
    for row in rows:
        print(f"  行 id      ：{row['id']}")
        print(f"  标题      ：{row['title']}")
        print(f"  类型      ：{row['type']}")
        print(f"  状态      ：{row['status']}")
        print(f"  执行器    ：{row['executor']}")
        print(f"  分支      ：{row['branch']}")
        print(f"  工作区    ：{row['workspace_id']}")
        print(f"  返修次数  ：{row['repair_count']}")
        print(f"  开过工    ：{'是' if row['started'] else '否'}")
        print(f"  收过尾    ：{'是' if row['finished'] else '否'}")
        contract = row["acceptance_contract"]
        print(f"  验收契约  ：{'已绑定' if contract else '未绑定'}")


async def _print_the_verification(engine: AsyncEngine, result: M1CycleResult) -> None:
    """第 ④ 步 · 独立验证落库。"""
    rows = await _rows(
        engine,
        "select id::text, task_id::text, cycle_id::text, result, verified_by, "
        "duration_ms, checks from verification_run where task_id = :id",
        id=result.task_id,
    )
    for row in rows:
        print(f"  行 id      ：{row['id']}")
        print(f"  挂在任务上：{row['task_id']}")
        print(f"  挂在周期上：{row['cycle_id']}")
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
        print(f"  内容哈希  ：{row['hash']}")


async def _print_the_approval(engine: AsyncEngine, result: M1CycleResult) -> None:
    """第 ⑤ 步 · 人工审批落库。"""
    rows = await _rows(
        engine,
        "select id::text, task_id::text, operation, status, requested_by, "
        "decided_by, diff_summary from approval_request where id = :id",
        id=result.approval_request_id,
    )
    for row in rows:
        print(f"  行 id      ：{row['id']}")
        print(f"  挂在任务上：{row['task_id']}")
        print(f"  操作      ：{row['operation']}")
        print(f"  状态      ：{row['status']}")
        print(f"  发起人    ：{row['requested_by']}")
        print(f"  批准人    ：{row['decided_by']}")
        print("  人看到的那份改动：")
        for line in str(row["diff_summary"] or "").splitlines():
            print(f"    {line}")


async def _print_the_snapshot_and_session(engine: AsyncEngine, result: M1CycleResult) -> None:
    """第 ⑥ 步 · 快照与会话。"""
    rows = await _rows(
        engine,
        "select id::text, version, commit, branch from project_snapshot where id = :id",
        id=result.records.get("project_snapshot", ""),
    )
    for row in rows:
        print(f"  快照行 id  ：{row['id']}")
        print(f"  版本      ：{row['version']}")
        print(f"  提交      ：{row['commit']}")
        print(f"  分支      ：{row['branch']}")
    rows = await _rows(
        engine,
        "select id::text, session_id, executor, agent_id, status from agent_session "
        "where task_id = :id",
        id=result.task_id,
    )
    for row in rows:
        print(f"  会话行 id  ：{row['id']}")
        print(f"  会话      ：{row['session_id']}")
        print(f"  执行器    ：{row['executor']}")
        print(f"  代理      ：{row['agent_id']}")
        print(f"  状态      ：{row['status']}")


async def _print_the_cycle(engine: AsyncEngine, result: M1CycleResult) -> None:
    """第 ⑦ 步 · 周期落库。"""
    rows = await _rows(
        engine,
        "select id::text, cycle_id, trigger, status, outcome, "
        "started_at is not null as started, ended_at is not null as ended "
        "from evolution_cycle where id = :id",
        id=result.cycle_ref,
    )
    for row in rows:
        print(f"  行 id      ：{row['id']}")
        print(f"  周期号    ：{row['cycle_id']}")
        print(f"  触发源    ：{row['trigger']}")
        print(f"  状态      ：{row['status']}")
        print(f"  结论      ：{row['outcome']}")
        print(
            f"  起止      ：{'有起点' if row['started'] else '没起点'} / "
            f"{'有终点' if row['ended'] else '没终点'}"
        )


async def _print_the_chain(engine: AsyncEngine, result: M1CycleResult) -> None:
    """把这一轮落下的库记录一条一条摊开。

    **这里读的是数据库，不是内存里的状态。** 演示要证明的是"每一步的 DB 记录
    可查" —— 打印内存里的东西证明不了这件事。
    """
    _banner("[5/7]", "第 ① 步 · 候选落库 —— evolution_candidate")
    await _print_the_candidate(engine, result)
    _banner("[5/7]", "第 ② 步 · 决策落库 —— evolution_decision")
    await _print_the_decision(engine, result)
    _banner("[5/7]", "第 ③ 步 · 任务落库 —— task")
    await _print_the_task(engine, result)
    _banner("[5/7]", "第 ④ 步 · 独立验证落库 —— verification_run 与 artifact")
    await _print_the_verification(engine, result)
    _banner("[5/7]", "第 ⑤ 步 · 人工审批落库 —— approval_request")
    await _print_the_approval(engine, result)
    _banner("[5/7]", "第 ⑥ 步 · 快照与会话 —— project_snapshot / agent_session")
    await _print_the_snapshot_and_session(engine, result)
    _banner("[5/7]", "第 ⑦ 步 · 周期落库 —— evolution_cycle")
    await _print_the_cycle(engine, result)


async def _print_the_event_trail(engine: AsyncEngine, result: M1CycleResult) -> None:
    """把循环 A 走过的节点与任务状态迁移摊开。

    **这是"全链"最硬的一份证据。** 每一行是一条 ``execution_event`` —— 循环 A
    每走一个节点写一条，带上那个节点经过的任务状态。序列连起来，就是这一轮
    任务从 ``APPROVED`` 走到 ``COMPLETED`` 的**全过程**，逐字可查。
    """
    _banner("[6/7]", "循环 A 的事件轨迹 —— execution_event")
    rows = await _rows(
        engine,
        "select sequence, event_type, payload from execution_event "
        "where task_id = :id order by sequence",
        id=result.task_id,
    )
    for row in rows:
        payload = row["payload"]
        states = payload.get("task_states") if isinstance(payload, dict) else None
        states_text = " → ".join(str(item) for item in states) if states else "（没有状态）"
        print(f"  #{row['sequence']:<3} {row['event_type']:<32} {states_text}")
    if rows:
        print(f"\n  合计 {len(rows)} 条事件，序号 {rows[0]['sequence']}…{rows[-1]['sequence']}")


def _print_the_budget(result: M1CycleResult, checks: tuple[M1BudgetCheck, ...]) -> None:
    """T2 的账 —— 这一轮落在基线的几倍。"""
    print(f"  端到端耗时：{result.elapsed_seconds} 秒")
    print(f"  本轮成本  ：{result.cost} {result.currency}")
    print()
    for check in checks:
        print(f"  {check.message()}")


# --------------------------------------------------------------------------
# 跑演示
# --------------------------------------------------------------------------


def _print_the_intro() -> None:
    """开头那段 —— 说清楚这个演示是什么、不是什么。"""
    print("=" * 78)
    print("ASES M1 单轮闭环演示（TASK-P1-081 · T3）")
    print("=" * 78)
    print("  链路是真的：真的图、真的状态机、真的策略、真的落库、真的 pytest。")
    print(f"  执行器是本地补丁器（{EXECUTOR_NAME}）：按写死的补丁改代码，")
    print("  不联网、不调模型、不花钱 —— M1 验的是链路，不是模型。")
    print("  唯一被模拟的是'人'：合并审批的那一按，演示替你按了。")


def _print_the_preparation(pilot: Path, work_root: Path) -> None:
    """准备那一节。"""
    _banner("[2/7]", "准备 —— 复制一份独立的试点仓库")
    print(f"  试点源码  ：{pilot}")
    print(f"  工作根目录：{work_root}")
    print("  原目录一个字都不动 —— 演示在副本上干活。")


def _print_the_injected_candidate() -> None:
    """手工注入的候选 —— 说清楚它的证据是什么。"""
    _banner("[3/7]", "手工注入候选 —— PILOT-001")
    print("  候选      ：CLI 在空输入时抛未捕获的 ValueError")
    print("  证据      ：src/tinylib/cli.py（没有空输入守卫）")
    print("              known_issues/test_known_issues.py（复现用例）")
    print("  假设      ：空输入是一句用法错误，不该是一段 traceback")
    print("  候选是手工注入的 —— M1 验的是'候选之后发生什么'。")


def _print_the_critic(result: M1CycleResult) -> None:
    """Critic 的九问 —— 决定这条决策能不能放行。

    **三态，不是两态。** "查过了，通过" / "查过了，不通过" / "没查成" 必须分得开
    —— 把"没查成"报成"通过"，Critic 就成了一台会点头的机器。
    """
    _banner("[4/7]", "Critic 的九问 —— 放行凭据从这里来")
    if not result.critic_outcomes:
        print("  （这一轮没有审查记录）")
        return
    for check, verdict, summary in result.critic_outcomes:
        mark = {"passed": "✅", "unverified": "⚠️ ", "failed": "❌"}.get(verdict, "·  ")
        print(f"  {mark} {check:30} {verdict}")
        print(f"      {summary}")
    print("  注：任务表查询会看到之前几轮演示留下的行 —— 演示不清库，")
    print("      那正是「每一步的 DB 记录可查」要留的东西。")


def _print_the_cycle_result(result: M1CycleResult) -> None:
    """闭环跑完之后的结论。"""
    print(f"  周期号    ：{result.cycle_id}")
    print(f"  候选      ：{result.candidate_id}")
    print(f"  决策      ：{result.decision_id}")
    print(f"  任务      ：{result.task_key}（{result.task_id}）")
    print(f"  验证结论  ：{result.verdict.value}")
    for check, passed in result.checks:
        print(f"    · {check}：{'通过' if passed else '没通过'}")
    for reason in result.reasons:
        print(f"    · {reason}")
    print(f"  验收      ：{'通过' if result.accepted else '没通过'}")
    print(f"  合并      ：{'已合入主分支' if result.merged else '没合并'}")
    if result.merge_commit:
        print(f"  合入提交  ：{result.merge_commit}")
    print(f"  改动文件  ：{result.changed_files} 个")
    print(f"  分支      ：{result.branch}")
    if result.cycle_outcome:
        print(f"  周期结局  ：{result.cycle_outcome}")
        print(f"             {result.cycle_reason}")


async def _run(args: argparse.Namespace) -> int:
    """跑一遍演示。"""
    _print_the_intro()

    _banner("[1/7]", "前置检查 —— 数据库在不在")
    engine = create_async_engine(args.database_url, poolclass=NullPool)
    try:
        try:
            async with engine.connect() as conn:
                version = await conn.scalar(text("select version()"))
        except Exception as exc:
            return _fail(
                f"连不上数据库 {args.database_url} —— {exc}\n"
                "  这一版演示要一个真的 PostgreSQL（那些记录得真的在库里）。\n"
                "  起全栈：make up"
            )
        print(f"  数据库    ：{args.database_url}")
        print(f"  版本      ：{str(version).splitlines()[0] if version else '（没问出来）'}")

        _print_the_preparation(args.pilot, args.work_root)
        _print_the_injected_candidate()

        _banner("[4/7]", "走完闭环 —— 决策 → 任务 → 执行 → 独立验证 → 记录")
        config = M1Config(
            project_file=REPO_ROOT / "samples/pilot_repo/project.yaml",
            pilot_source=args.pilot,
            work_root=args.work_root,
            python_executable=sys.executable,
            cycle_id=args.cycle_id,
        )
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        result = await run_m1_cycle(config, factory)
        _print_the_cycle_result(result)
        _print_the_critic(result)

        await _print_the_chain(engine, result)
        await _print_the_event_trail(engine, result)

        _banner("[7/7]", "T2 —— 耗时与成本")
        checks = check_m1_budget(
            repo_root=REPO_ROOT,
            cost=result.cost,
            elapsed_seconds=result.elapsed_seconds,
        )
        _print_the_budget(result, checks)
        over = [check for check in checks if not check.within]
        for check in checks:
            mark = "✅" if check.within else "❌"
            print(f"  {mark} {check.metric}在基线 2 倍以内")
        if over:
            print("\n❌ 有账超出基线 2 倍 —— T2 不通过")
            return 1
    finally:
        await engine.dispose()

    print("\n" + "=" * 78)
    print("演示结束 —— 这一轮的行还留在库里，可以自己查")
    print("=" * 78)
    print(f"  周期号：{result.cycle_id}")
    print(f"  任务  ：{result.task_id}")
    print(f"  成本  ：{result.cost} {result.currency}（名义值，执行器写死在代码里）")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="M1 单轮闭环演示（TASK-P1-081 · T3）")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("ASES_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="PostgreSQL 连接串（默认读 ASES_DATABASE_URL）",
    )
    parser.add_argument(
        "--pilot",
        type=Path,
        default=REPO_ROOT / "samples/pilot_repo",
        help="试点仓库源码目录（会被复制成一份独立的 git 仓库）",
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=None,
        help="工作根目录（默认在临时目录里建一个）",
    )
    parser.add_argument("--cycle-id", default="", help="周期号（不给就每次跑造一个新的）")
    parser.add_argument("--keep", action="store_true", help="保留工作目录（调试用）")
    args = parser.parse_args(argv)

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.work_root is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="ases-m1-demo-")
        args.work_root = Path(temp_dir.name)
    try:
        return asyncio.run(_run(args))
    finally:
        if temp_dir is not None:
            if args.keep:
                print(f"\n工作目录保留在：{temp_dir.name}")
            else:
                temp_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
