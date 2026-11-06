"""执行结果的判定 —— 任务成败由谁说了算。

设计依据：docs/design.md §3.4（统一执行结果 schema；"注意 ``self_assessment``
只是 Agent 的自我报告，不是验收结果"）、§3.5B（验证引擎独立于执行器）；
docs/tasks.md TASK-P1-030（验证 T1 ④）。

**这个模块存在的原因，是把一件事说清楚：谁有资格判定任务成功。**

执行器会汇报"我做完了"（``self_assessment.completed = true``）。但 Agent 的
自我报告不是验收结果 —— 它是**被验收的对象**对验收结论的预判。让执行器
自己说了算，等于让被测者给自己打分。

所以判定被拆成两个输入：

1. **执行器报的状态**（:class:`ExecutionStatus`）—— "我跑完了没有、失败没有、
   被取消没有、超时没有"；
2. **独立验证的结论**（:class:`VerificationReport`）—— 由执行器之外的验证
   引擎给出（design.md §3.5B）。

而 ``self_assessment`` **不出现在这两个输入里**。这不是"约定不看它"，而是
类型层面的隔离：:func:`resolve_outcome` 只接受 :class:`StatusSource` 协议 ——
那个协议**只有** ``status`` 一个成员。在判定函数里读 ``self_assessment``，
mypy 会直接报错。

**四条判定规则，每一条都对应一件不该被混为一谈的事**：

- ``COMPLETED`` + 验证通过 → 任务完成；
- ``COMPLETED`` + 验证未通过 → 任务**失败**（哪怕 Agent 说自己完成了）；
- ``COMPLETED`` + 验证引擎自己出错 → 结论**不可得**
  （:attr:`TaskOutcome.VERIFICATION_ERROR`）—— 与"验证判定失败"必须分开：
  前者要人来看，后者可以走自动修复；
- 超时 → :attr:`TaskOutcome.TIMED_OUT`，**不等于失败**（R-097 / I-03）——
  超时之后要先探测会话能不能恢复，直接置为失败会留下僵尸会话。

**还有一条容易被当成"小事"的**：执行器说完成了，但**根本没有独立验证
结论**时，判定是"不可得"而不是"完成"。fail-closed —— 没验过的东西不算
验过了。

**与 :mod:`core.task.state_machine` 的分工**：那里的 14 态 ``TaskState`` 是
任务在**工程流程**里的位置（DISCOVERED → … → COMPLETED）；这里的
:class:`TaskOutcome` 是**执行层**的判定。两者不是一回事，映射由编排层负责
（TASK-P1-060 / TASK-P1-062）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ExecutionStatus(StrEnum):
    """执行器报告的状态。

    五个终态之外还有两个非终态 —— ``PENDING`` / ``RUNNING`` 是会话进行到
    一半时的状态，**不能**用来判定任务结果。

    **超时单列成一个状态，不是失败的一种。** design.md R-097 / I-03：Agent
    超时不等于任务失败，需要先判断能不能恢复。把超时并进 ``FAILED``，
    编排层就会直接判死，而会话可能还活着 —— 那是僵尸会话的来源。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        """是不是终态 —— 只有终态能判定任务结果。"""
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES: frozenset[ExecutionStatus] = frozenset(
    {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.TIMED_OUT,
    }
)


class VerificationVerdict(StrEnum):
    """独立验证引擎给出的结论。"""

    PASSED = "passed"
    FAILED = "failed"
    #: 验证引擎**自己**出错 —— 不是"验证判定失败"，而是"没得出结论"。
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """独立验证的结论。

    ``reasons`` 记下"哪几条没过、为什么" —— 一个不带理由的 ``FAILED`` 与
    ``ERROR`` 在下游看起来一模一样，人只能靠猜来判断该不该修。
    """

    verdict: VerificationVerdict
    reasons: tuple[str, ...] = ()
    #: 各项检查的结论：名称 → 是否通过。
    checks: tuple[tuple[str, bool], ...] = ()


class TaskOutcome(StrEnum):
    """任务在执行层的最终判定。

    ``VERIFICATION_ERROR`` 与 ``FAILED`` 分开，是因为下游的处置完全不同：
    ``FAILED`` 可以走有界修复，``VERIFICATION_ERROR`` 得先让人看一眼 ——
    验证引擎自己崩了的时候，"自动修复"多半是在修一个不存在的问题。
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: 超时。**不等于失败** —— 还要先探测会话能不能恢复（R-097 / I-03）。
    TIMED_OUT = "timed_out"
    #: 结论不可得：没有验证结论，或验证引擎自己出错。
    VERIFICATION_ERROR = "verification_error"


@dataclass(frozen=True, slots=True)
class OutcomeResolution:
    """判定结果与理由。

    ``reason`` 是给人看的 —— 判定结果进了日志与看板，但"为什么"不会自己
    出现。少了它，一条 ``FAILED`` 就只是一句没有线索的结论。

    ``verdict`` 保留独立验证的原始结论（没有验证时为 ``None``），方便把
    判定与证据对起来。
    """

    outcome: TaskOutcome
    reason: str
    verdict: VerificationVerdict | None = None


@runtime_checkable
class StatusSource(Protocol):
    """执行结果的**最小**视图 —— 只有 ``status``。

    刻意不暴露 ``self_assessment``。这不是客气：判定函数一旦拿得到
    ``self_assessment``，"Agent 说自己做完了"就会变成一个迟早会被用上的
    捷径 —— 而那正是 design.md §3.4 明确禁止的（"self_assessment 只是
    Agent 的自我报告，不是验收结果"）。

    把可用的信息收窄到一个成员，捷径就没有落脚的地方。
    """

    @property
    def status(self) -> ExecutionStatus: ...


def resolve_outcome(result: StatusSource, report: VerificationReport | None) -> OutcomeResolution:
    """判定任务结果。

    ``report`` 为 ``None`` 表示**没有独立验证结论** —— 那不是"通过"，是
    "没验过"。按 fail-closed 处理（:attr:`TaskOutcome.VERIFICATION_ERROR`）。

    ``result`` 只需要暴露 ``status``：判定过程**不看** ``self_assessment``。
    """
    status = result.status
    if not status.is_terminal:
        raise ValueError(f"状态 {status.value} 不是终态，不能判定任务结果")

    verdict = report.verdict if report is not None else None

    if status is ExecutionStatus.COMPLETED:
        return _resolve_completed(report)
    if status is ExecutionStatus.FAILED:
        return OutcomeResolution(TaskOutcome.FAILED, "执行器报告执行失败", verdict)
    if status is ExecutionStatus.CANCELLED:
        return OutcomeResolution(TaskOutcome.CANCELLED, "执行已确认取消", verdict)
    return OutcomeResolution(
        TaskOutcome.TIMED_OUT,
        "执行超时 —— 超时不等于失败，须先探测会话能否恢复",
        verdict,
    )


def _resolve_completed(report: VerificationReport | None) -> OutcomeResolution:
    """执行器说"完成了" —— 还要看独立验证怎么说。"""
    if report is None:
        return OutcomeResolution(
            TaskOutcome.VERIFICATION_ERROR,
            "没有独立验证结论 —— 未经验证不得判为完成",
            None,
        )

    if report.verdict is VerificationVerdict.PASSED:
        return OutcomeResolution(TaskOutcome.COMPLETED, "独立验证通过", report.verdict)

    if report.verdict is VerificationVerdict.FAILED:
        detail = "；".join(report.reasons)
        reason = f"独立验证未通过：{detail}" if detail else "独立验证未通过"
        return OutcomeResolution(TaskOutcome.FAILED, reason, report.verdict)

    return OutcomeResolution(
        TaskOutcome.VERIFICATION_ERROR,
        "验证引擎自身出错，结论不可得",
        report.verdict,
    )


__all__ = [
    "ExecutionStatus",
    "OutcomeResolution",
    "StatusSource",
    "TaskOutcome",
    "VerificationReport",
    "VerificationVerdict",
    "resolve_outcome",
]
