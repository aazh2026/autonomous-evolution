"""任务结果的判定 —— 谁有资格说"做成了"。

验证（T1 ④）：``self_assessment.completed = true`` 但验证引擎判定失败时，
**任务整体状态仍为失败** —— 断言 self_assessment 不参与验收判定。

**这是本文件存在的全部理由。** design.md §3.4 写着一句警告："注意
``self_assessment`` 只是 Agent 的自我报告，不是验收结果。" 警告写在文档里，
靠人记着 —— 而人迟早会忘，或者在赶时间时"就这一次"地信了它。

所以这条规矩被做成了**结构**，而不是提醒：

- :func:`core.task.outcome.resolve_outcome` 只接受
  :class:`core.task.outcome.StatusSource` 协议，那个协议**只有** ``status``
  一个成员 —— 在判定函数里读 ``self_assessment``，mypy 直接报错；
- 判定结果只由**独立验证的结论**决定，而验证结论来自执行器之外
  （design.md §3.5B）。

这是一条**跨层**的测试：判定规则住在 ``core``，执行结果住在 ``executors``，
而要证明的正是这两者接上时，自我报告没有漏进判定里。所以这里同时用了两边的
东西 —— 这不是分层违规，是分层的**接缝**，接缝才是最该测的地方。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from decimal import Decimal

import pytest

from core.task.outcome import (
    ExecutionStatus,
    OutcomeResolution,
    StatusSource,
    TaskOutcome,
    VerificationReport,
    VerificationVerdict,
    resolve_outcome,
)
from executors.base import (
    ExecutionArtifacts,
    ExecutionMetrics,
    ExecutionResult,
    SelfAssessment,
)


@dataclass(frozen=True, slots=True)
class _StatusOnly:
    """只暴露 status 的最小实现 —— 与 :class:`StatusSource` 同形。"""

    status: ExecutionStatus


def _result(
    *,
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    self_assessment_completed: bool = True,
) -> ExecutionResult:
    """造一个统一结果。"""
    return ExecutionResult(
        task_id="TASK-1",
        executor="claude-code",
        status=status,
        artifacts=ExecutionArtifacts(branch="agent/TASK-1", commit="abc123", changed_files=3),
        execution=ExecutionMetrics(
            duration=Decimal("360"), tool_calls=35, cost=Decimal("2.4"), currency="USD"
        ),
        self_assessment=SelfAssessment(completed=self_assessment_completed, limitations=()),
        idempotency_key="TASK-1:1",
    )


def _report(verdict: VerificationVerdict, reasons: tuple[str, ...] = ()) -> VerificationReport:
    return VerificationReport(verdict=verdict, reasons=reasons)


class TestTheExecutorCannotGradeItself:
    """T1 ④ —— 自我报告不参与验收判定。"""

    def test_a_confident_executor_with_a_failed_verdict_is_still_failed(self) -> None:
        """Agent 说"我做完了"，验证引擎说"没过" —— 判决是**没过**。"""
        result = _result(status=ExecutionStatus.COMPLETED, self_assessment_completed=True)

        resolution = resolve_outcome(result, _report(VerificationVerdict.FAILED, ("测试未通过",)))

        assert resolution.outcome is TaskOutcome.FAILED

    def test_a_pessimistic_executor_with_a_passed_verdict_is_still_completed(self) -> None:
        """反向也一样：Agent 说"我做得不完美"，但验证通过了 —— 判决是**完成**。

        只堵住"自我报告把失败抬成成功"是不够的 —— 那只是让执行器**不能作弊**；
        真正该守的是"执行器不能替验证引擎说话"，两个方向都不行。
        """
        result = _result(status=ExecutionStatus.COMPLETED, self_assessment_completed=False)

        resolution = resolve_outcome(result, _report(VerificationVerdict.PASSED))

        assert resolution.outcome is TaskOutcome.COMPLETED

    @pytest.mark.parametrize("self_assessment_completed", [True, False])
    def test_flipping_the_self_assessment_does_not_change_the_verdict(
        self, self_assessment_completed: bool
    ) -> None:
        """把自我报告翻个面，结论必须一模一样 —— 这才是"不参与"的证据。"""
        report = _report(VerificationVerdict.FAILED, ("测试未通过",))

        flipped = resolve_outcome(
            _result(self_assessment_completed=not self_assessment_completed), report
        )
        unchanged = resolve_outcome(
            _result(self_assessment_completed=self_assessment_completed), report
        )

        assert flipped.outcome is unchanged.outcome
        assert flipped.outcome is TaskOutcome.FAILED

    def test_the_judgement_takes_no_self_assessment(self) -> None:
        """判定的入参就这两个 —— 自我报告连**进门**的机会都没有。"""
        parameters = set(inspect.signature(resolve_outcome).parameters)

        assert parameters == {"result", "report"}

    def test_the_protocol_exposes_only_the_status(self) -> None:
        """收窄输入面，捷径就没有落脚的地方。"""
        # ``__protocol_attrs__`` 是 typing 在建类时写进去的，mypy 不认它。
        protocol_attrs: frozenset[str] = StatusSource.__protocol_attrs__  # type: ignore[attr-defined]

        assert protocol_attrs == frozenset({"status"})


class TestTheVerificationVerdictDecides:
    """执行器说"完成了"之后，看独立验证怎么说。"""

    def test_a_passed_verdict_completes_the_task(self) -> None:
        resolution = resolve_outcome(_result(), _report(VerificationVerdict.PASSED))

        assert resolution.outcome is TaskOutcome.COMPLETED
        assert resolution.verdict is VerificationVerdict.PASSED

    def test_a_failed_verdict_fails_the_task(self) -> None:
        resolution = resolve_outcome(
            _result(), _report(VerificationVerdict.FAILED, ("覆盖率掉了",))
        )

        assert resolution.outcome is TaskOutcome.FAILED
        assert "覆盖率掉了" in resolution.reason

    def test_a_broken_verification_engine_is_not_a_failure(self) -> None:
        """验证引擎自己崩了 ≠ 验证判定失败。

        前者要人来看（自动修复多半在修一个不存在的问题），后者可以走有界修复。
        混成一个，下游就只能一律重试或一律放弃。
        """
        resolution = resolve_outcome(_result(), _report(VerificationVerdict.ERROR))

        assert resolution.outcome is TaskOutcome.VERIFICATION_ERROR

    def test_no_verification_is_not_a_pass(self) -> None:
        """没验过的东西不算验过了 —— fail-closed。"""
        resolution = resolve_outcome(_result(), None)

        assert resolution.outcome is TaskOutcome.VERIFICATION_ERROR
        assert resolution.verdict is None

    def test_the_reason_says_why(self) -> None:
        """一条没有理由的结论，读的人只能靠猜。"""
        resolution = resolve_outcome(
            _result(), _report(VerificationVerdict.FAILED, ("断言不成立",))
        )

        assert "断言不成立" in resolution.reason


class TestTheExecutorStatusDecides:
    """执行器报的状态本身也是判定的一部分。"""

    def test_a_failed_execution_is_failed(self) -> None:
        resolution = resolve_outcome(_result(status=ExecutionStatus.FAILED), None)

        assert resolution.outcome is TaskOutcome.FAILED

    def test_a_cancelled_execution_is_cancelled(self) -> None:
        resolution = resolve_outcome(_result(status=ExecutionStatus.CANCELLED), None)

        assert resolution.outcome is TaskOutcome.CANCELLED

    def test_a_timed_out_execution_is_not_a_failure(self) -> None:
        """超时 ≠ 失败（R-097 / I-03）。

        超时之后要先探测会话能不能恢复。直接判失败，会话可能还活着 ——
        那是僵尸会话的来源。
        """
        resolution = resolve_outcome(_result(status=ExecutionStatus.TIMED_OUT), None)

        assert resolution.outcome is not TaskOutcome.FAILED
        assert resolution.outcome is TaskOutcome.TIMED_OUT

    @pytest.mark.parametrize("status", [ExecutionStatus.PENDING, ExecutionStatus.RUNNING])
    def test_a_non_terminal_status_cannot_be_judged(self, status: ExecutionStatus) -> None:
        """还在跑的东西没有结论 —— 硬判一个就是编。"""
        with pytest.raises(ValueError, match="不是终态"):
            resolve_outcome(_StatusOnly(status=status), None)

    @pytest.mark.parametrize(
        "status",
        [ExecutionStatus.FAILED, ExecutionStatus.CANCELLED, ExecutionStatus.TIMED_OUT],
    )
    def test_the_verdict_is_carried_through(self, status: ExecutionStatus) -> None:
        """判定结果里带着**原始**验证结论 —— 判定与证据要对得上。

        执行超时但验证引擎已经跑完了，是常事：超时的处置是"先探测会话能不能
        恢复"，而恢复与否的判断要靠那份已经拿到的验证结论。结论被丢掉，就
        只剩一个"超时"两个字，下游什么都判断不了。
        """
        report = _report(VerificationVerdict.FAILED, ("测试未通过",))

        resolution = resolve_outcome(_result(status=status), report)

        assert resolution.verdict is VerificationVerdict.FAILED


class TestTheResolutionIsInspectable:
    def test_it_carries_the_outcome_the_reason_and_the_verdict(self) -> None:
        resolution: OutcomeResolution = resolve_outcome(
            _result(), _report(VerificationVerdict.PASSED)
        )

        assert resolution.outcome is TaskOutcome.COMPLETED
        assert resolution.reason.strip()
        assert resolution.verdict is VerificationVerdict.PASSED
