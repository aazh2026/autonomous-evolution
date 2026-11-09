"""TASK-P1-065 —— 确定性演练 runner 的测试。

设计依据：docs/tasks.md TASK-P1-065（T1 ① 同一脚本回放两次，产生**完全相同**
的状态迁移序列与 DB 记录；T1 ② 打桩 Agent 报告成功但打桩验证判失败 → 系统
进入修复路径；T1 ③ 打桩 Critic 否决 → 候选不进入 ``APPROVED``）、R-108。

**这里验的是"回放得出来"。** 能不能把一轮编排从观察一路走到收尾、走的还是
真的图与真的状态机 —— 那是一件事；回放两次是不是一样 —— 是另一件事。前者
验在 T1 ② ③，后者在 T1 ①。

**为什么"回放两次完全一样"是可能的。** 因为模型那一段被换成了脚本：
观察、差距、候选、评分、任务草稿都从 YAML 读，Agent 的产出、验证的结论、
Critic 的裁决也按脚本给。**被换掉的只有"会说话的那部分"** —— 决策规则
(:class:`core.evolution.decision.DecisionPolicy`) 与放行规则
(:func:`agents.critic.approval.approval_for`) 都还是真的。所以"两次一样"
不是因为系统是随机的，而是因为它**本来就该是确定的**：同样的输入，
同样的图，同样的规则，就得给出同样的结论。

**DB 记录那一份在集成测试里比。** 行 id 是 uuid、时间戳是墙钟 —— 它们天生
不可重复，比它们等于比运气。集成测试比的是**规范化之后**的行。

**为什么"决策 record ≠ 任务会被创建"要单独钉一条。** Critic 否决之后，
决策仍然是 record（决策说的是"值得做"，放行说的是"不许做"）—— 任务不进
路线图，连 ``APPROVED`` 都到不了。**断言的是"没进路线图"**，而不是"决策不是
record"：后者会让人以为否决改的是决策，而它改的是放行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.critic.checks import CriticCheck
from core.task.outcome import TaskOutcome
from core.task.state_machine import TaskState
from executors.base import ExecutionResult, ExecutionTask, SessionConfig
from tests.support.evolution_loop import StubJournal
from tests.support.execution_loop import RecordingJournal
from tests.system.replay_runner import (
    REPLAY_EXECUTOR,
    ReplayRunner,
    ReplayScript,
    ReplayScriptError,
    ScriptedCritic,
    ScriptedDraft,
    ScriptedExecutor,
)

#: 脚本目录 —— 三份样例脚本放在那儿。
SCRIPT_DIR = Path(__file__).parent / "scripts"

#: 三份脚本各演一条路。
HAPPY_PATH = SCRIPT_DIR / "happy_path.yaml"
VERIFICATION_FAILURE = SCRIPT_DIR / "verification_failure.yaml"
CRITIC_VETO = SCRIPT_DIR / "critic_veto.yaml"


async def _replay(path: Path) -> tuple[ReplayScript, StubJournal, RecordingJournal]:
    """回放一份脚本，返回脚本、落库口与轨迹口。"""
    script = ReplayScript.from_yaml(path)
    journal = StubJournal()
    sessions = RecordingJournal()
    runner = ReplayRunner(journal=journal, session_journal=sessions)
    await runner.replay(script)
    return script, journal, sessions


async def _execute_once(executor: ScriptedExecutor, *, attempt: int) -> ExecutionResult:
    """让打桩执行器跑一次，返回它收上来的结果。"""
    session = await executor.create_session(SessionConfig(workspace="/tmp", branch="main"))
    await executor.execute(
        ExecutionTask(
            session_id=session.session_id,
            task_id="task-replay",
            title="任务",
            instruction="做掉它",
            timeout_seconds=60,
            attempt=attempt,
        )
    )
    return await executor.collect_result(session.session_id)


# --------------------------------------------------------------------------
# T1 ① 回放两次完全一样
# --------------------------------------------------------------------------


class TestReplayIsRepeatable:
    """T1 ① —— 同一脚本回放两次，产生完全相同的状态迁移序列。"""

    @pytest.mark.parametrize(
        "path",
        [HAPPY_PATH, VERIFICATION_FAILURE, CRITIC_VETO],
        ids=["happy_path", "verification_failure", "critic_veto"],
    )
    async def test_two_replays_produce_the_same_transcript(self, path: Path) -> None:
        """两次回放的留痕逐字相同。"""
        script = ReplayScript.from_yaml(path)

        first = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(script)
        second = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(script)

        assert first == second
        assert first.digest == second.digest

    @pytest.mark.parametrize(
        "path",
        [HAPPY_PATH, VERIFICATION_FAILURE, CRITIC_VETO],
        ids=["happy_path", "verification_failure", "critic_veto"],
    )
    async def test_two_replays_write_the_same_rows(self, path: Path) -> None:
        """两次回放写进落库口的东西也一样。

        **写什么也要一样，不只是状态迁移一样。** 候选、决策、路线图、周期
        记录 —— 哪一条多了一句、哪一条少了一句，读库的人看得见，而状态
        迁移序列看不见。
        """
        _, first_journal, _ = await _replay(path)
        _, second_journal, _ = await _replay(path)

        assert first_journal.candidates == second_journal.candidates
        assert first_journal.decisions == second_journal.decisions
        assert first_journal.roadmaps == second_journal.roadmaps
        assert first_journal.cycles == second_journal.cycles
        assert first_journal.closed == second_journal.closed
        assert first_journal.calls == second_journal.calls

    async def test_the_transcript_records_the_state_transitions(self) -> None:
        """留痕里有这一轮走过的节点与任务的状态迁移。"""
        transcript = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(ReplayScript.from_yaml(HAPPY_PATH))

        assert "observe_project" in transcript.loop_b_nodes
        assert "execute_engineering_cycle" in transcript.loop_b_nodes
        assert transcript.task_states == (
            TaskState.RUNNING.value,
            TaskState.VERIFYING.value,
            TaskState.PASSED.value,
            TaskState.INTEGRATING.value,
            TaskState.MERGED.value,
            TaskState.COMPLETED.value,
        )

    async def test_the_digest_changes_when_the_outcome_changes(self) -> None:
        """留痕不是恒定的 —— 换了结论，摘要就得跟着变。

        **不然"两次一样"就是一句空话。** 一个恒为常数的摘要，任何脚本都能
        "回放两次一样" —— 那证明不了任何东西。
        """
        runner = ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        )
        happy = await runner.replay(ReplayScript.from_yaml(HAPPY_PATH))
        vetoed = await runner.replay(ReplayScript.from_yaml(CRITIC_VETO))

        assert happy.digest != vetoed.digest

    async def test_the_sessions_carry_the_fixed_time(self) -> None:
        """执行会话的开会时刻是**固定的**，不是墙钟。

        **"没有墙钟"这条性质要变成可验的。** 打桩执行器用的是
        :data:`tests.system.replay_runner.REPLAY_TIME` —— 它哪天改用
        ``now()``，两次回放的留痕就不再一样，而"一样"正是这个模块存在的
        理由。**时刻进留痕，就是为了让它可验。**

        **断言的是那个字面时刻，不是"等于 ``REPLAY_TIME``"。** 后者永远成立
        —— 哪怕 ``REPLAY_TIME`` 本身换成了别的时刻。**锚在一个具体的时刻上，
        才挡得住"把它换成墙钟"这种改法。**
        """
        transcript = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(ReplayScript.from_yaml(HAPPY_PATH))

        assert transcript.sessions
        for _, moment in transcript.sessions:
            assert moment == "2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------
# T1 ② Agent 报成功，验证判失败 → 修复路径
# --------------------------------------------------------------------------


class TestVerificationFailureTakesTheRepairPath:
    """T1 ② —— 自我报告不参与判定；验证判失败就要走修复路径。"""

    async def test_the_task_does_not_pass(self) -> None:
        """打桩 Agent 报"做完了"，任务**没有**通过。"""
        transcript = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(ReplayScript.from_yaml(VERIFICATION_FAILURE))

        assert transcript.outcomes == (("task-replay-2", TaskOutcome.FAILED.value),)

    async def test_the_repair_path_is_taken(self) -> None:
        """任务进了 ``REPAIRING``，也就是走了修复那条路。"""
        transcript = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(ReplayScript.from_yaml(VERIFICATION_FAILURE))

        assert TaskState.REPAIRING.value in transcript.task_states
        # 修复额度用完就停 —— 不是无限重试。
        assert transcript.task_states[-1] == TaskState.BLOCKED.value

    async def test_the_agent_execution_runs_more_than_once(self) -> None:
        """Agent 被叫了不止一次 —— 首跑之后还有返修。

        **"进了 REPAIRING"与"真的重跑了一遍"是两回事。** 只看状态迁移的话，
        一条"记录了修复意图但没执行"的路径也说得通。
        """
        _, _, sessions = await _replay(VERIFICATION_FAILURE)

        assert sessions.nodes.count("agent_executes") > 1
        # 每次执行开一次会话 —— 会话数与执行次数对得上。
        assert len(sessions.sessions) == sessions.nodes.count("agent_executes")

    async def test_a_passing_verification_never_enters_repair(self) -> None:
        """验证通过就不修 —— 那条路上一个 ``REPAIRING`` 都没有。"""
        transcript = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(ReplayScript.from_yaml(HAPPY_PATH))

        assert TaskState.REPAIRING.value not in transcript.task_states


# --------------------------------------------------------------------------
# 顺路那条：一条任务从头走到尾
# --------------------------------------------------------------------------


class TestTheHappyPath:
    """一条顺利的路 —— 决策、放行、拆解、执行、验收、合入，一步不缺。"""

    async def test_the_task_is_planned_and_completed(self) -> None:
        """决策 record → 放行 → 进路线图 → 执行通过。

        **"进了路线图"与"执行通过"要一起断言。** 只断言前者，放行那一环被
        绕过去了也看不出来；只断言后者，那条路可能压根没有任务。
        """
        transcript = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(ReplayScript.from_yaml(HAPPY_PATH))

        assert transcript.decisions == (("EVD-cycle-0001-001", "record"),)
        assert transcript.approvals == (("EVD-cycle-0001-001", True, "九项检查全部通过，无否决"),)
        assert transcript.planned_tasks == (("task-replay-1", "实现确定性演练 runner"),)
        assert transcript.outcomes == (("task-replay-1", TaskOutcome.COMPLETED.value),)
        assert transcript.cycle_status == "completed"

    async def test_the_cycle_b_walks_all_fifteen_nodes(self) -> None:
        """循环 B 的十五个节点一个不少，一个不多。

        **钉住节点序列，而不是节点个数。** 只数个数的话，顺序错了、多一个
        少一个而总数不变，都看不出来。
        """
        transcript = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(ReplayScript.from_yaml(HAPPY_PATH))

        assert transcript.loop_b_nodes == (
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


# --------------------------------------------------------------------------
# T1 ③ Critic 否决 → 不进入 APPROVED
# --------------------------------------------------------------------------


class TestCriticVetoBlocksTheTask:
    """T1 ③ —— 打桩 Critic 否决，候选不进入 ``APPROVED``。"""

    async def test_no_task_is_planned(self) -> None:
        """否决之后路线图是空的。"""
        transcript = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(ReplayScript.from_yaml(CRITIC_VETO))

        assert transcript.planned_tasks == ()
        assert transcript.outcomes == ()

    async def test_the_decision_is_still_record(self) -> None:
        """决策仍然是 record —— **否决改的是放行，不是决策**。

        这条断言钉住的是职责边界：Critic 说"不许做"，系统照实记下"决策说
        值得做"，两件事分开记。**要是把决策改成 reject，就分不清"系统觉得
        不值得"与"系统觉得值得但没通过审查"** —— 而下游对这两者的处置是
        不同的。
        """
        transcript = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(ReplayScript.from_yaml(CRITIC_VETO))

        assert transcript.decisions == (("EVD-cycle-0003-001", "record"),)

    async def test_the_approval_says_no_and_says_why(self) -> None:
        """放行凭据说"不放行"，而且说清了为什么。"""
        transcript = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(ReplayScript.from_yaml(CRITIC_VETO))

        assert len(transcript.approvals) == 1
        decision_id, approved, reason = transcript.approvals[0]
        assert decision_id == "EVD-cycle-0003-001"
        assert approved is False
        assert "Critic 否决" in reason
        assert "只有一条来源" in reason

    async def test_the_skipped_reason_is_kept(self) -> None:
        """被跳过的那条决策留下了理由 —— **而且是审查给的那条理由**。

        **理由要说清是谁不放行的。** 路线图上写着"没有放行凭据"与写着
        "Critic 否决 —— ……"是两回事：前者读的人还得回头去查是谁没放行，
        后者直接告诉他卡在哪一关。**这条理由就是放行凭据原样传下去的那条**
        —— 凭据丢了，理由也就没了。
        """
        transcript = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(ReplayScript.from_yaml(CRITIC_VETO))

        assert len(transcript.skipped) == 1
        decision_id, reason = transcript.skipped[0]
        assert decision_id == "EVD-cycle-0003-001"
        assert "Critic 否决" in reason
        assert "只有一条来源" in reason

    async def test_no_execution_happened(self) -> None:
        """任务都没建，就更没有执行 —— 循环 A 一个节点都没走。"""
        transcript = await ReplayRunner(
            journal=StubJournal(),
            session_journal=RecordingJournal(),
        ).replay(ReplayScript.from_yaml(CRITIC_VETO))

        assert transcript.loop_a_nodes == ()
        assert transcript.task_states == ()


# --------------------------------------------------------------------------
# 脚本本身
# --------------------------------------------------------------------------


class TestScriptValidation:
    """脚本写错了要当场报错，而不是回放出一份说不清的结果。"""

    def test_a_draft_must_reference_a_known_candidate(self) -> None:
        script = ReplayScript.from_yaml(HAPPY_PATH)
        broken = ScriptedDraft(key="task-x", candidate_id="cand-does-not-exist")

        with pytest.raises(ReplayScriptError, match="cand-does-not-exist"):
            ReplayScript(
                project_id=script.project_id,
                cycle_id=script.cycle_id,
                observation=script.observation,
                gaps=script.gaps,
                candidates=script.candidates,
                scores=script.scores,
                drafts=(broken,),
            )

    def test_every_candidate_needs_a_score(self) -> None:
        """没有评分的候选 —— 决策拿不到依据。"""
        script = ReplayScript.from_yaml(HAPPY_PATH)

        with pytest.raises(ReplayScriptError, match="没有评分"):
            ReplayScript(
                project_id=script.project_id,
                cycle_id=script.cycle_id,
                observation=script.observation,
                gaps=script.gaps,
                candidates=script.candidates,
                scores=(),
            )

    def test_a_veto_must_use_a_vetoing_check(self) -> None:
        """否决必须用可否决的检查。"""
        with pytest.raises(ReplayScriptError, match="不是可否决的检查"):
            ScriptedCritic(
                veto=True,
                check=CriticCheck.NO_UNNECESSARY_COMPLEXITY,
                summary="看着太复杂",
            )

    def test_a_critic_needs_a_conclusion(self) -> None:
        """裁决不能是空白 —— "没说话"与"说了没事"不是一回事。"""
        with pytest.raises(ReplayScriptError, match="一句话结论"):
            ScriptedCritic(veto=False, summary="   ")

    def test_a_missing_field_is_reported(self) -> None:
        with pytest.raises(ReplayScriptError, match="project_id"):
            ReplayScript.from_mapping({})

    def test_a_score_must_reference_a_known_candidate(self) -> None:
        with pytest.raises(ReplayScriptError, match="cand-nope"):
            ReplayScript.from_mapping(
                {
                    "project_id": "p",
                    "cycle_id": "c",
                    "observation": {"commit": "abc"},
                    "gaps": [
                        {
                            "id": "g",
                            "description": "有个差距",
                            "evidence": [{"kind": "issue", "ref": "issue://x"}],
                        }
                    ],
                    "candidates": [],
                    "scores": [{"candidate_id": "cand-nope", "value": "0.5"}],
                }
            )


# --------------------------------------------------------------------------
# 打桩件
# --------------------------------------------------------------------------


class TestScriptedExecutor:
    """打桩执行器 —— 它报"我做完了"，而这不该决定任何事。"""

    async def test_the_executor_is_registered_under_its_own_name(self) -> None:
        _, _, sessions = await _replay(HAPPY_PATH)

        assert sessions.sessions
        assert all(item.executor == REPLAY_EXECUTOR for item in sessions.sessions)

    async def test_each_attempt_takes_the_next_scripted_result(self) -> None:
        """返修时取脚本里的第二份 —— 脚本能描述"改了以后的样子"。"""
        script = ReplayScript.from_yaml(VERIFICATION_FAILURE)
        executor = ScriptedExecutor(script.attempts)

        first = await _execute_once(executor, attempt=1)
        second = await _execute_once(executor, attempt=2)

        assert first.artifacts.commit != second.artifacts.commit
        # 幂等键每次都不一样 —— 重试要能重做。
        assert first.idempotency_key != second.idempotency_key

    async def test_the_attempts_run_out_gives_the_last_one(self) -> None:
        """脚本写完了就重复最后一份 —— "一直修不好"这条路也要能演。"""
        script = ReplayScript.from_yaml(VERIFICATION_FAILURE)
        executor = ScriptedExecutor(script.attempts[:1])

        first = await _execute_once(executor, attempt=1)
        third = await _execute_once(executor, attempt=3)

        assert first.artifacts.commit == third.artifacts.commit
