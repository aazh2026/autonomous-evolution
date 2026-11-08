"""任务级授权上下文 —— Agent 身份不能替代任务身份。

设计依据：docs/design.md §3.5D（"每个 Agent 有独立执行身份"、"每个任务有独立
Task ID 和授权范围"、"Agent 的权限绑定到任务、项目、工作空间和有效期"、
"删除、发布、合并、部署等高风险操作有独立授权"、"每一次工具调用均记录主体、
任务、资源和执行结果"、"特别重要的是：Agent 身份不能替代任务身份"）；
docs/tasks.md TASK-P1-044、R-059 / R-061 / R-062 / R-063、I-24。

**这个模块只回答"准不准"。** 授权判定不看 diff（那是
:mod:`harness.security.gate` 的事），也不负责落库（那是
:mod:`harness.security.audit` 的事）。它只做一件事：给定"谁、在哪个任务、
哪个项目、哪个工作空间、要做哪个操作、能碰哪个资源"，回答"允许吗"。

**授权绑定的是任务，不是 Agent。** 同一个 Coding Agent 可以同时在多个任务上
干活；它在任务 A 里拿到仓库读权限，**不构成**它在任务 B 里的任何授权 ——
"它有权限"这句话在没有任务的前提下是不完整的。所以判定的 key 是五元组::

    (agent_id, task_id, project_id, workspace_id, expiry)

五个要素缺一不可：少了 ``task_id``，"这个 Agent 有权限"就变成了对整个仓库的
授权；少了 ``project_id``，A 项目的授权就能在 B 项目里用；少了
``workspace_id``，一条任务授权就能横向摸到别的工作空间；少了 ``expiry``，
一条十分钟后失效的授权会一直有效。

**两道闸，一起过。** 一条授权记录同时说两件事：

- :attr:`TaskAuthorization.allowed_operations` —— **做什么**（任务级操作）；
- :attr:`TaskAuthorization.granted_scopes` —— **能碰什么**（资源级能力）。

只写操作不写能力，等于"允许合并"却没有"允许动主干"；只写能力不写操作，
等于"允许动主干"却没有"允许合并"。少一道，授权就只剩一半语义，而"缺的
是哪一半"永远说不清。所以两道闸是**同时**判的，不是二选一。

**高风险能力要一份独立的授权记录。** 删除、发布、合并、部署（design.md
§3.5D）不靠任务授权本身放行：它们必须在**另一本册子**里另有一条记录，
且那条记录要引用一条审批。理由是"独立"这两个字：写在任务授权里的高风险
许可，与授权本身的签发过程是同一个 —— 它没有经过任何**额外的**检查。把它
拆到另一个册子，"这份许可是谁单独给的"才有一个可以查的答案。缺了这条记录，
高风险操作一律拒绝。

**判定结果一律留痕，留痕失败即阻断。** 每次判定（放行与拒绝一样）都交给
:class:`DecisionSink` 记录。写不下来的授权决定等于没有授权决定，所以留痕
失败时操作被**阻断**（fail-closed），而不是"先放行，回头再补记录"。

**与策略层的关系。** :mod:`core.policy.autonomy` 决定"这类操作在项目策略
下允许到什么程度"（自主 / 需审批 / 仅建议）；本模块决定"**这个**任务里的
**这个** Agent 有没有拿到那把钥匙"。两者都要过：策略说允许不等于这条
任务拿到了授权，授权说允许也不等于策略允许。**任务授权只能收紧策略，
不能放宽策略。**
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, NamedTuple, Protocol, runtime_checkable

from core.policy.autonomy import Operation

#: 路径型 scope 的前缀。``path:/abs/dir`` 表示"只授权这个子树"。
PATH_SCOPE_PREFIX: Final[str] = "path:"


class Capability(StrEnum):
    """资源级能力 —— 授权说"能碰什么"时用的词汇表。

    **这是个闭集。** 能力只有这几种：新增一种能力就是新增一条治理规则，
    该走外部配置 + 审批那条路，而不是在代码里随手加一个字符串 —— 后者
    会让"授权到底能干什么"这个问题没有确定答案。
    """

    REPO_READ = "repo:read"
    REPO_WRITE = "repo:write"
    REPO_DELETE = "repo:delete"
    REPO_PUBLISH = "repo:publish"
    REPO_MERGE = "repo:merge"
    REPO_DEPLOY = "repo:deploy"
    REPO_ADMIN = "repo:admin"


#: 高风险能力（design.md §3.5D「删除、发布、合并、部署等高风险操作有独立
#: 授权」）。**"等"字是认真的** —— 改自己的治理规则（:attr:`Capability.
#: REPO_ADMIN`）不比部署轻，所以一并算进去。
HIGH_RISK_CAPABILITIES: Final[frozenset[Capability]] = frozenset(
    {
        Capability.REPO_DELETE,
        Capability.REPO_PUBLISH,
        Capability.REPO_MERGE,
        Capability.REPO_DEPLOY,
        Capability.REPO_ADMIN,
    }
)

#: 任务级操作 → 它行使的资源级能力。
#:
#: 这张表是"做什么"与"能碰什么"之间唯一的翻译处。它只做**收紧**：一个操作
#: 的能力需求由表给出，授权里没有这项能力就是不行 —— 表里没有的操作
#: （未来新增的）拿不到能力，也就走不通。
_OPERATION_CAPABILITY: Final[Mapping[Operation, Capability]] = {
    Operation.ANALYZE_PROJECT: Capability.REPO_READ,
    Operation.PLAN_TASKS: Capability.REPO_READ,
    Operation.CODE_CHANGES: Capability.REPO_WRITE,
    Operation.TEST_EXECUTION: Capability.REPO_WRITE,
    Operation.BRANCH_CREATION: Capability.REPO_WRITE,
    Operation.MERGE_TO_MAIN: Capability.REPO_MERGE,
    Operation.PRODUCTION_DEPLOYMENT: Capability.REPO_DEPLOY,
    Operation.MODIFY_GOVERNANCE: Capability.REPO_ADMIN,
}


class AuthorizationIdentity(NamedTuple):
    """授权记录归属的四个身份要素。

    这是五元组 key 的**前四项** —— 它回答"这条授权是给谁的"，不回答
    "它什么时候失效"。
    """

    agent_id: str
    task_id: str
    project_id: str
    workspace_id: str


class AuthorizationKey(NamedTuple):
    """授权记录的 key —— 身份四元组 + 有效期。

    **有效期是 key 的一部分**，不是记录上的一个装饰。同一身份下"10:00 到期
    的那条"与"12:00 到期的那条"是**两条不同的授权**：把有效期排除在 key
    之外，就等于允许"续期"悄悄覆盖旧记录而没人看得见。

    判定用的 key 永远是这五项；少一项就没有授权 —— 查不到就是拒绝。
    """

    agent_id: str
    task_id: str
    project_id: str
    workspace_id: str
    expiry: datetime | None


def path_scope(path: Path | str) -> str:
    """构造一个路径型 scope。"""
    return f"{PATH_SCOPE_PREFIX}{Path(path)}"


def _as_utc(moment: datetime) -> datetime:
    """把 naive 时间当成 UTC。

    **没带时区的时间一律按 UTC 解释，不按本地时间。** 本地时间会让"到期"
    这件事随运行机器的时区而变 —— 同一个授权在两台机器上判断结果不同，
    那样的授权不能拿去审计。
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


@dataclass(frozen=True, slots=True)
class TaskAuthorization:
    """一条任务级授权记录。

    七个字段分成三组，各管一件事：

    - **身份**（``agent_id`` / ``task_id`` / ``project_id`` / ``workspace_id``）
      —— 这条授权是谁、在哪个任务、哪个项目、哪个工作空间里的。四个都
      不能为空：空身份意味着"谁都能用"，那是把授权记录变成公开资源。
    - **有效期**（``expiry``） —— 到期之后同一条记录不再为任何操作背书。
      ``None`` 表示长期有效；不是所有授权都需要过期时间，但需要的时候
      必须真的能过期。
    - **范围**（``allowed_operations`` / ``granted_scopes``） —— 做什么、
      能碰什么。**两道闸都要过**，见模块文档。
    """

    agent_id: str
    task_id: str
    project_id: str
    workspace_id: str
    expiry: datetime | None = None
    allowed_operations: frozenset[Operation] = frozenset()
    granted_scopes: frozenset[str] = frozenset()
    #: 高风险能力的独立授权记录所引用的审批编号（TASK-P1-046 的审批门）。
    #:
    #: **只有独立授权记录需要它。** 一条自己签发自己的高风险许可不构成
    #: 独立授权 —— "独立"要体现在签发过程上，而签发过程留下的唯一凭据
    #: 就是审批编号。没有编号的独立记录等于没有独立记录。
    approval_ref: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("agent_id", self.agent_id),
            ("task_id", self.task_id),
            ("project_id", self.project_id),
            ("workspace_id", self.workspace_id),
        ):
            if not value.strip():
                raise ValueError(f"授权记录的 {name} 不能为空")
        if self.expiry is not None:
            object.__setattr__(self, "expiry", _as_utc(self.expiry))
        object.__setattr__(self, "allowed_operations", frozenset(self.allowed_operations))
        object.__setattr__(self, "granted_scopes", frozenset(self.granted_scopes))

    @property
    def identity(self) -> AuthorizationIdentity:
        """归属四元组。"""
        return AuthorizationIdentity(
            self.agent_id, self.task_id, self.project_id, self.workspace_id
        )

    @property
    def key(self) -> AuthorizationKey:
        """授权判定的五元组 key。"""
        return AuthorizationKey(
            self.agent_id,
            self.task_id,
            self.project_id,
            self.workspace_id,
            self.expiry,
        )

    def expired_at(self, now: datetime) -> bool:
        """授权是否已过期。``expiry is None`` 表示长期有效。"""
        if self.expiry is None:
            return False
        return self.expiry <= _as_utc(now)

    def permits_operation(self, operation: Operation) -> bool:
        """操作是否在授权范围内。

        **空范围 = 什么都没授权。** 这与
        :meth:`core.policy.autonomy.TaskContext.permits` 的"空 = 不收窄"
        刻意不同：那个方法是在策略**之上**做加法（策略已经允许了，任务
        上下文不额外收窄），而这里是授权**本身** —— 一条没有写明操作的
        授权，就是一条什么操作都没授权的授权。按"不收窄"解释它，等于
        "忘了写范围"变成"全部允许"。
        """
        return operation in self.allowed_operations

    def permits_capability(self, capability: Capability) -> bool:
        """能力是否被授予。"""
        return capability.value in self.granted_scopes

    @property
    def granted_paths(self) -> frozenset[Path]:
        """授权里给出的路径范围。"""
        return frozenset(
            Path(scope[len(PATH_SCOPE_PREFIX) :])
            for scope in self.granted_scopes
            if scope.startswith(PATH_SCOPE_PREFIX)
        )


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """一次授权请求。

    **身份四要素必填**，缺一不可 —— 请求里没有任务，就无法回答"这条授权
    是不是给你的"。操作与能力至少给一个：两者都没有的请求说明不了要
    做什么。
    """

    agent_id: str
    task_id: str
    project_id: str
    workspace_id: str
    #: 任务级操作。``None`` 表示这次请求不涉及任务级操作（例如只碰一个
    #: 文件），此时必须给出 ``capability``。
    operation: Operation | None = None
    #: 资源级能力。与 ``operation`` 都给时，两者的能力**都要**被授予 ——
    #: 删一个文件既是"改代码"也是"删除"，少算一项就是漏了一道闸。
    capability: Capability | None = None
    #: 要碰的资源（文件路径）。给了就必须落在工作空间边界内。
    resource: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("agent_id", self.agent_id),
            ("task_id", self.task_id),
            ("project_id", self.project_id),
            ("workspace_id", self.workspace_id),
        ):
            if not value.strip():
                raise ValueError(f"授权请求的 {name} 不能为空")
        if self.operation is None and self.capability is None:
            raise ValueError("授权请求必须指明操作或能力")

    @property
    def identity(self) -> AuthorizationIdentity:
        """请求的归属四元组。"""
        return AuthorizationIdentity(
            self.agent_id, self.task_id, self.project_id, self.workspace_id
        )

    @property
    def required_capabilities(self) -> frozenset[Capability]:
        """这次请求要行使的能力集合。"""
        required: set[Capability] = set()
        if self.operation is not None:
            required.add(_OPERATION_CAPABILITY[self.operation])
        if self.capability is not None:
            required.add(self.capability)
        return frozenset(required)


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """一次授权判定的结果。

    **无论放行还是拒绝都会产生一条。** 拒绝的留痕比放行的更重要 ——
    放行会留下做事的结果，拒绝只留下一句"没做"，而"为什么没做"往往正是
    后来要复盘的东西。
    """

    agent_id: str
    task_id: str
    project_id: str
    workspace_id: str
    operation: Operation | None
    capability: Capability | None
    resource: str | None
    allowed: bool
    reason: str
    decided_at: datetime
    #: 判定时生效的那条授权记录。拒绝时可能为 ``None``（压根没有记录）。
    authorization_key: AuthorizationKey | None = None
    #: 高风险操作所依据的独立授权记录。
    independent_key: AuthorizationKey | None = None
    #: 判定是否已经留痕。留痕失败时为 ``False`` —— 而此时判定结果是拒绝。
    audit_recorded: bool = True

    @property
    def identity(self) -> AuthorizationIdentity:
        """判定所属的身份四元组。"""
        return AuthorizationIdentity(
            self.agent_id, self.task_id, self.project_id, self.workspace_id
        )


@runtime_checkable
class DecisionSink(Protocol):
    """授权判定的记录去向。

    :mod:`harness.security.audit`（TASK-P1-045）会提供实现。这里只声明
    接口，不依赖实现 —— 判定逻辑不该知道记录被写到哪儿（数据库、文件、
    远端），但**必须知道记录有没有成功**：写不下来就得阻断。
    """

    def record(self, decision: AuthorizationDecision) -> None:
        """记录一条判定。抛异常即视为留痕失败。"""
        ...


class AuthorizationDeniedError(RuntimeError):
    """授权被拒。"""

    def __init__(self, decision: AuthorizationDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class AuthorizationRegistry:
    """授权记录登记表。

    记录按**五元组 key** 存：``(agent_id, task_id, project_id, workspace_id,
    expiry)``。同一个身份四元组上可以有多条记录 —— 续期、收紧、加严都是
    新的一条，而不是就地改旧的（改旧的会让"当时授权的是什么"这个问题
    变得没法回答）。

    **这个类只存，不判。** 判定在 :class:`AuthorizationService` 里。
    """

    def __init__(self) -> None:
        self._grants: dict[AuthorizationKey, TaskAuthorization] = {}
        self._sequence: dict[AuthorizationKey, int] = {}
        self._by_identity: dict[AuthorizationIdentity, list[AuthorizationKey]] = {}
        self._counter = 0

    def register(self, grant: TaskAuthorization) -> AuthorizationKey:
        """登记一条授权，返回它的 key。

        同一个 key 只能登记一次 —— 重复登记意味着两条授权悄悄顶掉了另一
        条，而"顶掉了"这件事必须看得见。
        """
        key = grant.key
        if key in self._grants:
            raise ValueError(f"授权 key 已存在：{key}")
        self._counter += 1
        self._grants[key] = grant
        self._sequence[key] = self._counter
        self._by_identity.setdefault(grant.identity, []).append(key)
        return key

    def get(self, key: AuthorizationKey) -> TaskAuthorization | None:
        """按五元组 key 精确取一条。"""
        return self._grants.get(key)

    def effective(self, identity: AuthorizationIdentity) -> TaskAuthorization | None:
        """某个身份当前生效的那条记录。

        取**最后登记**的那条，理由是"后发接管"：

        - **续期** —— 新登记的记录到期更晚，它接管；
        - **收紧** —— 后登记的窄授权接管，先登记的宽授权不再生效。

        不取并集，是因为并集让"收回授权"根本做不到：一条被收窄的授权不会
        让先前的宽授权失效，而收紧的全部意义就是收回。
        """
        keys = self._by_identity.get(identity)
        if not keys:
            return None
        newest = max(keys, key=self._sequence.__getitem__)
        return self._grants[newest]

    def grants_for(self, identity: AuthorizationIdentity) -> tuple[TaskAuthorization, ...]:
        """某个身份名下的全部记录，按登记顺序。"""
        keys = self._by_identity.get(identity, [])
        return tuple(self._grants[key] for key in sorted(keys, key=self._sequence.__getitem__))

    def __len__(self) -> int:
        return len(self._grants)


class AuthorizationService:
    """任务级授权的判定入口。

    三样东西缺一不可：**授权登记册**（谁被授权了什么）、**工作空间边界**
    （路径能走到哪儿）、**判定留痕的去处**（判定结果记到哪儿）。三者都是
    构造参数而不是可选项 —— 忘配任何一样，后果分别是"谁都算被授权"、
    "路径不设防"、"判定不留痕"，而这三样都是**安静地**放行的。
    """

    def __init__(
        self,
        registry: AuthorizationRegistry,
        *,
        sink: DecisionSink,
        workspace_roots: Mapping[str, Path],
        independent_registry: AuthorizationRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """构造判定器。

        :param registry: 一般授权登记册。
        :param sink: 判定留痕的去处（:mod:`harness.security.audit` 提供实现）。
        :param workspace_roots: 工作空间 id → 目录。**判定时取快照** ——
            边界不能在判定中途变宽。
        :param independent_registry: 独立授权登记册。``None`` 表示没有独立
            册子，于是**所有高风险操作一律拒绝** —— 这是默认姿势。
        :param clock: 取当前时间的方式。测试用它把时间拨到授权到期之后。
        """
        self._registry = registry
        self._independent = independent_registry
        self._sink = sink
        self._workspace_roots = dict(workspace_roots)
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def registry(self) -> AuthorizationRegistry:
        return self._registry

    def authorize(self, request: AuthorizationRequest) -> AuthorizationDecision:
        """判定一次请求。**返回判定结果，不抛异常。**"""
        decision = self._judge(request)
        return self._record(decision)

    def is_allowed(self, request: AuthorizationRequest) -> bool:
        """这次请求允许吗。"""
        return self.authorize(request).allowed

    def require(self, request: AuthorizationRequest) -> AuthorizationDecision:
        """判定一次请求；被拒时抛 :class:`AuthorizationDeniedError`。

        给工具调用拦截点用：判定与"停在这里"是同一个动作，分成两步就
        会出现"判完了但没停"的缝。
        """
        decision = self.authorize(request)
        if not decision.allowed:
            raise AuthorizationDeniedError(decision)
        return decision

    # ---- 判定 ----

    def _judge(self, request: AuthorizationRequest) -> AuthorizationDecision:
        now = self._clock()
        identity = request.identity
        grant = self._registry.effective(identity)
        if grant is None:
            return self._deny(
                request,
                now,
                f"没有针对 agent={identity.agent_id} task={identity.task_id} "
                f"project={identity.project_id} workspace={identity.workspace_id} 的授权记录",
            )
        problem = self._grant_problem(grant, request, now)
        if problem is not None:
            return self._deny(request, now, problem, authorization_key=grant.key)
        independent = self._check_high_risk(request, now)
        if isinstance(independent, str):
            return self._deny(request, now, independent, authorization_key=grant.key)
        return AuthorizationDecision(
            agent_id=request.agent_id,
            task_id=request.task_id,
            project_id=request.project_id,
            workspace_id=request.workspace_id,
            operation=request.operation,
            capability=request.capability,
            resource=request.resource,
            allowed=True,
            reason="授权有效",
            decided_at=now,
            authorization_key=grant.key,
            independent_key=independent.key if independent is not None else None,
        )

    def _grant_problem(
        self, grant: TaskAuthorization, request: AuthorizationRequest, now: datetime
    ) -> str | None:
        """这条授权本身有没有问题。返回理由；``None`` 表示通过。"""
        if grant.expired_at(now):
            expiry = grant.expiry.isoformat() if grant.expiry is not None else "?"
            return f"授权已于 {expiry} 过期"
        if request.operation is not None and not grant.permits_operation(request.operation):
            return f"操作 {request.operation.value} 不在任务 {request.task_id} 的授权范围内"
        for capability in sorted(request.required_capabilities, key=lambda item: item.value):
            if not grant.permits_capability(capability):
                return f"授权未授予能力 {capability.value}"
        if request.resource is not None:
            return self._resource_problem(grant, request.resource)
        return None

    def _check_high_risk(
        self, request: AuthorizationRequest, now: datetime
    ) -> TaskAuthorization | str | None:
        """高风险能力需要一份独立授权记录。

        返回独立记录；返回字符串则表示拒绝理由；返回 ``None`` 表示这次
        请求不涉及高风险能力。
        """
        risky = sorted(
            request.required_capabilities & HIGH_RISK_CAPABILITIES, key=lambda item: item.value
        )
        if not risky:
            return None
        independent = self._independent.effective(request.identity) if self._independent else None
        if independent is None:
            names = "、".join(capability.value for capability in risky)
            return f"高风险能力 {names} 缺少独立授权记录"
        problem = self._independent_problem(independent, risky, request, now)
        if problem is not None:
            return problem
        return independent

    def _independent_problem(
        self,
        independent: TaskAuthorization,
        risky: list[Capability],
        request: AuthorizationRequest,
        now: datetime,
    ) -> str | None:
        """独立授权记录本身有没有问题。返回理由；``None`` 表示通过。"""
        if independent.expired_at(now):
            expiry = independent.expiry.isoformat() if independent.expiry is not None else "?"
            return f"高风险能力的独立授权记录已于 {expiry} 过期"
        for capability in risky:
            if not independent.permits_capability(capability):
                return f"独立授权记录未授予高风险能力 {capability.value}"
        if request.operation is not None and not independent.permits_operation(request.operation):
            return f"独立授权记录未覆盖操作 {request.operation.value}"
        if not independent.approval_ref:
            return "独立授权记录没有引用审批 —— 自己签发自己的许可不构成独立授权"
        return None

    def _resource_problem(self, grant: TaskAuthorization, resource: str) -> str | None:
        """资源边界检查。返回拒绝理由；``None`` 表示通过。"""
        root = self._workspace_roots.get(grant.workspace_id)
        if root is None:
            return f"工作空间 {grant.workspace_id} 没有登记目录边界"
        resolved_root = root.resolve()
        target = Path(resource)
        if not target.is_absolute():
            target = resolved_root / target
        resolved = target.resolve()
        if not _is_within(resolved_root, resolved):
            return f"路径 {resolved} 不在工作空间 {grant.workspace_id} 内"
        granted_paths = grant.granted_paths
        if granted_paths and not any(
            _is_within(path.resolve(), resolved) for path in granted_paths
        ):
            return f"路径 {resolved} 不在授权的路径范围内"
        return None

    # ---- 留痕 ----

    def _record(self, decision: AuthorizationDecision) -> AuthorizationDecision:
        """把判定交给留痕去处。**留痕失败即阻断。**"""
        try:
            self._sink.record(decision)
        except Exception as error:
            return replace(
                decision,
                allowed=False,
                reason=f"授权判定留痕失败（{type(error).__name__}），操作被阻断",
                audit_recorded=False,
            )
        return decision

    # ---- 构造判定 ----

    def _deny(
        self,
        request: AuthorizationRequest,
        now: datetime,
        reason: str,
        *,
        authorization_key: AuthorizationKey | None = None,
    ) -> AuthorizationDecision:
        return AuthorizationDecision(
            agent_id=request.agent_id,
            task_id=request.task_id,
            project_id=request.project_id,
            workspace_id=request.workspace_id,
            operation=request.operation,
            capability=request.capability,
            resource=request.resource,
            allowed=False,
            reason=reason,
            decided_at=now,
            authorization_key=authorization_key,
        )


def _is_within(root: Path, candidate: Path) -> bool:
    """``candidate`` 是否在 ``root`` 之内（含 root 本身）。

    **两边都已经 resolve 过。** 这一步是边界检查的全部要害：不 resolve
    的话，``/ws/../etc/passwd`` 与指向外部的符号链接都能从边界里"走出去"
    —— 路径检查的是**字符串**，而文件系统认的是**它解析之后指向哪里**。
    """
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = [
    "HIGH_RISK_CAPABILITIES",
    "PATH_SCOPE_PREFIX",
    "AuthorizationDecision",
    "AuthorizationDeniedError",
    "AuthorizationIdentity",
    "AuthorizationKey",
    "AuthorizationRegistry",
    "AuthorizationRequest",
    "AuthorizationService",
    "Capability",
    "DecisionSink",
    "TaskAuthorization",
    "path_scope",
]
