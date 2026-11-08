"""TASK-P1-047 —— 最小集成路径。

这一组验的是 T1 的三句话：

    ① 任务分支验证通过后可经审批合并进 main
    ② 未经验证或验证失败的分支无法进入合并流程
    ③ 合并后 main 的测试仍通过（合并动作本身不破坏基线）

**验的是"路径做了什么"，不是"git 做了什么"。** ③ 在
``tests/integration/test_integration_pipeline.py`` 里用真 git 验；这里用替身
把三件最容易写错的事钉住：

- **次序**：先暂存合并、再在 main 上复验、复验通过才提交。顺序反了就是
  "先合再验"，main 会在验之前就前进到没验过的内容上。
- **三态**：验了没过（返工）与没验成（阻塞）是两回事。混成一种，系统就会
  去修一个根本没坏的东西。
- **审批的绑定**：批的是**哪一个提交**。分支头变了还拿旧批准放行，等于把
  人没看过的东西合进了 main。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.policy.approval import ApprovalStatus, InMemoryApprovalStore
from core.policy.autonomy import Operation
from core.task.state_machine import TaskState
from harness.approval.gate import ApprovalGate
from harness.integration.pipeline import (
    IntegrationPipeline,
    MergeConflictError,
    MergeError,
    PipelineRequest,
    PipelineStage,
    StagedMerge,
)
from harness.verification.contract import AcceptanceContract, RequiredCheck
from harness.verification.engine import (
    CheckExecution,
    CheckSpec,
    VerificationEngine,
    VerificationPlan,
)
from tests.unit.harness.integration.doubles import FakeCheckRunner, RecordingMerger

TASK_ID = "TASK-1"
_HEAD = "a" * 40
_NOW = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


@dataclass(frozen=True)
class World:
    """一次测试里的两个工作区。"""

    task: Path
    main: Path

    def request(
        self,
        *,
        contract: AcceptanceContract | None = None,
        diff_summary: str | None = None,
        task_state: TaskState | None = None,
    ) -> PipelineRequest:
        """一份集成请求 —— 契约、计划与说明都是标准的那一份。"""
        return PipelineRequest(
            task_id=TASK_ID,
            agent_id="agent-1",
            project_id="proj-1",
            branch="task/TASK-1",
            task_workspace=self.task,
            main_workspace=self.main,
            contract=contract
            or AcceptanceContract(
                task_id=TASK_ID,
                required=(RequiredCheck.UNIT_TESTS_PASS,),
            ),
            plan=VerificationPlan(
                checks=(
                    CheckSpec(check=RequiredCheck.UNIT_TESTS_PASS, command=("fake", "unit-tests")),
                )
            ),
            diff_summary="README.md: 改一行" if diff_summary is None else diff_summary,
            requested_by="agent-1",
            task_state=TaskState.VERIFYING if task_state is None else task_state,
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    """两个真的目录 —— 验证引擎要求工作区存在。"""
    task = tmp_path / "task"
    main = tmp_path / "main"
    task.mkdir()
    main.mkdir()
    return World(task=task, main=main)


def _pipeline(merger: RecordingMerger, runner: FakeCheckRunner) -> IntegrationPipeline:
    """一套路径 —— 时钟固定，所以审批记录的时间戳不会自己乱跳。"""
    gate = ApprovalGate(InMemoryApprovalStore(), clock=lambda: _NOW)
    return IntegrationPipeline(
        engine=VerificationEngine(runner=runner),
        gate=gate,
        merger=merger,
    )


def _failing() -> CheckExecution:
    """检查跑了，没过。"""
    return CheckExecution(exit_code=1, stdout="", stderr="1 failed")


def _not_run() -> CheckExecution:
    """检查压根没起来。"""
    return CheckExecution(exit_code=127, stdout="", stderr="not found", started=False)


class TestTheHappyPath:
    """T1 ① —— 验证通过 → 审批 → 合进 main。"""

    async def test_the_branch_is_merged_into_main_after_the_approval(self, world: World) -> None:
        merger = RecordingMerger()
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)
        request = world.request()

        waiting = await pipeline.run(request)
        assert waiting.stage is PipelineStage.AWAITING_APPROVAL
        assert waiting.task_state is TaskState.PASSED
        assert waiting.merged_commit is None

        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")

        merged = await pipeline.run(request)
        assert merged.stage is PipelineStage.MERGED
        assert merged.task_state is TaskState.COMPLETED
        assert merged.merged_commit == "b" * 40

    async def test_the_merge_is_staged_verified_and_only_then_committed(self, world: World) -> None:
        """**次序就是这条路径的全部。**

        先合并再验，main 会在验之前就前进到没验过的内容上；先提交再验更是
        更糟 —— 那时想撤都撤不掉了。
        """
        events: list[str] = []
        merger = RecordingMerger(events=events)
        runner = FakeCheckRunner(events=events)
        pipeline = _pipeline(merger, runner)
        request = world.request()

        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")
        await pipeline.run(request)

        assert events == [
            f"head_commit:{world.task}",
            f"verify:{world.task}",
            f"head_commit:{world.task}",
            f"verify:{world.task}",
            "stage_merge:task/TASK-1",
            f"verify:{world.main}",
            "commit_staged_merge",
        ]

    async def test_the_approval_request_is_created_only_after_the_validation_passed(
        self, world: World
    ) -> None:
        """**没验过的改动不该出现在人的审批清单里。**

        人看到一张审批单，会以为"这东西已经验过了" —— 审批单上写着验证证据
        引用，它就该真的是。
        """
        merger = RecordingMerger()
        runner = FakeCheckRunner(default=_failing())
        pipeline = _pipeline(merger, runner)

        result = await pipeline.run(world.request())

        assert result.stage is PipelineStage.LOCAL_VALIDATION_FAILED
        assert await pipeline.gate.store.list_for_task(TASK_ID) == ()

    async def test_the_approval_request_names_the_commit_it_approves(self, world: World) -> None:
        """审批单要能回答"批的是哪一份代码"。"""
        merger = RecordingMerger()
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)

        result = await pipeline.run(world.request())

        stored = await pipeline.gate.store.get(result.approval_request_id or "")
        assert stored.diff_summary.splitlines()[0] == f"commit:{_HEAD}"
        assert "README.md" in stored.diff_summary

    async def test_the_evidence_reference_points_at_this_verification(self, world: World) -> None:
        merger = RecordingMerger()
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)

        result = await pipeline.run(world.request())

        stored = await pipeline.gate.store.get(result.approval_request_id or "")
        assert _HEAD in stored.evidence_ref
        assert TASK_ID in stored.evidence_ref


class TestTheUnverifiedBranch:
    """T1 ② —— 没验过的分支进不了合并流程。"""

    async def test_a_failed_validation_sends_the_task_back_for_repair(self, world: World) -> None:
        merger = RecordingMerger()
        runner = FakeCheckRunner(default=_failing())
        pipeline = _pipeline(merger, runner)

        result = await pipeline.run(world.request())

        assert result.stage is PipelineStage.LOCAL_VALIDATION_FAILED
        assert result.task_state is TaskState.REPAIRING

    async def test_a_failed_validation_never_touches_main(self, world: World) -> None:
        events: list[str] = []
        merger = RecordingMerger(events=events)
        runner = FakeCheckRunner(events=events, default=_failing())
        pipeline = _pipeline(merger, runner)

        await pipeline.run(world.request())

        assert [
            event for event in events if "stage_merge" in event or "commit_staged" in event
        ] == []
        # main 一次都没被碰过 —— 连"读一下它的头"都没有。
        assert not any(str(world.main) in event for event in events)

    async def test_a_validation_that_could_not_run_blocks_instead_of_repairing(
        self, world: World
    ) -> None:
        """**没验成不是"验了没过"。**

        环境坏了去返工，等于让 Agent 去修一个根本没坏的东西 —— 而"去修一个
        没坏的东西"会把真正坏的地方留在原地。
        """
        merger = RecordingMerger()
        runner = FakeCheckRunner(default=_not_run())
        pipeline = _pipeline(merger, runner)

        result = await pipeline.run(world.request())

        assert result.stage is PipelineStage.LOCAL_VALIDATION_ERRORED
        assert result.task_state is TaskState.BLOCKED

    async def test_a_revocation_that_failed_replans(self, world: World) -> None:
        """从 PASSED 起步时状态机没有 PASSED→REPAIRING 这条边。

        复验没过说明"当时通过的那份计划已经不成立" —— 走 REPLAN 回去重做，
        而不是硬造一条边出来。
        """
        merger = RecordingMerger()
        runner = FakeCheckRunner(default=_failing())
        pipeline = _pipeline(merger, runner)

        result = await pipeline.run(world.request(task_state=TaskState.PASSED))

        assert result.stage is PipelineStage.LOCAL_VALIDATION_FAILED
        assert result.task_state is TaskState.PLANNED


class TestTheApproval:
    """审批这一段的规矩。"""

    async def test_the_pipeline_waits_for_the_human_instead_of_merging(self, world: World) -> None:
        merger = RecordingMerger()
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)

        result = await pipeline.run(world.request())

        assert result.stage is PipelineStage.AWAITING_APPROVAL
        assert result.task_state is TaskState.PASSED
        assert result.decision is not None
        assert result.decision.is_allowed is False

    async def test_a_rejected_approval_blocks_the_task(self, world: World) -> None:
        merger = RecordingMerger()
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)
        request = world.request()

        waiting = await pipeline.run(request)
        await pipeline.gate.reject(
            request_id=waiting.approval_request_id or "", approver="alice", comment="改得太糙"
        )

        result = await pipeline.run(request)
        assert result.stage is PipelineStage.APPROVAL_REFUSED
        assert result.task_state is TaskState.BLOCKED

    async def test_the_same_commit_reuses_the_same_request(self, world: World) -> None:
        """反复问不该反复发 —— 同一个提交上只有一张未决的审批单。"""
        merger = RecordingMerger()
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)
        request = world.request()

        first = await pipeline.run(request)
        second = await pipeline.run(request)

        assert first.approval_request_id == second.approval_request_id
        assert len(await pipeline.gate.store.list_for_task(TASK_ID)) == 1

    async def test_a_new_commit_withdraws_the_stale_request_and_asks_again(
        self, world: World
    ) -> None:
        """分支在等人期间又动了 —— 旧的那张单子作废，重新提。"""
        merger = RecordingMerger()
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)
        request = world.request()

        first = await pipeline.run(request)
        merger.head = "c" * 40
        second = await pipeline.run(request)

        assert second.approval_request_id != first.approval_request_id
        stale = await pipeline.gate.store.get(first.approval_request_id or "")
        assert stale.status is ApprovalStatus.WITHDRAWN

    async def test_an_approval_for_another_commit_does_not_release_the_merge(
        self, world: World
    ) -> None:
        """**这是"批的是哪一份代码"那条规矩的落点。**

        人批的是 commit A；分支头已经变成 commit B —— 拿 A 的批准放行 B，
        合进 main 的是人没看过的东西。
        """
        merger = RecordingMerger()
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)
        request = world.request()

        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")
        merger.head = "c" * 40

        result = await pipeline.run(request)

        assert result.stage is PipelineStage.AWAITING_APPROVAL
        assert result.decision is not None
        assert result.decision.is_allowed is False

    async def test_a_rejected_commit_is_not_asked_about_again(self, world: World) -> None:
        """人已经说过不 —— 机器再提一次只是刷屏。"""
        merger = RecordingMerger()
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)
        request = world.request()

        waiting = await pipeline.run(request)
        await pipeline.gate.reject(request_id=waiting.approval_request_id or "", approver="alice")
        result = await pipeline.run(request)

        assert result.stage is PipelineStage.APPROVAL_REFUSED
        assert len(await pipeline.gate.store.list_for_task(TASK_ID)) == 1

    async def test_an_approval_for_another_operation_is_not_reused_for_the_merge(
        self, world: World
    ) -> None:
        """**别的操作的批准不能拿来放行合并。**

        同一个任务上可能挂着"建分支""改代码"各种的审批单 —— 它们批的是
        别的操作。拿一条"建分支已批准"去放行合并，等于把一次批准用在了它
        没批过的事情上。
        """
        merger = RecordingMerger()
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)
        request = world.request()

        other = await pipeline.gate.request(
            task_id=TASK_ID,
            operation=Operation.BRANCH_CREATION,
            diff_summary=f"commit:{_HEAD}\n建分支",
            evidence_ref="local-verification:别的操作",
            requested_by="agent-1",
        )
        await pipeline.gate.approve(request_id=other.request_id, approver="alice")

        result = await pipeline.run(request)

        # 走的是**自己那张**合并审批单，不是那张"建分支已批准"。
        assert result.approval_request_id != other.request_id
        assert result.stage is PipelineStage.AWAITING_APPROVAL
        assert result.decision is not None
        assert result.decision.is_allowed is False

    async def test_an_approved_request_wins_over_a_rejected_one_for_the_same_commit(
        self, world: World
    ) -> None:
        """同一份内容上既有批准又有拒绝时，取能放行的那条。

        **"能不能放行"优先于"哪条更新"。** 这条路径的语义是"这份内容有没有
        被批过"，不是"这个任务有没有被否决过" —— 被拒的那一条同样留在库里
        （审计查得到），但它否决的是另一次请求。

        不这么定的话，同一个提交上同时存在批准与拒绝时，结论会随"哪条
        更新"而变 —— 而"哪条更新"在两条请求时间戳相同时是没有答案的。
        """
        merger = RecordingMerger()
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)
        request = world.request()

        rejected = await pipeline.gate.request(
            task_id=TASK_ID,
            operation=Operation.MERGE_TO_MAIN,
            diff_summary=f"commit:{_HEAD}\n同一份内容",
            evidence_ref="local-verification:同一份内容",
            requested_by="agent-1",
        )
        await pipeline.gate.reject(request_id=rejected.request_id, approver="alice")
        approved = await pipeline.gate.request(
            task_id=TASK_ID,
            operation=Operation.MERGE_TO_MAIN,
            diff_summary=f"commit:{_HEAD}\n同一份内容",
            evidence_ref="local-verification:同一份内容",
            requested_by="agent-1",
        )
        await pipeline.gate.approve(request_id=approved.request_id, approver="alice")

        result = await pipeline.run(request)

        assert result.approval_request_id == approved.request_id
        assert result.stage is PipelineStage.MERGED


class TestTheMerge:
    """合并这一步的三种失败与撤销。"""

    async def test_a_conflict_aborts_the_merge_and_sends_the_task_back(self, world: World) -> None:
        events: list[str] = []
        merger = RecordingMerger(
            events=events, stage_error=MergeConflictError("CONFLICT (content): 撞了")
        )
        runner = FakeCheckRunner(events=events)
        pipeline = _pipeline(merger, runner)
        request = world.request()

        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")
        result = await pipeline.run(request)

        assert result.stage is PipelineStage.MERGE_CONFLICTED
        assert result.task_state is TaskState.REPAIRING
        assert "commit_staged_merge" not in events

    async def test_a_failed_verification_on_main_aborts_the_merge(self, world: World) -> None:
        """**R-057 真正落地的地方。**

        任务分支上验过了，合进 main 之后没过 —— 那么这次合并不该成立。撤销
        之后 main 一个提交都没前进，坏的东西进不去。
        """
        events: list[str] = []
        merger = RecordingMerger(events=events)
        runner = FakeCheckRunner(events=events, results={world.main: _failing()})
        pipeline = _pipeline(merger, runner)
        request = world.request()

        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")
        result = await pipeline.run(request)

        assert result.stage is PipelineStage.MERGED_CONTENT_FAILED
        assert result.task_state is TaskState.REPAIRING
        assert "abort_staged_merge" in events
        assert "commit_staged_merge" not in events

    async def test_a_verification_that_could_not_run_on_main_blocks(self, world: World) -> None:
        merger = RecordingMerger()
        runner = FakeCheckRunner(results={world.main: _not_run()})
        pipeline = _pipeline(merger, runner)
        request = world.request()

        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")
        result = await pipeline.run(request)

        assert result.stage is PipelineStage.MERGED_CONTENT_ERRORED
        assert result.task_state is TaskState.BLOCKED

    async def test_a_merge_that_could_not_run_blocks_the_task(self, world: World) -> None:
        """git 自己没干成 —— 这不是代码的问题，返工没有意义。"""
        merger = RecordingMerger(stage_error=MergeError("fatal: 工作区不干净"))
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)
        request = world.request()

        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")
        result = await pipeline.run(request)

        assert result.stage is PipelineStage.MERGE_FAILED
        assert result.task_state is TaskState.BLOCKED

    async def test_an_abort_that_failed_is_reported(self, world: World) -> None:
        """撤销不掉意味着 main 停在"合并进行中" —— 那比这次合并失败严重得多。

        不能被一句"合并失败"盖过去。
        """
        merger = RecordingMerger(abort_error=MergeError("fatal: 撤销不掉了"))
        runner = FakeCheckRunner(results={world.main: _failing()})
        pipeline = _pipeline(merger, runner)
        request = world.request()

        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")
        result = await pipeline.run(request)

        assert result.stage is PipelineStage.MERGED_CONTENT_FAILED
        assert any("撤销" in reason for reason in result.reasons)

    async def test_nothing_to_merge_still_completes_the_task(self, world: World) -> None:
        merger = RecordingMerger(staged=StagedMerge(changed=False, detail="已经是最新"))
        runner = FakeCheckRunner()
        pipeline = _pipeline(merger, runner)
        request = world.request()

        waiting = await pipeline.run(request)
        await pipeline.gate.approve(request_id=waiting.approval_request_id or "", approver="alice")
        result = await pipeline.run(request)

        assert result.stage is PipelineStage.MERGED
        assert result.task_state is TaskState.COMPLETED


class TestTheRequest:
    """请求本身的自检。"""

    def test_the_contract_must_belong_to_the_task(self, world: World) -> None:
        """拿错契约，验的就不是这件事。"""
        with pytest.raises(ValueError, match="拿错契约"):
            world.request(
                contract=AcceptanceContract(
                    task_id="TASK-OTHER", required=(RequiredCheck.UNIT_TESTS_PASS,)
                )
            )

    def test_the_diff_summary_is_required(self, world: World) -> None:
        with pytest.raises(ValueError, match="改动说明"):
            world.request(diff_summary="   ")

    def test_the_entry_state_must_be_verifying_or_passed(self, world: World) -> None:
        with pytest.raises(ValueError, match="VERIFYING 或 PASSED"):
            world.request(task_state=TaskState.RUNNING)
