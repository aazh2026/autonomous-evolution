"""工作空间的命名约定 —— 分支名、目录名、workspace id。

设计依据：docs/design.md §3.5A（每个开发任务独立工作空间）；docs/tasks.md
TASK-P1-032（分支 ``agent/<task_id>``；I-15 分支命名与重试的冲突约定）。

**这个模块很小，但它守的是一条会伤到别人的规则。** ``task_id`` 同时出现在
**分支名**和**目录路径**里 —— 于是 ``task_id`` 若不干净，伤害就有两条路：

- ``task_id = "../../etc"`` → 工作空间建到仓库外面去了（路径穿越）；
- ``task_id = "a/b"`` → 分支名里多一段斜杠，而 ``git worktree add`` 会按
  那个名字把分支建到一个谁也想不到的位置。

所以 ``task_id`` 先过白名单校验，再拿去拼路径与分支名。**校验失败就报错，
不"清洗后继续用"** —— 悄悄改掉一个 ID，比拒绝它更危险：日志里记的是
改过的 ID，而任务系统里记的是原来的 ID，两边对不上。

**一条任务一条分支，重试复用同一条。** 这条约定（I-15）解决的是"重试同一
个任务时会不会撞分支"：

- 分支跟着**尝试**走 → 每次重试一条新分支，分支名带 attempt 后缀。好处是
  两次尝试的产出泾渭分明；坏处是**产物在哪条分支上成了问题** ——
  ``ExecutionResult.artifacts.branch`` 报的是哪一条？调用方得先查"最后一
  次尝试是哪一条"，而这个信息本来应该是稳定的。
- 分支跟着**任务**走 → 重试复用同一条分支、同一个工作空间目录。产物始终
  在 ``agent/<task_id>`` 上，重试就是在这条分支上接着做。

选后者。理由是 git 的硬约束："一条分支不能同时被两个 worktree 检出"。
一任务一目录一分支，这三条同时成立时，**分支冲突在结构上不可能发生** ——
而不是靠调用方记得别并发。

重试与续跑的区别由调用方显式区分：

- **重试**（:meth:`~harness.workspace.manager.WorkspaceManager.reset`）——
  清掉上一次尝试**未提交**的残留，**已提交**的产出留着（那是上一次真正
  交付的东西）。
- **续跑**（TASK-P1-034 的恢复语义）—— 什么都不清，接着上次的状态往下走。

这条区分不该由工作空间管理器替调用方猜：一个是"重来一次"，一个是
"接着来"，两者的后果完全不同。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 分支前缀。集中在一处，是为了让"分支长什么样"只有一个答案。
BRANCH_PREFIX: str = "agent/"

#: 合法的 ``task_id``：字母数字开头，只含字母数字、点、下划线、连字符。
#:
#: 点是允许的，但**两个连续的点不允许**（``..``）—— 见
#: :func:`validate_task_id`。
_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: ``task_id`` 的长度上限。与 ``core/entities/task.py`` 里
#: ``Task.workspace_id`` 的列宽（``String(64)``）一致 —— 目录名与
#: ``workspace_id`` 用的是同一个值，超长就存不进库。
MAX_TASK_ID_LENGTH: int = 64


class InvalidTaskIdError(ValueError):
    """``task_id`` 不能用来拼路径或分支名。"""

    def __init__(self, task_id: str, reason: str) -> None:
        super().__init__(f"task_id {task_id!r} 不合法：{reason}")
        self.task_id = task_id
        self.reason = reason


def validate_task_id(task_id: str) -> str:
    """校验 ``task_id``，通过就原样返回。

    校验规则：

    1. 只含字母数字、点、下划线、连字符，且以字母数字开头；
    2. 不含 ``..``（路径穿越）；
    3. 长度不超过 :data:`MAX_TASK_ID_LENGTH`。

    **为什么不允许以点或连字符开头**：目录名以 ``-`` 开头会被命令行当成
    选项（``git worktree add -foo``）；以 ``.`` 开头会被当成隐藏目录。
    """
    if not task_id:
        raise InvalidTaskIdError(task_id, "不能为空")
    if len(task_id) > MAX_TASK_ID_LENGTH:
        raise InvalidTaskIdError(
            task_id, f"长度超过 {MAX_TASK_ID_LENGTH}（与 workspace_id 的列宽一致）"
        )
    if not _TASK_ID_PATTERN.match(task_id):
        raise InvalidTaskIdError(
            task_id, "只能含字母数字、点、下划线、连字符，且必须以字母数字开头"
        )
    if ".." in task_id:
        raise InvalidTaskIdError(task_id, "不能含连续的点（路径穿越）")
    return task_id


def branch_for_task(task_id: str) -> str:
    """这个任务的分支名 —— ``agent/<task_id>``。

    与尝试次数**无关**：重试复用同一条分支，产物位置因此是稳定的。
    """
    return f"{BRANCH_PREFIX}{validate_task_id(task_id)}"


def workspace_id_for_task(task_id: str) -> str:
    """工作空间 id —— 与 ``task_id`` 相同。

    两者相同是刻意的：工作空间**就是**某个任务的工作空间，一一对应。
    分成两个不同的 id 只会多一次查表，还会让人以为"一个任务可以有好几个
    工作空间" —— 而那正是分支冲突的来源。
    """
    return validate_task_id(task_id)


def workspace_path_for_task(workspace_root: Path, task_id: str) -> Path:
    """工作空间目录 —— ``<workspace_root>/<task_id>``。

    一任务一目录，所以两个任务并行时目录天然互不干扰（即使目录名撞了，
    也不会 —— 目录名就是 task_id，而 task_id 唯一）。
    """
    return Path(workspace_root) / workspace_id_for_task(task_id)


__all__ = [
    "BRANCH_PREFIX",
    "MAX_TASK_ID_LENGTH",
    "InvalidTaskIdError",
    "branch_for_task",
    "validate_task_id",
    "workspace_id_for_task",
    "workspace_path_for_task",
]
