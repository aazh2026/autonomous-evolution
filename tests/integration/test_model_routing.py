"""TASK-P4-020 ② —— 多模型路由落真库：模型按项目选，钱记在项目名下。

设计依据：docs/tasks.md TASK-P4-020（多模型路由：按任务 / 成本 / 能力路由模型；
验收条件"不同模型处理不同决策角色（如 Critic 用更强模型）；**成本归集到项目维
度**"）、R-018（Budget 管 Token / 模型调用 / 计算资源 / 时间预算）、I-13（三处
成本同源同币种）、TASK-P4-010（项目作用域与分项目记账）。

**为什么这一节必须落在真实数据库上。** 路由与计价是纯函数，在内存账本上验就
够了 —— 但"成本归集到项目维度"这句话在真库上才有分量：库里**只有 ``scope`` 这一
列**能表达"这是哪个项目的钱"，而 scope 写错一个字符，这笔钱就归了别人。内存账本
按字典键存，"键写错"这件事在那儿根本构造不出来。

**并行是真的并行。** 两个项目的调用用 ``asyncio.gather`` 同时跑 —— 串行跑证明
不了"同时发生时会不会串"，也证明不了行级锁在并发扣减下成立。

**模型是按项目选的，不是按角色全局定的。** 有钱的项目用强模型复核，快没钱的
项目在**它自己允许的档位里**退一档 —— 同一个角色、不同的模型、不同的账。

**为什么一个项目"没钱"时它就干脆不跑，而不是换个便宜模型接着跑。** 那是
Critic 的事：便宜模型复核过的结论与没人复核没有区别。而账本那边只管"钱够不
够" —— 两件事的边界不一样，所以路由失败与记账失败是两种错误，不混在一起。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agents.prompts.base import Role
from core.metrics.cost import CostConverter, ModelRate, RateTable, TokenUsage
from core.policy.budget import Budget
from core.policy.routing import ModelProfile, ModelRouter, ModelTier, NoRouteError
from core.project.scope import ProjectScope
from infrastructure.database.budget import PostgresBudgetLedger
from infrastructure.database.project_budget import (
    ProjectBudget,
    ProjectBudgetLedger,
)
from orchestration.model_dispatch import (
    DispatchBudgetError,
    ModelDispatch,
    ProjectModelDispatcher,
)

pytestmark = pytest.mark.integration

#: 固定"现在" —— 成本是生命周期累计，周期键不随时间变。
_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

#: 有钱的项目 —— 强档复核跑得起。
_RICH = "project-rich"
#: 快没钱的项目 —— 强档跑不起，但标准档还够。
_POOR = "project-poor"
#: 穷到连标准档都跑不起的项目。
_BROKE = "project-broke"

#: 一次调用的用量 —— 便宜档 3 美元、标准档 15 美元、强档 60 美元。
_USAGE = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)


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


@pytest_asyncio.fixture
async def factory(
    engine: AsyncEngine, session: AsyncSession
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """会话工厂。

    **依赖 ``session`` 是为了它的副作用。** 每个测试前把全部表清空 ——
    集成测试库在两次 pytest 运行之间**不会**自动清空，而这里的断言要数精确的
    行数（"两个项目各一行成本"），脏库会让断言变成"上一轮留下的"。
    """
    rows = await session.execute(text("SELECT count(*) FROM budget_account"))
    assert rows.scalar_one() == 0, "预算账户表不是干净的 —— 前置条件不满足"
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def ledger(factory: async_sessionmaker[AsyncSession]) -> ProjectBudgetLedger:
    """真库里的分项目账本。"""
    return ProjectBudgetLedger(PostgresBudgetLedger(factory))


def _dispatcher(
    project_id: str,
    budget: Budget,
    ledger: ProjectBudgetLedger,
) -> ProjectModelDispatcher:
    """给某个项目搭一个调度器。"""
    project_budget = ProjectBudget(
        ProjectScope(project_id),
        budget,
        ledger,
        clock=lambda: _NOW,
    )
    return ProjectModelDispatcher(ModelRouter(_ALL_TIERS), _converter(), project_budget)


def _rich() -> Budget:
    return Budget(currency="USD", max_total_cost=Decimal("1000"))


def _poor() -> Budget:
    """标准档跑得起、强档跑不起 —— 于是会退一档。"""
    return Budget(currency="USD", max_total_cost=Decimal("20"))


def _broke() -> Budget:
    """连标准档都跑不起 —— Critic 的偏好列表里没有便宜档，于是不跑。"""
    return Budget(currency="USD", max_total_cost=Decimal("10"))


class TestTheLedgerIsScopedToTheProject:
    """成本归集到项目维度 —— 在真库上验。"""

    async def test_the_account_row_carries_the_project_scope(
        self,
        factory: async_sessionmaker[AsyncSession],
        ledger: ProjectBudgetLedger,
    ) -> None:
        """**scope 那一列就是"钱归谁"的全部答案。**"""
        dispatcher = _dispatcher(_RICH, _rich(), ledger)
        await dispatcher.dispatch_role(Role.CRITIC, _USAGE)
        async with factory() as session:
            rows = await session.execute(
                text(
                    "SELECT scope, metric, spent FROM budget_account "
                    "WHERE metric = 'total_cost' ORDER BY scope"
                )
            )
        assert [tuple(row) for row in rows] == [(f"project:{_RICH}", "total_cost", Decimal("60"))]

    async def test_each_project_has_its_own_row(
        self,
        factory: async_sessionmaker[AsyncSession],
        ledger: ProjectBudgetLedger,
    ) -> None:
        """**一行一个项目** —— 混在一行里，两个口径的钱就永远分不开了。"""
        await asyncio.gather(
            _dispatcher(_RICH, _rich(), ledger).dispatch_role(Role.CRITIC, _USAGE),
            _dispatcher(_POOR, _poor(), ledger).dispatch_role(Role.CRITIC, _USAGE),
        )
        async with factory() as session:
            rows = await session.execute(
                text(
                    "SELECT scope, spent FROM budget_account "
                    "WHERE metric = 'total_cost' ORDER BY scope"
                )
            )
        assert [tuple(row) for row in rows] == [
            (f"project:{_POOR}", Decimal("15")),
            (f"project:{_RICH}", Decimal("60")),
        ]

    async def test_the_entries_never_cross_projects(
        self,
        factory: async_sessionmaker[AsyncSession],
        ledger: ProjectBudgetLedger,
    ) -> None:
        """**流水的 scope 也要对** —— 流水比累计额更能说明"钱花到哪儿了"。"""
        await asyncio.gather(
            _dispatcher(_RICH, _rich(), ledger).dispatch_role(Role.CRITIC, _USAGE),
            _dispatcher(_POOR, _poor(), ledger).dispatch_role(Role.CRITIC, _USAGE),
        )
        async with factory() as session:
            rows = await session.execute(
                text("SELECT DISTINCT scope FROM budget_entry ORDER BY scope")
            )
        assert [row[0] for row in rows] == [f"project:{_POOR}", f"project:{_RICH}"]

    async def test_the_model_calls_are_counted_per_project(
        self,
        factory: async_sessionmaker[AsyncSession],
        ledger: ProjectBudgetLedger,
    ) -> None:
        """**R-018 说 Budget 管模型调用** —— 次数也归到项目名下。"""
        rich = _dispatcher(_RICH, _rich(), ledger)
        await rich.dispatch_role(Role.CRITIC, _USAGE)
        await rich.dispatch_role(Role.ANALYST, _USAGE)
        async with factory() as session:
            rows = await session.execute(
                text(
                    "SELECT scope, spent FROM budget_account "
                    "WHERE metric = 'model_calls' ORDER BY scope"
                )
            )
        assert [tuple(row) for row in rows] == [(f"project:{_RICH}", Decimal("2"))]

    async def test_the_amount_is_the_price_of_the_model_that_ran(
        self,
        factory: async_sessionmaker[AsyncSession],
        ledger: ProjectBudgetLedger,
    ) -> None:
        """**钱是按选中的那个模型算的。** 强档 60、标准档 15 —— 不是同一个数。"""
        await asyncio.gather(
            _dispatcher(_RICH, _rich(), ledger).dispatch_role(Role.CRITIC, _USAGE),
            _dispatcher(_POOR, _poor(), ledger).dispatch_role(Role.CRITIC, _USAGE),
        )
        async with factory() as session:
            rows = await session.execute(
                text(
                    "SELECT scope, amount FROM budget_entry "
                    "WHERE metric = 'total_cost' ORDER BY scope"
                )
            )
        assert [tuple(row) for row in rows] == [
            (f"project:{_POOR}", Decimal("15")),
            (f"project:{_RICH}", Decimal("60")),
        ]

    async def test_a_project_that_cannot_afford_its_role_spends_nothing(
        self,
        factory: async_sessionmaker[AsyncSession],
        ledger: ProjectBudgetLedger,
    ) -> None:
        """**挑不出模型就一笔都不花。**"""
        dispatcher = _dispatcher(_BROKE, _broke(), ledger)
        with pytest.raises(NoRouteError):
            await dispatcher.dispatch_role(Role.CRITIC, _USAGE)
        async with factory() as session:
            rows = await session.execute(text("SELECT count(*) FROM budget_account"))
        assert rows.scalar_one() == 0


class TestTheModelIsChosenPerProject:
    """同一个角色，不同的项目，不同的模型。"""

    async def test_the_rich_project_runs_on_the_strong_model(
        self, ledger: ProjectBudgetLedger
    ) -> None:
        """**有钱就用最强的。**"""
        dispatcher = _dispatcher(_RICH, _rich(), ledger)
        dispatch = await dispatcher.dispatch_role(Role.CRITIC, _USAGE)
        assert dispatch.decision.model_id == "strong-model"
        assert dispatch.decision.downgraded_from is None

    async def test_the_poor_project_downgrades_one_tier(self, ledger: ProjectBudgetLedger) -> None:
        """**快没钱了就退一档** —— 退到 Critic 自己允许的那一档。"""
        dispatcher = _dispatcher(_POOR, _poor(), ledger)
        dispatch = await dispatcher.dispatch_role(Role.CRITIC, _USAGE)
        assert dispatch.decision.model_id == "standard-model"
        assert dispatch.decision.downgraded_from is ModelTier.STRONG

    async def test_the_downgrade_never_reaches_the_cheap_tier(
        self, ledger: ProjectBudgetLedger
    ) -> None:
        """**便宜模型不算复核** —— 所以退无可退时是不跑，不是降到底。"""
        dispatcher = _dispatcher(_BROKE, _broke(), ledger)
        with pytest.raises(NoRouteError):
            await dispatcher.dispatch_role(Role.CRITIC, _USAGE)

    async def test_the_two_projects_get_different_models(self, ledger: ProjectBudgetLedger) -> None:
        """**路由是按项目算的** —— 不是全局定一个模型给所有项目。"""
        rich, poor = await asyncio.gather(
            _dispatcher(_RICH, _rich(), ledger).dispatch_role(Role.CRITIC, _USAGE),
            _dispatcher(_POOR, _poor(), ledger).dispatch_role(Role.CRITIC, _USAGE),
        )
        assert rich.decision.model_id != poor.decision.model_id

    async def test_an_analyst_still_gets_the_cheap_model_in_a_rich_project(
        self, ledger: ProjectBudgetLedger
    ) -> None:
        """**有钱的项目也不给每个角色都上强档** —— 档位是角色定的。"""
        dispatcher = _dispatcher(_RICH, _rich(), ledger)
        dispatch = await dispatcher.dispatch_role(Role.ANALYST, _USAGE)
        assert dispatch.decision.model_id == "cheap-model"


class TestConcurrentDispatch:
    """并发 —— 行级锁在这一层才真的有用。"""

    async def test_two_projects_dispatching_at_once_stay_separate(
        self,
        factory: async_sessionmaker[AsyncSession],
        ledger: ProjectBudgetLedger,
    ) -> None:
        """**同时发生时也不会串。**"""
        await asyncio.gather(
            _dispatcher(_RICH, _rich(), ledger).dispatch_role(Role.CRITIC, _USAGE),
            _dispatcher(_POOR, _poor(), ledger).dispatch_role(Role.CRITIC, _USAGE),
        )
        async with factory() as session:
            rows = await session.execute(
                text(
                    "SELECT scope, spent FROM budget_account "
                    "WHERE metric = 'total_cost' ORDER BY scope"
                )
            )
        assert [tuple(row) for row in rows] == [
            (f"project:{_POOR}", Decimal("15")),
            (f"project:{_RICH}", Decimal("60")),
        ]

    async def test_two_calls_on_one_project_never_overspend(
        self,
        factory: async_sessionmaker[AsyncSession],
        ledger: ProjectBudgetLedger,
    ) -> None:
        """**同一个项目的并发扣减总和不超限。**

        额度 15 只够一次标准档调用。两笔同时发起时，只有**一笔**能记下 ——
        另一笔要么在路由前就发现"这个项目没额度了"（``DispatchBudgetError``），
        要么在记账时被账本拒下（``recorded`` 为假）。**两种都对**，因为账本那
        一下是底线。
        """
        budget = Budget(currency="USD", max_total_cost=Decimal("15"))
        dispatcher = _dispatcher(_RICH, budget, ledger)
        results = await asyncio.gather(
            dispatcher.dispatch_role(Role.CRITIC, _USAGE),
            dispatcher.dispatch_role(Role.CRITIC, _USAGE),
            return_exceptions=True,
        )
        recorded = [item for item in results if isinstance(item, ModelDispatch) and item.recorded]
        refused = [
            item
            for item in results
            if isinstance(item, DispatchBudgetError)
            or (isinstance(item, ModelDispatch) and not item.recorded)
        ]
        assert len(recorded) <= 1
        assert len(refused) == 2 - len(recorded)
        async with factory() as session:
            rows = await session.execute(
                text(
                    "SELECT spent FROM budget_account "
                    "WHERE scope = :scope AND metric = 'total_cost'"
                ),
                {"scope": f"project:{_RICH}"},
            )
        spent = rows.scalar_one()
        assert Decimal(spent) <= Decimal("15")
