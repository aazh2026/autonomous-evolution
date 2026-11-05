"""项目注册表 —— 多项目的登记、查询与列举。

设计依据：docs/design.md §3.1（Project Registry 管理多个项目及其仓库、环境和
技术栈）、docs/tasks.md TASK-P1-013。

阶段一只注册 1 个项目，但接口按多项目设计。理由不是"以后会用"，而是
**多项目是最容易暴露隔离缺陷的场景**：两个项目的目标、策略、预算一旦串了，
后果是系统在 A 项目里用了 B 项目的权限去合并代码。所以这里把隔离做成
可断言的性质，而不是靠"记得别搞混"。

三条设计约束：

1. **注册即冻结**。``Project`` 领域模型是深度不可变的（目标六要素、自主权
   规则、环境配置全是只读容器），所以注册表存进去的就是它自己 ——
   没有"注册之后再被改坏"这条路径。
2. **重复注册被拒绝**。静默覆盖等于允许任何人把一个项目的策略换成另一个
   项目的策略，且不留痕迹。改策略要走 :meth:`ProjectRegistry.replace`，
   那条路径要求给出审批凭据并留下审计。
3. **改动一个项目不影响另一个**。这是 TASK-P1-013 的 T1 断言，也是这个
   模块存在的意义。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from core.project.models import Project


class RegistryError(Exception):
    """注册表错误基类。"""


class DuplicateProjectError(RegistryError):
    """项目 id 已被占用。"""

    def __init__(self, project_id: str) -> None:
        super().__init__(f"项目 id 已存在：{project_id}")
        self.project_id = project_id


class ProjectNotFoundError(RegistryError):
    """项目不存在。"""

    def __init__(self, project_id: str) -> None:
        super().__init__(f"项目不存在：{project_id}")
        self.project_id = project_id


class MissingApprovalError(RegistryError):
    """替换项目缺少审批凭据。"""

    def __init__(self, project_id: str) -> None:
        super().__init__(
            f"替换项目 {project_id} 必须提供审批凭据：策略变更是治理动作，不能没有审批记录"
        )
        self.project_id = project_id


@dataclass(frozen=True, slots=True)
class RegistryChange:
    """一次项目替换的审计条目。

    保留**替换前**的项目定义 —— 只记"变成了什么"的话，
    事后无法回答"是谁在什么审批下把它从 A 改成了 B"。
    """

    project_id: str
    approval_ref: str
    previous: Project
    current: Project


class ProjectRegistry:
    """项目注册表。

    线程/协程安全靠**不提供写操作**来保证：注册表内容只在构造与显式
    :meth:`replace` 时变化，而这两者都在调用方自己的控制流里。
    """

    def __init__(self, projects: Iterable[Project] = ()) -> None:
        self._projects: dict[str, Project] = {}
        self._changes: list[RegistryChange] = []
        for project in projects:
            self.register(project)

    def register(self, project: Project) -> Project:
        """登记一个项目。

        id 重复时抛 :class:`DuplicateProjectError` —— 不覆盖。
        """
        if project.id in self._projects:
            raise DuplicateProjectError(project.id)
        self._projects[project.id] = project
        return project

    def replace(self, project: Project, *, approval_ref: str) -> Project:
        """替换已登记的项目，并留下审计。

        ``approval_ref`` 是审批凭据（审批单号 / PR 号 / 工单号）。
        空凭据会被拒绝 —— 治理变更必须能追到人。
        """
        if not approval_ref.strip():
            raise MissingApprovalError(project.id)
        previous = self._projects.get(project.id)
        if previous is None:
            raise ProjectNotFoundError(project.id)
        self._projects[project.id] = project
        self._changes.append(
            RegistryChange(
                project_id=project.id,
                approval_ref=approval_ref,
                previous=previous,
                current=project,
            )
        )
        return project

    def get(self, project_id: str) -> Project:
        """按 id 取项目；不存在时抛 :class:`ProjectNotFoundError`。"""
        try:
            return self._projects[project_id]
        except KeyError:
            raise ProjectNotFoundError(project_id) from None

    def find(self, project_id: str) -> Project | None:
        """按 id 取项目；不存在时返回 ``None``。"""
        return self._projects.get(project_id)

    def list_all(self) -> tuple[Project, ...]:
        """全部已登记项目，按 id 排序。

        排序而不是插入序 —— 列举的输出得是确定的，否则"当前有哪些项目"
        这个问题每次问出来的答案顺序都不一样，测试和日志都没法比。
        """
        return tuple(self._projects[key] for key in sorted(self._projects))

    def by_repository(self, url: str) -> tuple[Project, ...]:
        """按仓库地址取项目。

        同一个仓库理论上只有一个项目，但返回元组而不是单个项目 ——
        阶段四的"多项目共享同一 monorepo"是可能的，按单值返回会写死这个假设。
        """
        return tuple(p for p in self.list_all() if p.repository.url == url)

    def changes(self) -> tuple[RegistryChange, ...]:
        """项目替换的审计流水。"""
        return tuple(self._changes)

    def __len__(self) -> int:
        return len(self._projects)

    def __contains__(self, project_id: object) -> bool:
        return project_id in self._projects

    def __iter__(self) -> Iterator[Project]:
        return iter(self.list_all())

    def __repr__(self) -> str:
        return f"ProjectRegistry(projects={len(self._projects)})"


__all__ = [
    "DuplicateProjectError",
    "MissingApprovalError",
    "ProjectNotFoundError",
    "ProjectRegistry",
    "RegistryChange",
    "RegistryError",
]
