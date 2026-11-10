"""TASK-P4-020 ① —— 多模型路由的契约。

设计依据：docs/tasks.md TASK-P4-020（验收条件"不同模型处理不同决策角色（如
Critic 用更强模型）；成本归集到项目维度"）、R-018。

**这一层是纯函数。** 没有数据库、没有 IO —— 所以这里每一条断言都在证伪"路
由的口径"，而不是证伪"数据库连得上"。落在真账本上的后果由
``tests/integration/test_model_routing.py`` 验。

**为什么把"失败关闭"单列一节。** 路由最危险的失败模式不是选错，而是**选不
出来时悄悄换一个** —— 钱照花、能力照降，而日志里什么异常都没有。所以"挑不
出来就报错"这件事要和"挑得对"一样被钉住。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.policy.routing import (
    ModelProfile,
    ModelRouter,
    ModelTier,
    NoRouteError,
    RoutingPolicy,
    RoutingRequest,
    request_for,
)


#: 一份"什么都会"的画像 —— 多数用例拿它当基准，改一处就是改一处。
def _profile(
    model_id: str,
    tier: ModelTier,
    cost: str,
    *,
    capabilities: frozenset[str] = frozenset({"code", "reasoning", "long_context"}),
    available: bool = True,
) -> ModelProfile:
    return ModelProfile(
        model_id=model_id,
        tier=tier,
        cost_per_call=Decimal(cost),
        capabilities=capabilities,
        available=available,
    )


#: 三个档位各一个，都什么都会。
_ALL_TIERS: tuple[ModelProfile, ...] = (
    _profile("cheap-model", ModelTier.CHEAP, "0.01"),
    _profile("standard-model", ModelTier.STANDARD, "0.05"),
    _profile("strong-model", ModelTier.STRONG, "0.20"),
)


class TestCapabilityIsHard:
    """能力是硬条件 —— 不会就是不会。"""

    def test_a_model_missing_a_capability_is_not_eligible(self) -> None:
        """**缺能力就是不能干这活。** 让不能读长上下文的模型去读仓库，它不会
        报错，它会编 —— 而编出来的清单看起来跟真的没有区别。
        """
        router = ModelRouter(
            [_profile("small", ModelTier.STRONG, "0.20", capabilities=frozenset())]
        )
        with pytest.raises(NoRouteError):
            router.route(
                RoutingRequest(workload="分析", required_capabilities=frozenset({"long_context"}))
            )

    def test_the_reason_names_the_missing_capability(self) -> None:
        router = ModelRouter(
            [_profile("small", ModelTier.STRONG, "0.20", capabilities=frozenset())]
        )
        with pytest.raises(NoRouteError) as caught:
            router.route(
                RoutingRequest(workload="分析", required_capabilities=frozenset({"long_context"}))
            )
        assert "long_context" in str(caught.value)

    def test_extra_capabilities_do_not_disqualify(self) -> None:
        """多会一点没关系 —— 要求是"全部满足"，不是"恰好相等"。"""
        router = ModelRouter([_profile("big", ModelTier.STRONG, "0.20")])
        decision = router.route(
            RoutingRequest(workload="分析", required_capabilities=frozenset({"code"}))
        )
        assert decision.model_id == "big"

    def test_every_required_capability_must_be_covered(self) -> None:
        """两个能力缺一个就出局。"""
        router = ModelRouter(
            [_profile("half", ModelTier.STRONG, "0.20", capabilities=frozenset({"code"}))]
        )
        with pytest.raises(NoRouteError):
            router.route(
                RoutingRequest(
                    workload="分析",
                    required_capabilities=frozenset({"code", "reasoning"}),
                )
            )


class TestTierPreference:
    """层级偏好按次序试。"""

    def test_the_preferred_tier_wins(self) -> None:
        router = ModelRouter(_ALL_TIERS)
        decision = router.route(
            RoutingRequest(workload="复核", tier_preference=(ModelTier.STRONG,))
        )
        assert decision.model_id == "strong-model"
        assert decision.tier is ModelTier.STRONG

    def test_the_first_tier_in_the_list_that_has_a_model_wins(self) -> None:
        """**按次序试，不是挑最好的。** 偏好列表第一档有模型就用第一档。"""
        router = ModelRouter(_ALL_TIERS)
        decision = router.route(
            RoutingRequest(
                workload="复核",
                tier_preference=(ModelTier.STANDARD, ModelTier.STRONG),
            )
        )
        assert decision.model_id == "standard-model"

    def test_a_tier_outside_the_list_is_not_a_candidate(self) -> None:
        """**降级只能发生在这条列表里。** 偏好列表之外的层级一律不算候选。"""
        router = ModelRouter(_ALL_TIERS)
        decision = router.route(
            RoutingRequest(workload="复核", tier_preference=(ModelTier.STRONG,))
        )
        rejected = {item.model_id: item.reason for item in decision.rejected}
        assert rejected["cheap-model"] == "层级 cheap 不在偏好列表里"
        assert rejected["standard-model"] == "层级 standard 不在偏好列表里"
        assert "cheap-model" not in decision.eligible

    def test_the_second_tier_is_used_when_the_first_has_none(self) -> None:
        router = ModelRouter(
            (
                _profile("standard-model", ModelTier.STANDARD, "0.05"),
                _profile("cheap-model", ModelTier.CHEAP, "0.01"),
            )
        )
        decision = router.route(
            RoutingRequest(
                workload="复核",
                tier_preference=(ModelTier.STRONG, ModelTier.STANDARD),
            )
        )
        assert decision.model_id == "standard-model"
        assert decision.downgraded_from is ModelTier.STRONG


class TestCostCeiling:
    """成本是硬上限。"""

    def test_a_model_over_the_ceiling_is_not_eligible(self) -> None:
        router = ModelRouter(_ALL_TIERS)
        with pytest.raises(NoRouteError) as caught:
            router.route(
                RoutingRequest(
                    workload="复核",
                    tier_preference=(ModelTier.STRONG,),
                    max_cost_per_call=Decimal("0.10"),
                )
            )
        assert "超过上限" in str(caught.value)

    def test_the_ceiling_can_push_the_choice_down_the_preference_list(self) -> None:
        """**成本能把选择推到下一档。** 这是"按成本路由"最直接的样子。"""
        router = ModelRouter(_ALL_TIERS)
        decision = router.route(
            RoutingRequest(
                workload="复核",
                tier_preference=(ModelTier.STRONG, ModelTier.STANDARD),
                max_cost_per_call=Decimal("0.10"),
            )
        )
        assert decision.model_id == "standard-model"
        assert decision.downgraded_from is ModelTier.STRONG

    def test_the_ceiling_cannot_push_the_choice_outside_the_list(self) -> None:
        """上限不是万能的 —— 出了偏好列表就是出局。"""
        router = ModelRouter(_ALL_TIERS)
        with pytest.raises(NoRouteError):
            router.route(
                RoutingRequest(
                    workload="复核",
                    tier_preference=(ModelTier.STRONG,),
                    max_cost_per_call=Decimal("0.001"),
                )
            )

    def test_a_model_exactly_at_the_ceiling_is_eligible(self) -> None:
        """上限是"不超过"，不是"小于"。"""
        router = ModelRouter(_ALL_TIERS)
        decision = router.route(
            RoutingRequest(
                workload="复核",
                tier_preference=(ModelTier.STRONG,),
                max_cost_per_call=Decimal("0.20"),
            )
        )
        assert decision.model_id == "strong-model"


class TestFailsClosed:
    """挑不出来就报错 —— 绝不静默换一个。"""

    def test_no_model_raises(self) -> None:
        router = ModelRouter([])
        with pytest.raises(NoRouteError):
            router.route(RoutingRequest(workload="复核"))

    def test_the_error_names_the_workload(self) -> None:
        router = ModelRouter([])
        with pytest.raises(NoRouteError) as caught:
            router.route(RoutingRequest(workload="独立复核"))
        assert "独立复核" in str(caught.value)

    def test_every_candidate_is_explained(self) -> None:
        """**每个候选为什么出局都要说。** 只说"没选上"的话，复盘时无从下手。"""
        router = ModelRouter(
            (
                _profile("down", ModelTier.STRONG, "0.20", available=False),
                _profile("blind", ModelTier.STRONG, "0.20", capabilities=frozenset({"code"})),
            )
        )
        with pytest.raises(NoRouteError) as caught:
            router.route(
                RoutingRequest(
                    workload="复核",
                    required_capabilities=frozenset({"reasoning"}),
                )
            )
        reasons = {item.model_id: item.reason for item in caught.value.rejected}
        assert "现在不可用" in reasons["down"]
        assert "缺少能力" in reasons["blind"]

    def test_an_unavailable_model_is_not_chosen_even_when_it_is_cheapest(self) -> None:
        router = ModelRouter(
            (
                _profile("cheap-model", ModelTier.STRONG, "0.01", available=False),
                _profile("strong-model", ModelTier.STRONG, "0.20"),
            )
        )
        decision = router.route(
            RoutingRequest(workload="复核", tier_preference=(ModelTier.STRONG,))
        )
        assert decision.model_id == "strong-model"

    def test_the_rejection_reason_is_the_first_one_that_applies(self) -> None:
        """**只报最先卡住的那一条。** 一次报四条会让人以为"修好一条就能用了"。"""
        router = ModelRouter([_profile("down", ModelTier.CHEAP, "0.01", available=False)])
        with pytest.raises(NoRouteError) as caught:
            router.route(
                RoutingRequest(
                    workload="复核",
                    tier_preference=(ModelTier.STRONG,),
                    max_cost_per_call=Decimal("0"),
                )
            )
        reasons = {item.model_id: item.reason for item in caught.value.rejected}
        assert reasons["down"] == "现在不可用"


class TestDeterminism:
    """同样的输入必得同样的输出。"""

    def test_the_same_request_gives_the_same_model(self) -> None:
        router = ModelRouter(_ALL_TIERS)
        request = RoutingRequest(workload="复核", tier_preference=(ModelTier.STRONG,))
        assert router.route(request).model_id == router.route(request).model_id

    def test_within_a_tier_the_cheapest_wins(self) -> None:
        """同层里挑最便宜的 —— 层级已经把能力分档了，同档之内再比能力没有依据。"""
        router = ModelRouter(
            (
                _profile("pricey", ModelTier.STRONG, "0.50"),
                _profile("cheap", ModelTier.STRONG, "0.10"),
            )
        )
        decision = router.route(
            RoutingRequest(workload="复核", tier_preference=(ModelTier.STRONG,))
        )
        assert decision.model_id == "cheap"

    def test_a_tie_is_broken_by_the_model_id(self) -> None:
        """平局必须有确定的裁决 —— 否则"上次为什么用这个"答不上来。"""
        router = ModelRouter(
            (
                _profile("b-model", ModelTier.STRONG, "0.10"),
                _profile("a-model", ModelTier.STRONG, "0.10"),
            )
        )
        decision = router.route(
            RoutingRequest(workload="复核", tier_preference=(ModelTier.STRONG,))
        )
        assert decision.model_id == "a-model"

    def test_the_profiles_are_sorted(self) -> None:
        router = ModelRouter(
            (
                _profile("b-model", ModelTier.STRONG, "0.10"),
                _profile("a-model", ModelTier.STRONG, "0.10"),
            )
        )
        assert [item.model_id for item in router.profiles] == ["a-model", "b-model"]


class TestDecisionCarriesItsReason:
    """决策要能回答"当时为什么是它"。"""

    def test_the_reason_names_the_model_and_the_workload(self) -> None:
        router = ModelRouter(_ALL_TIERS)
        decision = router.route(
            RoutingRequest(workload="独立复核", tier_preference=(ModelTier.STRONG,))
        )
        assert "strong-model" in decision.reason
        assert "独立复核" in decision.reason

    def test_the_reason_says_when_it_downgraded(self) -> None:
        router = ModelRouter(_ALL_TIERS)
        decision = router.route(
            RoutingRequest(
                workload="独立复核",
                tier_preference=(ModelTier.STRONG, ModelTier.STANDARD),
                max_cost_per_call=Decimal("0.10"),
            )
        )
        assert "退到" in decision.reason

    def test_no_downgrade_means_no_downgraded_from(self) -> None:
        router = ModelRouter(_ALL_TIERS)
        decision = router.route(
            RoutingRequest(workload="复核", tier_preference=(ModelTier.STRONG,))
        )
        assert decision.downgraded_from is None
        assert decision.preferred_tier is ModelTier.STRONG

    def test_the_eligible_list_holds_the_chosen_one(self) -> None:
        router = ModelRouter(_ALL_TIERS)
        decision = router.route(
            RoutingRequest(workload="复核", tier_preference=(ModelTier.STRONG,))
        )
        assert decision.model_id in decision.eligible

    def test_the_rejected_list_holds_the_losers(self) -> None:
        router = ModelRouter(_ALL_TIERS)
        decision = router.route(
            RoutingRequest(workload="复核", tier_preference=(ModelTier.STRONG,))
        )
        rejected = {item.model_id for item in decision.rejected}
        assert "cheap-model" in rejected
        assert "standard-model" in rejected


class TestPolicy:
    """路由要求 —— 层级偏好加能力要求。"""

    def test_a_policy_builds_a_request(self) -> None:
        policy = RoutingPolicy(
            tier_preference=(ModelTier.STRONG, ModelTier.STANDARD),
            required_capabilities=frozenset({"reasoning"}),
        )
        request = request_for(policy, "复核")
        assert request.workload == "复核"
        assert request.tier_preference == (ModelTier.STRONG, ModelTier.STANDARD)
        assert request.required_capabilities == frozenset({"reasoning"})

    def test_the_ceiling_can_be_passed_in(self) -> None:
        policy = RoutingPolicy(tier_preference=(ModelTier.STRONG,))
        request = request_for(policy, "复核", max_cost_per_call=Decimal("1"))
        assert request.max_cost_per_call == Decimal("1")


class TestRejectedShapes:
    """不合法的输入当场拒掉。"""

    def test_a_blank_model_id_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            _profile("   ", ModelTier.STRONG, "0.20")

    def test_a_negative_cost_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            _profile("m", ModelTier.STRONG, "-1")

    def test_an_empty_tier_preference_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            RoutingRequest(workload="复核", tier_preference=())

    def test_a_blank_workload_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            RoutingRequest(workload="  ")

    def test_a_negative_ceiling_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            RoutingRequest(workload="复核", max_cost_per_call=Decimal("-1"))

    def test_an_empty_policy_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            RoutingPolicy(tier_preference=())
