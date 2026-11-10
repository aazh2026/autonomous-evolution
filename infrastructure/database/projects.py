"""数据库里的项目注册表 —— 多项目的登记、读取、列举与治理变更。

设计依据：docs/tasks.md ``TASK-P4-010``（多项目管理：Project Registry 完整化、
项目隔离；验收条件"两个项目并行演进，任务 / 候选 / 决策互不串扰；预算分别
核算"）、R-016；docs/design.md §3.1（Project Registry 管理多个项目及其仓库、
环境和技术栈）、§7（系统不得自主修改治理自身的策略）。

**"完整化"补的是哪一块。** 阶段一已经有 ``core.project.registry.
ProjectRegistry`` —— 一个内存注册表，接口按多项目设计；数据库里也早有
``project`` 表与 ``core.entities.Project.from_domain`` / ``to_domain``。缺的是
**中间的持久化实现**：没有"登记"的入口（各处自己 ``session.add``），没有"读"
的路径（表里的项目读不回来），更没有"改"的路径 —— **治理策略怎么改，没有一
条带审计的路**。

**为什么"登记"不覆盖已有的项目。** 静默覆盖等于允许任何人把一个项目的策略
换成另一个项目的策略，且不留痕迹。所以重复登记**同样的**内容是幂等的（演
示与长跑都会反复跑这条路径），**不一样的**内容直接报错 —— 要改就走
:meth:`PostgresProjectRegistry.replace`，那条路要求审批凭据。

**为什么"改"必须带审批。** R-102 与 design.md §7：系统**不得自主修改治理
自身的策略**。所以这里不接受"调用方说有审批"这种说法 —— 它去查那条审批**记
录**：操作必须是 ``modify_governance``、必须批的是**这个项目**、必须**现在还
算数**（过期盖住批准，见 ``core/policy/approval.py`` 的 ``outcome``）。**查不
到就报错，绝不"查不到就当有"** —— 审计写入失败时必须**失败关闭**。

**``local_path`` 不入库。** 它是**这台机器上的**路径：换一台机器它就没意义
了，而把它写进库里会让人以为"库里那个路径是权威的"。所以库里比的是"存下来
的那些东西"，而不是两份领域对象是否逐字段相等。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.entities.project import Project as ProjectRow
from core.policy.approval import ApprovalError
from core.policy.autonomy import Operation
from core.project import models as domain
from infrastructure.database.approval import PostgresApprovalStore


class ProjectRegistryError(Exception):
    """项目注册表错误基类。"""


class ProjectNotFoundError(ProjectRegistryError):
    """项目不存在。"""

    def __init__(self, project_id: str) -> None:
        super().__init__(f"项目不存在：{project_id}")
        self.project_id = project_id


class ProjectConflictError(ProjectRegistryError):
    """项目 id 已被占用 —— **且内容不一样**。

    **不一样才是冲突。** 同样的内容重复登记是幂等的，不该报错。
    """

    def __init__(self, project_id: str) -> None:
        super().__init__(
            f"项目 {project_id} 已经登记过了，且定义与这次的不一样 —— "
            "改定义要走带审批的替换，不是重新登记"
        )
        self.project_id = project_id


class MissingApprovalError(ProjectRegistryError):
    """治理变更缺少审批凭据。"""

    def __init__(self, project_id: str) -> None:
        super().__init__(
            f"替换项目 {project_id} 必须提供审批凭据：治理策略变更是治理动作，不能没有审批记录"
        )
        self.project_id = project_id


class UnapprovedChangeError(ProjectRegistryError):
    """治理变更引用的审批**不成立**。

    **四种不成立要分开说**，因为它们要查的地方不一样：找不到、批的不是这件
    事、批的不是这个项目、批了但现在不算数（过期 / 被拒 / 未决）。
    """

    def __init__(self, project_id: str, approval_ref: str, reason: str) -> None:
        super().__init__(f"审批 {approval_ref} 不能放行项目 {project_id} 的治理变更：{reason}")
        self.project_id = project_id
        self.approval_ref = approval_ref
        self.reason = reason


class PostgresProjectRegistry:
    """数据库里的项目注册表。

    **自己管理事务。** 登记与替换都是"一步做完"的事：要么项目进库了，要么
    没进 —— 中间状态留在库里只会让"这个项目到底登记了没有"变成一个要靠猜
    的问题。

    **审批检查在写入之前。** 顺序不能反：先查审批、后写库。反过来的话，
    "审批查不到"与"写了一半"会同时发生 —— 而"写了一半"意味着治理策略已经
    被改过了，只差一张凭据。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        approval_store: PostgresApprovalStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._approvals = approval_store or PostgresApprovalStore(session_factory)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def register(self, spec: domain.Project) -> None:
        """登记一个项目。

        **幂等。** 同样的内容重复登记什么也不做 —— 撞主键冲突报出来的是"违
        反唯一约束"，看不出是"这个项目已经登记过了"。

        **内容不一样就报错。** 见 :class:`ProjectConflictError`。
        """
        async with self._session_factory() as session, session.begin():
            existing = await session.get(ProjectRow, spec.id)
            if existing is not None:
                if _same_content(existing, spec):
                    return
                raise ProjectConflictError(spec.id)
            session.add(ProjectRow.from_domain(spec))

    async def get(self, project_id: str) -> domain.Project:
        """取一个项目的定义。

        **库里被改坏的项目要当场暴露。** ``to_domain`` 会重新校验六要素 ——
        校验不过就抛，而不是带着残缺的目标继续跑。
        """
        async with self._session_factory() as session:
            row = await session.get(ProjectRow, project_id)
            if row is None:
                raise ProjectNotFoundError(project_id)
            return row.to_domain()

    async def list(self) -> tuple[domain.Project, ...]:
        """列举全部项目 —— **按 id 排序**。

        **排序是给"两份列举结果逐项对照"用的。** 数据库不保证返回顺序，不排
        的话"注册表里有三个项目"这句话没法复现。
        """
        statement = select(ProjectRow).order_by(ProjectRow.id)
        async with self._session_factory() as session:
            rows = await session.scalars(statement)
            return tuple(row.to_domain() for row in rows)

    async def replace(self, spec: domain.Project, *, approval_ref: str) -> None:
        """替换一个项目的定义 —— **治理变更，要审批**。

        :param spec: 新的定义。
        :param approval_ref: 审批请求 id。**必须是已批准的 ``modify_governance``
            请求，且批的是这个项目。**
        :raises MissingApprovalError: 没给凭据。
        :raises UnapprovedChangeError: 凭据不成立。
        :raises ProjectNotFoundError: 项目不存在。
        """
        if not approval_ref.strip():
            raise MissingApprovalError(spec.id)
        await self._assert_released(spec.id, approval_ref)
        async with self._session_factory() as session, session.begin():
            row = await session.get(ProjectRow, spec.id)
            if row is None:
                raise ProjectNotFoundError(spec.id)
            row.name = spec.name
            row.goal = spec.goal.model_dump(mode="json")
            row.policy = spec.goal.autonomy.model_dump(mode="json")
            row.budget = spec.budget.model_dump(mode="json")
            row.repository_url = spec.repository.url
            row.default_branch = spec.repository.default_branch
            row.tech_stack = list(spec.tech_stack)
            row.environment = dict(spec.environment)

    async def _assert_released(self, project_id: str, approval_ref: str) -> None:
        """这条审批现在放不放行 —— **查不到就报错，绝不"查不到就当有"**。

        **复用审批层自己的规则。** "过期盖住批准""拒绝最优先"这些是
        ``core/policy/approval.py`` 的 ``outcome`` 定的 —— 这里**不重新实现
        一遍**：两处各判一次，迟早会有一处判错，而判错的方向是"放行了一次
        没人批过的治理变更"。

        **只 catch 审批层的错。** 数据库连不上、事务失败这类异常**照样往上
        抛** —— 那不是"这条审批不成立"，那是"不知道这条审批算不算数"。两
        件事都不能当成"有审批"，但只有前者能报成审批的问题。
        """
        try:
            request = await self._approvals.get(approval_ref)
        except ApprovalError as error:
            raise UnapprovedChangeError(
                project_id, approval_ref, f"查不到审批记录：{error}"
            ) from error

        if request.operation != Operation.MODIFY_GOVERNANCE.value:
            raise UnapprovedChangeError(
                project_id,
                approval_ref,
                f"这条审批批的是 {request.operation}，不是治理变更",
            )
        if request.task_id != project_id:
            raise UnapprovedChangeError(
                project_id,
                approval_ref,
                f"这条审批批的是 {request.task_id}，不是这个项目",
            )
        now = self._clock()
        if not request.releases(now):
            raise UnapprovedChangeError(
                project_id,
                approval_ref,
                f"这条审批现在的结论是 {request.outcome(now).value}",
            )


def _same_content(row: ProjectRow, spec: domain.Project) -> bool:
    """库里这一行与这份定义**是不是同一件事**。

    **比的是"存下来的那些东西"，不是两份领域对象是否逐字段相等。** 领域
    模型里的 ``repository.local_path`` 压根不入库（那是这台机器上的路径），
    拿它比会得出"同一个项目两次登记不一样"的结论 —— 而实际上它们是同一
    个项目。
    """
    return (
        row.name == spec.name
        and row.goal == spec.goal.model_dump(mode="json")
        and row.policy == spec.goal.autonomy.model_dump(mode="json")
        and row.budget == spec.budget.model_dump(mode="json")
        and row.repository_url == spec.repository.url
        and row.default_branch == spec.repository.default_branch
        and list(row.tech_stack) == list(spec.tech_stack)
        and dict(row.environment) == dict(spec.environment)
    )


__all__ = [
    "MissingApprovalError",
    "PostgresProjectRegistry",
    "ProjectConflictError",
    "ProjectNotFoundError",
    "ProjectRegistryError",
    "UnapprovedChangeError",
]
