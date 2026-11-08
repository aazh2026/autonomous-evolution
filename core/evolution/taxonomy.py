"""演进机会的分类法 —— 五类机会与三档优先级。

设计依据：docs/design.md §3.2（五类演进机会）、§5（``project_assessment``
里的 ``category`` / ``priority``）、docs/tasks.md TASK-P1-052（R-024 五类
演进机会分类法）、TASK-P1-051（R-083 project_assessment 结构）。

**为什么分类法要定死。** "五类"不是随便分的：产品、架构、工程、质量、技术
分别对应"往哪儿走""怎么搭""怎么写""写得对不对""用什么写"这五个正交的问题。
不定死的话，发现器会各写各的 —— 一个把"升级依赖"报成产品机会，另一个报成
工程机会，最后没法横向比较优先级，因为比较的压根不是同一维度。

**分类法只管分类，不管排序。** 优先级由 :mod:`core.evolution.priority` 的
六因子算；这里的 :class:`PriorityLevel` 是认知阶段给出的**粗档**（高/中/低），
它对应 design.md 示例里的 ``priority: high`` —— 那是还没进入正式评分时的
初步判断。两者不冲突：粗档先定方向，正式评分再定次序。
"""

from __future__ import annotations

from enum import StrEnum


class OpportunityCategory(StrEnum):
    """五类演进机会。

    - ``product`` —— 产品本身：功能缺失、体验不对、需求变了。
    - ``architecture`` —— 结构：模块划分、耦合、依赖方向。
    - ``engineering`` —— 工程实现：写法、重复、可维护性。
    - ``quality`` —— 质量：缺陷、测试、性能、可观测性。
    - ``technology`` —— 技术选型：语言、框架、依赖、基础设施。

    **技术类是最容易被滥用的。** design.md §3.2 明写"技术趋势不应直接转化
    为升级任务，必须先证明它对当前项目有实际收益" —— 所以这一类候选要额外
    带一条收益论证（见 :mod:`core.evolution.candidate`）。
    """

    PRODUCT = "product"
    ARCHITECTURE = "architecture"
    ENGINEERING = "engineering"
    QUALITY = "quality"
    TECHNOLOGY = "technology"


class PriorityLevel(StrEnum):
    """粗档优先级。

    三档而不是五档、不是连续分数：认知阶段给出的判断本来就粗，给一个
    ``0.73`` 只会让人误以为它精确。真正要比次序，用六因子模型。
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


__all__ = ["OpportunityCategory", "PriorityLevel"]
