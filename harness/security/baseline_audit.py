"""基线变更的审计 —— **用的是审计那本册子，不另起一本**。

设计依据：docs/tasks.md TASK-P1-042（T1 ②"尝试下调基线被拒绝并留下审计
记录"）、TASK-P1-045（R-062 / R-063 的四要素）、docs/design.md §3.5D。

**为什么不为基线另建一套留痕。** 仓库里已经有了一本审计册子
（:mod:`harness.security.audit`），它管的是"谁、在哪个任务上、对什么做了什么、
结果如何"。基线下调也是"谁、在哪个任务上、对什么做了什么" —— 另起一本的
后果是**两本册子都得查**，而查的人只查得动一本：他查了审计册子，没看到
那次下调，于是以为没人动过标准。

**翻译，而不是复刻。** 领域层给的是"一次下调尝试"
（:class:`core.task.coverage_baseline.LoweringAttempt`），这里把它翻成审计
册子认识的一行：

- **主体**是提下调的人；
- **任务**是这次尝试发生在哪个任务上 —— 没有任务就用占位值，不用空串
  （空串会让读的人以为"漏填了"）；
- **资源**是那个模块的基线；
- **结果与判定**分开放：被拒的是"没做成"，不是"做砸了"。

**写失败就抛。** 审计写不下来 = 这件事没发生过 = 不能当作发生了 ——
:mod:`core.task.coverage_baseline` 那边的顺序是"先写审计、后改基线"，所以
这里抛出异常，基线就不会被改。
"""

from __future__ import annotations

from core.task.coverage_baseline import LoweringAttempt
from harness.security.audit import (
    NOT_APPLICABLE,
    AuditDecision,
    AuditRecorder,
    AuditResult,
)

#: 审计里记的操作名 —— **一次下调尝试**。
LOWER_BASELINE_OPERATION: str = "lower_coverage_baseline"

#: 资源名的前缀 —— 资源是"某个模块的覆盖率基线"。
RESOURCE_PREFIX: str = "coverage_baseline"


class CoverageBaselineAuditSink:
    """把基线下调尝试记进审计册子。

    :param recorder: 审计的写入口。**只给写入口，不给存储** —— 业务代码
        拿不到存储，就绕不过审计。
    """

    __slots__ = ("_recorder",)

    def __init__(self, recorder: AuditRecorder) -> None:
        self._recorder = recorder

    def record_lowering_attempt(self, attempt: LoweringAttempt) -> None:
        """记下一次下调尝试 —— **写失败就抛**。

        **成功与被拒都记。** 被拒的更要记：拒绝只留下一句"没做"，而"为什么
        没做"往往正是后来要复盘的东西。
        """
        approved = attempt.approved
        self._recorder.record_tool_call(
            agent_id=attempt.requested_by,
            task_id=attempt.task_id or NOT_APPLICABLE,
            project_id=attempt.project_id,
            # 基线不是工作空间里的东西 —— 没有工作空间。用占位值而不是空
            # 字符串：审计记录的元素不许为空，空的会让读的人以为"漏填了"。
            workspace_id=NOT_APPLICABLE,
            resource=f"{RESOURCE_PREFIX}:{attempt.module}",
            operation=LOWER_BASELINE_OPERATION,
            result=AuditResult.SUCCESS if approved else AuditResult.DENIED,
            decision=AuditDecision.ALLOW if approved else AuditDecision.DENY,
            reason=_reason(attempt),
        )


def _reason(attempt: LoweringAttempt) -> str:
    """把理由说全 —— **数字要写在理由里**。

    只写"不予放行"不够：过几个月再看这一行，得知道它拦的是"从多少降到
    多少"。数字与理由写在一起，这条记录才能自己读懂。
    """
    before = "（没有基线）" if attempt.current is None else str(attempt.current)
    after = str(attempt.proposed)
    verdict = "有批准，放行" if attempt.approved else "没有批准，不予放行"
    return (
        f"把 {attempt.module} 的覆盖率基线从 {before} 降到 {after} —— {verdict} —— {attempt.reason}"
    )


__all__ = ["LOWER_BASELINE_OPERATION", "RESOURCE_PREFIX", "CoverageBaselineAuditSink"]
