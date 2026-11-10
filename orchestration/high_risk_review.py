"""高风险变更的放行流程 —— **独立评审齐了、有资格的人签了字，才准执行**。

设计依据：docs/tasks.md ``TASK-P4-060``（高风险变更治理：架构级变更的独立评审
与放行流程；验收条件"**标记为高风险的变更 100% 经过独立评审后才可执行**"）、
R-061（高风险操作独立授权）、R-102（不得自主修改控制自身的系统）、
design.md §3.5D（"删除、发布、合并、部署等高风险操作有独立授权"）、§7。

**这一层是真正的闸。** 领域层（:mod:`core.policy.high_risk`）的状态机是**纵深防
御** —— 它让"漏写一步"在代码里就发生不了。但 Python 里的对象是**可以伪造的**：
绕过冻结直接改字段，谁都能把状态改成"已放行"。

**所以放行前要重新核对，而不是相信对象。** :meth:`HighRiskReviewFlow.release`
每次放行都**从评审存储里把结论重新取回来**，再拿它跟变更当前的内容摘要、评审
数、独立性、**评审人名册**逐项对。**对象说什么不算，存储里那份与名册说了算**。

**名册是这一层与 ``TASK-P4-070`` 最大的不同。** 自我改进流程里，评审人的资格
记在他自己签的那份结论上；**这里不这么干** —— 资格自己声明自己就不独立了。资格
住在 :class:`~core.policy.high_risk.ReviewerRegistry` 里，**流程去查名册**。

**审计写在效果之前。** 审计写不进去，就**不动手** —— 失败关闭。没留痕的放行比没
放行的变更更糟：前者连"它发生过"都答不上来。

**这一层不碰数据库、不跑 LLM。** 它只认端口 —— 实现分两处：内存版在测试里，数
据库版在 ``infrastructure/database/``。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from core.evolution.proposal import MINIMUM_RISK_BY_KIND, ProposalKind
from core.evolution.risk import ReviewerScope, max_risk
from core.evolution.taxonomy import RiskLevel
from core.policy.autonomy import Operation
from core.policy.high_risk import (
    HighRiskChange,
    HighRiskError,
    HighRiskPolicy,
    HighRiskReview,
    HighRiskScopeError,
    risk_for_operation,
)


class HighRiskReviewError(HighRiskError):
    """放行流程里的错误 —— **卡住了就说卡在哪**。"""


class ReleaseAuditError(HighRiskReviewError):
    """审计写不进去 —— **因此不放行**。"""


@dataclass(frozen=True, slots=True)
class ChangeDraft:
    """一份高风险变更的**内容** —— **还没有风险标记的那部分**。

    **为什么要单独一个类型。** 提变更的人给得出的是"改什么、为什么改"，给不出的
    是"算多高风险""要几份评审""谁来审" —— 后三样是**规矩与治理**的事。把它们分
    开，提变更的人就没法自己给自己降低标准。

    :param change_id: 变更编号。
    :param project_id: 归属项目。
    :param operation: 这是什么操作。
    :param kind: 改动的种类。
    :param title: 一句话标题。
    :param target: 要改什么。
    :param summary: 改了什么。
    :param proposed_by: 谁提的。
    :param risk: **要不要往高里标** —— **只能往高标，不能往低标**。
    :param details: 补充说明。
    """

    change_id: str
    project_id: str
    operation: Operation
    kind: ProposalKind
    title: str
    target: str
    summary: str
    proposed_by: str
    risk: RiskLevel | None = None
    details: str = ""


@dataclass(frozen=True, slots=True)
class ReleaseAuditEvent:
    """一条放行审计 —— **执行之前写下的那一笔**。

    :param action: 做了什么 —— ``submit`` / ``review`` / ``release``。
    :param change_id: 哪一份变更。
    :param project_id: 哪个项目。
    :param digest: **那一版内容的摘要** —— 事后核对的凭据。
    :param detail: 补充说明。
    """

    action: str
    change_id: str
    project_id: str
    digest: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReleaseEntry:
    """放行台账上的一行 —— **放行了什么，凭的是哪几份评审**。

    :param change_id: 哪一份变更。
    :param project_id: 哪个项目。
    :param operation: 什么操作。
    :param kind: 哪一类改动。
    :param risk: 风险档。
    :param digest: 放行时那一版内容的摘要。
    :param reviewers: **批过的人** —— 按名字排好序，逐字可比。
    :param covered_scopes: **名册查出来的**、被覆盖到的评审资格。
    :param required_reviews: 这一份要几份独立评审。
    :param released_at: 什么时候放行的。
    """

    change_id: str
    project_id: str
    operation: str
    kind: str
    risk: str
    digest: str
    reviewers: tuple[str, ...]
    covered_scopes: tuple[str, ...]
    required_reviews: int
    released_at: datetime | None = None


@runtime_checkable
class ChangeStorePort(Protocol):
    """变更的存放处 —— **流程读回来的一切都从这儿来**。"""

    async def save(self, change: HighRiskChange) -> None:
        """存下这份变更。"""
        ...

    async def get(self, change_id: str) -> HighRiskChange | None:
        """取这份变更 —— **没有就是没有**。"""
        ...


@runtime_checkable
class ReviewStorePort(Protocol):
    """评审结论的存放处 —— **权威的那一份**。"""

    async def request(self, change: HighRiskChange) -> str:
        """发起评审 —— **返回评审请求号**。"""
        ...

    async def record(self, request_id: str, review: HighRiskReview) -> None:
        """记下一份评审结论。"""
        ...

    async def fetch(self, request_id: str) -> tuple[HighRiskReview, ...]:
        """取回这个请求下的全部评审结论 —— **一条都不能少**。"""
        ...


@runtime_checkable
class ReviewerRegistryPort(Protocol):
    """评审人名册 —— **谁有资格评审什么**。"""

    async def scopes_of(self, reviewer: str) -> frozenset[ReviewerScope]:
        """这个人持有哪些资格 —— **不在册就是没有**。"""
        ...

    async def covers(self, reviewer: str, required: Sequence[ReviewerScope]) -> bool:
        """这个人**是否每一项资格都有**。"""
        ...


@runtime_checkable
class ReleaseAuditPort(Protocol):
    """放行审计的写入口 —— **写失败就抛**。"""

    async def record(self, event: ReleaseAuditEvent) -> None:
        """记一条审计 —— **写不进去就抛异常**。"""
        ...


@runtime_checkable
class ChangeExecutorPort(Protocol):
    """变更的落地处 —— **只有流程能碰到它**。"""

    async def execute(self, change: HighRiskChange) -> None:
        """把这份变更真的做出来。"""
        ...


@runtime_checkable
class ReleaseLedgerPort(Protocol):
    """放行台账 —— **放行了什么，事后查得到**。"""

    async def append(self, entry: ReleaseEntry) -> None:
        """记一行台账。"""
        ...


@dataclass(frozen=True, slots=True)
class HighRiskReviewPorts:
    """流程要用的那几个端口 —— **一个一个列出来，比一个大接口清楚**。"""

    changes: ChangeStorePort
    reviews: ReviewStorePort
    registry: ReviewerRegistryPort
    audit: ReleaseAuditPort
    executor: ChangeExecutorPort
    ledger: ReleaseLedgerPort


@dataclass(frozen=True, slots=True)
class ReleaseOutcome:
    """放行的结果。

    :param change: **已放行的那份变更**。
    :param reviewers: **批过的人** —— 事后核对的凭据。
    :param covered_scopes: **名册查出来的**、被覆盖到的评审资格。
    """

    change: HighRiskChange
    reviewers: tuple[str, ...]
    covered_scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HighRiskReviewFlow:
    """高风险变更的放行流程 —— **四步：提交、发起评审、记录评审、放行**。

    :param policy: 规矩 —— **风险档与评审资格**。
    :param ports: 端口 —— **变更、评审、名册、审计、执行、台账**。
    """

    policy: HighRiskPolicy
    ports: HighRiskReviewPorts

    # -- 提交 ---------------------------------------------------------------

    async def submit(self, draft: ChangeDraft) -> HighRiskChange:
        """提交一份变更 —— **从草稿进流程**。

        **风险档由规矩给，提变更的人给不了。** 提的人能写的只有"改什么、为什么
        改"；**算多高风险、要几份评审、要什么资格**，是规矩说了算。而**提的人可
        以往高里标，不能往低里标** —— 往低里标的那一份，压根建不出来。
        """
        floor = max_risk(
            risk_for_operation(draft.operation),
            MINIMUM_RISK_BY_KIND[draft.kind],
        )
        risk = floor if draft.risk is None else max_risk(floor, draft.risk)
        change = HighRiskChange(
            change_id=draft.change_id,
            project_id=draft.project_id,
            operation=draft.operation,
            kind=draft.kind,
            title=draft.title,
            target=draft.target,
            summary=draft.summary,
            proposed_by=draft.proposed_by,
            risk=risk,
            required_reviews=self.policy.reviews_for(risk),
            required_scopes=self.policy.scopes_for(draft.kind),
        )
        submitted = change.submit()
        await self._audit("submit", submitted, "变更已提交，等独立评审")
        await self.ports.changes.save(submitted)
        return submitted

    # -- 评审 ---------------------------------------------------------------

    async def request_review(self, change_id: str) -> HighRiskChange:
        """发起评审 —— **绑在这一版内容上**。"""
        change = await self._load(change_id)
        request_id = await self.ports.reviews.request(change)
        updated = change.request_review(request_id)
        await self._audit("review", updated, f"已发起评审：{request_id}")
        await self.ports.changes.save(updated)
        return updated

    async def record_review(self, change_id: str, review: HighRiskReview) -> HighRiskChange:
        """记一份评审 —— **评审人不能是提出人，评的要是这一版**。

        **先过实体那一关，再落存储。** 实体那一关是"这条结论成不成立"（独立性、
        绑定）；不成立的那一条**压根不该进存储** —— 进了存储，它就成了"有评审"
        的证据。
        """
        change = await self._load(change_id)
        updated = change.record_review(review)
        request_id = change.review_request_id
        if request_id is None:
            raise HighRiskReviewError(
                f"变更 {change.change_id} 没有发起过评审 —— 没有可记录的评审请求"
            )
        await self.ports.reviews.record(request_id, review)
        await self._audit("review", updated, f"{review.reviewer} 表了态")
        await self.ports.changes.save(updated)
        return updated

    # -- 放行 ---------------------------------------------------------------

    async def release(self, change_id: str) -> ReleaseOutcome:
        """放行 —— **唯一一条执行路径**。

        **六步，顺序不能换。**

        1. **从评审存储里取回结论** —— 不相信对象上的状态。
        2. **逐项核对** —— 有没有拒过、有几份、是不是同一版、有没有人自己批。
        3. **查名册** —— 要求的评审资格，是不是真有批过的人**在册**且**持有**。
        4. **实体层的闸** —— 状态、份数、独立性、资格覆盖。
        5. **写审计** —— 写不进去就**到此为止**（失败关闭）。
        6. **执行变更**，然后**记台账**。

        **为什么台账在执行之后写。** 台账记的是"**放行了什么**" —— 写在执行之
        前，执行失败就会留下一行"放行了"却什么都没发生。**审计才是权威的**（它
        写在执行之前，且失败关闭）；台账只是一份事后好查的索引。
        """
        change = await self._load(change_id)
        reviews = await self._fetch_reviews(change)
        approvers = self._approvers(reviews)
        self._verify(change, reviews, approvers)
        covered = await self._covered_scopes(change, approvers)
        released = change.release(covered_scopes=covered)

        await self._audit(
            "release",
            released,
            f"评审人：{', '.join(sorted(approvers))}",
        )
        await self.ports.executor.execute(released)
        await self.ports.ledger.append(
            ReleaseEntry(
                change_id=released.change_id,
                project_id=released.project_id,
                operation=released.operation.value,
                kind=released.kind.value,
                risk=released.risk.value,
                digest=released.content_digest,
                reviewers=tuple(sorted(approvers)),
                covered_scopes=tuple(scope.value for scope in covered),
                required_reviews=released.required_reviews,
                released_at=released.released_at,
            )
        )
        await self.ports.changes.save(released)
        return ReleaseOutcome(
            change=released,
            reviewers=tuple(sorted(approvers)),
            covered_scopes=tuple(scope.value for scope in covered),
        )

    # -- 内部 ---------------------------------------------------------------

    async def _load(self, change_id: str) -> HighRiskChange:
        """取一份变更 —— **没有就是没有**。"""
        change = await self.ports.changes.get(change_id)
        if change is None:
            raise HighRiskReviewError(f"没有这份变更：{change_id}")
        return change

    async def _fetch_reviews(self, change: HighRiskChange) -> tuple[HighRiskReview, ...]:
        """从评审存储里取回结论 —— **没有请求号就没有结论**。"""
        if change.review_request_id is None:
            raise HighRiskReviewError(
                f"变更 {change.change_id} 没有发起过评审 —— 没有可核对的评审结论"
            )
        return await self.ports.reviews.fetch(change.review_request_id)

    def _approvers(self, reviews: Sequence[HighRiskReview]) -> frozenset[str]:
        """批过的人 —— **去重之后**。

        **为什么按人算。** 一个人点两次通过不算两份意见 —— 独立评审数按**人**算。
        """
        return frozenset(review.reviewer for review in reviews if review.approved)

    def _verify(
        self,
        change: HighRiskChange,
        reviews: Sequence[HighRiskReview],
        approvers: frozenset[str],
    ) -> None:
        """把存储里的结论与变更当前的样子逐项对上 —— **对不上就停**。"""
        if not reviews:
            raise HighRiskReviewError(f"变更 {change.change_id} 没有评审结论")
        if any(not review.approved for review in reviews):
            raise HighRiskReviewError(f"变更 {change.change_id} 有人拒过 —— 拒绝是终态，放行不了")
        if len(approvers) < change.required_reviews:
            raise HighRiskReviewError(
                f"变更 {change.change_id} 只有 {len(approvers)} 份独立评审，"
                f"要 {change.required_reviews} 份"
            )
        if change.proposed_by in approvers:
            raise HighRiskReviewError("评审人里有提出人自己 —— 这不算独立评审")
        digest = change.content_digest
        for review in reviews:
            if review.reviewed_digest != digest:
                raise HighRiskReviewError(
                    f"评审 {review.reviewer} 评的不是这一版内容 —— 内容变了，评审作废"
                )

    async def _covered_scopes(
        self,
        change: HighRiskChange,
        approvers: frozenset[str],
    ) -> frozenset[ReviewerScope]:
        """**名册查出来的**、被覆盖到的评审资格。

        **为什么每一项资格都要有人持有，而不是一个人持有全部。** 这一份改动的几
        项资格说的是"这几件事各得有人看得懂" —— 架构与安全是两个人分别看得懂的
        东西，让一个人同时具备两项资格，是把门槛抬高到另一种不必要。

        **为什么资格只能从名册查。** 结论上的自称是自己给自己签发的 —— 签发的
        东西不独立。
        """
        covered: set[ReviewerScope] = set()
        for scope in change.required_scopes:
            for reviewer in approvers:
                if await self.ports.registry.covers(reviewer, (scope,)):
                    covered.add(scope)
                    break
        missing = set(change.required_scopes) - covered
        if missing:
            names = "、".join(scope.value for scope in ReviewerScope if scope in missing)
            raise HighRiskScopeError(
                f"评审资格不足：缺 {names} —— 批过的人里没有在册且持有这些资格的"
            )
        return frozenset(covered)

    async def _audit(
        self,
        action: str,
        change: HighRiskChange,
        detail: str = "",
    ) -> None:
        """写一条审计 —— **写不进去就抛，因而后面的事不会发生**。"""
        event = ReleaseAuditEvent(
            action=action,
            change_id=change.change_id,
            project_id=change.project_id,
            digest=change.content_digest,
            detail=detail,
        )
        try:
            await self.ports.audit.record(event)
        except Exception as error:
            raise ReleaseAuditError(
                f"变更 {change.change_id} 的审计写不进去 —— 不往下走：{error}"
            ) from error


__all__ = [
    "ChangeDraft",
    "ChangeExecutorPort",
    "ChangeStorePort",
    "HighRiskReviewError",
    "HighRiskReviewFlow",
    "HighRiskReviewPorts",
    "ReleaseAuditError",
    "ReleaseAuditEvent",
    "ReleaseAuditPort",
    "ReleaseEntry",
    "ReleaseLedgerPort",
    "ReleaseOutcome",
    "ReviewStorePort",
    "ReviewerRegistryPort",
]
