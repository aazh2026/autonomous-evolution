"""多任务集成引擎的测试替身。

**为什么 git 口要假的。** 这一层最要紧的规矩有三条：先验后合、冲突要撤销、
失败要清场。用真 git 测它们，每条断言都要先搭一个仓库、造一次冲突、再去看
main 到底动没动 —— 慢，更要命的是失败时的报错是 git 的英文，而不是"引擎为
什么撤销了合并"。

**但真 git 的行为另有一组测试守着**（``tests/integration/test_multi_task_integration.py``）：
那里真的建仓库、真的撞冲突、真的看 main 有没有前进。假的那一套守"次序与处
置"，真的那套守"git 是不是真的这么说的"。

**事件流水记的是"在哪一步、做了哪件事"。** 每一件都带工作区 —— 因为这一层
的规矩全在次序上，而在哪个工作区上做的，正是次序的一部分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from harness.integration.pipeline import MergeError, StagedMerge


@dataclass
class FakeIntegrationGit:
    """记录调用序列的集成 git 口。

    **它记的是"做了哪几步"，不是"最后是什么状态"。** 这一层的规矩全在次序
    上：本地验证在合并之前、联合回归在提交之前、失败要清场。记状态的话，这
    些都验不到。

    **它同时是一份"仓库内容表"。** 分支改了哪些文件、某个版本上某个文件是
    什么内容，都在这里登记 —— 符号冲突的判定就靠这张表。
    """

    #: 工作区 → HEAD 提交号。
    heads: dict[Path, str] = field(default_factory=dict)
    #: 分支 → HEAD 提交号。
    branch_heads: dict[str, str] = field(default_factory=dict)
    #: 工作区 → 检出的分支。
    checked_out: dict[Path, str] = field(default_factory=dict)
    #: 分支 → 该分支改过的文件。
    changed: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: (版本, 路径) → 文件内容。
    files: dict[tuple[str, str], str] = field(default_factory=dict)
    #: (祖先, 后代) → 共同祖先。
    merge_bases: dict[tuple[str, str], str] = field(default_factory=dict)
    #: 求不到共同祖先时给这个。
    default_merge_base: str = "base0000"
    #: 让暂存失败 —— 撞冲突就抛这个。
    stage_error: MergeError | None = None
    #: 让提交失败。
    commit_error: MergeError | None = None
    #: 与检查运行器共用的事件流水。
    events: list[str] = field(default_factory=list)
    #: 提交号计数器。
    commits: int = 0

    def head_commit(self, workspace: Path) -> str:
        self.events.append(f"head_commit:{workspace}")
        return self.heads.get(workspace, "main0000")

    def merge_base(self, *, workspace: Path, ancestor: str, descendant: str) -> str:
        self.events.append(f"merge_base:{workspace}:{ancestor}..{descendant}")
        return self.merge_bases.get((ancestor, descendant), self.default_merge_base)

    def changed_files(self, *, workspace: Path, base: str, branch: str) -> tuple[str, ...]:
        self.events.append(f"changed_files:{workspace}:{base}..{branch}")
        return self.changed.get(branch, ())

    def show_file(self, *, workspace: Path, ref: str, path: str) -> str | None:
        self.events.append(f"show_file:{workspace}:{ref}:{path}")
        return self.files.get((ref, path))

    def open_branch(self, *, workspace: Path, branch: str, start_point: str) -> str:
        self.events.append(f"open_branch:{workspace}:{branch}@{start_point}")
        self.heads[workspace] = start_point
        self.branch_heads[branch] = start_point
        self.checked_out[workspace] = branch
        return start_point

    def reset_branch(self, *, workspace: Path, branch: str, target: str) -> None:
        self.events.append(f"reset_branch:{workspace}:{branch}@{target}")
        self.heads[workspace] = target
        self.branch_heads[branch] = target

    def stage_merge(self, *, main_workspace: Path, branch: str) -> StagedMerge:
        self.events.append(f"stage_merge:{main_workspace}:{branch}")
        if self.stage_error is not None:
            raise self.stage_error
        return StagedMerge(changed=True, detail="")

    def commit_staged_merge(self, *, main_workspace: Path, message: str) -> str:
        self.events.append(f"commit_staged_merge:{main_workspace}:{message}")
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1
        commit = f"c{self.commits:039d}"
        self.heads[main_workspace] = commit
        branch = self.checked_out.get(main_workspace)
        if branch is not None:
            self.branch_heads[branch] = commit
        return commit

    def abort_staged_merge(self, *, main_workspace: Path) -> None:
        self.events.append(f"abort_staged_merge:{main_workspace}")
