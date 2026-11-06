"""TASK-P1-032 —— 真 git 的 worktree 生命周期。

**单元测试用假 git，是因为"管理器算错了"在假 git 上表现为一条清楚的异常；
在真 git 上它表现为一段看不懂的 stderr。** 但真 git 才是"分支冲突不可能
发生"这句话的最终依据 —— 所以这一组用真的 ``git`` 命令跑一遍：

- 两个任务并行创建 worktree，目录互不干扰（T1 ①）；
- 重试同一个任务复用分支与目录，**不产生分支冲突**；
- 租约过期后 workspace 被 GC 回收（T1 ③）；
- 崩溃遗留的 workspace 可被 GC 识别并清理（T1 ④）。

需要真的装了 git。不需要 Docker 或数据库，所以不依赖那些夹具。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.workspace.gc import WorkspaceGC
from harness.workspace.git import GitError, SubprocessGitClient
from harness.workspace.manager import WorkspaceManager
from harness.workspace.models import ReclaimReason
from tests.unit.harness.workspace.doubles import FakeClock

LEASE_TTL = 3600


def _git(cwd: Path, *args: str) -> str:
    """跑一条 git 命令，返回 stdout。"""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：\n{result.stdout}\n{result.stderr}")
    return result.stdout


def _branches(repo: Path) -> list[str]:
    """仓库里的分支名。

    用 ``--format=%(refname:short)`` 而不是直接 ``split()``：``git branch
    --list`` 会在**别的 worktree 里检出的**分支前面加一个 ``+``，直接
    split 会把那个 ``+`` 当成一条分支名。
    """
    raw = _git(repo, "branch", "--list", "--format=%(refname:short)")
    return [line.strip() for line in raw.splitlines() if line.strip()]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个真 git 仓库，有一个初始提交。

    初始提交是必需的：``git worktree add`` 要从 HEAD 检出，没有 HEAD 的
    仓库建不出 worktree。
    """
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "ases@example.invalid")
    _git(path, "config", "user.name", "ASES")
    (path / "README.md").write_text("基础\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "初始提交")
    return path


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    path = tmp_path / "workspaces"
    path.mkdir()
    return path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def git_client() -> SubprocessGitClient:
    return SubprocessGitClient()


@pytest.fixture
def manager(
    repo: Path,
    workspace_root: Path,
    git_client: SubprocessGitClient,
    clock: FakeClock,
) -> WorkspaceManager:
    return WorkspaceManager(
        repo_path=repo,
        workspace_root=workspace_root,
        git=git_client,
        clock=clock,
        lease_ttl_seconds=LEASE_TTL,
    )


@pytest.fixture
def gc(
    repo: Path,
    workspace_root: Path,
    git_client: SubprocessGitClient,
    clock: FakeClock,
) -> WorkspaceGC:
    return WorkspaceGC(
        repo_path=repo,
        workspace_root=workspace_root,
        git=git_client,
        clock=clock,
    )


class TestTwoTasksInParallel:
    """T1 ① —— 两个任务并行创建 worktree，目录互不干扰。"""

    def test_both_worktrees_exist(self, manager: WorkspaceManager) -> None:
        first = manager.acquire("task-1", holder="executor-a")
        second = manager.acquire("task-2", holder="executor-b")
        assert first.path.exists()
        assert second.path.exists()
        assert first.path != second.path

    def test_git_knows_both_worktrees(self, manager: WorkspaceManager, repo: Path) -> None:
        manager.acquire("task-1", holder="executor-a")
        manager.acquire("task-2", holder="executor-b")
        listed = _git(repo, "worktree", "list", "--porcelain")
        assert listed.count("worktree ") == 3  # 主仓库 + 两个任务

    def test_each_worktree_is_on_its_own_branch(
        self, manager: WorkspaceManager, repo: Path
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        manager.acquire("task-2", holder="executor-b")
        branches = _branches(repo)
        assert "agent/task-1" in branches
        assert "agent/task-2" in branches

    def test_a_file_written_in_one_is_invisible_in_the_other(
        self, manager: WorkspaceManager
    ) -> None:
        first = manager.acquire("task-1", holder="executor-a")
        second = manager.acquire("task-2", holder="executor-b")
        (first.path / "note.txt").write_text("任务一", encoding="utf-8")
        assert not (second.path / "note.txt").exists()

    def test_the_worktree_list_is_parsed(self, manager: WorkspaceManager, repo: Path) -> None:
        """``git worktree list --porcelain`` 的解析不能错分支名。"""
        manager.acquire("task-1", holder="executor-a")
        entries = SubprocessGitClient().list_worktrees(repo)
        branches = {entry.branch for entry in entries}
        assert "agent/task-1" in branches
        assert "main" in branches


class TestTheRetryDoesNotConflict:
    """重试同一任务 —— 复用分支与目录，不产生分支冲突。"""

    def test_the_retry_reuses_the_same_branch(self, manager: WorkspaceManager) -> None:
        first = manager.acquire("task-1", holder="executor-a")
        second = manager.acquire("task-1", holder="executor-a")
        assert first.branch == second.branch
        assert first.path == second.path

    def test_the_retry_does_not_add_a_second_branch(
        self, manager: WorkspaceManager, repo: Path
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        manager.acquire("task-1", holder="executor-a")
        assert [b for b in _branches(repo) if b.startswith("agent/")] == ["agent/task-1"]

    def test_the_retry_does_not_add_a_second_worktree(
        self, manager: WorkspaceManager, repo: Path
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        manager.acquire("task-1", holder="executor-a")
        assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2

    def test_another_task_cannot_steal_the_branch(
        self, manager: WorkspaceManager, tmp_path: Path, repo: Path
    ) -> None:
        """同一条分支不能同时被两个 worktree 检出 —— git 自己会拒绝。"""
        manager.acquire("task-1", holder="executor-a")
        with pytest.raises(GitError):
            SubprocessGitClient().add_worktree(repo, tmp_path / "steal", "agent/task-1")

    def test_the_reacquire_after_release_reuses_the_branch(
        self, manager: WorkspaceManager, repo: Path
    ) -> None:
        """释放之后再拿 —— 分支还在，必须**复用**而不是新建。

        "目录没了、分支还在"是常态（释放刻意留着分支，因为那是产出）。
        这时再建 worktree 必须复用那条分支：新建会撞上 "branch already
        exists"，而且会让产出分家到两条分支上。
        """
        manager.acquire("task-1", holder="executor-a")
        manager.release("task-1")
        manager.acquire("task-1", holder="executor-a")
        assert [b for b in _branches(repo) if b.startswith("agent/")] == ["agent/task-1"]


class TestTheResetIsTheRetryStep:
    def test_the_uncommitted_changes_are_discarded(self, manager: WorkspaceManager) -> None:
        workspace = manager.acquire("task-1", holder="executor-a")
        (workspace.path / "scratch.txt").write_text("没提交的", encoding="utf-8")
        manager.reset("task-1")
        assert not (workspace.path / "scratch.txt").exists()

    def test_the_committed_work_is_kept(self, manager: WorkspaceManager) -> None:
        """已提交的产出留着 —— 那是上一次真正交付过的东西。"""
        workspace = manager.acquire("task-1", holder="executor-a")
        (workspace.path / "delivered.txt").write_text("交付了", encoding="utf-8")
        _git(workspace.path, "add", ".")
        _git(workspace.path, "commit", "-m", "交付")
        manager.reset("task-1")
        assert (workspace.path / "delivered.txt").exists()

    def test_the_untracked_files_are_removed(self, manager: WorkspaceManager) -> None:
        workspace = manager.acquire("task-1", holder="executor-a")
        (workspace.path / "untracked.txt").write_text("残留", encoding="utf-8")
        manager.reset("task-1")
        assert not (workspace.path / "untracked.txt").exists()


class TestTheReleaseKeepsTheBranch:
    def test_the_directory_is_removed(self, manager: WorkspaceManager) -> None:
        workspace = manager.acquire("task-1", holder="executor-a")
        manager.release("task-1")
        assert not workspace.path.exists()

    def test_the_branch_survives(self, manager: WorkspaceManager, repo: Path) -> None:
        manager.acquire("task-1", holder="executor-a")
        manager.release("task-1")
        assert "agent/task-1" in _branches(repo)

    def test_git_forgets_the_worktree(self, manager: WorkspaceManager, repo: Path) -> None:
        manager.acquire("task-1", holder="executor-a")
        manager.release("task-1")
        assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


class TestTheExpiredLeaseIsReclaimed:
    """T1 ③ —— 租约过期后 workspace 被 GC 回收。"""

    def test_the_workspace_is_reclaimed(
        self, manager: WorkspaceManager, gc: WorkspaceGC, clock: FakeClock
    ) -> None:
        workspace = manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL + 1)
        reclaimed = gc.collect()
        assert [item.workspace_id for item in reclaimed] == ["task-1"]
        assert not workspace.path.exists()

    def test_the_reason_is_the_expired_lease(
        self, manager: WorkspaceManager, gc: WorkspaceGC, clock: FakeClock
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL + 1)
        assert gc.candidates()[0].reason is ReclaimReason.LEASE_EXPIRED

    def test_the_branch_survives_the_reclaim(
        self, manager: WorkspaceManager, gc: WorkspaceGC, repo: Path, clock: FakeClock
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL + 1)
        gc.collect()
        assert "agent/task-1" in _branches(repo)

    def test_git_forgets_the_reclaimed_worktree(
        self, manager: WorkspaceManager, gc: WorkspaceGC, repo: Path, clock: FakeClock
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL + 1)
        gc.collect()
        assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1

    def test_a_live_lease_is_not_reclaimed(
        self, manager: WorkspaceManager, gc: WorkspaceGC
    ) -> None:
        workspace = manager.acquire("task-1", holder="executor-a")
        assert gc.collect() == ()
        assert workspace.path.exists()


class TestTheCrashLeftoverIsReclaimed:
    """T1 ④ —— 崩溃遗留的 workspace 可被 GC 识别并清理。"""

    def test_a_directory_without_a_lease_is_orphaned(
        self, gc: WorkspaceGC, workspace_root: Path
    ) -> None:
        leftover = workspace_root / "task-crashed"
        leftover.mkdir()
        candidates = gc.candidates()
        assert [c.workspace_id for c in candidates] == ["task-crashed"]
        assert candidates[0].reason is ReclaimReason.ORPHANED

    def test_the_orphan_is_cleaned(self, gc: WorkspaceGC, workspace_root: Path) -> None:
        leftover = workspace_root / "task-crashed"
        leftover.mkdir()
        (leftover / "半路写的文件.txt").write_text("崩了", encoding="utf-8")
        gc.collect()
        assert not leftover.exists()

    def test_a_worktree_without_a_lease_is_also_orphaned(
        self, manager: WorkspaceManager, gc: WorkspaceGC
    ) -> None:
        """崩在工作空间建好之后、租约写完之前 —— 目录在、记录没了。"""
        workspace = manager.acquire("task-1", holder="executor-a")
        manager.lease_store.remove("task-1")
        candidates = gc.candidates()
        assert [c.workspace_id for c in candidates] == ["task-1"]
        assert candidates[0].reason is ReclaimReason.ORPHANED
        gc.collect()
        assert not workspace.path.exists()
