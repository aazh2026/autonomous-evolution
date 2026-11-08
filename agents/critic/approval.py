"""把审查报告翻译成放行凭据 —— 角色层与领域层之间的那一步。

设计依据：docs/tasks.md TASK-P1-056（T1 ④ 未被 Critic 放行的 Decision 不产生
Task）、TASK-P1-055（否决必须带证据）；docs/design.md §5（规划阶段的输入是
"已批准的 Decisions"）。

**为什么要有这一层。** 审查报告是角色层的东西（``agents/``），领域层不认识
它 —— 领域层反向依赖角色层就把层次搞反了。所以领域层定义的是一个**中性凭据**
(:class:`~core.planning.roadmap.DecisionApproval`)，由这里把"这份报告否不否决
这条决策"翻译成"放行 / 不放行 + 理由"。

**翻译的时候不能丢信息。** 不放行的理由里带着那几项检查的结论 —— 否决理由
读不到，就等于这次审查白做了。而放行的理由里要写清**有没有未核实的项**：
一份九项里有三项"未核实"的报告，和一份九项全"通过"的报告，放行起来不是
同一回事，虽然都放行。

**"没审"与"审了没否决"不是一回事。** 没审过的决策压根不会有凭据 ——
领域层把"缺凭据"当成不放行处理，所以这里不需要（也不该）造一个"未审过"
的凭据出来：造出来就等于给"忘了审"发了一张通行证。
"""

from __future__ import annotations

from agents.critic.checks import CHECK_QUESTIONS
from agents.critic.report import CriticReport
from core.evolution.decision import EvolutionDecision
from core.planning.roadmap import DecisionApproval


class ReportMismatchError(ValueError):
    """报告与决策对不上。"""


def approval_for(decision: EvolutionDecision, report: CriticReport) -> DecisionApproval:
    """把一份审查报告翻译成这条决策的放行凭据。

    **报告必须审的就是这条决策的候选。** 审的是 A 候选、放行的是 B 决策 ——
    那是一份张冠李戴的报告，而它张冠李戴的时候**不会报错**，所以这里必须
    自己核。
    """
    if report.candidate_id != decision.candidate_id:
        raise ReportMismatchError(
            f"报告审的是候选 {report.candidate_id!r}，"
            f"这条决策来自候选 {decision.candidate_id!r} —— 对不上"
        )

    if report.vetoed:
        reasons = "；".join(
            f"{CHECK_QUESTIONS.get(outcome.check, outcome.check.value)}：{outcome.summary}"
            for outcome in report.vetoing_outcomes
        )
        return DecisionApproval(
            decision_id=decision.id,
            candidate_id=decision.candidate_id,
            approved=False,
            reason=f"Critic 否决 —— {reasons}",
        )

    unverified = report.unverified
    if unverified:
        detail = "、".join(
            CHECK_QUESTIONS.get(outcome.check, outcome.check.value) for outcome in unverified
        )
        reason = f"九项检查无否决，但有 {len(unverified)} 项未核实：{detail}"
    else:
        reason = "九项检查全部通过，无否决"
    return DecisionApproval(
        decision_id=decision.id,
        candidate_id=decision.candidate_id,
        approved=True,
        reason=reason,
    )


__all__ = ["ReportMismatchError", "approval_for"]
