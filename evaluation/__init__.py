"""评估层 —— 把"系统是不是真的在让项目变好"变成可计算的指标。

设计依据：docs/design.md §10（四级指标体系）、§12（可验证的改善）；
docs/tasks.md TASK-P1-071（R-117 / R-118）。

**这里不碰数据库。** 指标计算是纯函数 —— 输入是若干条记录，输出是一个
带上下文的结果对象。数据从哪来是别的事（仓储层或看板），这样每个指标都能
被单独测：给它一组已知输入，断言它算出什么。

**三层已实现，第四层只有一半。** 自主发现、决策质量、工程交付三层各有
可算的子集；项目演进层阶段一只做达成度差值 —— 质量趋势要等覆盖率基线
（TASK-P1-042 / TASK-P1-072），单位成本收益推迟到阶段四（收益归因在阶段一
没有可辩护的做法，硬算出来的是噪声）。

**R-117 是这一层的一条硬约束。** 任务数 / Token 数 / 提交量**不得**作为
核心指标 —— 它们很容易被优化，却不代表项目真的变好了。注册表主动拒绝
它们，而不只是初始集合里没有它们。
"""

from __future__ import annotations

from evaluation.common import (
    MetricError,
    MetricLayer,
    MetricResult,
    MetricState,
    Verdict,
    rate,
)
from evaluation.evolution_evaluation import (
    DecisionRecord,
    Discovery,
    apply_labels,
    decision_adoption_rate,
    duplicate_discovery_rate,
    false_positive_rate,
    false_positive_rate_from_labels,
    valid_discovery_rate,
)
from evaluation.labels import (
    EXPECTED_LABEL_COUNT,
    PILOT_LABELS_PATH,
    JsonlLabelSetStore,
    LabelSet,
    LabelSetError,
    LabelSetMissingError,
    LabelSetStore,
    PilotLabel,
    pilot_label_store,
)
from evaluation.project_evaluation import CycleAttainment, attainment_delta
from evaluation.registry import (
    CORE_REGISTRY,
    ForbiddenMetricError,
    MetricDefinition,
    MetricRegistry,
    is_forbidden,
)
from evaluation.task_evaluation import (
    TaskOutcome,
    regression_defect_rate,
    rework_rate,
    task_success_rate,
)

__all__ = [
    "CORE_REGISTRY",
    "EXPECTED_LABEL_COUNT",
    "PILOT_LABELS_PATH",
    "CycleAttainment",
    "DecisionRecord",
    "Discovery",
    "ForbiddenMetricError",
    "JsonlLabelSetStore",
    "LabelSet",
    "LabelSetError",
    "LabelSetMissingError",
    "LabelSetStore",
    "MetricDefinition",
    "MetricError",
    "MetricLayer",
    "MetricRegistry",
    "MetricResult",
    "MetricState",
    "PilotLabel",
    "TaskOutcome",
    "Verdict",
    "apply_labels",
    "attainment_delta",
    "decision_adoption_rate",
    "duplicate_discovery_rate",
    "false_positive_rate",
    "false_positive_rate_from_labels",
    "is_forbidden",
    "pilot_label_store",
    "rate",
    "regression_defect_rate",
    "rework_rate",
    "task_success_rate",
    "valid_discovery_rate",
]
