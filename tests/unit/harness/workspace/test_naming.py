"""TASK-P1-032 —— 命名约定。

**这一组测试守的是一条会伤到别人的规则。** ``task_id`` 同时出现在**分支名**
和**目录路径**里 —— 它若不干净，伤害有两条路：路径穿越，以及 git 把分支建
到一个谁也想不到的位置。

还有一个跨模块的不变量：``MAX_TASK_ID_LENGTH`` 必须与
``core/entities/task.py`` 里 ``Task.workspace_id`` 的列宽一致 —— 目录名用的
就是 ``workspace_id``，超长就存不进库。这条不变量横跨两个模块，靠约定
是守不住的，只能靠一条断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import String

from core.entities.task import Task
from harness.workspace.naming import (
    BRANCH_PREFIX,
    MAX_TASK_ID_LENGTH,
    InvalidTaskIdError,
    branch_for_task,
    validate_task_id,
    workspace_id_for_task,
    workspace_path_for_task,
)


class TestTheBranchNaming:
    """分支名跟着**任务**走，不跟着尝试走。"""

    def test_the_branch_is_prefixed_by_agent(self) -> None:
        """前缀是**字面量** ``agent/`` —— 这条约定写在任务书里，改它就是改约定。

        刻意不写成 ``f"{BRANCH_PREFIX}task-1"``：那样断言会跟着常量一起变，
        改错了前缀测试仍然是绿的，等于没断言。
        """
        assert branch_for_task("task-1") == "agent/task-1"
        assert BRANCH_PREFIX == "agent/"

    def test_the_branch_does_not_depend_on_the_attempt(self) -> None:
        """重试复用同一条分支 —— 所以产物位置是稳定的。

        分支名里如果带 attempt，两次尝试的产出就分家了，调用方得先查
        "最后一次尝试是哪一条"才能知道产物在哪。这个信息本该是稳定的。
        """
        assert branch_for_task("task-1") == branch_for_task("task-1")

    def test_different_tasks_get_different_branches(self) -> None:
        assert branch_for_task("task-1") != branch_for_task("task-2")


class TestTheWorkspaceIdentity:
    """工作空间 id 就是任务 id —— 一一对应。"""

    def test_the_workspace_id_is_the_task_id(self) -> None:
        assert workspace_id_for_task("task-1") == "task-1"

    def test_the_path_is_under_the_root(self) -> None:
        assert workspace_path_for_task(Path("/tmp/ws"), "task-1") == Path("/tmp/ws/task-1")

    def test_different_tasks_get_different_paths(self) -> None:
        root = Path("/tmp/ws")
        assert workspace_path_for_task(root, "a") != workspace_path_for_task(root, "b")


class TestTheTaskIdIsChecked:
    """不合法的 ``task_id`` 一律拒绝，不"清洗后继续用"。"""

    @pytest.mark.parametrize(
        "task_id",
        [
            "",
            "../etc",
            "a/b",
            "a\\b",
            ".hidden",
            "-flag",
            "..",
            "a..b",
            "with space",
            "with:colon",
            "任务-1",
        ],
    )
    def test_an_unsafe_task_id_is_rejected(self, task_id: str) -> None:
        with pytest.raises(InvalidTaskIdError):
            validate_task_id(task_id)

    @pytest.mark.parametrize("task_id", ["task-1", "TASK_1", "a.b.c", "0", "A" * 64])
    def test_a_safe_task_id_passes_through(self, task_id: str) -> None:
        assert validate_task_id(task_id) == task_id

    def test_an_overlong_task_id_is_rejected(self) -> None:
        with pytest.raises(InvalidTaskIdError):
            validate_task_id("A" * (MAX_TASK_ID_LENGTH + 1))

    def test_the_boundary_length_is_accepted(self) -> None:
        task_id = "A" * MAX_TASK_ID_LENGTH
        assert validate_task_id(task_id) == task_id

    def test_the_rejection_carries_the_reason(self) -> None:
        with pytest.raises(InvalidTaskIdError) as caught:
            validate_task_id("../etc")
        assert caught.value.task_id == "../etc"
        assert caught.value.reason


class TestTheLengthMatchesTheDatabase:
    """跨模块不变量 —— 目录名就是 ``workspace_id``，列宽必须容得下。"""

    def test_the_max_length_matches_the_column_width(self) -> None:
        column = Task.__table__.c.workspace_id
        # 先确认列类型是字符串 —— 否则 ``.length`` 这个属性根本不存在，
        # 比较的会是别的东西。
        assert isinstance(column.type, String)
        assert column.type.length == MAX_TASK_ID_LENGTH

    def test_the_longest_accepted_id_fits_in_the_column(self) -> None:
        task_id = "A" * MAX_TASK_ID_LENGTH
        assert len(workspace_id_for_task(task_id)) <= MAX_TASK_ID_LENGTH
