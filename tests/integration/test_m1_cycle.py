"""TASK-P1-081 —— M1 单轮闭环的落库验证（T1）。

**这一条要打到数据库上，因为"每一步的 DB 记录可查"这句话只有在库里才有意义。**
内存里的状态说"每一步都落库了"是自说自话 —— 只有真把行读回来，才知道这一步
真的写了一行，还是只是把一个对象传下去了。

**验的是四件事。**

1. **每一步都有一行。** 候选、决策、任务、验证、产物、审批、快照、会话、
   周期 —— 九张表，九行，一行都不能少，而且行与行之间的外键要真的指着对方
   （任务挂在决策上、决策挂在候选上、验证挂在任务与周期上）。挂错了**不会
   报错**：外键查得到，只是查到了另一行。

2. **全链走完了。** 循环 A 走过十一个节点、留下十一条事件，那串任务状态迁移
   是这一轮从 ``APPROVED`` 走到 ``COMPLETED`` 的全过程，逐条可查。

3. **回放 runner 跑出同一条链。** 这一条最容易被自己骗过去：真跑一遍看起来
   挺顺，不等于换个执行器还顺。回放 runner（:mod:`tests.system.replay_runner`）
   用打桩的执行器与验证器跑**同一条链** —— 走过的节点、事件、任务状态迁移、
   决策、任务标识与标题必须与真跑的那一遍一致。**一致说明编排层没有被执行器
   牵着走**；不一致就说明"这条链"其实只是"这个执行器的那条链"。

4. **账落在基线 2 倍以内（T2）。** 判定的口径只有一份
   （:func:`apps.director.m1_budget.check_m1_budget`），基线从
   ``docs/evaluation/baselines.yaml`` 里读 —— 抄进代码的基线一更新就过期，
   而过期的基线会安静地给出"通过"。

**要一个真的 PostgreSQL。** 那些记录得**真的**在数据库里。没有 PostgreSQL 时
整个目录跳过（见 ``tests/integration/conftest.py``）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.director.m1_budget import check_m1_budget
from apps.director.m1_cycle import M1Config, M1CycleResult, run_m1_cycle
from apps.director.m1_executor import EXECUTOR_NAME
from core.entities import Project
from infrastructure.database.evolution_journal import EvolutionJournal
from infrastructure.database.execution_journal import DatabaseExecutionJournal
from tests.system.replay_runner import ReplayRunner, ReplayScript

pytestmark = pytest.mark.integration

#: 仓库根目录。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: M1 那条链的镜像脚本 —— 打桩的执行器与验证器，别的都不换。
M1_SCRIPT = Path(__file__).resolve().parents[1] / "system" / "scripts" / "m1_cycle.yaml"

#: 回放那一轮用的项目 id —— 与 M1 那一轮**分开**。
#:
#: 分开是为了不共用快照版本号：``project_snapshot`` 上有
#: ``(project_id, version)`` 唯一约束，两轮共用一个项目就会在同一版本上撞车。
#: 链路走出来的形状与项目无关，分开不影响比较。
REPLAY_PROJECT_ID = "pilot-tinylib-replay"

#: 循环 B 走过的那十五个节点 —— 写死，不从实现里抄。
EXPECTED_LOOP_B_NODES = (
    "observe_project",
    "build_project_snapshot",
    "analyze_goal_gap",
    "discover_candidates",
    "gather_evidence",
    "evaluate_value_risk_cost",
    "worth_doing",
    "record_defer_reject",
    "generate_evolution_plan",
    "decompose_into_tasks",
    "dependency_and_resource_scheduling",
    "execute_engineering_cycle",
    "evaluate_actual_outcome",
    "update_project_knowledge",
    "update_goal_gap",
)

#: 循环 A 走过的那十一个节点 —— 一条一条，写死。
EXPECTED_LOOP_A_NODES = (
    "task_ready",
    "task_planner",
    "select_coding_agent",
    "create_isolated_workspace",
    "agent_executes",
    "collect_diff_and_artifacts",
    "independent_verification",
    "acceptance_passed",
    "integration_and_merge_gate",
    "update_project_state",
    "execution_complete",
)

#: 这一轮任务走过的状态 —— 从 ``APPROVED`` 一直到 ``COMPLETED``。
#:
#: **合并那一步走两格**（``INTEGRATING`` → ``MERGED``），所以状态比节点多。
EXPECTED_TASK_STATES = (
    "RUNNING",
    "VERIFYING",
    "PASSED",
    "INTEGRATING",
    "MERGED",
    "COMPLETED",
)


async def _rows(session: AsyncSession, sql: str, **params: object) -> list[dict[str, Any]]:
    """跑一句查询，把行取回来。"""
    result = await session.execute(text(sql), params)
    return [dict(row) for row in result.mappings()]


async def _one(session: AsyncSession, sql: str, **params: object) -> dict[str, Any]:
    """跑一句查询，取第一行。**没有行就报错** —— 空行不是"值是空"。"""
    rows = await _rows(session, sql, **params)
    assert rows, f"查询没有取到行：{sql}"
    return rows[0]


async def _run_m1(engine: AsyncEngine, work_root: Path) -> M1CycleResult:
    """真跑一轮 M1 闭环。

    **用引擎另开会话，不复用测试的会话。** 闭环自己开事务、自己提交 ——
    复用同一个会话会让"它提交了没有"这件事说不清，而"落库了"正是这里要验的。
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    config = M1Config(
        project_file=REPO_ROOT / "samples/pilot_repo/project.yaml",
        pilot_source=REPO_ROOT / "samples/pilot_repo",
        work_root=work_root,
        python_executable=sys.executable,
    )
    return await run_m1_cycle(config, factory)


async def _seed_replay_project(session: AsyncSession) -> None:
    """建一个给回放用的项目 —— 候选、周期、快照都挂在它下面。"""
    session.add(
        Project(
            id=REPLAY_PROJECT_ID,
            name="M1 链路回放项目",
            goal={"objective": [{"id": "OBJ-1", "description": "把 M1 那条链回放一遍"}]},
            policy={"merge_to_main": "approval_required"},
            budget={"currency": "USD"},
            repository_url="https://example.invalid/m1-replay.git",
        )
    )
    await session.flush()


class TestTheChainLeavesAQueryableRowEverywhere:
    """T1 ① —— 每一步的 DB 记录可查。"""

    async def test_the_candidate_the_decision_and_the_task_are_there(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """候选 → 决策 → 任务：三张表，三行，外键指着对方。"""
        result = await _run_m1(engine, tmp_path)

        candidate = await _one(
            session,
            "select id::text, status, title, confidence, evidence from evolution_candidate "
            "where id = :id",
            id=result.records["evolution_candidate"],
        )
        assert candidate["status"] == "accepted"
        assert candidate["title"] == "CLI 在空输入时抛未捕获的 ValueError"
        assert candidate["evidence"], "候选必须带着证据落库"

        decision = await _one(
            session,
            "select id::text, candidate_id::text, decision, rationale, overridden_by "
            "from evolution_decision where id = :id",
            id=result.records["evolution_decision"],
        )
        assert decision["decision"] == "record"
        assert decision["candidate_id"] == candidate["id"]
        assert decision["rationale"]
        assert decision["overridden_by"] is None

        task = await _one(
            session,
            "select id::text, decision_id::text, title, type, status, executor, branch, "
            "workspace_id, repair_count, started_at, completed_at, acceptance_contract, extra "
            "from task where id = :id",
            id=result.task_id,
        )
        assert task["decision_id"] == decision["id"]
        assert task["type"] == "bugfix"
        assert task["status"] == "COMPLETED"
        assert task["acceptance_contract"], "验收契约必须绑在任务行上"
        assert task["started_at"] is not None
        assert task["completed_at"] is not None

    async def test_the_task_row_says_who_did_it_and_where(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """任务行上记着"谁做的"与"在哪儿做的"。

        **光看任务行就知道它被谁做了。** 会话行上确实记着执行器，可会话与任务
        是两件事，靠会话反推任务是猜。
        """
        result = await _run_m1(engine, tmp_path)

        task = await _one(
            session,
            "select executor, branch, workspace_id, extra from task where id = :id",
            id=result.task_id,
        )
        assert task["executor"] == EXECUTOR_NAME
        assert task["branch"], "任务行上必须记着分支"
        assert task["workspace_id"], "任务行上必须记着工作区"
        assert task["extra"]["roadmap_key"] == "T-M1-001"

    async def test_the_verification_and_the_artifact_are_there(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """独立验证与它的产物 —— 挂在任务与周期上。"""
        result = await _run_m1(engine, tmp_path)

        run = await _one(
            session,
            "select id::text, task_id::text, cycle_id::text, result, verified_by, "
            "duration_ms, checks from verification_run where task_id = :id",
            id=result.task_id,
        )
        assert run["result"] == "passed"
        assert run["verified_by"], "验证者必须记下来 —— 否则事后没人知道是谁验的"
        assert run["cycle_id"] == result.records["evolution_cycle"]
        assert run["duration_ms"] is not None
        assert run["checks"], "检查项必须记下来"

        artifact = await _one(
            session,
            "select id::text, task_id::text, verification_run_id::text, type, uri, hash, "
            "size_bytes from artifact where task_id = :id",
            id=result.task_id,
        )
        assert artifact["verification_run_id"] == run["id"]
        assert artifact["hash"], "产物必须有内容哈希"
        assert artifact["size_bytes"] is not None

    async def test_the_approval_the_snapshot_and_the_session_are_there(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """人工审批、项目快照与执行会话。"""
        result = await _run_m1(engine, tmp_path)

        approval = await _one(
            session,
            "select id::text, task_id::text, operation, status, requested_by, decided_by, "
            "diff_summary from approval_request where id = :id",
            id=result.approval_request_id,
        )
        assert approval["task_id"] == result.task_id
        assert approval["operation"] == "merge_to_main"
        assert approval["status"] == "approved"
        assert approval["decided_by"], "批准人必须记下来"
        assert approval["diff_summary"], "人看到的那份改动必须留在库里"

        snapshot = await _one(
            session,
            "select id::text, version, commit, branch from project_snapshot where id = :id",
            id=result.records["project_snapshot"],
        )
        assert snapshot["version"] >= 1
        assert snapshot["commit"]
        assert snapshot["branch"] == "main"

        agent_session = await _one(
            session,
            "select id::text, session_id, executor, task_id::text from agent_session "
            "where task_id = :id",
            id=result.task_id,
        )
        assert agent_session["executor"] == EXECUTOR_NAME

    async def test_the_cycle_row_records_how_it_ended(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """周期行：跑完了，结局是 ``no_change``。

        **``no_change`` 是诚实的答案，不是缺陷。** 结局判定要看"目标达成度前后
        有没有变"，而这一版观察者不报达成度 —— 没有可比的前后值，判定只能是
        "没有可验证的改善"（:func:`core.metrics.cycle.classify_cycle` 的第四
        条）。把达成度算出来是 M2 的事。
        """
        result = await _run_m1(engine, tmp_path)

        cycle = await _one(
            session,
            "select id::text, cycle_id, trigger, status, outcome, started_at, ended_at "
            "from evolution_cycle where id = :id",
            id=result.records["evolution_cycle"],
        )
        assert cycle["status"] == "completed"
        assert cycle["outcome"] == "no_change"
        assert cycle["started_at"] is not None
        assert cycle["ended_at"] is not None
        assert result.cycle_outcome == "no_change"
        assert result.cycle_reason


class TestTheWholeChainIsTraceable:
    """T1 ② —— 全链走完了，逐条可查。"""

    async def test_loop_b_visited_the_fifteen_nodes(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """循环 B 走过了那十五个节点。

        **走过的路要跟库里那一行对得上。** 节点序列是内存里的事实，周期行是
        库里的事实 —— 两者指的是同一轮，对不上就说明"走过的路"与"记下来的账"
        说的是两件事。
        """
        result = await _run_m1(engine, tmp_path)

        assert result.visited_nodes == EXPECTED_LOOP_B_NODES

        cycle = await _one(
            session,
            "select cycle_id, status from evolution_cycle where id = :id",
            id=result.records["evolution_cycle"],
        )
        assert cycle["cycle_id"] == result.cycle_id
        assert cycle["status"] == "completed"

    async def test_loop_a_left_eleven_events_in_order(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """循环 A 留下十一条事件，序号连着，节点次序与状态机一致。"""
        result = await _run_m1(engine, tmp_path)

        rows = await _rows(
            session,
            "select sequence, event_type, payload from execution_event "
            "where task_id = :id order by sequence",
            id=result.task_id,
        )

        assert [row["event_type"] for row in rows] == list(EXPECTED_LOOP_A_NODES)
        assert [row["sequence"] for row in rows] == list(range(1, len(EXPECTED_LOOP_A_NODES) + 1))

    async def test_the_task_walked_the_states_in_order(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """任务的状态迁移序列 —— 这一轮的全过程。

        **状态藏在事件载荷里，不在单独一张表里。** 每个节点写的事件都带着
        ``task_states``（"这一步经过了哪些任务状态"）—— 把它们摊平，就是这一轮
        从开工到收尾的全过程。
        """
        result = await _run_m1(engine, tmp_path)

        rows = await _rows(
            session,
            "select payload from execution_event where task_id = :id order by sequence",
            id=result.task_id,
        )
        states: list[str] = []
        for row in rows:
            payload = row["payload"]
            assert isinstance(payload, dict)
            for state in payload.get("task_states") or ():
                states.append(str(state))

        assert tuple(states) == EXPECTED_TASK_STATES


class TestTheReplayRunnerReproducesTheSameChain:
    """T1 ③ —— 回放 runner 跑通同一条链。"""

    async def test_the_replayed_chain_has_the_same_shape(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """打桩的执行器与验证器，跑出来的链与真跑的那一遍一样。

        **比的是形状，不是内容。** 真跑那一遍有真的提交、真的 diff、真的耗时；
        回放那一遍全是打桩的固定值 —— 比那些等于比运气。**比的是链路走出来的
        结构**：走过哪些节点、留下哪些事件、任务怎么迁移、决策是什么、任务是谁。
        """
        result = await _run_m1(engine, tmp_path)
        await _seed_replay_project(session)

        script = ReplayScript.from_yaml(M1_SCRIPT)
        runner = ReplayRunner(
            journal=EvolutionJournal(session),
            session_journal=DatabaseExecutionJournal(session),
        )
        transcript = await runner.replay(script)
        await session.flush()

        assert transcript.loop_b_nodes == result.visited_nodes
        assert transcript.loop_a_nodes == EXPECTED_LOOP_A_NODES
        assert transcript.task_states == EXPECTED_TASK_STATES
        assert [state for _, state in transcript.decisions] == ["record"]
        assert [approved for _, approved, _ in transcript.approvals] == [True]
        assert transcript.planned_tasks == (("T-M1-001", "CLI 在空输入时抛未捕获的 ValueError"),)
        assert [outcome for _, outcome in transcript.outcomes] == ["completed"]
        assert transcript.cycle_status == "completed"

    async def test_the_replayed_chain_also_leaves_rows_behind(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """回放那一轮的行也落在库里 —— 而且挂在它自己的项目下。

        **回放不是"打印一遍"。** 它走的是真的图、真的落库口 —— 库里能查到它
        留下的候选、决策、任务与事件，才说明回放跑的是同一条链，而不是另画了
        一张图。

        **任务行停在 ``APPROVED``，而 M1 那一轮停在 ``COMPLETED``** —— 这个
        差别要说清楚：把事件里报的状态搬到任务行上，是 **M1 驱动自己的落库
        适配器**（``apps.director.m1_cycle`` 的镜像落库口）做的事；回放 runner
        （P1-065）只负责把**链路**回放一遍，任务行停在落库那一刻的初态。两者
        比的是**链路**（走过哪些节点、留下哪些事件、状态怎么迁移），不是落库
        镜像 —— 事件序列已经逐条比过了。
        """
        await _run_m1(engine, tmp_path)
        await _seed_replay_project(session)

        script = ReplayScript.from_yaml(M1_SCRIPT)
        runner = ReplayRunner(
            journal=EvolutionJournal(session),
            session_journal=DatabaseExecutionJournal(session),
        )
        await runner.replay(script)
        await session.flush()

        cycles = await _rows(
            session,
            "select cycle_id, status, outcome from evolution_cycle where project_id = :id",
            id=REPLAY_PROJECT_ID,
        )
        tasks = await _rows(
            session,
            "select title, status, extra from task where project_id = :id",
            id=REPLAY_PROJECT_ID,
        )
        events = await _rows(
            session,
            "select event_type from execution_event where project_id = :id order by sequence",
            id=REPLAY_PROJECT_ID,
        )

        assert len(cycles) == 1
        assert cycles[0]["status"] == "completed"
        assert len(tasks) == 1
        assert tasks[0]["status"] == "APPROVED"
        assert tasks[0]["extra"]["roadmap_key"] == "T-M1-001"
        assert [row["event_type"] for row in events] == list(EXPECTED_LOOP_A_NODES)


class TestTheCostAndTheTimeStayWithinTwiceTheBaseline:
    """T2 —— 端到端耗时与成本落在 ``TASK-P1-072`` 基线的 2 倍以内。"""

    async def test_the_round_is_within_two_times_the_baseline(
        self, session: AsyncSession, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """成本与耗时各比一次基线。

        **基线从文件里读，不写死在测试里。** 基线是量出来的（见
        ``docs/evaluation/baselines.yaml``）—— 抄一个数进代码，基线一更新代码
        就跟着过期，而过期的基线会安静地给出"通过"。

        **用的是中位数，不是最大值。** T2 问的是"一个任务典型要花多少"。

        **先确认这一轮真的跑过。** 测的是一轮闭环的账，不是空跑一趟的账 ——
        周期行有起点有终点，才说明这两个数说的是一次真的执行。
        """
        result = await _run_m1(engine, tmp_path)

        cycle = await _one(
            session,
            "select started_at, ended_at from evolution_cycle where id = :id",
            id=result.records["evolution_cycle"],
        )
        assert cycle["started_at"] is not None
        assert cycle["ended_at"] is not None

        checks = check_m1_budget(
            repo_root=REPO_ROOT,
            cost=result.cost,
            elapsed_seconds=result.elapsed_seconds,
        )

        over = [check.message() for check in checks if not check.within]
        assert not over, "有账超出基线 2 倍：" + "；".join(over)
