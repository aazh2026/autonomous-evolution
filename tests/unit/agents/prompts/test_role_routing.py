"""TASK-P4-020 ② —— 不同模型处理不同决策角色。

设计依据：docs/tasks.md TASK-P4-020（验收条件"**不同模型处理不同决策角色
（如 Critic 用更强模型）**"）、``TASK-P1-057``（六套角色化 Prompt）。

**这一层是纯函数。** 路由本身的口径由 ``tests/unit/policy/test_routing.py``
钉住；这里钉的是**角色到档位的策略** —— 哪套 Prompt 该配多强的模型，以及
**降级的边界在哪**。

**为什么"降级的边界"要单独钉。** 降级本身是有用的（首选档位太贵就退一档），
但**降级降过头就是事故**：一个便宜模型复核过的结论，和没人复核没有区别。所以
"Critic 不会退到便宜档"这件事必须是断言，不是注释。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agents.prompts import prompt_set_for
from agents.prompts.base import Role
from agents.prompts.routing import ROLE_ROUTING, apply_routing, role_request, route_role
from core.policy.routing import (
    ModelProfile,
    ModelRouter,
    ModelTier,
    NoRouteError,
)


def _profile(
    model_id: str,
    tier: ModelTier,
    cost: str,
    *,
    capabilities: frozenset[str] = frozenset({"code", "reasoning", "long_context"}),
) -> ModelProfile:
    return ModelProfile(
        model_id=model_id,
        tier=tier,
        cost_per_call=Decimal(cost),
        capabilities=capabilities,
    )


_ALL_TIERS: tuple[ModelProfile, ...] = (
    _profile("cheap-model", ModelTier.CHEAP, "0.01"),
    _profile("standard-model", ModelTier.STANDARD, "0.05"),
    _profile("strong-model", ModelTier.STRONG, "0.20"),
)


class TestRolesGetDifferentModels:
    """不同的角色走不同的档位。"""

    def test_the_critic_gets_a_strong_model(self) -> None:
        """**验收条件里的那句"如 Critic 用更强模型"。**"""
        router = ModelRouter(_ALL_TIERS)
        decision = route_role(Role.CRITIC, router)
        assert decision.tier is ModelTier.STRONG
        assert decision.model_id == "strong-model"

    def test_the_analyst_gets_a_cheap_model(self) -> None:
        """分析师的产出是带证据引用的清单 —— 弱模型够用，而它跑得最频繁。"""
        router = ModelRouter(_ALL_TIERS)
        decision = route_role(Role.ANALYST, router)
        assert decision.tier is ModelTier.CHEAP
        assert decision.model_id == "cheap-model"

    def test_two_roles_do_not_share_a_model(self) -> None:
        router = ModelRouter(_ALL_TIERS)
        assert route_role(Role.CRITIC, router).model_id != route_role(Role.ANALYST, router).model_id

    def test_every_role_has_a_policy(self) -> None:
        """六个角色一个都不能漏 —— 漏掉的那个会静默地用全局默认模型。"""
        assert set(ROLE_ROUTING) == set(Role)

    def test_the_policy_is_the_one_we_said(self) -> None:
        """**策略表本身要被钉住。** 改了策略却不改这里，说明改的人没想过后果。"""
        expected = {
            Role.ANALYST: (ModelTier.CHEAP,),
            Role.STRATEGIST: (ModelTier.STANDARD,),
            Role.ARCHITECT: (ModelTier.STRONG, ModelTier.STANDARD),
            Role.QUALITY: (ModelTier.STANDARD, ModelTier.CHEAP),
            Role.DIRECTOR: (ModelTier.STRONG, ModelTier.STANDARD),
            Role.CRITIC: (ModelTier.STRONG, ModelTier.STANDARD),
        }
        assert {role: policy.tier_preference for role, policy in ROLE_ROUTING.items()} == expected


class TestTheDowngradeBoundary:
    """降级有边界 —— 越界就是事故。"""

    def test_the_critic_never_drops_to_the_cheap_tier(self) -> None:
        """**一次没人复核的合并，比一次贵一点的复核更糟。**"""
        router = ModelRouter(_ALL_TIERS)
        with pytest.raises(NoRouteError):
            route_role(Role.CRITIC, router, max_cost_per_call=Decimal("0.02"))

    def test_the_architect_never_drops_to_the_cheap_tier(self) -> None:
        """架构判断错了，重构代价很高 —— 便宜档不算架构判断。"""
        router = ModelRouter(_ALL_TIERS)
        with pytest.raises(NoRouteError):
            route_role(Role.ARCHITECT, router, max_cost_per_call=Decimal("0.02"))

    def test_the_critic_may_drop_to_the_standard_tier(self) -> None:
        """退到标准档**仍然算复核** —— 所以这一档在偏好列表里。"""
        router = ModelRouter(_ALL_TIERS)
        decision = route_role(Role.CRITIC, router, max_cost_per_call=Decimal("0.10"))
        assert decision.tier is ModelTier.STANDARD
        assert decision.downgraded_from is ModelTier.STRONG

    def test_the_quality_role_may_drop_to_the_cheap_tier(self) -> None:
        """质量评估的产出要靠证据引用兜底 —— 降档是可接受的。"""
        router = ModelRouter(_ALL_TIERS)
        decision = route_role(Role.QUALITY, router, max_cost_per_call=Decimal("0.02"))
        assert decision.tier is ModelTier.CHEAP


class TestCapabilitiesAreHard:
    """能力是硬条件。"""

    def test_a_model_that_cannot_read_code_cannot_serve_the_critic(self) -> None:
        router = ModelRouter(
            [_profile("text-only", ModelTier.STRONG, "0.20", capabilities=frozenset({"reasoning"}))]
        )
        with pytest.raises(NoRouteError):
            route_role(Role.CRITIC, router)

    def test_a_model_that_cannot_read_code_cannot_serve_the_quality_role(self) -> None:
        router = ModelRouter(
            [_profile("text-only", ModelTier.STRONG, "0.20", capabilities=frozenset({"reasoning"}))]
        )
        with pytest.raises(NoRouteError):
            route_role(Role.QUALITY, router)

    def test_the_analyst_needs_a_long_context_model(self) -> None:
        router = ModelRouter(
            [_profile("short", ModelTier.CHEAP, "0.01", capabilities=frozenset({"code"}))]
        )
        with pytest.raises(NoRouteError):
            route_role(Role.ANALYST, router)


class TestApplyRouting:
    """把路由结果套到 Prompt 上 —— 只换模型。"""

    def test_the_model_is_replaced(self) -> None:
        router = ModelRouter(_ALL_TIERS)
        decision = route_role(Role.CRITIC, router)
        prompt_set = prompt_set_for(Role.CRITIC)
        routed = apply_routing(prompt_set, decision)
        assert routed.model.model == "strong-model"

    def test_the_body_is_untouched(self) -> None:
        """**只换模型，正文一个字不动。** 换了正文，"同一套 Prompt 在两个模型
        上跑出的结果不一样"这件事就不可比了。
        """
        router = ModelRouter(_ALL_TIERS)
        decision = route_role(Role.CRITIC, router)
        prompt_set = prompt_set_for(Role.CRITIC)
        routed = apply_routing(prompt_set, decision)
        assert routed.system_prompt == prompt_set.system_prompt
        assert routed.output_schema is prompt_set.output_schema
        assert routed.role is prompt_set.role

    def test_the_temperature_and_token_budget_stay(self) -> None:
        """路由决定的是"谁干"，不是"干得多急"。"""
        router = ModelRouter(_ALL_TIERS)
        decision = route_role(Role.CRITIC, router)
        prompt_set = prompt_set_for(Role.CRITIC)
        routed = apply_routing(prompt_set, decision)
        assert routed.model.temperature == prompt_set.model.temperature
        assert routed.model.max_tokens == prompt_set.model.max_tokens

    def test_the_original_is_not_modified(self) -> None:
        """返回**新**的一套 —— 原来那套还得留在原样。"""
        router = ModelRouter(_ALL_TIERS)
        decision = route_role(Role.CRITIC, router)
        prompt_set = prompt_set_for(Role.CRITIC)
        apply_routing(prompt_set, decision)
        assert prompt_set.model.model != "strong-model"

    def test_the_request_carries_the_model_into_the_call(self) -> None:
        """**请求里的模型就是路由选的那个。** 这是"路由"落到调用上的那一步。"""
        router = ModelRouter(_ALL_TIERS)
        decision = route_role(Role.CRITIC, router)
        routed = apply_routing(prompt_set_for(Role.CRITIC), decision)
        assert routed.request("复核一下").model == "strong-model"


class TestRoleRequest:
    """角色请求 —— 没有策略就报错。"""

    def test_the_request_names_the_role(self) -> None:
        request = role_request(Role.CRITIC)
        assert request.workload == "critic"

    def test_the_request_carries_the_policy(self) -> None:
        request = role_request(Role.CRITIC)
        assert request.tier_preference == ROLE_ROUTING[Role.CRITIC].tier_preference
        assert request.required_capabilities == ROLE_ROUTING[Role.CRITIC].required_capabilities

    def test_the_ceiling_reaches_the_request(self) -> None:
        request = role_request(Role.CRITIC, max_cost_per_call=Decimal("1"))
        assert request.max_cost_per_call == Decimal("1")
