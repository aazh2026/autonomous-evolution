"""高风险变更的规矩与实体 —— **风险越高，要求越严；改架构，还得有人有资格**。

设计依据：docs/tasks.md ``TASK-P4-060``（高风险变更治理：架构级变更的独立评审
与放行流程；验收条件"**标记为高风险的变更 100% 经过独立评审后才可执行**"）、
R-061（高风险操作独立授权）、R-102（不得自主修改控制自身的系统）、
``TASK-P4-070``（``SystemEvolutionProposal`` 实体 + 独立测试 + 审批后发布）、
design.md §3.5D（"删除、发布、合并、部署等高风险操作有独立授权"）、§7。

**这一层回答两个问题。**

1. **这类改动要几份独立评审？** —— 由**风险档**决定（不是由"改动的种类"决定，
   理由见 :mod:`core.evolution.risk`）。
2. **谁有资格评审它？** —— 评审人名册 :class:`ReviewerRegistry`。

**为什么"要几份"与"谁有资格"要分成两件事。** "有几个人批了"回答不了"这几个人
看得懂吗"。让一个只懂测试的人去批改架构的改动，等于没批 —— 所以还要求**至少有一
位具备该类评审资格的人**。**而资格不能由评审人自己声明** —— 自己给自己标
"架构评审人"，那这份资格就不独立了。所以资格住在**名册**里，由流程去查。

**这一层只许往严里加。** 风险档对应的评审数是**天生下限**
（:data:`core.evolution.risk.MINIMUM_REVIEWS_BY_RISK`），项目可以在下限之上再
抬；**写一个比下限更松的值，策略压根建不出来**。评审资格同理：**只能加，不能减**
—— 减去一个"架构评审人"，就是让改架构的改动没人看得懂。

**这一层还住着一个实体。** :class:`HighRiskChange` —— **一份被标记了风险档的高
风险变更长什么样**，以及哪些一致性必须成立。它与
:class:`~core.evolution.proposal.SystemEvolutionProposal` 是两件事：那份是**系
统对自己的自我修改**，这份是**任何一处被标记为高风险的变更**（合并到主干、部
署、改治理都算）。**为什么不复用同一份实体** —— "自我改进提案"这个名字就答不
了"这次合并要不要独立评审"，硬套上去会让"提案"与"变更"两个概念互相污染。

**这一层不存评审结论。** 结论存在评审存储里（别人写的、别人改不了的那一份），
而**真正的闸在流程层**（:mod:`orchestration.high_risk_review`）：它放行前把结论
重新取回来核对，对不上就停。

**这一层用冻结数据类而不是 Pydantic 模型。** 理由与
:mod:`core.evolution.proposal` 相同：数据类的校验由 ``__post_init__`` **显式写
出来**，不依赖任何 per-module 豁免，也不给"这一项忘了校验"留位置。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from core.evolution.common import content_digest, not_blank, specific_text, unique_scopes
from core.evolution.proposal import MINIMUM_RISK_BY_KIND, REQUIRED_SCOPES_BY_KIND, ProposalKind
from core.evolution.risk import MINIMUM_REVIEWS_BY_RISK, RISK_ORDER, ReviewerScope, max_risk
from core.evolution.taxonomy import RiskLevel
from core.policy.autonomy import Operation

#: 高风险操作 —— **哪些操作天生是高风险**。
#:
#: **为什么这张表在这里而不在策略里。** 它是**事实**，不是策略：删除、发布、
#: 合并、部署、改治理，在任何项目里都是高风险（design.md §3.5D）。项目可以加
#: 严（把更多操作列为高风险），但**这张表本身不该由项目配置**。
#:
#: **为什么它与 :data:`harness.security.authorization.HIGH_RISK_CAPABILITIES`
#: 是两张表而不是一张。** 那张表按**资源能力**分（能不能碰主干、能不能部署），
#: 这张表按**操作**分 —— 一张回答"这个能力危不危险"，另一张回答"这个动作危不
#: 危险"。两张表由 ``tests/unit/policy/test_high_risk.py`` 里的
#: :class:`TestConsistency` **逐项对齐** —— 加了这边而忘了另一边，那条测试会红。
HIGH_RISK_OPERATIONS: Final[frozenset[Operation]] = frozenset(
    {
        Operation.MERGE_TO_MAIN,
        Operation.PRODUCTION_DEPLOYMENT,
        Operation.MODIFY_GOVERNANCE,
    }
)


def risk_for_operation(operation: Operation) -> RiskLevel:
    """这个操作的风险档 —— **高风险操作一律是高**。

    **为什么高风险操作不给"中"。** 合并到主干、部署、改治理 —— 任何一条做错了
    都**回不去**（主干上的提交、已经发出去的版本、已经改掉的规则）。而"中风险"
    在这套规矩里的含义是"一份评审就够" —— 对着回不去的操作，一份不够。
    """
    if operation in HIGH_RISK_OPERATIONS:
        return RiskLevel.HIGH
    return RiskLevel.MEDIUM


@dataclass(frozen=True, slots=True)
class HighRiskPolicy:
    """高风险变更的规矩 —— **一个项目对高风险变更的额外要求**。

    :param reviews_by_risk: 每档风险要几份独立评审 —— **只能比下限更严**。
    :param extra_scopes_by_kind: 每类改动**额外**要什么评审资格 —— **只能加，
        不能减**。

    **这两张表都是"往严里加"的旋钮。** 表里没写的就按天生下限来；表里写了的，
    只在**更严**的时候才算数 —— 写一个更松的值会直接**报错**，而不是被悄悄忽略。
    """

    #: 每档风险要几份独立评审。
    reviews_by_risk: Mapping[RiskLevel, int] = field(default_factory=dict)
    #: 每类改动额外要的评审资格。
    extra_scopes_by_kind: Mapping[ProposalKind, tuple[ReviewerScope, ...]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        """**只能比下限更严 —— 写松了就是策略建不出来。**

        顺带把两张表**冻成只读** —— 规矩本身不该在运行中被谁改掉。
        """
        for risk, count in self.reviews_by_risk.items():
            review_floor = MINIMUM_REVIEWS_BY_RISK[risk]
            if count < review_floor:
                raise ValueError(
                    f"{risk.value} 风险至少要 {review_floor} 份独立评审，写了 {count} —— "
                    "规矩只能往严里加"
                )
        for kind, scopes in self.extra_scopes_by_kind.items():
            scope_floor = REQUIRED_SCOPES_BY_KIND[kind]
            missing = set(scope_floor) - set(scopes)
            if missing:
                names = "、".join(sorted(scope.value for scope in missing))
                raise ValueError(
                    f"{kind.value} 类改动至少要 {names} 评审资格 —— 规矩只能往严里加，不能减"
                )
        object.__setattr__(self, "reviews_by_risk", MappingProxyType(dict(self.reviews_by_risk)))
        object.__setattr__(
            self,
            "extra_scopes_by_kind",
            MappingProxyType(
                {kind: tuple(scopes) for kind, scopes in self.extra_scopes_by_kind.items()}
            ),
        )

    def reviews_for(self, risk: RiskLevel) -> int:
        """这一档风险要几份独立评审 —— **下限与项目加严取更大的那个**。"""
        return max(MINIMUM_REVIEWS_BY_RISK[risk], self.reviews_by_risk.get(risk, 0))

    def scopes_for(self, kind: ProposalKind) -> tuple[ReviewerScope, ...]:
        """这一类改动要什么评审资格 —— **天生的下限加上项目加的那部分**。"""
        floor = set(REQUIRED_SCOPES_BY_KIND[kind])
        floor.update(self.extra_scopes_by_kind.get(kind, ()))
        return tuple(scope for scope in ReviewerScope if scope in floor)


@dataclass(frozen=True, slots=True)
class ReviewerProfile:
    """一位评审人 —— **他是谁，他能审什么**。

    :param reviewer: 评审人标识。
    :param scopes: 他持有的评审资格。
    """

    reviewer: str
    scopes: frozenset[ReviewerScope]


class ReviewerRegistry:
    """评审人名册 —— **谁有资格评审什么**。

    **为什么资格不住在评审结论上。** 评审结论是评审人**自己写的** —— 他可以在
    结论上写"我是架构评审人"。**自己给自己签发资格，那份资格就不独立。** 所以
    资格住在**名册**里，流程去查名册，而不是查结论上的自称。

    **没登记的人不能评审。** 这是"谁有资格批"的第一道 —— 一份没登记的人签的评审
    结论，在名册这一层就查不出来。
    """

    def __init__(self) -> None:
        self._profiles: dict[str, ReviewerProfile] = {}

    def register(self, reviewer: str, scopes: Iterable[ReviewerScope]) -> None:
        """把一个人登记进名册 —— **登记即生效，重复登记以最后一次为准**。"""
        name = reviewer.strip()
        if not name:
            raise ValueError("评审人不能是空字符串")
        self._profiles[name] = ReviewerProfile(reviewer=name, scopes=frozenset(scopes))

    def is_registered(self, reviewer: str) -> bool:
        """这个人**在不在册**。"""
        return reviewer in self._profiles

    def scopes_of(self, reviewer: str) -> frozenset[ReviewerScope]:
        """这个人持有哪些资格 —— **不在册就是没有**。"""
        profile = self._profiles.get(reviewer)
        if profile is None:
            return frozenset()
        return profile.scopes

    def covers(self, reviewer: str, required: Iterable[ReviewerScope]) -> bool:
        """这个人**是否每一项资格都有**。

        **为什么不用"有其中一项就行"。** 那会让"至少一位架构评审人"变成"至少一
        位什么都会一点的人" —— 而**通用资格（``general``）不顶替专门资格**：改
        架构的改动，就得有人**真的懂架构**。
        """
        wanted = set(required)
        if not wanted:
            return self.is_registered(reviewer)
        return wanted <= self.scopes_of(reviewer)


class HighRiskStatus(StrEnum):
    """高风险变更的生命周期状态。

    **状态不多不少** —— 每多一个状态，"这份变更现在到哪一步了"就多一个说不清的
    地方。

    - ``draft`` —— 刚写出来，还没提交。
    - ``submitted`` —— 已提交，等发起评审。
    - ``under_review`` —— 评审进行中。
    - ``approved`` —— 评审齐备，可以放行。
    - ``rejected`` —— 被人拒了 —— **终态**，也是粘的：过期盖不住拒绝。
    - ``released`` —— 已放行 —— **终态**。
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    RELEASED = "released"


class HighRiskError(Exception):
    """高风险变更相关的错误基类。"""


class HighRiskStateError(HighRiskError):
    """状态不对 —— **这一步现在不成立**。"""


class HighRiskIndependenceError(HighRiskError):
    """独立性被破坏 —— **自己批自己的不算独立评审**。"""


class HighRiskIntegrityError(HighRiskError):
    """内容对不上 —— **评的不是现在这一版**。"""


class HighRiskScopeError(HighRiskError):
    """评审资格不足 —— **批过的人里没有看得懂这一类的那位**。"""


@dataclass(frozen=True, slots=True)
class HighRiskReview:
    """一份评审结论 —— **绑在这一版内容上**。

    :param reviewer: 评审人 —— **不能是变更提出人**。
    :param reviewed_digest: 评的是哪一版内容 —— **由摘要说了算**。
    :param approved: 批了还是拒了。
    :param comment: 意见 —— **拒了更该写清楚为什么**。
    :param decided_at: 什么时候表的态。

    **为什么这一份上不记"评审资格"。** 资格若记在这儿，就成了**评审人自己声明
    的东西** —— 自己给自己签发资格，那份资格就不独立。资格住在**名册**
    （:class:`ReviewerRegistry`）里，流程去查名册，而不是查结论上的自称。
    """

    reviewer: str
    reviewed_digest: str
    approved: bool
    comment: str = ""
    decided_at: datetime | None = None

    def __post_init__(self) -> None:
        """评审人不能空，绑定的内容摘要不能空 —— **空的就不是一份结论**。"""
        object.__setattr__(self, "reviewer", not_blank(self.reviewer, "评审人"))
        object.__setattr__(
            self, "reviewed_digest", not_blank(self.reviewed_digest, "评审绑定的内容摘要")
        )


@dataclass(frozen=True, slots=True)
class HighRiskChange:
    """一份被标记了风险档的高风险变更 —— **放行的唯一载体**。

    **它记录的是"谁在什么时候要改什么"，以及这一份后来过了哪几道关**。**每一道
    关的凭据都留在实体上**，所以事后能答得出"这次放行凭什么"。

    **状态只能由状态机推。** 构造出来的变更一律是 ``draft``；别的状态只能由
    :meth:`submit`、:meth:`request_review`、:meth:`record_review`、:meth:`release`
    推出来。
    """

    #: 变更编号。
    change_id: str
    #: 归属项目。
    project_id: str
    #: 这是什么操作 —— **决定风险下限**（合并/部署/改治理 = 高）。
    operation: Operation
    #: 改动的种类 —— **决定评审资格下限**（改架构要架构评审人）。
    kind: ProposalKind
    #: 一句话标题 —— **给人看的**。
    title: str
    #: 要改什么 —— **路径、模块、配置项**。
    target: str
    #: 改了什么 —— **人评的就是这段话**。
    summary: str
    #: 谁提的 —— **不能自己批自己**。
    proposed_by: str
    #: 风险档 —— **不得低于操作与种类给出的下限**。
    risk: RiskLevel
    #: 需要几份独立评审 —— **不得低于该风险档的下限**。
    required_reviews: int = 1
    #: **这份变更要求评审人持有的资格** —— **空 = 按种类的天生下限**。
    required_scopes: tuple[ReviewerScope, ...] = ()
    #: 状态。
    status: HighRiskStatus = HighRiskStatus.DRAFT
    #: 评审结论 —— **一份都不能少，且都绑在这一版内容上**。
    reviews: tuple[HighRiskReview, ...] = ()
    #: 评审请求号 —— **流程据此把结论从存储里取回来核对**。
    review_request_id: str | None = None
    #: 提交时间。
    submitted_at: datetime | None = None
    #: 放行时间。
    released_at: datetime | None = None

    def __post_init__(self) -> None:
        """**六道校验，构造时就得全过。**

        1. 文本要写清楚 —— **占位写法不算**。
        2. 风险**不得低于**"这个操作"与"这个种类"给出的下限。
        3. 评审数**不得低于**该风险档的下限。
        4. 评审资格**不得少于**该种类的下限。
        5. 评审人不能是提出人。
        6. **只能以草稿状态建出来。**

        **为什么下限钉在校验里而不是只写在文档里。** 规矩写在文档里，谁都能
        "这次情况特殊"地绕过去；钉在校验里，绕不过去就是绕不过去 —— **变更
        压根建不出来**。
        """
        object.__setattr__(self, "change_id", not_blank(self.change_id, "变更编号"))
        object.__setattr__(self, "project_id", not_blank(self.project_id, "归属项目"))
        object.__setattr__(self, "title", specific_text(self.title, "标题"))
        object.__setattr__(self, "target", specific_text(self.target, "要改什么"))
        object.__setattr__(self, "summary", specific_text(self.summary, "改了什么"))
        object.__setattr__(self, "proposed_by", not_blank(self.proposed_by, "提出人"))
        object.__setattr__(self, "required_scopes", unique_scopes(self.required_scopes))
        floor = max_risk(
            risk_for_operation(self.operation),
            MINIMUM_RISK_BY_KIND[self.kind],
        )
        if RISK_ORDER[self.risk] < RISK_ORDER[floor]:
            raise ValueError(
                f"{self.operation.value} / {self.kind.value} 的风险不得低于 {floor.value}，"
                f"收到 {self.risk.value}"
            )
        minimum = MINIMUM_REVIEWS_BY_RISK[self.risk]
        if self.required_reviews < minimum:
            raise ValueError(
                f"{self.risk.value} 风险的变更至少要 {minimum} 份独立评审，"
                f"收到 {self.required_reviews}"
            )
        scope_floor = set(REQUIRED_SCOPES_BY_KIND[self.kind])
        if not self.required_scopes:
            # **没额外要求 = 用天生的下限** —— 而不是"什么都不要"。
            object.__setattr__(self, "required_scopes", REQUIRED_SCOPES_BY_KIND[self.kind])
        else:
            missing = scope_floor - set(self.required_scopes)
            if missing:
                names = "、".join(scope.value for scope in ReviewerScope if scope in missing)
                raise ValueError(
                    f"{self.kind.value} 类变更至少要 {names} 评审资格 —— 规矩只能往严里加，不能减"
                )
        if self.status is not HighRiskStatus.DRAFT:
            raise ValueError("变更只能以草稿状态创建 —— 别的状态只能由状态机推出来")

    # -- 内容摘要 -----------------------------------------------------------

    @property
    def content_digest(self) -> str:
        """内容摘要 —— **评审绑的就是它**。

        **它只覆盖"这次要改什么"。** 状态、评审记录、时间戳都不在里面 —— 它们
        是这份变更**走过的路**，不是**改动的内容**。内容没变，评审就还有效；内容
        变了（哪怕是被人手改了一下），摘要就变了，评审就作废。

        **"要几份评审"与"要什么资格"在里面** —— 它们是**这份变更被判成什么样**
        的一部分。**理由很实在**：改低评审数或去掉资格要求，是**放松规矩**，而放
        松规矩若不使旧评审作废，就等于事后给自己开后门。

        **这里取的是模块级的 :func:`content_digest`，不是这个属性** —— 方法体里
        看不到类属性。
        """
        return content_digest(
            {
                "project_id": self.project_id,
                "operation": self.operation.value,
                "kind": self.kind.value,
                "title": self.title,
                "target": self.target,
                "summary": self.summary,
                "risk": self.risk.value,
                "required_reviews": self.required_reviews,
                "required_scopes": [scope.value for scope in self.required_scopes],
            }
        )

    @property
    def approved_reviewers(self) -> frozenset[str]:
        """批过的人 —— **去重之后**。

        **为什么去重。** 一个人点两次通过不该算两份意见 —— 一个人可以有多份评审
        记录，但独立评审数按**人**算。
        """
        return frozenset(review.reviewer for review in self.reviews if review.approved)

    # -- 状态机 -------------------------------------------------------------

    def submit(self, *, now: datetime | None = None) -> HighRiskChange:
        """提交 —— **草稿进流程**，接下来只能发起评审。

        **提交不是放行。** 提交只是把这份变更摆到评审面前。
        """
        if self.status is not HighRiskStatus.DRAFT:
            raise HighRiskStateError(f"只有草稿能提交，当前状态是 {self.status.value}")
        return self._replace(status=HighRiskStatus.SUBMITTED, submitted_at=now)

    def request_review(self, request_id: str) -> HighRiskChange:
        """记下评审请求号 —— **流程据此把结论从存储里取回来核对**。

        **为什么连请求号都要记。** 因为对象上的 ``status`` 是**可以伪造的**
        （绕过冻结直接改字段，谁都能做到）。真正说了算的是评审存储里那份结论
        —— 流程放行前会重新取回来核对，而它得知道去哪儿取。
        """
        text = not_blank(request_id, "评审请求号")
        return self._replace(review_request_id=text, status=HighRiskStatus.UNDER_REVIEW)

    def record_review(self, review: HighRiskReview) -> HighRiskChange:
        """记一份评审 —— **评审人不能是提出人，评的要是这一版**。

        **拒了就是拒了。** 一份拒绝会把状态推到 ``rejected``，而那之后不再接受
        评审 —— **过期盖不住拒绝，人拒过的东西不会因为没人再表态就变成"没人反
        对"**。
        """
        if self.status is not HighRiskStatus.UNDER_REVIEW:
            raise HighRiskStateError(
                f"只有评审中的变更可以记录评审结论，当前状态是 {self.status.value}"
            )
        if review.reviewer == self.proposed_by:
            raise HighRiskIndependenceError("评审人不能是提出人 —— 自己批自己不算独立评审")
        if review.reviewed_digest != self.content_digest:
            raise HighRiskIntegrityError("评的不是这一版内容 —— 评完之后内容变了")
        reviews = (*self.reviews, review)
        if not review.approved:
            return self._replace(reviews=reviews, status=HighRiskStatus.REJECTED)
        enough = len(self.approved_reviewers | {review.reviewer}) >= self.required_reviews
        status = HighRiskStatus.APPROVED if enough else self.status
        return self._replace(reviews=reviews, status=status)

    def release(
        self,
        *,
        covered_scopes: frozenset[ReviewerScope],
        now: datetime | None = None,
    ) -> HighRiskChange:
        """放行 —— **唯一一条执行路径**。

        :param covered_scopes: **名册查出来的**、批过的人合起来覆盖的评审资格。

        **为什么资格要由调用方给。** 资格住在**名册**里，而**名册只有流程能查**
        —— 实体自己手里没有那份名册，硬让它自己判，就等于让结论上的自称说了算。
        所以流程去名册查一遍，把查出来的结果交给实体判。

        **四道校验，一道都不能少。**

        1. 状态必须是 ``approved`` —— 没批过的不许发。
        2. 独立评审要**够数**，且批的都是这一版内容。
        3. 提案人不能给自己批。
        4. **要求的评审资格要有人覆盖**。
        """
        if self.status is not HighRiskStatus.APPROVED:
            raise HighRiskStateError(f"只有已批准的变更可以放行，当前状态是 {self.status.value}")
        reviewers = self.approved_reviewers
        if len(reviewers) < self.required_reviews:
            raise HighRiskStateError(f"还缺 {self.required_reviews - len(reviewers)} 份独立评审")
        if self.proposed_by in reviewers:
            raise HighRiskIndependenceError("提出人不能给自己批")
        for review in self.reviews:
            if review.approved and review.reviewed_digest != self.content_digest:
                raise HighRiskIntegrityError("有一份评审评的不是这一版内容")
        missing = set(self.required_scopes) - set(covered_scopes)
        if missing:
            names = "、".join(scope.value for scope in ReviewerScope if scope in missing)
            raise HighRiskScopeError(f"评审资格不足：缺 {names} —— 批过的人里没有持有这些资格的")
        return self._replace(status=HighRiskStatus.RELEASED, released_at=now)

    # -- 内部 ---------------------------------------------------------------

    def _replace(self, **changes: object) -> HighRiskChange:
        """换掉几个字段 —— **返回新的一份，不改原来那份**。

        **为什么不用 :func:`dataclasses.replace`。** 它会重跑 ``__post_init__``，
        而那里有一条"变更只能以草稿状态建出来"—— 那条是给**外面**看的（防止有
        人凭空造一个已批准的变更），状态机自己推状态时不能被它拦住。
        """
        values = {item.name: getattr(self, item.name) for item in fields(self)}
        values.update(changes)
        clone = object.__new__(type(self))
        for name, value in values.items():
            object.__setattr__(clone, name, value)
        return clone


__all__ = [
    "HIGH_RISK_OPERATIONS",
    "HighRiskChange",
    "HighRiskError",
    "HighRiskIndependenceError",
    "HighRiskIntegrityError",
    "HighRiskPolicy",
    "HighRiskReview",
    "HighRiskScopeError",
    "HighRiskStateError",
    "HighRiskStatus",
    "ReviewerProfile",
    "ReviewerRegistry",
    "risk_for_operation",
]
