"""TASK-P4-020 ③ —— 一次模型调用：路由 → 计价 → 记到项目名下。

设计依据：docs/tasks.md TASK-P4-020（验收条件"**成本归集到项目维度**"）、
R-018、I-13（三处成本同源同币种）、``TASK-P4-010``（项目作用域与分项目记账）。

**这一层用内存账本。** 落真库的后果由 ``tests/integration/test_model_routing.py``
验 —— 内存账本与数据库账本的差别在**并发原子性**，不在"钱记到谁名下"。

**钉的是三件事。**

1. **钱记到项目名下** —— 账本的 scope 是 ``project:<id>``，不是全局口径；
2. **钱是按选中的那个模型算的** —— 换了模型，价就变了，账也要跟着变；
3. **上限来自项目余额** —— 项目没钱了，路由就该退档或者干脆不跑。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agents.prompts.base import Role
from core.metrics.cost import CostConverter, ModelRate, RateTable, TokenUsage
from core.planning.roadmap import TaskType
from core.policy.budget import Budget, BudgetMetric, InMemoryBudgetLedger
from core.policy.routing import ModelProfile, ModelRouter, ModelTier, NoRouteError
from core.project.scope import ProjectScope
from infrastructure.database.project_budget import ProjectBudget, ProjectBudgetLedger
from orchestration.loop_a import ModelResolverPort
from orchestration.model_dispatch import (
    DispatchBudgetError,
    ModelResolutionError,
    ProjectModelDispatcher,
    ProjectModelResolver,
)

_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


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
    _profile("cheap-model", ModelTier.CHEAP, "3"),
    _profile("standard-model", ModelTier.STANDARD, "15"),
    _profile("strong-model", ModelTier.STRONG, "60"),
)


def _converter() -> CostConverter:
    """一份三档单价表 —— 强档贵、便宜档便宜。"""
    return CostConverter(
        RateTable(
            base_currency="USD",
            model_rates={
                "cheap-model": ModelRate(
                    model="cheap-model",
                    currency="USD",
                    input_price_per_million=Decimal("1"),
                    output_price_per_million=Decimal("2"),
                ),
                "standard-model": ModelRate(
                    model="standard-model",
                    currency="USD",
                    input_price_per_million=Decimal("5"),
                    output_price_per_million=Decimal("10"),
                ),
                "strong-model": ModelRate(
                    model="strong-model",
                    currency="USD",
                    input_price_per_million=Decimal("20"),
                    output_price_per_million=Decimal("40"),
                ),
            },
        )
    )


def _dispatcher(
    project_id: str,
    *,
    budget: Budget | None = None,
    ledger: InMemoryBudgetLedger | None = None,
    router: ModelRouter | None = None,
) -> tuple[ProjectModelDispatcher, InMemoryBudgetLedger]:
    """搭一个绑定了项目的调度器。"""
    inner = ledger if ledger is not None else InMemoryBudgetLedger()
    project_budget = ProjectBudget(
        scope=ProjectScope(project_id),
        budget=budget
        if budget is not None
        else Budget(currency="USD", max_total_cost=Decimal("100")),
        ledger=ProjectBudgetLedger(inner),
        clock=lambda: _NOW,
    )
    return ProjectModelDispatcher(
        router or ModelRouter(_ALL_TIERS), _converter(), project_budget
    ), inner


#: 一次调用的**估算**单次成本（模型画像里的那个数）与**实际**成本（按 token 用
#: 量算出来的数）在这一份夹具里**相等** —— 估算乐观时会发生什么，见
#: ``TestOverLimit`` 里那一条。
#:
#: **这两个数各有各的用处。** 估算管**路由**（还没定用哪个模型时只能拿估算做
#: 比较），实际管**记账**（账本记的是真金白银）。所以路由说"买得起"**不保证**
#: 这一笔记得进去 —— 记不进去时账本自己会拒，那才是底线。


_USAGE = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)


class TestCostLandsOnTheProject:
    """成本归集到项目维度。"""

    @pytest.mark.asyncio
    async def test_the_ledger_scope_is_the_project(self) -> None:
        """**账本里的 scope 是 `project:<id>`** —— 不是一个说不清的口径。"""
        dispatcher, ledger = _dispatcher("alpha")
        await dispatcher.dispatch_role(Role.ANALYST, _USAGE)
        entries = await ledger.entries("project:alpha")
        assert len(entries) == 2  # 成本一笔 + 模型调用次数一笔

    @pytest.mark.asyncio
    async def test_the_cost_is_priced_by_the_chosen_model(self) -> None:
        """**换了模型，价就变了。** 分析师走便宜档，Critic 走强档 —— 两笔账
        不是一个数。
        """
        dispatcher, ledger = _dispatcher("alpha")
        await dispatcher.dispatch_role(Role.ANALYST, _USAGE)
        await dispatcher.dispatch_role(Role.CRITIC, _USAGE)
        spent = await ledger.spent("project:alpha", BudgetMetric.TOTAL_COST, _NOW)
        # 便宜档 3 美元（1 + 2），强档 60 美元（20 + 40）。
        assert spent == Decimal("63")

    @pytest.mark.asyncio
    async def test_another_project_is_not_charged(self) -> None:
        """**甲项目花钱，乙项目不受影响。**"""
        dispatcher_a, ledger = _dispatcher("alpha")
        dispatcher_b, _ = _dispatcher("beta", ledger=ledger)
        await dispatcher_a.dispatch_role(Role.CRITIC, _USAGE)
        await dispatcher_b.dispatch_role(Role.ANALYST, _USAGE)
        alpha = await ledger.spent("project:alpha", BudgetMetric.TOTAL_COST, _NOW)
        beta = await ledger.spent("project:beta", BudgetMetric.TOTAL_COST, _NOW)
        assert alpha == Decimal("60")
        assert beta == Decimal("3")

    @pytest.mark.asyncio
    async def test_the_model_call_is_counted_on_the_project(self) -> None:
        """**模型调用次数也归到项目名下。** R-018 说 Budget 管模型调用。"""
        dispatcher, ledger = _dispatcher("alpha")
        await dispatcher.dispatch_role(Role.ANALYST, _USAGE)
        assert await ledger.spent("project:alpha", BudgetMetric.MODEL_CALLS, _NOW) == Decimal("1")

    @pytest.mark.asyncio
    async def test_the_reported_cost_is_the_one_that_was_charged(self) -> None:
        """**报出来的钱就是账本记下的钱。** 三处同源同币种（I-13）。"""
        dispatcher, ledger = _dispatcher("alpha")
        dispatch = await dispatcher.dispatch_role(Role.CRITIC, _USAGE)
        spent = await ledger.spent("project:alpha", BudgetMetric.TOTAL_COST, _NOW)
        assert dispatch.cost.amount == spent
        assert dispatch.cost.currency == "USD"


class TestRoutingIsApplied:
    """路由真的生效。"""

    @pytest.mark.asyncio
    async def test_the_critic_runs_on_the_strong_model(self) -> None:
        dispatcher, _ = _dispatcher("alpha")
        dispatch = await dispatcher.dispatch_role(Role.CRITIC, _USAGE)
        assert dispatch.decision.model_id == "strong-model"

    @pytest.mark.asyncio
    async def test_the_analyst_runs_on_the_cheap_model(self) -> None:
        dispatcher, _ = _dispatcher("alpha")
        dispatch = await dispatcher.dispatch_role(Role.ANALYST, _USAGE)
        assert dispatch.decision.model_id == "cheap-model"

    @pytest.mark.asyncio
    async def test_a_task_routes_by_its_type(self) -> None:
        """**按任务路由。** 重构走强档，写文档走便宜档。"""
        dispatcher, _ = _dispatcher("alpha")
        refactor = await dispatcher.dispatch_task(TaskType.REFACTORING, _USAGE)
        docs = await dispatcher.dispatch_task(TaskType.DOCS, _USAGE)
        assert refactor.decision.tier is ModelTier.STRONG
        assert docs.decision.tier is ModelTier.CHEAP

    @pytest.mark.asyncio
    async def test_no_route_raises_instead_of_falling_back(self) -> None:
        """**挑不出来就报错。** 静默回退会让系统照跑、照花钱、照出错。

        这个项目只买得起便宜档，而便宜档**不在 Critic 的偏好列表里** ——
        所以它不跑，而不是"降级到便宜模型去复核"。
        """
        dispatcher, ledger = _dispatcher(
            "alpha",
            budget=Budget(currency="USD", max_total_cost=Decimal("4")),
        )
        with pytest.raises(NoRouteError):
            await dispatcher.dispatch_role(Role.CRITIC, _USAGE)
        assert await ledger.spent("project:alpha", BudgetMetric.TOTAL_COST, _NOW) == Decimal("0")


class TestTheCeilingComesFromTheProject:
    """上限来自项目余额。"""

    @pytest.mark.asyncio
    async def test_a_tight_budget_pushes_the_route_down(self) -> None:
        """**项目没钱了就在它自己允许的档位里退一档。**

        余额 50：强档 60 超了，标准档 15 够 —— **退一档**。
        """
        dispatcher, _ = _dispatcher(
            "alpha",
            budget=Budget(currency="USD", max_total_cost=Decimal("50")),
        )
        dispatch = await dispatcher.dispatch_role(Role.CRITIC, _USAGE)
        assert dispatch.decision.model_id == "standard-model"
        assert dispatch.decision.downgraded_from is ModelTier.STRONG

    @pytest.mark.asyncio
    async def test_a_generous_budget_keeps_the_preferred_tier(self) -> None:
        """余额 100：强档 60 够 —— **不降级**。"""
        dispatcher, _ = _dispatcher(
            "alpha",
            budget=Budget(currency="USD", max_total_cost=Decimal("100")),
        )
        dispatch = await dispatcher.dispatch_role(Role.CRITIC, _USAGE)
        assert dispatch.decision.model_id == "strong-model"
        assert dispatch.decision.downgraded_from is None

    @pytest.mark.asyncio
    async def test_the_downgrade_stops_at_the_policy_boundary(self) -> None:
        """余额 10：只有便宜档（3）够 —— 但**便宜档不在 Critic 的偏好列表
        里**，所以退无可退，不跑。
        """
        dispatcher, _ = _dispatcher(
            "alpha",
            budget=Budget(currency="USD", max_total_cost=Decimal("10")),
        )
        with pytest.raises(NoRouteError):
            await dispatcher.dispatch_role(Role.CRITIC, _USAGE)

    @pytest.mark.asyncio
    async def test_an_exhausted_budget_stops_the_call(self) -> None:
        """**额度用完了就不跑。** 这与"扣减被拒"是两件事：钱还没花。"""
        dispatcher, _ = _dispatcher(
            "alpha",
            budget=Budget(currency="USD", max_total_cost=Decimal("3")),
        )
        await dispatcher.dispatch_role(Role.ANALYST, _USAGE)
        with pytest.raises(DispatchBudgetError):
            await dispatcher.dispatch_role(Role.ANALYST, _USAGE)

    @pytest.mark.asyncio
    async def test_the_exhausted_budget_names_the_project(self) -> None:
        dispatcher, _ = _dispatcher(
            "alpha",
            budget=Budget(currency="USD", max_total_cost=Decimal("3")),
        )
        await dispatcher.dispatch_role(Role.ANALYST, _USAGE)
        with pytest.raises(DispatchBudgetError) as caught:
            await dispatcher.dispatch_role(Role.ANALYST, _USAGE)
        assert "alpha" in str(caught.value)

    @pytest.mark.asyncio
    async def test_no_limit_means_no_ceiling(self) -> None:
        """**没设上限的项目不受余额限制。**"""
        dispatcher, _ = _dispatcher("alpha", budget=Budget(currency="USD"))
        dispatch = await dispatcher.dispatch_role(Role.CRITIC, _USAGE)
        assert dispatch.decision.model_id == "strong-model"


class TestOverLimit:
    """超限 —— 记下来，但不把已经干完的活变成失败。

    **这一节靠的是"估算比实际便宜"。** 路由拿估算比余额（那时还不知道会用哪
    个模型），记账拿实际比上限 —— 两者不是同一个数，于是"路由说买得起、账本
    说记不进"是可能的。**账本那一下才是底线。**
    """

    @pytest.mark.asyncio
    async def test_an_optimistic_estimate_lets_the_ledger_refuse(self) -> None:
        """路由看估算 1 ≤ 余额 2 → 过了；账本看实际 3 > 上限 2 → 拒。"""
        dispatcher, _ = _dispatcher(
            "alpha",
            budget=Budget(currency="USD", max_total_cost=Decimal("2")),
            router=ModelRouter([_profile("cheap-model", ModelTier.CHEAP, "1")]),
        )
        dispatch = await dispatcher.dispatch_role(Role.ANALYST, _USAGE)
        assert dispatch.recorded is False

    @pytest.mark.asyncio
    async def test_the_blocked_entry_is_still_there(self) -> None:
        """**被拒绝的流水要留下。** 那是超预算的证据。"""
        dispatcher, ledger = _dispatcher(
            "alpha",
            budget=Budget(currency="USD", max_total_cost=Decimal("2")),
            router=ModelRouter([_profile("cheap-model", ModelTier.CHEAP, "1")]),
        )
        await dispatcher.dispatch_role(Role.ANALYST, _USAGE)
        entries = await ledger.entries("project:alpha", BudgetMetric.TOTAL_COST)
        assert len(entries) == 1
        assert entries[0].is_blocked is True

    @pytest.mark.asyncio
    async def test_the_call_count_is_marked_separately(self) -> None:
        """**次数记不下不影响成本记录。** 钱花了就是花了。"""
        dispatcher, _ = _dispatcher(
            "alpha",
            budget=Budget(currency="USD", max_total_cost=Decimal("2"), max_model_calls=1),
            router=ModelRouter([_profile("cheap-model", ModelTier.CHEAP, "1")]),
        )
        dispatch = await dispatcher.dispatch_role(Role.ANALYST, _USAGE)
        assert dispatch.recorded is False
        assert dispatch.calls_recorded is True


class TestTheResolver:
    """给循环 A 用的那个口子 —— **只挑模型，不记账**。"""

    @pytest.mark.asyncio
    async def test_it_picks_a_model_by_task_type(self) -> None:
        """**按任务路由** —— 重构走强档，写文档走便宜档。"""
        dispatcher, _ = _dispatcher("alpha")
        resolver = ProjectModelResolver({"alpha": dispatcher})
        assert await resolver.resolve(project_id="alpha", task_type=TaskType.REFACTORING) == (
            "strong-model"
        )
        assert await resolver.resolve(project_id="alpha", task_type=TaskType.DOCS) == "cheap-model"

    @pytest.mark.asyncio
    async def test_it_resolves_per_project(self) -> None:
        """**同一个任务，不同项目，不同模型** —— 路由是按项目算的。"""
        rich, _ = _dispatcher(
            "alpha", budget=Budget(currency="USD", max_total_cost=Decimal("1000"))
        )
        poor, _ = _dispatcher("beta", budget=Budget(currency="USD", max_total_cost=Decimal("20")))
        resolver = ProjectModelResolver({"alpha": rich, "beta": poor})
        assert await resolver.resolve(project_id="alpha", task_type=TaskType.REFACTORING) == (
            "strong-model"
        )
        assert await resolver.resolve(project_id="beta", task_type=TaskType.REFACTORING) == (
            "standard-model"
        )

    @pytest.mark.asyncio
    async def test_an_unknown_project_is_refused(self) -> None:
        """**没配路由就报错** —— 不悄悄用执行器惯用的模型。"""
        dispatcher, _ = _dispatcher("alpha")
        resolver = ProjectModelResolver({"alpha": dispatcher})
        with pytest.raises(ModelResolutionError) as caught:
            await resolver.resolve(project_id="gamma", task_type=TaskType.REFACTORING)
        assert "gamma" in str(caught.value)

    @pytest.mark.asyncio
    async def test_resolving_does_not_spend_anything(self) -> None:
        """**路由不扣钱** —— 钱按实际用量算，执行器跑完才报。

        拿估算扣账的话，账上的数与真实花的钱永远对不上 —— 而"对不上"这件事没
        人会去核（I-13 说的"三处同源"就是为了这个）。
        """
        dispatcher, ledger = _dispatcher("alpha")
        resolver = ProjectModelResolver({"alpha": dispatcher})
        await resolver.resolve(project_id="alpha", task_type=TaskType.REFACTORING)
        assert await ledger.spent("project:alpha", BudgetMetric.TOTAL_COST, _NOW) == Decimal("0")
        assert await ledger.entries("project:alpha") == ()

    @pytest.mark.asyncio
    async def test_an_exhausted_project_refuses_to_resolve(self) -> None:
        """**额度用完就不跑** —— 与"扣减被拒"是两件事。"""
        dispatcher, _ = _dispatcher(
            "alpha", budget=Budget(currency="USD", max_total_cost=Decimal("3"))
        )
        await dispatcher.dispatch_role(Role.ANALYST, _USAGE)
        resolver = ProjectModelResolver({"alpha": dispatcher})
        with pytest.raises(DispatchBudgetError):
            await resolver.resolve(project_id="alpha", task_type=TaskType.REFACTORING)

    def test_it_satisfies_the_loop_a_port(self) -> None:
        """**结构化类型靠方法名对齐** —— 端口与实现对不上，编译期就会喊。"""
        dispatcher, _ = _dispatcher("alpha")
        assert isinstance(ProjectModelResolver({"alpha": dispatcher}), ModelResolverPort)

    def test_the_configured_projects_are_visible(self) -> None:
        """**配了哪些项目要能查** —— 排障时第一句要问的就是这个。"""
        dispatcher, _ = _dispatcher("alpha")
        resolver = ProjectModelResolver({"alpha": dispatcher})
        assert resolver.project_ids == ("alpha",)
