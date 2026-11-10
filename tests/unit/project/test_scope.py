"""TASK-P4-010 ① —— 项目作用域的契约。

设计依据：docs/tasks.md TASK-P4-010（验收条件"两个项目并行演进，任务 / 候选 /
决策互不串扰；预算分别核算"）、R-016。

**这一层是纯函数。** 没有数据库、没有 I/O —— 所以这里每一条断言都在证伪
"作用域的口径"，而不是证伪"数据库连得上"。串扰与算错账的**后果**在
``tests/integration/test_multi_project.py`` 里验。

**为什么作用域要拒掉一些 id。** 作用域键是 ``project:`` + id 拼出来的。id
里带空白或冒号，键就"人读得懂、机器解析不回来" —— 而解析不回来的键意味
着**两个不同的作用域可能算出同一个键**。那时隔离就是假的，而且假得**看不
出来**：两个项目共用一行预算，报表上只有一行，谁也看不出哪里不对。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core.project.scope import (
    PROJECT_SCOPE_PREFIX,
    SCOPE_SEPARATOR,
    ProjectScope,
    ProjectScopeError,
)


class TestScopeKey:
    """作用域键的形状。"""

    def test_the_key_is_prefixed(self) -> None:
        """**带前缀，不是裸 id。**

        账本里可能有别的口径的 scope（"global" 之类）。裸 id 会跟它们撞在
        一起 —— 撞了以后两个口径的钱混在一行里，而那一行的数字从此不再代表
        任何东西。
        """
        scope = ProjectScope("my-product")
        assert scope.budget_scope == f"{PROJECT_SCOPE_PREFIX}my-product"

    def test_two_projects_never_share_a_key(self) -> None:
        assert ProjectScope("a").budget_scope != ProjectScope("b").budget_scope

    def test_the_key_round_trips(self) -> None:
        """键能解析回项目 —— 否则"按项目查账"查不了。"""
        scope = ProjectScope("my-product")
        assert ProjectScope.from_budget_scope(scope.budget_scope) == scope

    def test_the_separator_is_the_one_used_in_the_key(self) -> None:
        """分隔符只有一个 —— 解析的时候没有第二种可能。"""
        assert PROJECT_SCOPE_PREFIX.endswith(SCOPE_SEPARATOR)


class TestRejectedIds:
    """不合法的项目 id —— 在作用域层就拒掉。"""

    def test_an_empty_id_is_rejected(self) -> None:
        with pytest.raises(ProjectScopeError):
            ProjectScope("")

    def test_a_blank_id_is_rejected(self) -> None:
        with pytest.raises(ProjectScopeError):
            ProjectScope("   ")

    def test_leading_whitespace_is_rejected(self) -> None:
        with pytest.raises(ProjectScopeError):
            ProjectScope(" my-product")

    def test_trailing_whitespace_is_rejected(self) -> None:
        with pytest.raises(ProjectScopeError):
            ProjectScope("my-product ")

    def test_embedded_whitespace_is_rejected(self) -> None:
        """id 里有空白 → 键解析回来会少一截。"""
        with pytest.raises(ProjectScopeError):
            ProjectScope("my product")

    def test_a_tab_inside_is_rejected(self) -> None:
        with pytest.raises(ProjectScopeError):
            ProjectScope("my\tproduct")

    def test_a_colon_inside_is_rejected(self) -> None:
        """**冒号是分隔符** —— id 里有它，"project:a:b" 解析回来会变成两个 id。"""
        with pytest.raises(ProjectScopeError):
            ProjectScope("my:product")

    def test_the_error_names_the_project(self) -> None:
        """报错要能指认是哪个 id —— 不然项目一多就不知道是谁被拒了。"""
        with pytest.raises(ProjectScopeError) as caught:
            ProjectScope("my product")
        assert "my product" in str(caught.value)


class TestFromBudgetScope:
    """从账本的 scope 键还原作用域。"""

    def test_an_unknown_prefix_is_rejected(self) -> None:
        """不认识的键就报错 —— 硬还原会造出一个**不存在的项目**。

        那个假项目会有一份自己的账，而没人知道它是假的。
        """
        with pytest.raises(ProjectScopeError):
            ProjectScope.from_budget_scope("global")

    def test_an_empty_scope_is_rejected(self) -> None:
        with pytest.raises(ProjectScopeError):
            ProjectScope.from_budget_scope("")

    def test_a_bare_project_id_is_rejected(self) -> None:
        """没有前缀的裸 id 不是作用域键 —— 它可能是别的口径的 scope。"""
        with pytest.raises(ProjectScopeError):
            ProjectScope.from_budget_scope("my-product")

    def test_a_prefix_with_nothing_after_it_is_rejected(self) -> None:
        with pytest.raises(ProjectScopeError):
            ProjectScope.from_budget_scope(PROJECT_SCOPE_PREFIX)


class TestEquality:
    """作用域的相等就是"同一个项目"。"""

    def test_the_same_id_is_the_same_scope(self) -> None:
        assert ProjectScope("a") == ProjectScope("a")

    def test_different_ids_are_different_scopes(self) -> None:
        assert ProjectScope("a") != ProjectScope("b")

    def test_owns_answers_about_membership(self) -> None:
        scope = ProjectScope("a")
        assert scope.owns("a") is True
        assert scope.owns("b") is False

    def test_a_scope_is_immutable(self) -> None:
        """作用域是值 —— 改了它，"同一个项目"这句话就没意义了。"""
        scope = ProjectScope("a")
        with pytest.raises(FrozenInstanceError):
            scope.project_id = "b"  # type: ignore[misc]
