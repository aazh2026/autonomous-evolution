"""默认的判断来源 —— 按候选分类给的先验。

设计依据：docs/design.md §3.3 第三层"演进价值评估（Prioritize）"；
docs/tasks.md TASK-P3-014（价值模型 + 用实际收益校准；R-031、R-033）、
TASK-P1-053（六因子公式）。

**这一层回答的是"在没有真判断的情况下，先按什么排"。** 四个因子（``V`` /
``I`` / ``U`` / ``E``）是判断，不是观察 —— 上游给不出它们（见
:mod:`core.evolution.judgement`）。真判断的来源是 Director 的评估阶段或者
人，**那些还没接上**。所以这里给一个**先验**：按候选分类给一组数。

**这些数是仓库默认值，不是实测值。** 它们回答的是"这一类机会通常长什么样"，
不是"这一件事值多少"。**所以每一个都写着"这是拍的"** —— 一个看起来像测量
结果的数字，比一个明写着"这是拍的"的数字危险得多。

**同一类里排不出细次序 —— 这是先验的固有代价，说清楚比藏着好。** 同一分类
的候选拿到同一组先验因子，差异只来自候选自己的置信度与风险档。**要拆开，
得有真判断或者真收益**：前者是 Director 的事，后者是校准的事。

**这个端口的实现是可替换的。** 换一个判断来源，下游的评分与校准一行都不用
改 —— 那正是把端口留在 :mod:`core.evolution.judgement` 的理由。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from core.evolution.candidate import EvolutionCandidate
from core.evolution.judgement import FactorJudgement
from core.evolution.taxonomy import OpportunityCategory

#: 候选分类 → 先验因子。**仓库默认值，不是实测值。**
#:
#: **每一个数都是拍的，理由如下**（"为什么这么定"比"定成多少"更重要）：
#:
#: - **质量**（没通过的用例）：价值 0.70、影响 0.70、紧迫 0.90、成本 0.30 ——
#:   缺陷就在那儿放着，代价通常是"改一处"，**最该先做**。
#: - **产品**（人报上来的问题）：价值 0.70、影响 0.60、紧迫 0.70、成本 0.40 ——
#:   已经有人报上来了，说明它不是想象出来的需求。
#: - **架构**（依赖方向不对）：价值 0.60、影响 0.80、紧迫 0.40、成本 0.70 ——
#:   **影响最大、最不急、最贵**：改对了是长期的，改错了也是长期的。
#: - **工程实现**（反复改动 / 跑起来出事）：价值 0.50、影响 0.60、紧迫 0.60、
#:   成本 0.30 ——
#: - **技术**（趋势带来的机会）：价值 0.50、影响 0.50、紧迫 0.30、成本 0.60 ——
#:   **先验最弱的一类**：它要的是趋势与收益论证，而那一层还没接上。
#:
#: **为什么不给"技术"更高的先验。** 先验给的是"这一类通常值多少"，而技术类
#: 候选在本仓库里**还没有产生过** —— 给一个没见过的类一个高先验，就是让
#: 它凭想象排在别人前面。
CATEGORY_PRIORS: Final[dict[OpportunityCategory, FactorJudgement]] = {
    OpportunityCategory.QUALITY: FactorJudgement(
        value=Decimal("0.70"),
        impact=Decimal("0.70"),
        urgency=Decimal("0.90"),
        cost=Decimal("0.30"),
    ),
    OpportunityCategory.PRODUCT: FactorJudgement(
        value=Decimal("0.70"),
        impact=Decimal("0.60"),
        urgency=Decimal("0.70"),
        cost=Decimal("0.40"),
    ),
    OpportunityCategory.ARCHITECTURE: FactorJudgement(
        value=Decimal("0.60"),
        impact=Decimal("0.80"),
        urgency=Decimal("0.40"),
        cost=Decimal("0.70"),
    ),
    OpportunityCategory.ENGINEERING: FactorJudgement(
        value=Decimal("0.50"),
        impact=Decimal("0.60"),
        urgency=Decimal("0.60"),
        cost=Decimal("0.30"),
    ),
    OpportunityCategory.TECHNOLOGY: FactorJudgement(
        value=Decimal("0.50"),
        impact=Decimal("0.50"),
        urgency=Decimal("0.30"),
        cost=Decimal("0.60"),
    ),
}


@dataclass(frozen=True, slots=True)
class CategoryPriorAssessor:
    """按分类给先验的判断来源。

    :param priors: 分类 → 先验因子。**默认是 :data:`CATEGORY_PRIORS`。**

    **它永远给得出一个数（分类在表里的话）。** 判不出来这件事由别的实现负
    责 —— 这里的先验表覆盖了全部五类候选，所以**"判不出来"与"判成 0"在这里
    分不开**：一个先验为 0.30 的分类不是"判不出来"，是"这一类按先验只值
    0.30"。
    """

    priors: Mapping[OpportunityCategory, FactorJudgement] = field(
        default_factory=lambda: CATEGORY_PRIORS
    )

    def assess(self, candidate: EvolutionCandidate) -> FactorJudgement | None:
        """按分类取先验。**分类不在表里就判不出来**（返回 ``None``）。"""
        return self.priors.get(candidate.category)


__all__ = ["CATEGORY_PRIORS", "CategoryPriorAssessor"]
