"""项目作用域 —— 多项目隔离的值对象。

设计依据：docs/tasks.md ``TASK-P4-010``（多项目管理：Project Registry 完整化、
项目隔离；验收条件"两个项目并行演进，任务 / 候选 / 决策互不串扰；预算分别
核算"）、R-016；docs/architecture/long-run.md §4.3（十轮长跑的库表链路）。

**为什么把作用域做成值对象，而不是散落的字符串。** 两个项目的目标、策略、
预算一旦串了，后果是**系统在 A 项目里用了 B 项目的权限去合并代码** ——
而串了**不会报错**：候选照记、决策照下、任务照派，只是全都记在了别人的账
上。所以隔离必须是**构造出来就拦得住**的性质，不能靠"记得别搞混"。

三条设计约束：

1. **项目 id 必须是可判定的。** 空白、含空白字符、含分隔符的 id 一律拒掉 ——
   作用域键要能被人读、被机器解析。
2. **作用域键带前缀。** ``project:<id>`` —— 预算账本的 scope 是自由文本，
   带前缀才能与"全局"这类别的口径区分开。
3. **作用域不碰数据库。** 它只说"这件事属于哪个项目"，落库是仓储层的事。

**这一层是纯 Python** —— 没有 pydantic、没有 SQLAlchemy，也没有 I/O。所以
"两个作用域是不是同一个"能被单独证伪（``tests/unit/project/test_scope.py``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: 作用域键的前缀。预算账本的 scope 是自由文本 —— 带前缀才能与别的口径区分开。
PROJECT_SCOPE_PREFIX: Final[str] = "project:"

#: 作用域键里的分隔符。项目 id 里不许出现它。
SCOPE_SEPARATOR: Final[str] = ":"


class ProjectScopeError(ValueError):
    """项目作用域不合法。"""


@dataclass(frozen=True, slots=True)
class ProjectScope:
    """一个项目的作用域 —— **值的相等就是"同一个项目"**。

    :param project_id: 项目 id。与 ``core.project.models.Project.id`` 同源。
    """

    project_id: str

    def __post_init__(self) -> None:
        """校验项目 id —— **在这里拦住，不留到用的时候才炸**。

        **为什么值得单独校验。** 作用域键是拼出来的：``project:`` + id。id
        里带空白或分隔符，键就"人读得懂、机器解析不回来" —— 而解析不回来的
        键意味着**两个不同的作用域可能算出同一个键**，那隔离就是假的。

        **代价要说清楚。** 领域模型 ``Project.id`` 只要求非空，所以**带空白
        或冒号的项目 id 在作用域层被拒掉** —— 注册表装得下它，但它没法参与
        按项目核算的账。这是有意的取舍：宁可少支持一种 id，也不要一个算不
        清的账。
        """
        if not self.project_id.strip():
            raise ProjectScopeError("项目 id 不能为空")
        if self.project_id != self.project_id.strip():
            raise ProjectScopeError(f"项目 id 前后有空白：{self.project_id!r}")
        if any(char.isspace() for char in self.project_id):
            raise ProjectScopeError(f"项目 id 里不能有空白字符：{self.project_id!r}")
        if SCOPE_SEPARATOR in self.project_id:
            raise ProjectScopeError(
                f"项目 id 里不能有 {SCOPE_SEPARATOR!r}：{self.project_id!r} —— "
                "它会让作用域键解析不回来"
            )

    @property
    def budget_scope(self) -> str:
        """这个项目在预算账本里的 scope 键。

        **带前缀，不是裸的 id。** 账本里可能有别的口径的 scope（"global"
        之类），裸 id 会跟它们撞在一起 —— 撞了以后两个口径的钱混在一行里，
        而那一行的数字从此不再代表任何东西。
        """
        return f"{PROJECT_SCOPE_PREFIX}{self.project_id}"

    def owns(self, project_id: str) -> bool:
        """这个作用域是不是这个项目的。"""
        return self.project_id == project_id

    @classmethod
    def from_budget_scope(cls, scope: str) -> ProjectScope:
        """从预算账本的 scope 键还原出作用域。

        **不认识的键就报错。** 账本里可能有别的口径的 scope，硬还原会造出一
        个**不存在的项目** —— 而那个假项目会有一份自己的账。
        """
        prefix = PROJECT_SCOPE_PREFIX
        if not scope.startswith(prefix):
            raise ProjectScopeError(f"scope {scope!r} 不是项目作用域")
        return cls(scope[len(prefix) :])


__all__ = [
    "PROJECT_SCOPE_PREFIX",
    "SCOPE_SEPARATOR",
    "ProjectScope",
    "ProjectScopeError",
]
