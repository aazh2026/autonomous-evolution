"""角色的模型路由策略 —— **哪个角色配哪个模型**。

设计依据：docs/tasks.md ``TASK-P4-020``（多模型路由：按任务 / 成本 / 能力路
由模型；验收条件"**不同模型处理不同决策角色（如 Critic 用更强模型）**"）、
``TASK-P1-057``（六套角色化 Prompt）、R-018。

**这一层回答"角色"到"模型"的映射，路由本身在** :mod:`core.policy.routing`。
分开是因为**依赖方向**：``core`` 不认识角色，角色是 ``agents/`` 的概念；反过来
``agents/`` 认识角色也认识模型画像，两边都认识的东西放在低层。

**分档的依据是"判错的代价"，不是"活的大小"。**

| 角色 | 层级偏好 | 能力要求 | 为什么这么分 |
| --- | --- | --- | --- |
| Analyst | 只用便宜档 | 长上下文 | 输出是带证据引用的清单，弱模型够用 |
| Strategist | 标准档 | 推理 | 价值与优先级判断，要推理，不必读代码 |
| Architect | 强档 → 标准档 | 推理 + 代码 | 架构判断错了，重构代价很高；**不退到便宜档** |
| Quality | 标准档 → 便宜档 | 代码 | 质量与测试充分性评估，要读代码 |
| Director | 强档 → 标准档 | 推理 | 最终决策，综合目标、风险、成本与收益 |
| Critic | 强档 → 标准档 | 推理 + 代码 | 独立复核；**退到标准档仍算复核，退到便宜档就不算了** |

**为什么 Analyst 可以用便宜档。** 它的产出是**清单**，而清单的每一条都要带证
据引用 —— 下游按"有没有依据"筛它。有依据的清单，弱模型写出来的和强模型写
出来的一样能用；而它往往是整条链上跑得最频繁的一环，省下来的预算在别处
更值钱。

**为什么 Critic 不退到便宜档。** Critic 是**独立复核** —— 它的价值来自"看的是
另一个人写的东西"。让便宜模型复核，产出的是"便宜模型觉得没问题"，那与没有
复核没有区别。所以它的偏好列表到标准档为止。**一次没人复核的合并，比一次贵
一点的复核更糟。**

**为什么不写死"哪个角色用哪个模型名"。** 写死的是**档位与能力要求**，具体
是哪个模型由路由按当下的可用性与成本决定 —— 模型会下线、会涨价、会有额度
限制，写死的映射在第一次换模型时就过期了。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from agents.prompts.base import PromptSet, Role, RoleOutput
from core.policy.routing import (
    ModelRouter,
    ModelTier,
    RoutingDecision,
    RoutingPolicy,
    RoutingRequest,
    request_for,
)

__all__ = [
    "ROLE_ROUTING",
    "apply_routing",
    "role_request",
    "route_role",
]


#: 六个角色各自的路由要求。
#:
#: **这是策略，不是实现。** 具体挑哪个模型是 :class:`ModelRouter` 的事 ——
#: 它按当下可用性、能力与成本选。策略只说"这个活要多强的模型"。
ROLE_ROUTING: Mapping[Role, RoutingPolicy] = {
    Role.ANALYST: RoutingPolicy(
        tier_preference=(ModelTier.CHEAP,),
        required_capabilities=frozenset({"long_context"}),
    ),
    Role.STRATEGIST: RoutingPolicy(
        tier_preference=(ModelTier.STANDARD,),
        required_capabilities=frozenset({"reasoning"}),
    ),
    Role.ARCHITECT: RoutingPolicy(
        tier_preference=(ModelTier.STRONG, ModelTier.STANDARD),
        required_capabilities=frozenset({"reasoning", "code"}),
    ),
    Role.QUALITY: RoutingPolicy(
        tier_preference=(ModelTier.STANDARD, ModelTier.CHEAP),
        required_capabilities=frozenset({"code"}),
    ),
    Role.DIRECTOR: RoutingPolicy(
        tier_preference=(ModelTier.STRONG, ModelTier.STANDARD),
        required_capabilities=frozenset({"reasoning"}),
    ),
    Role.CRITIC: RoutingPolicy(
        tier_preference=(ModelTier.STRONG, ModelTier.STANDARD),
        required_capabilities=frozenset({"reasoning", "code"}),
    ),
}


def role_request(
    role: Role,
    *,
    max_cost_per_call: Decimal | None = None,
) -> RoutingRequest:
    """这个角色要发起的路由请求。

    :raises KeyError: 这个角色没有路由策略 —— **没有就是没有，不猜**。
    """
    policy = ROLE_ROUTING.get(role)
    if policy is None:
        raise KeyError(f"角色 {role.value} 没有路由策略")
    return request_for(policy, role.value, max_cost_per_call=max_cost_per_call)


def route_role(
    role: Role,
    router: ModelRouter,
    *,
    max_cost_per_call: Decimal | None = None,
) -> RoutingDecision:
    """给某个角色选一个模型。

    :param role: 哪个角色。
    :param router: 路由器。
    :param max_cost_per_call: 单次成本上限（通常是调用方项目剩下的额度）。
    :raises KeyError: 这个角色没有路由策略 —— **没有就是没有，不猜**。
    :raises core.policy.routing.NoRouteError: 没有模型接得住这个角色。
    """
    return router.route(role_request(role, max_cost_per_call=max_cost_per_call))


def apply_routing[OutputT: RoleOutput](
    prompt_set: PromptSet[OutputT], decision: RoutingDecision
) -> PromptSet[OutputT]:
    """把路由结果套到一套 Prompt 上，返回**新**的一套。

    **只换模型，正文一个字不动。** 于是"同一套 Prompt 在两个模型上跑出的结果
    不一样"这件事是可比的，而不是两次调用之间被别处的改动悄悄改掉了。

    **温度与 token 上限跟着原来那套走。** 路由决定的是"谁干"，不是"干得多
    急"—— 那两件事是 Prompt 集自己的。
    """
    return prompt_set.with_model(prompt_set.model.model_copy(update={"model": decision.model_id}))
