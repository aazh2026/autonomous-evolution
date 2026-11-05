"""指标注册表 —— 系统认可哪些指标是"核心指标"。

设计依据：docs/design.md §10；docs/tasks.md TASK-P1-071（R-117 / R-118）。

**这个注册表存在的唯一理由，是执行 R-117。** design.md §10 开宗明义：

    我建议不要使用 Agent 执行任务数量、Token 数量、代码提交量作为核心指标。
    它们很容易被优化，却不代表项目真的变好了。

这条要求如果只写在文档里，它就只是一句建议 —— 而建议在这个系统里很容易
被绕过：某个指标好算，就被加进看板；看板上的数字被当成成绩；系统于是
开始优化那个数字。**任务数是最容易优化、也最不该被优化的东西** —— 让
Agent 多派任务，多派任务不费什么力气。

所以注册表**主动拒绝**这三类指标，而不是只把它们排除在初始集合之外。
排除只能防住"一开始就没加"，拒绝能防住"后来被加进来"。

**代价**：注册表拒绝的东西并不是不能用。Token 数、成本、提交量仍然是
**诊断输入**（TASK-P1-072 要采集它们做成本基线），只是不能**冒充核心
指标** —— 一个量只要不回答"项目是否真的变好"，就只是活动量的别名。
"""

from __future__ import annotations

from dataclasses import dataclass

from evaluation.common import MetricError, MetricLayer


class ForbiddenMetricError(MetricError):
    """这个指标不得作为核心指标（R-117）。

    被拒绝的不是"这个数字没用"，而是"这个数字不能回答'项目是否真的
    变好'"。它们仍然可以作为诊断输入存在。
    """


#: R-117 禁止的"活动量"主体词 —— 任务、Token、提交。
#:
#: 只写死 "task_count" 这三个精确形式是不够的：绕过它只要改个名字 ——
#: "token_usage"、"num_tasks"、"commits_total" 都是同一个量。列不完的变体
#: 说明匹配方式本身选错了，所以这里按**主体词 + 量词**两段判定。
ACTIVITY_SUBJECTS: frozenset[str] = frozenset(
    {
        "task",
        "token",
        "commit",
    }
)

#: 表示"多寡"的词。与主体词**同时**出现时，才判定为活动量指标。
#:
#: 这个"同时"是关键：R-117 禁的是"任务**数**"，不是"任务"。"任务成功率"
#: （task_success_rate）说的正是"干出来的东西对不对"，恰恰是核心指标 ——
#: 把 "rate" 收进量词表就会误杀它，所以量词表里刻意没有 rate / ratio。
VOLUME_WORDS: frozenset[str] = frozenset(
    {
        "amount",
        "count",
        "created",
        "dispatched",
        "frequency",
        "generated",
        "issued",
        "num",
        "number",
        "sent",
        "total",
        "usage",
        "used",
        "volume",
    }
)


@dataclass(frozen=True)
class MetricDefinition:
    """一个核心指标的登记条目。

    这里登记的是**元信息** —— 它属于哪一层、怎么解读、单位是什么 —— 不是
    计算结果。计算入口由各子包的函数提供，这里只记下它的**位置**，让
    看板与文档能发现"这个指标由谁算"。
    """

    metric_id: str
    layer: MetricLayer
    name: str
    unit: str
    description: str
    #: 计算入口（``模块:函数``），供文档与看板发现。
    provider: str


class MetricRegistry:
    """核心指标的目录。

    只装**核心**指标 —— 即那些能回答"项目是否真的变好"的量。诊断量
    （Token 数、耗时、工具调用数）不进这里。
    """

    def __init__(self) -> None:
        self._metrics: dict[str, MetricDefinition] = {}

    def register(self, definition: MetricDefinition) -> MetricDefinition:
        """登记一个核心指标。

        重复登记与登记 R-117 禁止的指标都会报错 —— 这个注册表要能挡住
        "顺手加一个"，否则它只是个摆设。
        """
        if is_forbidden(definition.metric_id):
            raise ForbiddenMetricError(
                f"指标 {definition.metric_id!r} 不得作为核心指标（R-117）："
                f"任务数 / Token 数 / 提交量很容易被优化，却不代表项目真的变好了。"
                f"它仍可作为诊断输入使用。"
            )
        if definition.metric_id in self._metrics:
            raise MetricError(f"指标 {definition.metric_id!r} 已经登记过了")
        self._metrics[definition.metric_id] = definition
        return definition

    def get(self, metric_id: str) -> MetricDefinition:
        for definition in self._metrics.values():
            if definition.metric_id == metric_id:
                return definition
        raise MetricError(f"未登记的指标：{metric_id!r}")

    def all(self) -> tuple[MetricDefinition, ...]:
        """全部核心指标，按登记顺序。"""
        return tuple(self._metrics.values())

    def by_layer(self, layer: MetricLayer) -> tuple[MetricDefinition, ...]:
        """某一层的全部核心指标。"""
        return tuple(item for item in self._metrics.values() if item.layer is layer)

    def metric_ids(self) -> tuple[str, ...]:
        return tuple(item.metric_id for item in self._metrics.values())

    def __len__(self) -> int:
        return len(self._metrics)

    def __contains__(self, metric_id: object) -> bool:
        return metric_id in self._metrics


def normalize(metric_id: str) -> str:
    """归一化指标 id —— 大小写与连字符差异不应绕开检查。"""
    return metric_id.strip().lower().replace("-", "_").replace(" ", "_")


def is_forbidden(metric_id: str) -> bool:
    """这个 id 是否命中 R-117 禁止的量。

    判定 = 命中**主体词**且命中**量词**。两者缺一不可 —— 缺主体词会误杀
    "任务成功率"，缺量词会放行"任务数"改成"任务总量"这类改写。
    """
    normalized = normalize(metric_id)
    has_subject = any(subject in normalized for subject in ACTIVITY_SUBJECTS)
    has_volume = any(word in normalized for word in VOLUME_WORDS)
    return has_subject and has_volume


#: 全局核心指标注册表。
CORE_REGISTRY = MetricRegistry()


def _define(
    metric_id: str,
    layer: MetricLayer,
    name: str,
    unit: str,
    description: str,
    provider: str,
) -> MetricDefinition:
    return CORE_REGISTRY.register(
        MetricDefinition(
            metric_id=metric_id,
            layer=layer,
            name=name,
            unit=unit,
            description=description,
            provider=provider,
        )
    )


# --- 自主发现层 -----------------------------------------------------------
_define(
    "valid_discovery_rate",
    MetricLayer.DISCOVERY,
    "有效问题发现率",
    "ratio",
    "已标注的演进机会中，确属真实问题的比例。分母只算**已标注**的 —— "
    "未标注的不进分母，因为它们既不能算对也不能算错。",
    "evaluation.evolution_evaluation.discovery:valid_discovery_rate",
)
_define(
    "duplicate_discovery_rate",
    MetricLayer.DISCOVERY,
    "重复发现率",
    "ratio",
    "候选中与更早的候选共享证据引用的比例 —— 同一个问题被反复发现。",
    "evaluation.evolution_evaluation.discovery:duplicate_discovery_rate",
)

# --- 决策质量层 -----------------------------------------------------------
_define(
    "decision_adoption_rate",
    MetricLayer.DECISION,
    "决策采纳率",
    "ratio",
    "Director 提议要做的决策中，未被 Critic 否决或人工推翻的比例。",
    "evaluation.evolution_evaluation.decision:decision_adoption_rate",
)
_define(
    "false_positive_rate",
    MetricLayer.DECISION,
    "误报率",
    "ratio",
    "被采纳去做的决策中，落在**无效**候选上的比例 —— 白做的比例。",
    "evaluation.evolution_evaluation.decision:false_positive_rate",
)

# --- 工程交付层 -----------------------------------------------------------
_define(
    "task_success_rate",
    MetricLayer.DELIVERY,
    "任务成功率",
    "ratio",
    "已完成任务占任务总数的比例。",
    "evaluation.task_evaluation.delivery:task_success_rate",
)
_define(
    "rework_rate",
    MetricLayer.DELIVERY,
    "返工率",
    "ratio",
    "经历过返工的任务占任务总数的比例。",
    "evaluation.task_evaluation.delivery:rework_rate",
)
_define(
    "regression_defect_rate",
    MetricLayer.DELIVERY,
    "回归缺陷率",
    "ratio",
    "已完成任务中，引入过新缺陷的比例。",
    "evaluation.task_evaluation.delivery:regression_defect_rate",
)

# --- 项目演进层 -----------------------------------------------------------
_define(
    "attainment_delta",
    MetricLayer.EVOLUTION,
    "目标达成度差值",
    "ratio_delta",
    "一轮演进前后目标达成度的差值。阶段一只做差值，不做单位成本收益（阶段四）。",
    "evaluation.project_evaluation.attainment:attainment_delta",
)


__all__ = [
    "ACTIVITY_SUBJECTS",
    "CORE_REGISTRY",
    "VOLUME_WORDS",
    "ForbiddenMetricError",
    "MetricDefinition",
    "MetricError",
    "MetricRegistry",
    "is_forbidden",
    "normalize",
]
