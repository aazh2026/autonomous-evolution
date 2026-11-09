"""决策质量层 —— 能否选对问题。

设计依据：docs/design.md §10（决策质量层：决策采纳率、误报率、价值预测
偏差）；docs/tasks.md TASK-P1-071。

这一层量的不是"系统动作多不多"，而是"系统**挑**的那个东西对不对"。

**误报率为什么限定在"被采纳去做的决策"上。** 一个无效候选被 reject 掉，
没有任何损失 —— 它甚至说明系统判断对了。把它算进误报率，等于因为系统
**没有**浪费资源而惩罚它。只有那些**真的被派了任务**的无效候选才构成
浪费。所以这里的分母是"被 record 的决策"，不是"全部决策"。

**决策采纳率量的是另一件事：系统的建议有多少被人/Critic 接受。** 一个
总是被推翻的系统，说明它的建议质量不够 —— 人在替它做决定，那它就没有
在自主决策。

**价值预测偏差（§10 里的第三项）阶段一不做。** 它需要"预测的价值"与
"实际兑现的价值"可比，而实际价值依赖达成度之外的归因 —— 一个候选被
reject 时，究竟是选错了还是当时信息不足？这在没有额外标注的情况下无法
判定。硬算出来的是噪声。

**分母从标注集来，不从决策记录来。** 一条决策记录自己带着的
``candidate_label`` 是调用方塞进去的 —— 让被评价的一方决定分母，那分母就
不可信了。:func:`false_positive_rate_from_labels` 强制从标注集
（TASK-P1-083）取分母：标注集里没有的候选一律不进分母，标注集不在则报
**基线未建立**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from evaluation.common import MetricResult, MetricState, Verdict, rate
from evaluation.labels.pilot import LabelSet, LabelSetError, LabelSetMissingError, LabelSetStore

#: 决策采纳率的指标 id。
DECISION_ADOPTION_RATE = "decision_adoption_rate"
#: 误报率的指标 id。
FALSE_POSITIVE_RATE = "false_positive_rate"

#: "决定去做"的决策。任务三态（record / defer / reject）之一。
DECISION_RECORD = "record"


@dataclass(frozen=True)
class DecisionRecord:
    """一条演进决策 —— 决策质量层指标的唯一输入。

    ``candidate_label`` 是该决策所依据的候选的人工标注（TASK-P1-083）；
    ``None`` 表示尚未标注。
    """

    decision_id: str
    candidate_id: str
    #: record / defer / reject。
    decision: str
    #: 该决策被 Critic 否决或人工推翻时留痕（``core.entities.EvolutionDecision``）。
    overridden_by: str | None = None
    #: 候选的人工标注结论。
    candidate_label: Verdict | None = None


def decision_adoption_rate(decisions: Sequence[DecisionRecord]) -> MetricResult:
    """决策采纳率 = 未被推翻的 record 决策数 / record 决策总数。

    只看 ``record`` 决策：``defer`` 与 ``reject`` 本来就不是"要执行它"的
    提议，把它们算进分母，指标就变成了"系统动作的激进程度"，而那与
    决策**质量**无关 —— 一个什么都 reject 的系统在这里会拿满分。
    """
    proposals = [item for item in decisions if item.decision == DECISION_RECORD]
    if not proposals:
        return rate(DECISION_ADOPTION_RATE, 0, 0, empty_state=MetricState.NO_SAMPLES)
    adopted = sum(1 for item in proposals if not item.overridden_by)
    return rate(
        DECISION_ADOPTION_RATE,
        adopted,
        len(proposals),
        detail={
            "total_decisions": len(decisions),
            "proposals": len(proposals),
            "overridden": len(proposals) - adopted,
        },
    )


def false_positive_rate(decisions: Sequence[DecisionRecord]) -> MetricResult:
    """误报率 = 落在无效候选上的 record 决策数 / **已标注**的 record 决策数。

    分母只算已标注的 —— 未标注的决策既不能说它错也不能说它对。

    没有标注时报 ``baseline_missing``，不报 0：**"还没标"与"全是无效"是
    两件相反的事**，报 0 会让看板显示"误报率 0%"，读的人会以为系统很准。
    """
    proposals = [item for item in decisions if item.decision == DECISION_RECORD]
    labelled = [item for item in proposals if item.candidate_label is not None]
    if not labelled:
        state = MetricState.NO_SAMPLES if not proposals else MetricState.BASELINE_MISSING
        empty = rate(FALSE_POSITIVE_RATE, 0, 0, empty_state=state)
        return MetricResult(
            metric_id=empty.metric_id,
            state=empty.state,
            value=empty.value,
            numerator=empty.numerator,
            denominator=empty.denominator,
            detail={
                "reason": "没有任何决策" if not proposals else "缺少人工标注基线（TASK-P1-083）",
                "total_decisions": len(decisions),
                "proposals": len(proposals),
                "labelled_proposals": len(labelled),
            },
        )
    false_positives = sum(1 for item in labelled if item.candidate_label is Verdict.INVALID)
    return rate(
        FALSE_POSITIVE_RATE,
        false_positives,
        len(labelled),
        detail={
            "total_decisions": len(decisions),
            "proposals": len(proposals),
            "labelled_proposals": len(labelled),
        },
    )


def apply_labels(
    decisions: Sequence[DecisionRecord],
    label_set: LabelSet,
) -> tuple[DecisionRecord, ...]:
    """按标注集给决策打标注 —— **分母只从标注集来**。

    标注集里没有的候选，标注一律清成 ``None`` —— 调用方自己带的那份标注
    在这里会被丢掉。留着它就等于"分母可以来自任何地方"，而误报率最不能
    容忍的就是分母由被评价的一方决定。

    试点仓库里，**候选 id 就是问题编号**（``issue_ref``）—— 试点阶段"系统
    提出的候选"与"人工审过的问题"是一一对应的，所以分母可以直接从标注集
    取。
    """
    return tuple(
        replace(item, candidate_label=label_set.verdict_of(item.candidate_id)) for item in decisions
    )


def false_positive_rate_from_labels(
    decisions: Sequence[DecisionRecord],
    store: LabelSetStore,
) -> MetricResult:
    """误报率 —— **分母取自标注集**。

    标注集不在 → 报**基线未建立**（``baseline_missing``），**不报 0**。
    "还没标"与"全是无效"是相反的两件事，报 0 会让看板显示"误报率 0%"，
    读的人会以为系统很准。

    标注集在但内容坏了 → 同样报**基线未建立**，并在 ``detail`` 里说清是
    **文件坏了**（:class:`~evaluation.labels.pilot.LabelSetError`）而不是
    **文件不在**（:class:`~evaluation.labels.pilot.LabelSetMissingError`）——
    这两件事的处置方式不同：前者要修文件，后者要补标注。
    """
    try:
        label_set = store.load()
    except LabelSetMissingError as exc:
        return _missing_baseline(f"缺少人工标注基线（TASK-P1-083）：{exc}")
    except LabelSetError as exc:
        return _missing_baseline(f"人工标注基线不可用（TASK-P1-083）：{exc}")

    result = false_positive_rate(apply_labels(decisions, label_set))
    return MetricResult(
        metric_id=result.metric_id,
        state=result.state,
        value=result.value,
        numerator=result.numerator,
        denominator=result.denominator,
        detail={**result.detail, "label_set_size": len(label_set)},
    )


def _missing_baseline(reason: str) -> MetricResult:
    """基线未建立 —— **不是 0**。"""
    empty = rate(FALSE_POSITIVE_RATE, 0, 0, empty_state=MetricState.BASELINE_MISSING)
    return MetricResult(
        metric_id=empty.metric_id,
        state=empty.state,
        value=empty.value,
        numerator=empty.numerator,
        denominator=empty.denominator,
        detail={"reason": reason},
    )


__all__ = [
    "DECISION_ADOPTION_RATE",
    "FALSE_POSITIVE_RATE",
    "DecisionRecord",
    "apply_labels",
    "decision_adoption_rate",
    "false_positive_rate",
    "false_positive_rate_from_labels",
]
