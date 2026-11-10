"""系统自身改进提案 —— **自我修改只能走提案、独立测试、审批、发布这条路**。

设计依据：docs/design.md §7（"允许系统自主改进被管理的软件项目，不等于允许
它自主修改控制自己的系统。如果 Director 想修改自己的决策逻辑、权限策略、验
收规则、预算限制或安全机制，应当生成一个独立的 System Evolution Proposal，
经过独立测试和审批后再发布。否则系统可能逐步修改验收标准，让自己更容易通
过验收"）、docs/tasks.md ``TASK-P4-070``（交付物"``SystemEvolutionProposal``
实体 + 独立测试 + 审批后发布"；验收条件"系统提出的自我修改全部走独立测试与
审批；**不存在绕过审批的发布路径**"）、R-102（不得自主修改控制自身的系统）、
R-104（防止系统自改验收标准）、``TASK-P1-011``（治理外置 + 变更需审批）。

**这一模块回答一个很具体的问题：系统能不能改自己。**

答案不是"不能" —— 那等于把"系统改进被管理项目的代码"也一并否掉；也不是
"能" —— 那正是 design.md §7 划的那条边界。答案是"**能提提案，落地要走完
全程**"：提交 → 独立测试 → 审批 → 发布，**四步缺一不可**，每一步都留下能
核对的凭据。

**四条硬约束。**

1. **测试人不能是提案人。** 自己提、自己测 —— 那叫自证。
2. **审批人不能是提案人。** 同上。
3. **测试与审批都绑在内容摘要上。** 绑的是"这一版内容"
   （:attr:`SystemEvolutionProposal.content_digest`），不是"这条提案"——
   **内容改了，批准就作废**，因为人批的是他当时看到的那份改动。
4. **审批还要绑在验收标准版本上。** 这是 R-104 的落点：批完之后验收标准可能
   已经变了，那份批准就不再成立 —— **系统不能先改标准，再回头拿旧批准过关**。

**发布只有一条路。** :meth:`SystemEvolutionProposal.publish` 是状态机上唯一
能把状态推到 ``published`` 的动作。**而真正的闸在流程层**
（:mod:`orchestration.self_evolution`）：流程每次发布前都**从审批存储里把结论
重新取回来核对** —— 所以哪怕有人伪造一个 ``status=published`` 的对象，流程
照样发不出去。

**为什么这一层用冻结数据类而不是 Pydantic 模型。** 数据类的校验由
``__post_init__`` **显式写出来**：不依赖任何 per-module 豁免，也不给"这一项
忘了校验"留位置。而那份豁免清单本身是受测试守着的窄门
（``tests/unit/core/test_no_any_escape.py``：清单多一个就红）—— **能不登记就
不登记**。这与 :mod:`core.policy.approval` 用的是同一套写法。

**这个模块只管"一份提案长什么样"与"哪些一致性必须成立"。** 它不碰数据库、
不跑测试、不发审批请求、不写审计 —— 那是 :mod:`orchestration.self_evolution`
与仓储层的事。规矩（这类提案要多几个人批、风险怎么往上抬）也不在这儿，在
:mod:`core.policy.self_evolution`。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from core.evolution.common import content_digest, not_blank, specific_text, unique_scopes
from core.evolution.risk import MINIMUM_REVIEWS_BY_RISK, RISK_ORDER, ReviewerScope
from core.evolution.taxonomy import RiskLevel


class ProposalKind(StrEnum):
    """提案的种类 —— **决定它的风险下限与最少审批数**。

    分得比 :class:`~core.evolution.taxonomy.OpportunityCategory` 细，是因为这里
    要回答的不是"往哪儿走"，而是"**这一改动的分量有多重**"：改一段代码与改
    验收标准，风险压根不在一个量级上。

    - ``code_change`` —— 改自己的实现代码。
    - ``configuration_change`` —— 改配置（阈值、超时、并发数）。
    - ``dependency_change`` —— 换依赖、升版本。
    - ``architecture_change`` —— **改架构**（分层、依赖方向、模块划分）。
    - ``acceptance_criteria_change`` —— **改验收标准**（R-104 直接针对的那一类）。
    - ``policy_change`` —— 改决策策略、权限策略、预算限制。
    - ``governance_change`` —— 改治理机制本身（谁能批、怎么批）。
    """

    CODE_CHANGE = "code_change"
    CONFIGURATION_CHANGE = "configuration_change"
    DEPENDENCY_CHANGE = "dependency_change"
    ARCHITECTURE_CHANGE = "architecture_change"
    ACCEPTANCE_CRITERIA_CHANGE = "acceptance_criteria_change"
    POLICY_CHANGE = "policy_change"
    GOVERNANCE_CHANGE = "governance_change"


class ProposalStatus(StrEnum):
    """提案的生命周期状态。

    与 :class:`~core.evolution.decision.DecisionState` 一样，**状态不多不少**
    —— 每多一个状态，"这个提案现在到哪一步了"就多一个说不清的地方。

    - ``draft`` —— 刚写出来，还没提交。
    - ``submitted`` —— 已提交，等独立测试。
    - ``verified`` —— 独立测试通过，等审批。
    - ``failed_test`` —— 独立测试没过 —— **终态**，改了要重新提。
    - ``approved`` —— 审批齐备，可以发布。
    - ``rejected`` —— 被人拒了 —— **终态**，也是粘的：过期盖不住拒绝。
    - ``published`` —— 已发布 —— **终态**。
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"
    VERIFIED = "verified"
    FAILED_TEST = "failed_test"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"


class ProposalError(Exception):
    """提案相关的错误基类。"""


class ProposalStateError(ProposalError):
    """状态不对 —— **这一步现在不成立**。"""


class ProposalIndependenceError(ProposalError):
    """独立性被破坏 —— **自己测自己、自己批自己**。"""


class ProposalIntegrityError(ProposalError):
    """内容对不上 —— **测的、批的不是现在这一版**。"""


class ProposalScopeError(ProposalError):
    """评审资格不足 —— **批了的人里没有看得懂这一类的那位**。"""


#: 验收标准版本的形状 —— **定死**。
#:
#: **为什么要定死。** 版本号要能被机械地比大小，也要能被机械地推进
#: （:func:`bump_criteria_version`）。"v3"、"三"、"3.0" 三种写法都能塞进一个
#: 字符串字段，而只有一种写法能保证推进这件事不会算错。
CRITERIA_VERSION_PATTERN: Final[str] = r"^criteria-v\d+$"

#: 风险档的次序 —— **在 :mod:`core.evolution.risk` 里**。
#:
#: **为什么挪走。** 风险档要能比大小，而"低 < 中 < 高"这件事本仓库里有两处需
#: 要判（提案的风险下限、高风险变更的风险下限）。两处各写一份序，早晚会有一处
#: 忘了改 —— 而"两处的序不一样"意味着**同一档风险在两条路上算得不一样**。

#: 每类提案**天生**的风险下限 —— **规矩只能往上抬，不能往下压**。
#:
#: **为什么把下限钉在种类上。** 风险是这类改动的**内在属性**，不是项目的
#: 心情：改验收标准在任何项目里都是高风险，把它降到中风险等于给了一个口子。
#: 项目可以**更严**（见 :mod:`core.policy.self_evolution`），但不能更松。
#:
#: **这一张表是"最少几份审批"的唯一来源。** 审批数由风险档推出来（见
#: :data:`MINIMUM_APPROVALS_BY_KIND`）—— 两张表各写一遍的话，迟早会有一处
#: 忘了改，而**高风险只要一份审批**正是这张表最不该出的错。
MINIMUM_RISK_BY_KIND: Final[Mapping[ProposalKind, RiskLevel]] = MappingProxyType(
    {
        ProposalKind.CODE_CHANGE: RiskLevel.MEDIUM,
        ProposalKind.CONFIGURATION_CHANGE: RiskLevel.MEDIUM,
        ProposalKind.DEPENDENCY_CHANGE: RiskLevel.HIGH,
        ProposalKind.ARCHITECTURE_CHANGE: RiskLevel.HIGH,
        ProposalKind.ACCEPTANCE_CRITERIA_CHANGE: RiskLevel.HIGH,
        ProposalKind.POLICY_CHANGE: RiskLevel.HIGH,
        ProposalKind.GOVERNANCE_CHANGE: RiskLevel.HIGH,
    }
)

#: 每类提案**最少**要有几份独立审批 —— **由风险档推出来，不再另写一份**。
#:
#: **为什么要推而不是写死。** ``TASK-P4-060`` 的验收条件是"**标记为高风险的变更
#: 100% 经过独立评审后才可执行**" —— 关键词是"**标记为**"。若按种类写死，就会
#: 出现"代码改动这一类只要一份审批"的口子，而风险是**逐次判断**的：同一类改动
#: 这一次是高风险，那一次是低风险，都是常事。**尺子必须对着每一次的标记。**
#:
#: 所以这里只有一个来源：**风险档 → 审批数**
#: （:data:`core.evolution.risk.MINIMUM_REVIEWS_BY_RISK`）。推出来的结果是：
#: 中风险 1 份、高风险 **2 份**。
#:
#: **高风险为什么是 2 份。** 一份是自证（提的人自己批）；两份意味着另一个人也
#: 看了，而高风险改动的影响面大到一个人的判断不够。
MINIMUM_APPROVALS_BY_KIND: Final[Mapping[ProposalKind, int]] = MappingProxyType(
    {kind: MINIMUM_REVIEWS_BY_RISK[risk] for kind, risk in MINIMUM_RISK_BY_KIND.items()}
)

#: 每类提案**要求评审人持有的资格** —— **天生的下限**。
#:
#: **为什么这一张表按"种类"分，而审批数按"风险档"分。** 两件事问的不是同一个
#: 问题。"要几份"问的是**份数**，"谁来批"问的是**看得懂吗**。份数跟着风险走 ——
#: 风险是**逐次**判断的（理由见 :data:`MINIMUM_APPROVALS_BY_KIND` 的说明）；而
#: "看得懂吗"跟着**改动的种类**走 —— 改分层要架构评审人，换依赖要安全评审人，
#: 改验收标准要治理评审人。**一份改动的种类不变，它需要的资格也不变。**
#:
#: **为什么通用资格（``general``）不顶替这三种。** 顶替了就等于"至少有一位
#: 架构评审人"这句话是空的 —— 而那正是这条要求唯一要防的事。
#:
#: **为什么代码与配置改动没有专门资格。** 它们要的是"看得懂这段改动"，而那正是
#: 通用评审资格的定义。它们同样是**中风险**，一样要一份独立评审 —— 只是那份评审
#: 不必是"架构评审人"。
REQUIRED_SCOPES_BY_KIND: Final[Mapping[ProposalKind, tuple[ReviewerScope, ...]]] = (
    MappingProxyType(
        {
            ProposalKind.CODE_CHANGE: (),
            ProposalKind.CONFIGURATION_CHANGE: (),
            ProposalKind.DEPENDENCY_CHANGE: (ReviewerScope.SECURITY,),
            ProposalKind.ARCHITECTURE_CHANGE: (ReviewerScope.ARCHITECTURE,),
            ProposalKind.ACCEPTANCE_CRITERIA_CHANGE: (ReviewerScope.GOVERNANCE,),
            ProposalKind.POLICY_CHANGE: (ReviewerScope.GOVERNANCE,),
            ProposalKind.GOVERNANCE_CHANGE: (ReviewerScope.GOVERNANCE,),
        }
    )
)


def is_valid_criteria_version(version: str) -> bool:
    """这个验收标准版本长得对不对 —— **形状定死**。"""
    return re.match(CRITERIA_VERSION_PATTERN, version) is not None


def bump_criteria_version(version: str) -> str:
    """把验收标准版本推进一版 —— **从 ``criteria-v3`` 到 ``criteria-v4``**。

    **为什么推进要机械。** 版本号是人写的话就会写歪（"v4"、"4"、"四点零"），
    而推进版本是**改标准那条链上的一步** —— 它算错一次，系统就能拿着一个说不清
    的版本号继续批下去。形状不对就**报错**，不猜。
    """
    if not is_valid_criteria_version(version):
        raise ValueError(f"验收标准版本 {version!r} 的形状不对 —— 应当形如 criteria-v1")
    number = int(version.removeprefix("criteria-v"))
    return f"criteria-v{number + 1}"


def _digest(payload: Mapping[str, object]) -> str:
    """算一份内容的摘要 —— **同样的内容永远给同样的摘要**。

    **实现在 :mod:`core.evolution.common`** —— 那是两类受管控记录共用的一层。
    """
    return content_digest(payload)


@dataclass(frozen=True, slots=True)
class IndependentTestReport:
    """一次独立测试的结果 —— **测的不是提案人自己**。

    :param tester: 测试人 —— **不能是提案人**。
    :param tested_digest: 测的是哪一版内容 —— **由摘要说了算**。
    :param passed: 过没过。
    :param summary: 测了什么、怎么测的、为什么这么判。
    :param ran_at: 什么时候跑的。
    """

    tester: str
    tested_digest: str
    passed: bool
    summary: str = ""
    ran_at: datetime | None = None

    def __post_init__(self) -> None:
        """测试人不能空，内容摘要不能空 —— **没有它们的报告是空的**。"""
        object.__setattr__(self, "tester", not_blank(self.tester, "测试人"))
        object.__setattr__(self, "tested_digest", not_blank(self.tested_digest, "内容摘要"))


@dataclass(frozen=True, slots=True)
class ProposalApproval:
    """一份审批结论 —— **绑在这一版内容与这一版标准上**。

    :param approver: 审批人 —— **不能是提案人**。
    :param approved_digest: 批的是哪一版内容 —— **由摘要说了算**。
    :param criteria_version: 批的时候用的是哪一版验收标准 —— **R-104 的落点**。
    :param approved: 批了还是拒了。
    :param comment: 意见 —— **拒了更该写清楚为什么**。
    :param decided_at: 什么时候表的态。
    :param scopes: **这位审批人当时持有的评审资格**。
    """

    approver: str
    approved_digest: str
    criteria_version: str
    approved: bool
    comment: str = ""
    decided_at: datetime | None = None
    scopes: tuple[ReviewerScope, ...] = ()

    def __post_init__(self) -> None:
        """审批人、绑定对象、版本号都不能空 —— **空的就不是一份结论**。"""
        object.__setattr__(self, "approver", not_blank(self.approver, "审批人"))
        object.__setattr__(
            self, "approved_digest", not_blank(self.approved_digest, "审批绑定的内容摘要")
        )
        object.__setattr__(
            self, "criteria_version", not_blank(self.criteria_version, "审批用的验收标准版本")
        )
        object.__setattr__(self, "scopes", unique_scopes(self.scopes))


@dataclass(frozen=True, slots=True)
class SystemEvolutionProposal:
    """一份系统自身改进提案 —— **落地的唯一载体**。

    **它记录的是"谁在什么时候提出了什么改动"**，以及这份改动后来过了哪几道
    关：独立测试、审批、发布。**每一道关的凭据都留在实体上**，所以事后能回答
    "这次改动凭什么被发布"。

    **状态只能由状态机推。** 构造出来的提案一律是 ``draft``；别的状态只能由
    :meth:`submit`、:meth:`record_test`、:meth:`record_approval`、
    :meth:`publish` 推出来。
    """

    #: 提案编号。
    proposal_id: str
    #: 归属项目。
    project_id: str
    #: 提案的种类 —— **决定风险下限与最少审批数**。
    kind: ProposalKind
    #: 一句话标题 —— **给人看的**。
    title: str
    #: 要改什么 —— **路径、模块、配置项**。
    target: str
    #: 改了什么 —— **人批的就是这段话**。
    change_summary: str
    #: 谁提的 —— **不能自己测自己批**。
    proposed_by: str
    #: 判定它时所用的验收标准版本 —— **R-104 的落点**。
    criteria_version: str
    #: 风险档 —— **不得低于该种类的天生下限**。
    risk: RiskLevel
    #: 需要几份独立审批 —— **不得低于该种类的天生下限**。
    required_approvals: int = 1
    #: 补充说明。
    details: str = ""
    #: **这份改动要求评审人持有的资格** —— **空 = 不额外要求**。
    #:
    #: **为什么放在提案上而不是只放在策略里。** 策略说的是"这类改动通常要什么
    #: 资格"，提案上记的是"**这一份**要什么资格" —— 后者要跟着这一份改动走
    #: 一路（审核、审计、事后追责都得答得出"当时要求的是什么"）。
    required_scopes: tuple[ReviewerScope, ...] = ()
    #: 状态。
    status: ProposalStatus = ProposalStatus.DRAFT
    #: 独立测试报告 —— **一份就够，但必须绑在这一版内容上**。
    test_report: IndependentTestReport | None = None
    #: 审批结论 —— **一份都不能少，且都绑在这一版内容上**。
    approvals: tuple[ProposalApproval, ...] = ()
    #: 审批请求号 —— **流程据此把结论从存储里取回来核对**。
    approval_request_id: str | None = None
    #: 提交时间。
    submitted_at: datetime | None = None
    #: 发布时间。
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        """**五道校验，构造时就得全过。**

        1. 文本要写清楚 —— **占位写法不算**。
        2. 验收标准版本形状定死。
        3. 风险与审批数**不得低于该种类的天生下限**。
        4. **评审资格不得少于该种类的天生下限**。
        5. **只能以草稿状态建出来。**

        **为什么下限钉在校验里而不是只写在文档里。** 规矩写在文档里，谁都能
        "这次情况特殊"地绕过去；钉在校验里，绕不过去就是绕不过去 —— **提案
        压根建不出来**。
        """
        object.__setattr__(self, "proposal_id", not_blank(self.proposal_id, "提案编号"))
        object.__setattr__(self, "project_id", not_blank(self.project_id, "归属项目"))
        object.__setattr__(self, "title", specific_text(self.title, "标题"))
        object.__setattr__(self, "target", specific_text(self.target, "要改什么"))
        object.__setattr__(self, "change_summary", specific_text(self.change_summary, "改了什么"))
        object.__setattr__(self, "proposed_by", not_blank(self.proposed_by, "提案人"))
        object.__setattr__(self, "required_scopes", unique_scopes(self.required_scopes))
        object.__setattr__(
            self, "criteria_version", not_blank(self.criteria_version, "验收标准版本")
        )
        if not is_valid_criteria_version(self.criteria_version):
            raise ValueError(
                f"验收标准版本 {self.criteria_version!r} 的形状不对 —— 应当形如 criteria-v1"
            )
        floor = MINIMUM_RISK_BY_KIND[self.kind]
        if RISK_ORDER[self.risk] < RISK_ORDER[floor]:
            raise ValueError(
                f"{self.kind.value} 类提案的风险不得低于 {floor.value}，收到 {self.risk.value}"
            )
        minimum = MINIMUM_APPROVALS_BY_KIND[self.kind]
        if self.required_approvals < minimum:
            raise ValueError(
                f"{self.kind.value} 类提案至少要 {minimum} 份独立审批，"
                f"收到 {self.required_approvals}"
            )
        required_floor = set(REQUIRED_SCOPES_BY_KIND[self.kind])
        if not self.required_scopes:
            # **没额外要求 = 用天生的下限** —— 而不是"什么都不要"。
            object.__setattr__(
                self, "required_scopes", REQUIRED_SCOPES_BY_KIND[self.kind]
            )
        else:
            missing = required_floor - set(self.required_scopes)
            if missing:
                names = "、".join(scope.value for scope in ReviewerScope if scope in missing)
                raise ValueError(
                    f"{self.kind.value} 类提案至少要 {names} 评审资格 —— "
                    "规矩只能往严里加，不能减"
                )
        if self.status is not ProposalStatus.DRAFT:
            raise ValueError("提案只能以草稿状态创建 —— 别的状态只能由状态机推出来")

    # -- 内容摘要 -----------------------------------------------------------

    @property
    def content_digest(self) -> str:
        """内容摘要 —— **测试与审批绑的就是它**。

        **它只覆盖"这次要改什么"。** 状态、审批记录、时间戳都不在里面 ——
        它们是这份提案**走过的路**，不是**改动的内容**。内容没变，批准就还有效；
        内容变了（哪怕是被人手改了一下），摘要就变了，批准就作废。

        **"要几份审批"与"要什么资格"在里面** —— 它们是**这份改动被判成什么样**
        的一部分，不是走过的路。**理由很实在**：改低审批数或去掉资格要求，是
        **放松规矩**，而放松规矩若不使旧批准作废，就等于事后给自己开后门。
        """
        return _digest(
            {
                "project_id": self.project_id,
                "kind": self.kind.value,
                "title": self.title,
                "target": self.target,
                "change_summary": self.change_summary,
                "details": self.details,
                "risk": self.risk.value,
                "required_approvals": self.required_approvals,
                "required_scopes": [scope.value for scope in self.required_scopes],
            }
        )

    @property
    def approved_approvers(self) -> frozenset[str]:
        """批过的人 —— **去重之后**。

        **为什么去重。** 一个人点两次批准不该算两份意见 —— 一个人可以有三份
        审批记录，但独立审批数按**人**算。
        """
        return frozenset(approval.approver for approval in self.approvals if approval.approved)

    @property
    def changes_governance(self) -> bool:
        """这一改是不是在动"谁说了算" —— **那三类**。"""
        return self.kind in (
            ProposalKind.ACCEPTANCE_CRITERIA_CHANGE,
            ProposalKind.POLICY_CHANGE,
            ProposalKind.GOVERNANCE_CHANGE,
        )

    @property
    def covered_scopes(self) -> frozenset[ReviewerScope]:
        """**批过的人合起来覆盖了哪些评审资格**。

        **只算批了的。** 拒了的人持有什么资格与这条改动能不能放行无关 —— 拒
        决本身就是一条结论（而且是终态）。
        """
        covered: set[ReviewerScope] = set()
        for approval in self.approvals:
            if approval.approved:
                covered.update(approval.scopes)
        return frozenset(covered)

    # -- 状态机 -------------------------------------------------------------

    def submit(self, *, now: datetime | None = None) -> SystemEvolutionProposal:
        """提交 —— **草稿进流程**，接下来只能测。

        **提交不是发布。** 提交只是把这份改动摆到独立测试面前。
        """
        if self.status is not ProposalStatus.DRAFT:
            raise ProposalStateError(f"只有草稿能提交，当前状态是 {self.status.value}")
        return self._replace(status=ProposalStatus.SUBMITTED, submitted_at=now)

    def record_test(self, report: IndependentTestReport) -> SystemEvolutionProposal:
        """记一次独立测试 —— **测试人不能是提案人，测的要是这一版**。

        :param report: 测试报告。
        """
        if self.status is not ProposalStatus.SUBMITTED:
            raise ProposalStateError(
                f"只有已提交的提案可以记录测试结果，当前状态是 {self.status.value}"
            )
        if report.tester == self.proposed_by:
            raise ProposalIndependenceError("测试人不能是提案人 —— 自己测自己不算独立测试")
        if report.tested_digest != self.content_digest:
            raise ProposalIntegrityError("测的不是这一版内容 —— 测完之后内容变了")
        status = ProposalStatus.VERIFIED if report.passed else ProposalStatus.FAILED_TEST
        return self._replace(test_report=report, status=status)

    def record_approval(self, approval: ProposalApproval) -> SystemEvolutionProposal:
        """记一份审批 —— **审批人不能是提案人，批的要是这一版与这一版标准**。

        **拒了就是拒了。** 一份拒绝会把状态推到 ``rejected``，而那之后不再接
        受审批 —— **过期盖不住拒绝，人拒过的东西不会因为没人再表态就变成"没
        人反对"**。
        """
        if self.status is not ProposalStatus.VERIFIED:
            raise ProposalStateError(
                f"只有通过独立测试的提案可以进入审批，当前状态是 {self.status.value}"
            )
        if approval.approver == self.proposed_by:
            raise ProposalIndependenceError("审批人不能是提案人 —— 自己批自己不算审批")
        if approval.approved_digest != self.content_digest:
            raise ProposalIntegrityError("批的不是这一版内容 —— 批完之后内容变了")
        if approval.criteria_version != self.criteria_version:
            raise ProposalIntegrityError(
                "审批用的验收标准版本与判定这份提案时的不一致 —— "
                f"提案按 {self.criteria_version} 判定，审批按 {approval.criteria_version} 判定"
            )
        approvals = (*self.approvals, approval)
        if not approval.approved:
            return self._replace(approvals=approvals, status=ProposalStatus.REJECTED)
        enough = len(self.approved_approvers | {approval.approver}) >= self.required_approvals
        status = ProposalStatus.APPROVED if enough else self.status
        return self._replace(approvals=approvals, status=status)

    def request_approval(self, request_id: str) -> SystemEvolutionProposal:
        """记下审批请求号 —— **流程据此把结论从存储里取回来核对**。

        **为什么连请求号都要记。** 因为对象上的 ``status`` 是**可以伪造的**
        （绕过冻结直接改字段，谁都能做到）。真正说了算的是审批存储里那份结论
        —— 流程发布前会重新取回来核对，而它得知道去哪儿取。
        """
        text = not_blank(request_id, "审批请求号")
        return self._replace(approval_request_id=text)

    def publish(
        self,
        *,
        current_criteria_version: str,
        now: datetime | None = None,
    ) -> SystemEvolutionProposal:
        """发布 —— **唯一一条落地路径**。

        :param current_criteria_version: **现在生效的**验收标准版本。
        :param now: 发布时间。

        **四道校验，一道都不能少。**

        1. 状态必须是 ``approved`` —— 没批过的不许发。
        2. 必须有一份**通过了的**独立测试，且它测的就是这一版内容。
        3. 独立审批要**够数**，且批的都是这一版内容；**要求的评审资格要有人持
           有**（:attr:`required_scopes`）。
        4. **现在的验收标准版本必须与判定时的那一版一致** —— 标准变了，那份
           批准就不再成立（R-104）。

        **第 4 条为什么在这里而不在流程层。** 流程层也会比一遍，但**这一条是
        这份提案自己的性质**：它是在"标准是 v3"这件事上被批准的。把它写进实
        体，任何调用方都绕不过去。

        **第 3 条后半段（评审资格）为什么也在实体里。** "有几个人批了"回答不了
        "这几个人看得懂吗" —— 改架构的改动让一个只懂测试的人批，等于没批。而
        **实体这一层只认审批记录上记着的资格** —— 资格本身是不是真的，流程层
        要拿**在册的评审人名册**再核一遍（:mod:`orchestration.high_risk_review`
        干的就是这件事）。两层都要过。
        """
        if self.status is not ProposalStatus.APPROVED:
            raise ProposalStateError(f"只有已审批的提案可以发布，当前状态是 {self.status.value}")
        if self.test_report is None or not self.test_report.passed:
            raise ProposalStateError("没有通过的独立测试，不得发布")
        if self.test_report.tested_digest != self.content_digest:
            raise ProposalIntegrityError("独立测试测的不是这一版内容")
        approvers = self.approved_approvers
        if len(approvers) < self.required_approvals:
            raise ProposalStateError(f"还缺 {self.required_approvals - len(approvers)} 份独立审批")
        if self.proposed_by in approvers:
            raise ProposalIndependenceError("提案人不能给自己批")
        for approval in self.approvals:
            if approval.approved and approval.approved_digest != self.content_digest:
                raise ProposalIntegrityError("有一份审批批的不是这一版内容")
        missing = set(self.required_scopes) - self.covered_scopes
        if missing:
            names = "、".join(scope.value for scope in ReviewerScope if scope in missing)
            raise ProposalScopeError(
                f"评审资格不足：缺 {names} —— 批过的人里没有持有这些资格的"
            )
        if current_criteria_version != self.criteria_version:
            raise ProposalIntegrityError(
                "验收标准在审批之后变了，这份批准不再成立 —— "
                f"提案按 {self.criteria_version} 判定，现在生效的是 {current_criteria_version}"
            )
        return self._replace(status=ProposalStatus.PUBLISHED, published_at=now)

    # -- 内部 ---------------------------------------------------------------

    def _replace(self, **changes: object) -> SystemEvolutionProposal:
        """换掉几个字段 —— **返回新的一份，不改原来那份**。

        **为什么不用 :func:`dataclasses.replace`。** 它会重跑 ``__post_init__``，
        而那里有一条"提案只能以草稿状态建出来"—— 那条是给**外面**看的（防止
        有人凭空造一个已审批的提案），状态机自己推状态时不能被它拦住。

        **这里换的字段都是生命周期字段**，内容一个都不动；真正要防的"内容被
        偷改"由 :attr:`content_digest` 兜住 —— 内容一变，摘要就变，绑定在摘要
        上的测试与审批就全都失效。
        """
        values = {field.name: getattr(self, field.name) for field in fields(self)}
        values.update(changes)
        clone = object.__new__(type(self))
        for name, value in values.items():
            object.__setattr__(clone, name, value)
        return clone


__all__ = [
    "CRITERIA_VERSION_PATTERN",
    "MINIMUM_APPROVALS_BY_KIND",
    "MINIMUM_RISK_BY_KIND",
    "REQUIRED_SCOPES_BY_KIND",
    "IndependentTestReport",
    "ProposalApproval",
    "ProposalError",
    "ProposalIndependenceError",
    "ProposalIntegrityError",
    "ProposalKind",
    "ProposalScopeError",
    "ProposalStateError",
    "ProposalStatus",
    "SystemEvolutionProposal",
    "bump_criteria_version",
    "is_valid_criteria_version",
]
