"""多模型路由 —— 按任务、成本与能力挑一个模型。

设计依据：docs/tasks.md ``TASK-P4-020``（多模型路由：按任务 / 成本 / 能力
路由模型；验收条件"不同模型处理不同决策角色（如 Critic 用更强模型）；**成本
归集到项目维度**"）、R-018（Budget 管 Token / 模型调用 / 计算资源 / 时间预
算）、I-13（三处成本同源同币种）。

**这一层是纯函数。** 没有数据库、没有 IO、没有配置 —— 它只回答"这活该谁
干"。调用方拿一个 :class:`RoutingRequest` 来，问出一个 :class:`RoutingDecision`
走。**为什么放在 ``core`` 而不是 ``agents/``**：路由要按"角色"分档，但
``core`` 不认识角色（角色是 ``agents/`` 的概念），所以这里用的是"工作负载"
这个更一般的说法 —— 角色怎么映射到工作负载，是 ``agents/`` 的事。

**三个轴，各管各的。**

| 轴 | 是什么 | 硬还是软 |
| --- | --- | --- |
| 能力 | 这个模型会不会干这活（要读代码？要长上下文？） | **硬** —— 不会就是不会 |
| 任务 | 这活要多强的模型（层级偏好） | **硬到软之间**：按偏好列表依次试 |
| 成本 | 这次调用最多花多少 | **硬上限** —— 超过就不选它 |

**能力是硬的，因为"不会"没有降级空间。** 让一个不能读长上下文的模型去读一
个仓库，它不会报错，它会**编** —— 而编出来的差距清单看起来跟真的没有区别。

**成本是上限，不是目标。** 预算是"不许超过"，不是"尽量少花"。所以同一层
级里挑**最便宜**的，而不是最贵的：层级已经把能力分档了，同档之内再比能力
是自欺欺人。

**降级是显式的，而且有边界。** 偏好列表是**这个活自己给的**：Critic 允许
"首选强模型、退而求其次标准模型"，但**绝不包括便宜模型** —— 一次没人复核
的合并比一次贵一点的复核更糟。所以路由不会因为项目没钱就把 Critic 悄悄降
到便宜模型上；它要么退到该角色自己允许的下一档，要么**不跑**（抛
:class:`NoRouteError`）。

**没有合适的模型就报错，绝不静默回退到默认模型。** "回退到默认"是这类系统
最常见的静默失败：钱照花、能力照降，而日志里什么异常都没有。所以这里失败就
失败，并把**每一个候选为什么被排除**带出来。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "ModelProfile",
    "ModelRouter",
    "ModelTier",
    "NoRouteError",
    "RejectedModel",
    "RoutingDecision",
    "RoutingPolicy",
    "RoutingRequest",
    "request_for",
]


class ModelTier(StrEnum):
    """模型的能力层级。

    **三档够用，而且是有意的。** 档位越多，"这一档比上一档强在哪"就越说不清
    —— 而说不清的档位会让"降级"变成"随便挑一个"。三档（便宜 / 标准 / 强）
    能回答绝大多数路由问题，且每一档的取舍都写在角色策略里。
    """

    #: 便宜档 —— 适合摘要、提取、整理这类有结构化输出可兜底的活。
    CHEAP = "cheap"
    #: 标准档 —— 大多数分析类活。
    STANDARD = "standard"
    #: 强档 —— 判错代价大的活：架构判断、最终决策、独立复核。
    STRONG = "strong"


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """一个模型的能力画像。

    :param model_id: 模型标识 —— 单价表里的那个名字。**路由与计价用同一个名字**，
        否则"路由到 A 模型、钱按 B 模型算"这种错就没人查得出来。
    :param tier: 能力层级。
    :param cost_per_call: 一次典型调用的估算成本（记账币种）。**这是估算**，
        真正的钱是按实际 token 用量算出来的（见 ``core.metrics.cost``）——
        路由要的是"哪个便宜"的相对判断，不是账单。
    :param capabilities: 这个模型会做什么。角色要求的能力必须被它**全部**覆盖。
    :param available: 现在能不能用（额度用尽、服务不可用时置假）。
    """

    model_id: str
    tier: ModelTier
    cost_per_call: Decimal
    capabilities: frozenset[str] = frozenset()
    available: bool = True

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("模型标识不能是空白")
        if self.cost_per_call < 0:
            raise ValueError(f"单次成本不能为负：{self.cost_per_call}")


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    """一次路由请求 —— "这活该谁干"。

    :param workload: 这活叫什么。**它只进理由文本**，不参与判断 —— 判断看的是
        能力、层级与成本。留着它是为了让决策记录读得懂："Critic 这次用哪个
        模型"比"这次用哪个模型"有用得多。
    :param required_capabilities: 必须具备的能力，**全部**满足才算能干这活。
    :param tier_preference: 偏好的层级，**按次序**试。第一个有可用模型的层级
        胜出。**降级只能发生在这条列表里** —— 列表之外的层级一律不算候选。
    :param max_cost_per_call: 单次成本上限。``None`` 表示不限。
    """

    workload: str
    required_capabilities: frozenset[str] = frozenset()
    tier_preference: tuple[ModelTier, ...] = (ModelTier.STRONG,)
    max_cost_per_call: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.workload.strip():
            raise ValueError("工作负载不能是空白")
        if not self.tier_preference:
            raise ValueError("层级偏好不能是空的 —— 没有偏好就没有路由")
        if self.max_cost_per_call is not None and self.max_cost_per_call < 0:
            raise ValueError(f"单次成本上限不能为负：{self.max_cost_per_call}")


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """一类活的路由要求 —— **层级偏好 + 能力要求**。

    **它不指定模型名。** 写死"哪个角色用哪个模型"的问题是模型会下线、会涨
    价、会有额度限制 —— 第一次换模型就过期了。写死的是**要多强**与**要会什
    么**，具体是哪个模型由路由按当下的可用性与成本决定。

    :param tier_preference: 偏好的层级，按次序试。**降级只能发生在这条列表
        里** —— 列表之外的层级一律不算候选。
    :param required_capabilities: 必须具备的能力。
    """

    tier_preference: tuple[ModelTier, ...]
    required_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.tier_preference:
            raise ValueError("层级偏好不能是空的 —— 没有偏好就没有路由")


def request_for(
    policy: RoutingPolicy,
    workload: str,
    *,
    max_cost_per_call: Decimal | None = None,
) -> RoutingRequest:
    """把一份路由要求变成一次请求。"""
    return RoutingRequest(
        workload=workload,
        required_capabilities=policy.required_capabilities,
        tier_preference=policy.tier_preference,
        max_cost_per_call=max_cost_per_call,
    )


@dataclass(frozen=True, slots=True)
class RejectedModel:
    """一个被排除的模型，**以及它为什么被排除**。

    **理由是路由结果的一部分，不是日志。** "为什么没选它"要能跟决策放在同一
    个对象里 —— 事后复盘时，能回答"当时为什么选了 B 而不是 A"和"当时为什么
    A 没被选"是同一件重要的事。
    """

    model_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """一次路由的结果。

    :param model_id: 选中的模型。
    :param tier: 选中模型的层级。
    :param cost_per_call: 选中模型的单次成本估算。
    :param reason: **为什么是它** —— 一句话，写给人看。
    :param preferred_tier: 请求里偏好的**首选**层级。
    :param downgraded_from: 实际选的层级不是首选层级时，填首选那个 —— **降级是
        要被看见的事实**。``None`` 表示按首选层级走的。
    :param eligible: 通过全部硬条件（可用、能力、层级、成本）的模型，按标识
        排好序 —— **被选中的那个就在里面**。
    :param rejected: 被排除的模型与理由，按标识排好序。
    """

    model_id: str
    tier: ModelTier
    cost_per_call: Decimal
    reason: str
    preferred_tier: ModelTier
    downgraded_from: ModelTier | None = None
    eligible: tuple[str, ...] = ()
    rejected: tuple[RejectedModel, ...] = field(default=())


class NoRouteError(Exception):
    """没有任何模型能接这活。

    **这是失败，不是降级的理由。** 挑不出模型时唯一的正确做法是报错并说清
    "每个候选为什么被排除" —— 静默回退到默认模型会让系统**照跑、照花钱、照
    出错**，而日志里什么异常都没有。
    """

    def __init__(self, request: RoutingRequest, rejected: Sequence[RejectedModel]) -> None:
        detail = "；".join(f"{item.model_id}：{item.reason}" for item in rejected)
        super().__init__(f"没有模型能接「{request.workload}」：{detail or '（没有候选模型）'}")
        self.request = request
        self.rejected = tuple(rejected)


class ModelRouter:
    """模型路由器 —— **纯函数式的**：同样的输入必得同样的输出。

    **为什么结果要可复现。** 路由结果会被写进轨迹、被拿去算钱、被下游核验。
    如果同样的请求两次给出不同的模型，那"这次为什么用这个模型"就永远答不上
    来 —— 而多模型系统里最常见的事故恰恰是"说不清是哪个模型干的"。

    所以排序是**全序**的：先按层级偏好，再按成本，最后按模型标识兜底。
    """

    def __init__(self, profiles: Sequence[ModelProfile]) -> None:
        #: 按模型标识排好序 —— 顺序固定，平局才有确定的裁决。
        self._profiles: tuple[ModelProfile, ...] = tuple(
            sorted(profiles, key=lambda profile: profile.model_id)
        )

    @property
    def profiles(self) -> tuple[ModelProfile, ...]:
        """全部模型画像 —— **按标识排序**。"""
        return self._profiles

    def route(self, request: RoutingRequest) -> RoutingDecision:
        """给这活挑一个模型。

        :param request: 这次路由的请求。
        :raises NoRouteError: 没有任何模型通过硬条件。
        """
        eligible: list[ModelProfile] = []
        rejected: list[RejectedModel] = []
        for profile in self._profiles:
            reason = _rejection_reason(profile, request)
            if reason is None:
                eligible.append(profile)
            else:
                rejected.append(RejectedModel(model_id=profile.model_id, reason=reason))

        if not eligible:
            raise NoRouteError(request, tuple(rejected))

        chosen = _pick(eligible, request)
        preferred = request.tier_preference[0]
        downgraded = preferred if chosen.tier is not preferred else None
        return RoutingDecision(
            model_id=chosen.model_id,
            tier=chosen.tier,
            cost_per_call=chosen.cost_per_call,
            reason=_reason_of(chosen, request, downgraded),
            preferred_tier=preferred,
            downgraded_from=downgraded,
            eligible=tuple(profile.model_id for profile in eligible),
            rejected=tuple(sorted(rejected, key=lambda item: item.model_id)),
        )


def _rejection_reason(profile: ModelProfile, request: RoutingRequest) -> str | None:
    """这个模型为什么不能接这活；``None`` 表示它**可以**。

    **理由按固定次序给出，只给一条。** 一次给四条理由会让人以为"修好一条就
    能用了"，而实际上四条都得满足 —— 所以只报**最先卡住的那一条**。
    """
    if not profile.available:
        return "现在不可用"
    missing = request.required_capabilities - profile.capabilities
    if missing:
        return "缺少能力：" + "、".join(sorted(missing))
    if profile.tier not in request.tier_preference:
        return f"层级 {profile.tier.value} 不在偏好列表里"
    if request.max_cost_per_call is not None and profile.cost_per_call > request.max_cost_per_call:
        return f"单次成本 {profile.cost_per_call} 超过上限 {request.max_cost_per_call}"
    return None


def _pick(eligible: Sequence[ModelProfile], request: RoutingRequest) -> ModelProfile:
    """在通过硬条件的模型里挑一个。

    **按层级偏好依次试，同一层级里取最便宜的。** 同层取最便宜的理由：层级
    已经把能力分档了，同档之内再比能力没有依据；而同档里挑最便宜的，是把
    预算留给别处。
    """
    for tier in request.tier_preference:
        at_tier = [profile for profile in eligible if profile.tier is tier]
        if at_tier:
            return min(at_tier, key=lambda profile: (profile.cost_per_call, profile.model_id))
    raise AssertionError("通过硬条件的模型里没有偏好层级上的 —— 上游应当已经拦住")


def _reason_of(chosen: ModelProfile, request: RoutingRequest, downgraded: ModelTier | None) -> str:
    """把"为什么是它"写成一句话。"""
    if downgraded is None:
        return (
            f"「{request.workload}」用 {chosen.model_id}"
            f"（{chosen.tier.value} 档，单次成本 {chosen.cost_per_call}）"
        )
    return (
        f"「{request.workload}」首选 {downgraded.value} 档但没有负担得起的模型，"
        f"退到 {chosen.tier.value} 档的 {chosen.model_id}"
        f"（单次成本 {chosen.cost_per_call}）"
    )
