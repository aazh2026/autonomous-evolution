"""集成路径的测试替身。

**为什么合并口与验证运行器都要假的。** 这条路径上有两处最容易写错、又最难
用真东西测的地方：

- **合并的次序。** 暂存 → 在 main 上复验 → 提交（不过就撤销）。用真 git
  测它，每条断言都要先搭一个仓库、造一次冲突、再去看 main 到底动没动 ——
  慢，更要命的是失败时的报错是 git 的英文，不是"路径为什么撤销了合并"。
- **验证的三态。** 通过 / 没过 / 没验成。第三种（进程挂了）用真命令很难稳定
  复现 —— 而它恰恰是最要紧的一种：处置是"阻塞"，不是"返工"。

所以两边都给替身，并且**共用一条事件流水** —— 这样"先暂存再复验、复验通过
才提交"这件事能被一条断言钉住，而不是靠读代码顺序。

真 git 的行为由 ``tests/integration/test_integration_pipeline.py`` 验。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from harness.integration.pipeline import MergeError, StagedMerge
from harness.verification.engine import CheckExecution

#: 事件流水的名字 —— 断言直接读它。
EVENTS = "events"


@dataclass
class FakeCheckRunner:
    """按工作区给出检查结果的运行器。

    **结果按工作区给，而不是按命令给。** 路径在两个不同的工作区上各验一次
    —— 任务分支一次、main 上一次 —— 而这两次的结果必须能分别控制：任务分支
    过了、main 上没过，是这条路径最重要的一种情形。
    """

    #: 工作区 → 这一次的检查结果。
    results: Mapping[Path, CheckExecution] = field(default_factory=dict)
    #: 没登记的工作区用这个。
    default: CheckExecution = field(default_factory=lambda: CheckExecution(0, "", ""))
    #: 与合并口共用的事件流水。
    events: list[str] = field(default_factory=list)

    async def run(
        self,
        command: Sequence[str],
        *,
        workspace: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> CheckExecution:
        self.events.append(f"verify:{workspace}")
        return self.results.get(workspace, self.default)


@dataclass
class RecordingMerger:
    """记录调用序列的合并口。

    **它记的是"做了哪几步"，不是"最后是什么状态"。** 这条路径的规矩全在
    次序上：撤销必须发生在提交之前，复验必须发生在暂存与提交之间。记状态
    的话，这些都验不到。
    """

    #: 任务分支当前的头。
    head: str = "a" * 40
    #: main 当前的头（按工作区分别记）。
    heads: dict[Path, str] = field(default_factory=dict)
    #: 暂存合并的结果。
    staged: StagedMerge = field(default_factory=lambda: StagedMerge(changed=True, detail=""))
    #: 让暂存失败 —— 用来看路径怎么处置"合并没成"。
    stage_error: MergeError | None = None
    #: 让提交失败。
    commit_error: MergeError | None = None
    #: 让撤销失败 —— 那是"main 停在合并进行中"，必须被报出来。
    abort_error: MergeError | None = None
    #: 与验证运行器共用的事件流水。
    events: list[str] = field(default_factory=list)

    def head_commit(self, workspace: Path) -> str:
        self.events.append(f"head_commit:{workspace}")
        return self.heads.get(workspace, self.head)

    def stage_merge(self, *, main_workspace: Path, branch: str) -> StagedMerge:
        self.events.append(f"stage_merge:{branch}")
        if self.stage_error is not None:
            raise self.stage_error
        return self.staged

    def commit_staged_merge(self, *, main_workspace: Path, message: str) -> str:
        self.events.append("commit_staged_merge")
        if self.commit_error is not None:
            raise self.commit_error
        commit = "b" * 40
        self.heads[main_workspace] = commit
        return commit

    def abort_staged_merge(self, *, main_workspace: Path) -> None:
        self.events.append("abort_staged_merge")
        if self.abort_error is not None:
            raise self.abort_error


__all__ = [
    "EVENTS",
    "FakeCheckRunner",
    "RecordingMerger",
]
