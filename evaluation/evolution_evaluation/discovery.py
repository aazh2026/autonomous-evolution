"""自主发现层 —— 能否找到真实问题。

设计依据：docs/design.md §10（自主发现层：有效问题发现率、重复发现率）；
docs/tasks.md TASK-P1-071。

这一层回答的是**发现漏斗**的问题：系统报上来的东西，有多少是真的？同一个
问题被报了几次？

**两个指标量的不是同一件事。** 有效问题发现率量的是**候选全集**的精度；
误报率（在 ``decision.py`` 里）量的是**被选中去做的**那些候选的精度。后者
才是"选错"的代价 —— 一个无效候选被 reject 掉，没有任何损失；它被 record
下来并派了任务，代价是真金白银。所以两个指标不能互相替代。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from evaluation.common import MetricResult, MetricState, Verdict, rate

#: 有效问题发现率的指标 id。
VALID_DISCOVERY_RATE = "valid_discovery_rate"
#: 重复发现率的指标 id。
DUPLICATE_DISCOVERY_RATE = "duplicate_discovery_rate"


@dataclass(frozen=True)
class Discovery:
    """一条演进机会 —— 自主发现层指标的唯一输入。

    ``label`` 是**人工**标注的结论（TASK-P1-083）。没标注就是 ``None``，
    它表示"还不知道"，而不是"无效"。
    """

    candidate_id: str
    cycle_id: str
    title: str
    #: 证据引用。候选存在的唯一理由（design.md §3.3）。
    evidence_refs: tuple[str, ...] = ()
    #: 人工标注；``None`` 表示尚未标注。
    label: Verdict | None = None


def valid_discovery_rate(discoveries: Sequence[Discovery]) -> MetricResult:
    """有效问题发现率 = 有效候选数 / **已标注**候选数。

    分母只算已标注的候选 —— 未标注的既不能算对也不能算错，把它们塞进
    分母会让指标随标注进度漂移：一个刚标了 3 个候选的系统，"有效率"会
    是 1/3，与它实际的水准毫无关系。

    没有标注时**不返回 0**，报 ``baseline_missing`` —— "还没标"与"全是
    无效"是两件相反的事。
    """
    labelled = [item for item in discoveries if item.label is not None]
    if not labelled:
        state = MetricState.NO_SAMPLES if not discoveries else MetricState.BASELINE_MISSING
        empty = rate(VALID_DISCOVERY_RATE, 0, 0, empty_state=state)
        return replace(
            empty,
            detail={
                "reason": "没有任何演进机会"
                if not discoveries
                else "缺少人工标注基线（TASK-P1-083）",
                "total_candidates": len(discoveries),
                "labelled_candidates": len(labelled),
            },
        )
    valid = sum(1 for item in labelled if item.label is Verdict.VALID)
    return rate(
        VALID_DISCOVERY_RATE,
        valid,
        len(labelled),
        detail={
            "total_candidates": len(discoveries),
            "labelled_candidates": len(labelled),
        },
    )


def duplicate_discovery_rate(discoveries: Sequence[Discovery]) -> MetricResult:
    """重复发现率 = 重复候选数 / 候选总数。

    **重复的判定规则：与更早的候选共享至少一条证据引用。** 同一个失败的
    测试、同一个告警、同一份报告被两个不同的 Agent 各报一次 —— 这是
    "同一个问题被重复发现"最常见的样子，而它不需要人工标注就能算出来。

    **这条规则是代理指标，不是 ground truth。** 两个**确实不同**的问题若
    恰好引用了同一份证据，会被误判成重复。反过来，人工发现的重复（同一
    个问题、不同的证据引用）抓不到。这两种误差的方向相反，所以这里不
    假装它是精确值 —— 它的绝对值要结合上下文看，**趋势**比数值可靠。

    候选按调用方给定的顺序处理（调用方负责按发现时间排序）。
    """
    seen: set[str] = set()
    duplicates = 0
    for item in discoveries:
        refs = set(item.evidence_refs)
        if refs & seen:
            duplicates += 1
        seen |= refs
    return rate(
        DUPLICATE_DISCOVERY_RATE,
        duplicates,
        len(discoveries),
        detail={"total_candidates": len(discoveries)},
    )


__all__ = [
    "DUPLICATE_DISCOVERY_RATE",
    "VALID_DISCOVERY_RATE",
    "Discovery",
    "duplicate_discovery_rate",
    "valid_discovery_rate",
]
