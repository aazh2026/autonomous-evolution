"""优先级决策 —— 给候选排次序，并让实际收益回过头来校准这个次序。

设计依据：docs/design.md §3.3 第三层"演进价值评估（Prioritize）"；
docs/tasks.md TASK-P3-014（交付物：价值模型 + 用实际收益校准；验收条件"校准
后模型的排序与'事后实际收益'有正相关（Spearman ρ > 0.3），否则退回启发式模型
并记录"；验证 T2）、TASK-P1-053（六因子公式与排序）、R-031（跨方案优先级判断）、
R-033（价值模型须用实际收益校准）。

**这一层是流水线的后段。** 上游是候选（TASK-P3-013），下游是决策（P1-054 的
record / defer / reject）。它做两件事：**打分排序**，以及**用实际收益校准**这个
排序 —— 校准不通过时**退回启发式模型，并把这件事记下来**。

**排序在先，校准在后。** 启发式模型先给出一个次序（六因子相乘，见
:func:`~core.evolution.priority.raw_priority`）；校准用"当初的判断"与"事后的
收益"去学"哪个因子值得信"；学出来的模型可信，就用它重排。

**"可信"要有门槛，门槛要有量法。** 门槛是 Spearman ρ > 0.3，但那个 ρ 必须是
**留出法**量出来的（k 折交叉验证，见
:func:`~core.evolution.calibration.cross_validated_rho`）—— 拿拟合过的那批数
据去量相关性，得到的数一定好看，**那是自我验证，不是证据**。

**没过门槛就退回。** "退回"不是失败 —— 启发式模型一直都在，它仍然给得出一个
次序。**要记下来的是"我们试过校准，没成"**：不记的话，下一次会有人以为这个
模型是校准过的（:attr:`CalibrationReport.accepted` 与
:attr:`CalibrationReport.reason` 就是那份记录）。

**排不进次序的候选也要记。** 判断不出来（:meth:`FactorAssessor.assess` 返回
``None``）的候选**排不了序** —— 它既不在次序里，也不能悄悄消失。"判不出来"
与"排到最后"是两件事，前者要补信息，后者是结论。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from core.evolution.calibration import (
    CROSS_VALIDATION_FOLDS,
    MINIMUM_CALIBRATION_SAMPLES,
    SPEARMAN_THRESHOLD,
    CalibrationModel,
    CalibrationReport,
    CalibrationSample,
    calibrate,
)
from core.evolution.candidate import EvolutionCandidate
from core.evolution.judgement import FactorAssessor
from core.evolution.priority import (
    ActualOutcome,
    PriorityFactors,
    ScoredCandidate,
    rank,
)
from core.evolution.taxonomy import OpportunityCategory


@dataclass(frozen=True, slots=True)
class UnrankedCandidate:
    """排不进次序的候选，连同理由。

    :param candidate_id: 候选 id。
    :param reason: 为什么排不了。**理由要能分辨"判断不出来"与"id 撞车"** ——
        前者要补判断，后者要改上游。
    """

    candidate_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class PriorityDecision:
    """一次优先级决策的结果。

    :param report: 校准的结论 —— **采纳与退回都要有理由**。
    :param ranked: 排好次序的候选（分数降序，并列时按 id 升序）。
    :param unranked: 排不进次序的，连同理由。

    **次序、没排上的、以及校准的结论，全都在结果里。** 只返回次序，会让
    "这一轮排出来了"与"这一轮排出来了但有几个没排上"变成同一个结果 —— 而
    后者是需要有人看的。
    """

    report: CalibrationReport
    ranked: tuple[ScoredCandidate, ...] = ()
    unranked: tuple[UnrankedCandidate, ...] = ()


@dataclass
class PriorityDecider:
    """优先级决策器 —— **候选进，次序出**。

    :param threshold: 校准结果的可信门槛（留出法量出来的 Spearman ρ）。
    :param minimum_samples: 至少要多少条样本才尝试校准。
    :param folds: 交叉验证的折数。

    **参数全是仓库默认值，不是实测值。** 门槛 0.3 的理由见
    :data:`~core.evolution.calibration.SPEARMAN_THRESHOLD`。
    """

    threshold: Decimal = SPEARMAN_THRESHOLD
    minimum_samples: int = MINIMUM_CALIBRATION_SAMPLES
    folds: int = CROSS_VALIDATION_FOLDS

    def decide(
        self,
        candidates: Sequence[EvolutionCandidate],
        *,
        assessor: FactorAssessor,
        outcomes: Mapping[str, ActualOutcome] | None = None,
    ) -> PriorityDecision:
        """给一批候选排次序。

        :param candidates: 待排的候选。
        :param assessor: 判断端口 —— 四个因子的来源。
        :param outcomes: **已经发生的事**（候选 id → 实际收益）。**没有就是
            没发生**，不是"收益是 0"：没有收益数据时，校准会走"样本不足"那条
            路，退回启发式模型。
        """
        recorded = dict(outcomes or {})
        factors, categories, unranked = self._factors(candidates, assessor)
        samples = self._samples(factors, recorded)
        model, report = calibrate(
            samples,
            threshold=self.threshold,
            minimum_samples=self.minimum_samples,
            folds=self.folds,
        )
        scored = self._score(factors, categories, model)
        return PriorityDecision(
            report=report,
            ranked=rank(scored),
            unranked=tuple(unranked),
        )

    def _factors(
        self,
        candidates: Sequence[EvolutionCandidate],
        assessor: FactorAssessor,
    ) -> tuple[
        dict[str, PriorityFactors],
        dict[str, OpportunityCategory],
        list[UnrankedCandidate],
    ]:
        """装配每个候选的六因子 —— 四个取判断，两个取候选自己的。

        **装配不出来就记下来，不静默跳过。** 判断不出来（返回 ``None``）与 id
        撞车是两种不同的排不了序，理由也得分开。

        **撞车的只留第一条。** id 是主键 —— 两条一样的 id 排进次序里，落地
        时会撞车；悄悄合并更糟：那等于替上游做了它该做的去重。
        """
        factors: dict[str, PriorityFactors] = {}
        categories: dict[str, OpportunityCategory] = {}
        unranked: list[UnrankedCandidate] = []
        for candidate in candidates:
            if candidate.id in factors:
                unranked.append(
                    UnrankedCandidate(
                        candidate_id=candidate.id,
                        reason="候选 id 重复了 —— id 是主键，撞车的这条排不了序",
                    )
                )
                continue
            judgement = assessor.assess(candidate)
            if judgement is None:
                unranked.append(
                    UnrankedCandidate(
                        candidate_id=candidate.id,
                        reason="判断不出这四个因子 —— 排不进次序",
                    )
                )
                continue
            factors[candidate.id] = PriorityFactors.from_candidate(
                candidate,
                value=judgement.value,
                impact=judgement.impact,
                urgency=judgement.urgency,
                cost=judgement.cost,
            )
            categories[candidate.id] = candidate.category
        return factors, categories, unranked

    def _samples(
        self,
        factors: Mapping[str, PriorityFactors],
        outcomes: Mapping[str, ActualOutcome],
    ) -> tuple[CalibrationSample, ...]:
        """把"当初的判断"与"事后的收益"配成对。

        **两边都得有才成样本。** 只有判断没有收益，那是还没发生；只有收益没
        有判断，那也不知道当初是怎么想的。
        """
        samples: list[CalibrationSample] = []
        for candidate_id, factor in factors.items():
            outcome = outcomes.get(candidate_id)
            if outcome is None:
                continue
            samples.append(
                CalibrationSample(
                    candidate_id=candidate_id,
                    factors=factor,
                    realized_value=outcome.realized_value,
                )
            )
        return tuple(samples)

    def _score(
        self,
        factors: Mapping[str, PriorityFactors],
        categories: Mapping[str, OpportunityCategory],
        model: CalibrationModel | None,
    ) -> list[ScoredCandidate]:
        """打分 —— 校准被采纳就用校准后的因子，否则用原样的。

        **校准不过就一个字都不改。** 退回启发式模型的意思就是**用原样的因子
        排** —— 不是"用校准过的模型排，但打个折"。

        **只给排得上的候选打分。** 判断不出来与 id 撞车的那几条不在这里 ——
        它们在 ``unranked`` 里带着理由。
        """
        scored: list[ScoredCandidate] = []
        for candidate_id, factor in factors.items():
            calibrated = model.apply(factor) if model is not None else factor
            scored.append(
                ScoredCandidate(
                    candidate_id=candidate_id,
                    category=categories.get(candidate_id),
                    factors=calibrated,
                )
            )
        return scored


__all__ = ["PriorityDecider", "PriorityDecision", "UnrankedCandidate"]
