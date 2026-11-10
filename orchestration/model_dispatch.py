"""把路由、计价与记账接成一次调用 —— **成本归集到项目维度**。

设计依据：docs/tasks.md ``TASK-P4-020``（多模型路由；验收条件"不同模型处理不
同决策角色（如 Critic 用更强模型）；**成本归集到项目维度**"）、R-018（Budget 管
Token / 模型调用 / 计算资源 / 时间预算）、I-13（三处成本同源同币种）、
``TASK-P4-010``（项目作用域与分项目记账）。

**一次调用走三步，缺一步都不成。**

.. code-block:: text

    路由（挑模型）→ 计价（算出钱）→ 记账（记到项目名下）

**为什么"路由"和"记账"必须在同一个对象里。** 分开的话，中间那个"用了哪个模
型"就只是日志里的一行字 —— 而钱是按**实际 token 用量**算的，不是按"选了哪
个模型"算的。两件事分属两处时，账本上的数字没法反推"是哪个模型花的"，多模型
系统里最常见的事故恰恰是"说不清是哪个模型干的"。所以这里把三步接成一次。

**上限来自项目余额。** 路由时的单次成本上限取**这个项目还剩多少额度** —— 而
不是某个全局数字。多项目的意义正在于此：甲项目有钱就用强模型，乙项目快没钱
了就在**它自己允许的档位里**退一档。**上限是硬边界**：宁可这次不跑，也不要
花掉不属于这个项目的钱。

**观测不在这一层做。** 观测端口要的是任务 id 与会话 id，而决策角色的模型调用
**没有执行器会话**。这里返回的是 :attr:`ModelDispatch.cost` —— **账本记下的
就是这一个对象**，调用方拿它去观测，三处就同源同币种（I-13）。

**钱花了就是花了。** 账本拒绝超限时不抛异常给调用方，而是记一条"被拒绝"的流
水并把 ``recorded`` 置假 —— 这次活已经干完了，把它变成失败等于丢掉已经做好的
工作，而重做一遍更贵。该拦的是"下一轮要不要继续"，那件事由预算检查去做。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from agents.prompts.base import Role
from agents.prompts.routing import role_request
from core.metrics.cost import CostAmount, CostConverter, TokenUsage
from core.planning.roadmap import TaskType
from core.policy.budget import BudgetExhausted, BudgetMetric
from core.policy.routing import ModelRouter, RoutingDecision, RoutingRequest
from core.task.routing import task_request
from infrastructure.database.project_budget import ProjectBudget

__all__ = [
    "DispatchBudgetError",
    "ModelDispatch",
    "ModelResolutionError",
    "ProjectModelDispatcher",
    "ProjectModelResolver",
]


class DispatchBudgetError(Exception):
    """这个项目没有额度跑这次调用了 —— **还没开始就发现没钱**。

    **它不是 :class:`~core.policy.budget.BudgetExhausted`。** 那个是"扣减被账
    本拒绝"，金额是已知的；这个是"连一次调用的价格都还没定"。两件事的补救动
    作不一样：前者是"这次花了钱但超了"，后者是"这个项目该停下来了"。
    """

    def __init__(self, project_id: str, remaining: Decimal) -> None:
        super().__init__(f"项目 {project_id} 的额度已经用完（剩余 {remaining}），这次调用不会发生")
        self.project_id = project_id
        self.remaining = remaining


@dataclass(frozen=True, slots=True)
class ModelDispatch:
    """一次模型调用的结果。

    :param decision: 路由结果 —— **用了哪个模型、为什么**。
    :param cost: 这次调用的成本。**账本记下的就是这一个对象** —— 观测端拿它
        记，三处就同源同币种。
    :param recorded: 成本有没有真的记进账本。``False`` 表示被预算拦下了。
    :param calls_recorded: 这次模型调用有没有记进"模型调用次数"。``False`` 表
        示次数配额用完了（**成本照样记着** —— 钱花了就是花了）。
    """

    decision: RoutingDecision
    cost: CostAmount
    recorded: bool
    calls_recorded: bool


class ModelResolutionError(Exception):
    """这个项目没有配模型路由 —— **没有就是没有，不猜**。

    **为什么不退回到"随便挑一个模型"。** 路由失败时用一个没被路由过的模型跑
    完，钱照花、能力照降，而日志里什么异常都没有 —— 那正是路由要防的事。
    """

    def __init__(self, project_id: str) -> None:
        super().__init__(f"项目 {project_id} 没有配模型路由 —— 挑不出模型就不跑")
        self.project_id = project_id


class ProjectModelDispatcher:
    """一个项目的一次模型调用 —— 路由、计价、记到项目名下。

    **它绑着一个项目。** 不是"传一个 scope 进去"，而是**这个对象就属于这个
    项目** —— 作用域写在 :class:`~infrastructure.database.project_budget.
    ProjectBudget` 里，扣减时以它为准（见 ``TASK-P4-010`` 的"扣减以作用域为
    准"）。这样调用方**填错项目**这件事根本构造不出来。
    """

    def __init__(
        self,
        router: ModelRouter,
        converter: CostConverter,
        budget: ProjectBudget,
    ) -> None:
        self._router = router
        self._converter = converter
        self._budget = budget

    @property
    def router(self) -> ModelRouter:
        """路由器。"""
        return self._router

    @property
    def project_id(self) -> str:
        """这个调度器属于哪个项目。"""
        return self._budget.scope.project_id

    async def dispatch(
        self,
        request: RoutingRequest,
        usage: TokenUsage,
    ) -> ModelDispatch:
        """跑一次模型调用 —— 路由、计价、记账一次做完。

        :param request: 这次调用的路由请求。**请求里的成本上限会被项目余额
            压低** —— 取两者更严的那个。
        :param usage: 这次调用的实际 token 用量。
        :raises DispatchBudgetError: 项目额度已用完。
        :raises core.policy.routing.NoRouteError: 没有模型接得住这次调用。
        """
        effective = await self._effective_request(request)
        decision = self._router.route(effective)
        cost = self._cost_of(decision, usage)
        recorded = await self._charge(cost)
        calls_recorded = await self._count_call()
        return ModelDispatch(
            decision=decision,
            cost=cost,
            recorded=recorded,
            calls_recorded=calls_recorded,
        )

    async def dispatch_role(self, role: Role, usage: TokenUsage) -> ModelDispatch:
        """跑一次**决策角色**的模型调用。

        **角色的层级偏好在这里生效。** Critic 走强档、Analyst 走便宜档 ——
        差别来自 :mod:`agents.prompts.routing` 的策略，不是这里的硬编码。
        """
        request = role_request(role)
        return await self.dispatch(request, usage)

    async def dispatch_task(
        self,
        task_type: TaskType,
        usage: TokenUsage,
    ) -> ModelDispatch:
        """跑一次**任务**的模型调用。

        :param task_type: :class:`~core.planning.roadmap.TaskType`。
        """
        request = task_request(task_type)
        return await self.dispatch(request, usage)

    async def resolve_task_model(self, task_type: TaskType) -> RoutingDecision:
        """只挑模型，**不记账**。

        **为什么路由这一下不扣钱。** 记账算的是**实际**发生的钱 —— 执行器跑完
        才知道用了多少 token。路由时能算的只是**估算**，而估算与实际之间的差
        正是"账上的数与真实花的钱对不上"这件事的来源（I-13 说的"三处同源"就
        是为了这个）。所以钱由执行器跑完后按实际用量上报，路由只管"谁干"。

        :raises DispatchBudgetError: 项目额度已用完。
        :raises core.policy.routing.NoRouteError: 没有模型接得住这个任务。
        """
        request = task_request(task_type)
        effective = await self._effective_request(request)
        return self._router.route(effective)

    async def _effective_request(self, request: RoutingRequest) -> RoutingRequest:
        """把请求里的成本上限压到项目余额之内。"""
        remaining = await self._budget.remaining(BudgetMetric.TOTAL_COST)
        if remaining is None:
            # 这个项目没设总额上限 —— 请求里带的上限就是上限。
            return request
        if remaining <= 0:
            raise DispatchBudgetError(self.project_id, remaining)
        if request.max_cost_per_call is None or remaining < request.max_cost_per_call:
            return _with_ceiling(request, remaining)
        return request

    def _cost_of(self, decision: RoutingDecision, usage: TokenUsage) -> CostAmount:
        """按**选中的那个模型**给用量算钱。

        **换算只做一次。** 算出来的这个 :class:`CostAmount` 就是账本记下的、
        观测端要记的同一个对象 —— 三处各算一遍的话，换算规则改一次就要改三
        处，而漏掉的那一处不会报错，它只会安静地记下另一个数。
        """
        cost = self._converter.price(usage, model=decision.model_id)
        return self._converter.convert(cost, to_currency=self._budget.budget.currency)

    async def _charge(self, cost: CostAmount) -> bool:
        """把这笔钱记到项目账本上。返回记下了没有。

        **超限不抛异常给调用方。** 账本自己会留下"被拒绝"的流水 —— 那条流水
        就是超预算的证据；而这次活已经干完了，把它变成失败等于丢掉已经做好的
        工作。
        """
        try:
            await self._budget.consume(BudgetMetric.TOTAL_COST, cost.amount)
        except BudgetExhausted:
            return False
        return True

    async def _count_call(self) -> bool:
        """记一次"模型调用"。返回记下了没有。

        **记不下调用次数不影响成本记录** —— 成本已经记了，次数配额用完只是
        "下次不许再调了"。
        """
        try:
            await self._budget.consume(BudgetMetric.MODEL_CALLS, Decimal(1))
        except BudgetExhausted:
            return False
        return True


class ProjectModelResolver:
    """按项目与任务类型挑一个执行模型 —— **给循环 A 用**。

    **它实现 :class:`orchestration.loop_a.ModelResolverPort`。** 那个端口是
    循环 A 问"这次该用哪个模型"的口子；实现放在这里，因为"有哪些模型、各自多
    贵、这个项目还剩多少额度"是**路由**的事，编排层一概不知。

    **为什么路由这一层不扣钱。** 记账算的是**实际**发生的钱（执行器跑完才知道
    用了多少 token）；路由时能算的只是**估算**。拿估算扣账，账上的数与真实花的
    钱永远对不上 —— 而"对不上"这件事没人会去核。所以这里只挑模型，钱由执行器
    跑完后按实际用量上报（见 :mod:`orchestration.cost_accounting`）。

    **项目没配路由就报错。** 悄悄用执行器惯用的模型跑完，等于把"这个项目该用
    什么模型"这件事绕过去了 —— 而那正是路由存在的理由。
    """

    def __init__(self, dispatchers: Mapping[str, ProjectModelDispatcher]) -> None:
        self._dispatchers = dict(dispatchers)

    @property
    def project_ids(self) -> tuple[str, ...]:
        """配了路由的项目 —— 按标识排序。"""
        return tuple(sorted(self._dispatchers))

    async def resolve(self, *, project_id: str, task_type: TaskType) -> str:
        """挑一个模型名。

        :raises ModelResolutionError: 这个项目没有配路由。
        :raises DispatchBudgetError: 项目额度已用完。
        :raises core.policy.routing.NoRouteError: 没有模型接得住这个任务。
        """
        dispatcher = self._dispatchers.get(project_id)
        if dispatcher is None:
            raise ModelResolutionError(project_id)
        decision = await dispatcher.resolve_task_model(task_type)
        return decision.model_id


def _with_ceiling(request: RoutingRequest, ceiling: Decimal) -> RoutingRequest:
    """把请求的成本上限换成给定值。"""
    return RoutingRequest(
        workload=request.workload,
        required_capabilities=request.required_capabilities,
        tier_preference=request.tier_preference,
        max_cost_per_call=ceiling,
    )
