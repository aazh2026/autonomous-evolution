"""任务类型的模型路由策略 —— **这活要多强的模型**。

设计依据：docs/tasks.md ``TASK-P4-020``（多模型路由：**按任务** / 成本 / 能力
路由模型）、R-076（七种任务类型）、R-018。

**这一层只说"要多强"，不说"用哪个"。** 具体挑哪个模型由
:mod:`core.policy.routing` 按当下的可用性、能力与成本决定 —— 模型会下线、会
涨价、会有额度限制，写死的映射在第一次换模型时就过期了。

**分档的依据是"判错的代价"。**

| 任务类型 | 层级偏好 | 能力要求 | 为什么这么分 |
| --- | --- | --- | --- |
| Feature | 强档 → 标准档 | 代码 + 推理 | 新功能写错了要改回来；设计取舍也要推理 |
| Bugfix | 标准档 → 便宜档 | 代码 | 有复现用例兜底，判断空间小 |
| Refactoring | 强档 → 标准档 | 代码 + 推理 | **行为保持最难验** —— 改错了测试未必拦得住 |
| Test | 标准档 → 便宜档 | 代码 | 写用例是"照着实现写"，判断空间小 |
| Performance | 标准档 → 便宜档 | 代码 + 推理 | 要推理，但结论由基准数字兜底 |
| Dependency | 标准档 | 代码 | 依赖升级的判断空间主要在影响面分析 |
| Docs | 便宜档 | 代码 | 文档要与实现一致，读得懂代码就够 |

**为什么重构用强档而测试用便宜档。** 两者都在写代码，但**错的代价差得远**：
重构错了，行为变了而测试没覆盖到，缺陷会一路溜到线上；测试写错了，多半是
一条用例没生效，改掉就行。**"都在写代码"不等于"难度一样"。**

**降级有边界。** 偏好列表之外的层级一律不算候选 —— 重构不会因为项目没钱就
退到便宜档，它要么退到标准档（它允许的下一档），要么不跑。
"""

from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType
from typing import Final

from core.planning.roadmap import TaskType
from core.policy.routing import (
    ModelRouter,
    ModelTier,
    RoutingDecision,
    RoutingPolicy,
    RoutingRequest,
    request_for,
)

__all__ = [
    "TASK_ROUTING",
    "route_task",
    "task_request",
]


#: 七种任务类型各自的路由要求。
#:
#: **不可变。** 它是策略，策略在运行期被改了就等于"同一类活这次按 A 标准、
#: 下次按 B 标准"，而那样的结果没法复盘。
TASK_ROUTING: Final[MappingProxyType[TaskType, RoutingPolicy]] = MappingProxyType(
    {
        TaskType.FEATURE: RoutingPolicy(
            tier_preference=(ModelTier.STRONG, ModelTier.STANDARD),
            required_capabilities=frozenset({"code", "reasoning"}),
        ),
        TaskType.BUGFIX: RoutingPolicy(
            tier_preference=(ModelTier.STANDARD, ModelTier.CHEAP),
            required_capabilities=frozenset({"code"}),
        ),
        TaskType.REFACTORING: RoutingPolicy(
            tier_preference=(ModelTier.STRONG, ModelTier.STANDARD),
            required_capabilities=frozenset({"code", "reasoning"}),
        ),
        TaskType.TEST: RoutingPolicy(
            tier_preference=(ModelTier.STANDARD, ModelTier.CHEAP),
            required_capabilities=frozenset({"code"}),
        ),
        TaskType.PERFORMANCE: RoutingPolicy(
            tier_preference=(ModelTier.STANDARD, ModelTier.CHEAP),
            required_capabilities=frozenset({"code", "reasoning"}),
        ),
        TaskType.DEPENDENCY: RoutingPolicy(
            tier_preference=(ModelTier.STANDARD,),
            required_capabilities=frozenset({"code"}),
        ),
        TaskType.DOCS: RoutingPolicy(
            tier_preference=(ModelTier.CHEAP,),
            required_capabilities=frozenset({"code"}),
        ),
    }
)


def task_request(
    task_type: TaskType,
    *,
    max_cost_per_call: Decimal | None = None,
) -> RoutingRequest:
    """这种任务要发起的路由请求。

    :raises KeyError: 这种任务没有路由策略 —— **没有就是没有，不猜**。
    """
    policy = TASK_ROUTING.get(task_type)
    if policy is None:
        raise KeyError(f"任务类型 {task_type.value} 没有路由策略")
    return request_for(policy, task_type.value, max_cost_per_call=max_cost_per_call)


def route_task(
    task_type: TaskType,
    router: ModelRouter,
    *,
    max_cost_per_call: Decimal | None = None,
) -> RoutingDecision:
    """给这种任务选一个模型。

    :raises KeyError: 这种任务没有路由策略。
    :raises core.policy.routing.NoRouteError: 没有模型接得住这种任务。
    """
    return router.route(task_request(task_type, max_cost_per_call=max_cost_per_call))
