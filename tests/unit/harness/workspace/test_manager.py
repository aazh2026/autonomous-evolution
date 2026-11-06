"""TASK-P1-032 —— 工作空间管理器。

覆盖任务书的 T1 ①（两个任务并行创建 worktree，目录互不干扰）与
"重试同一任务时产生独立分支或复用分支的行为符合约定（不产生分支冲突）"。

**这一组测试要证明的不是"代码跑通了"，而是"冲突不可能发生"。** 所以除了
正向断言，还刻意构造了三种冲突场景（分支被别人检出、目录不是 git 认得的
worktree、目录在但分支不对），断言它们**都被拒绝**而不是被"处理掉"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.workspace.git import GitError
from harness.workspace.manager import WorkspaceManager
from harness.workspace.models import WorkspaceBusyError, WorkspaceError, WorkspaceNotFoundError
from tests.unit.harness.workspace.doubles import FakeClock, FakeGitClient

LEASE_TTL = 3600


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    (path / ".git").mkdir(parents=True)
    return path


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    path = tmp_path / "workspaces"
    path.mkdir()
    return path


@pytest.fixture
def git() -> FakeGitClient:
    return FakeGitClient()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def manager(
    repo: Path, workspace_root: Path, git: FakeGitClient, clock: FakeClock
) -> WorkspaceManager:
    return WorkspaceManager(
        repo_path=repo,
        workspace_root=workspace_root,
        git=git,
        clock=clock,
        lease_ttl_seconds=LEASE_TTL,
    )


class TestTheWorkspaceIsAllocated:
    def test_acquiring_creates_the_worktree(self, manager: WorkspaceManager) -> None:
        workspace = manager.acquire("task-1", holder="executor-a")
        assert workspace.path.exists()
        assert workspace.branch == "agent/task-1"
        assert workspace.workspace_id == "task-1"

    def test_the_branch_follows_the_convention(self, manager: WorkspaceManager) -> None:
        manager.acquire("task-1", holder="executor-a")
        assert manager.branch_of("task-1") == "agent/task-1"

    def test_the_lease_is_held_in_memory(self, manager: WorkspaceManager) -> None:
        manager.acquire("task-1", holder="executor-a")
        lease = manager.lease_of("task-1")
        assert lease is not None
        assert lease.holder == "executor-a"

    def test_the_lease_is_written_to_disk(self, manager: WorkspaceManager) -> None:
        """磁盘上那份是给 GC 用的 —— 进程重启后内存那份没了，它还在。"""
        manager.acquire("task-1", holder="executor-a")
        assert manager.lease_store.path_for("task-1").exists()

    def test_the_lease_file_lives_outside_the_workspace(self, manager: WorkspaceManager) -> None:
        """租约记录不能落在 worktree 里 —— Agent 可能把它一起提交进去。"""
        manager.acquire("task-1", holder="executor-a")
        lease_file = manager.lease_store.path_for("task-1")
        assert manager.path_of("task-1") not in lease_file.parents

    def test_the_lease_expires_after_the_ttl(
        self, manager: WorkspaceManager, clock: FakeClock
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        lease = manager.lease_of("task-1")
        assert lease is not None
        assert not lease.is_expired(clock.now())
        clock.advance(LEASE_TTL + 1)
        assert lease.is_expired(clock.now())

    def test_acquiring_twice_by_the_same_holder_reuses_the_workspace(
        self, manager: WorkspaceManager, git: FakeGitClient
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        manager.acquire("task-1", holder="executor-a")
        assert len(git.added) == 1

    def test_acquiring_twice_keeps_the_original_creation_time(
        self, manager: WorkspaceManager, clock: FakeClock
    ) -> None:
        first = manager.acquire("task-1", holder="executor-a")
        clock.advance(60)
        second = manager.acquire("task-1", holder="executor-a")
        assert second.created_at == first.created_at


class TestTwoTasksDoNotInterfere:
    """T1 ① —— 两个任务并行创建 worktree，目录互不干扰。"""

    def test_they_get_different_directories(self, manager: WorkspaceManager) -> None:
        first = manager.acquire("task-1", holder="executor-a")
        second = manager.acquire("task-2", holder="executor-b")
        assert first.path != second.path

    def test_they_get_different_branches(self, manager: WorkspaceManager) -> None:
        first = manager.acquire("task-1", holder="executor-a")
        second = manager.acquire("task-2", holder="executor-b")
        assert first.branch != second.branch

    def test_they_get_different_leases(self, manager: WorkspaceManager) -> None:
        manager.acquire("task-1", holder="executor-a")
        manager.acquire("task-2", holder="executor-b")
        assert len(manager.leases()) == 2

    def test_a_file_written_in_one_is_invisible_in_the_other(
        self, manager: WorkspaceManager
    ) -> None:
        first = manager.acquire("task-1", holder="executor-a")
        second = manager.acquire("task-2", holder="executor-b")
        (first.path / "note.txt").write_text("任务一", encoding="utf-8")
        assert not (second.path / "note.txt").exists()

    def test_the_second_task_does_not_disturb_the_first(self, manager: WorkspaceManager) -> None:
        first = manager.acquire("task-1", holder="executor-a")
        manager.acquire("task-2", holder="executor-b")
        assert first.path.exists()
        assert manager.lease_of("task-1") is not None


class TestTheLeaseIsExclusive:
    """两个执行者共用一个工作空间，产出会混在一起 —— 所以直接拒绝。"""

    def test_another_holder_is_refused(self, manager: WorkspaceManager) -> None:
        manager.acquire("task-1", holder="executor-a")
        with pytest.raises(WorkspaceBusyError):
            manager.acquire("task-1", holder="executor-b")

    def test_the_error_carries_the_current_holder(self, manager: WorkspaceManager) -> None:
        manager.acquire("task-1", holder="executor-a")
        with pytest.raises(WorkspaceBusyError) as caught:
            manager.acquire("task-1", holder="executor-b")
        assert caught.value.holder == "executor-a"

    def test_an_expired_lease_can_be_taken_over(
        self, manager: WorkspaceManager, clock: FakeClock
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL + 1)
        workspace = manager.acquire("task-1", holder="executor-b")
        assert workspace.path.exists()

    def test_renewing_extends_the_lease(self, manager: WorkspaceManager, clock: FakeClock) -> None:
        manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL - 10)
        renewed = manager.renew("task-1", holder="executor-a")
        assert not renewed.is_expired(clock.now())

    def test_renewing_by_another_holder_is_refused(self, manager: WorkspaceManager) -> None:
        manager.acquire("task-1", holder="executor-a")
        with pytest.raises(WorkspaceBusyError):
            manager.renew("task-1", holder="executor-b")

    def test_renewing_without_a_lease_is_not_found(self, manager: WorkspaceManager) -> None:
        with pytest.raises(WorkspaceNotFoundError):
            manager.renew("task-unknown", holder="executor-a")

    def test_the_renewed_lease_is_written_to_disk(self, manager: WorkspaceManager) -> None:
        manager.acquire("task-1", holder="executor-a")
        renewed = manager.renew("task-1", holder="executor-a")
        stored = manager.lease_store.load("task-1")
        assert stored is not None
        assert stored.expires_at == renewed.expires_at


class TestTheResetIsTheRetryStep:
    """重试 = 丢掉未提交的残留，**留着已提交的产出**。"""

    def test_reset_discards_the_uncommitted_changes(
        self, manager: WorkspaceManager, git: FakeGitClient
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        manager.reset("task-1")
        assert manager.path_of("task-1") in git.resets
        assert manager.path_of("task-1") in git.cleans

    def test_resetting_a_missing_workspace_is_not_found(self, manager: WorkspaceManager) -> None:
        with pytest.raises(WorkspaceNotFoundError):
            manager.reset("task-unknown")

    def test_resetting_does_not_drop_the_lease(self, manager: WorkspaceManager) -> None:
        manager.acquire("task-1", holder="executor-a")
        manager.reset("task-1")
        assert manager.lease_of("task-1") is not None


class TestTheRelease:
    def test_release_removes_the_directory(self, manager: WorkspaceManager) -> None:
        workspace = manager.acquire("task-1", holder="executor-a")
        manager.release("task-1")
        assert not workspace.path.exists()

    def test_release_removes_the_worktree_registration(
        self, manager: WorkspaceManager, git: FakeGitClient
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        manager.release("task-1")
        assert manager.path_of("task-1") not in {
            entry.path for entry in git.list_worktrees(manager.repo_path)
        }

    def test_release_keeps_the_branch(self, manager: WorkspaceManager, git: FakeGitClient) -> None:
        """分支是任务的产出 —— 删了就没了。"""
        manager.acquire("task-1", holder="executor-a")
        manager.release("task-1")
        assert git.branch_exists(manager.repo_path, "agent/task-1")

    def test_release_drops_the_lease(self, manager: WorkspaceManager) -> None:
        manager.acquire("task-1", holder="executor-a")
        manager.release("task-1")
        assert manager.lease_of("task-1") is None
        assert not manager.lease_store.path_for("task-1").exists()

    def test_releasing_a_missing_workspace_is_silent(self, manager: WorkspaceManager) -> None:
        manager.release("task-unknown")


class TestTheConflictsAreRefused:
    """分支冲突不是"处理掉"的，是**不可能发生**的。

    这里的三个场景都是"看起来能凑合"的：目录在、分支名对、git 认得。
    但每一个背后都有一个说不清归属的情况，所以都拒绝。
    """

    def test_a_branch_checked_out_elsewhere_is_refused(
        self, manager: WorkspaceManager, git: FakeGitClient, tmp_path: Path
    ) -> None:
        """git 自己就会拒绝 —— 这就是"冲突结构上不可能发生"的实现。

        一条分支不能同时被两个 worktree 检出，这是 git 的硬约束。管理器
        不需要（也无法）绕过它。
        """
        git.checkout_elsewhere(tmp_path / "other", "agent/task-1")
        with pytest.raises(GitError):
            manager.acquire("task-1", holder="executor-a")
        assert not manager.exists("task-1")

    def test_a_failed_acquire_leaves_no_lease(
        self, manager: WorkspaceManager, git: FakeGitClient, tmp_path: Path
    ) -> None:
        git.checkout_elsewhere(tmp_path / "other", "agent/task-1")
        with pytest.raises(GitError):
            manager.acquire("task-1", holder="executor-a")
        assert manager.lease_of("task-1") is None
        assert not manager.lease_store.path_for("task-1").exists()

    def test_a_duplicate_branch_registration_is_refused(
        self, manager: WorkspaceManager, git: FakeGitClient
    ) -> None:
        """同一条分支出现在两个 worktree 上 —— 真 git 不会允许这种状态。

        但如果它真的出现了（脏数据、被手改过的登记），管理器必须**拒绝**，
        而不是"挑一个继续用"。挑一个 = 有一个执行者的产出写到了别人的
        工作空间里，而谁也说不清是哪个。
        """
        manager.acquire("task-1", holder="executor-a")
        git.checkout_elsewhere(manager.path_of("task-1").parent / "task-1-copy", "agent/task-1")
        with pytest.raises(WorkspaceError, match="同一条分支"):
            manager.acquire("task-1", holder="executor-a")

    def test_a_crash_leftover_is_refused(
        self, manager: WorkspaceManager, git: FakeGitClient
    ) -> None:
        """目录在、git 不认得 —— 崩在半路的工作空间。"""
        manager.path_of("task-1").mkdir(parents=True)
        git.forget(manager.path_of("task-1"))
        with pytest.raises(WorkspaceError, match="GC"):
            manager.acquire("task-1", holder="executor-a")

    def test_a_directory_on_the_wrong_branch_is_refused(
        self, manager: WorkspaceManager, git: FakeGitClient
    ) -> None:
        git.checkout_elsewhere(manager.path_of("task-1"), "agent/somebody-else")
        with pytest.raises(WorkspaceError):
            manager.acquire("task-1", holder="executor-a")


class TestTheConstructionIsChecked:
    def test_the_lease_ttl_must_be_positive(
        self, repo: Path, workspace_root: Path, git: FakeGitClient, clock: FakeClock
    ) -> None:
        with pytest.raises(ValueError, match="租约时长"):
            WorkspaceManager(
                repo_path=repo,
                workspace_root=workspace_root,
                git=git,
                clock=clock,
                lease_ttl_seconds=0,
            )

    def test_the_repo_must_exist(self, tmp_path: Path, git: FakeGitClient) -> None:
        with pytest.raises(WorkspaceError, match="不存在"):
            WorkspaceManager(
                repo_path=tmp_path / "nope",
                workspace_root=tmp_path,
                git=git,
                clock=FakeClock(),
                lease_ttl_seconds=LEASE_TTL,
            )

    def test_the_repo_must_be_a_git_repository(self, tmp_path: Path, git: FakeGitClient) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(WorkspaceError, match="不是一个 git 仓库"):
            WorkspaceManager(
                repo_path=plain,
                workspace_root=tmp_path,
                git=git,
                clock=FakeClock(),
                lease_ttl_seconds=LEASE_TTL,
            )

    def test_the_workspace_root_must_not_be_inside_the_repo(
        self, repo: Path, git: FakeGitClient
    ) -> None:
        """仓库里的 worktree 会让主仓库的 ``git status`` 永远不干净。"""
        with pytest.raises(WorkspaceError, match="之内"):
            WorkspaceManager(
                repo_path=repo,
                workspace_root=repo / "workspaces",
                git=git,
                clock=FakeClock(),
                lease_ttl_seconds=LEASE_TTL,
            )


class TestAFailedWorktreeLeavesNoLease:
    """建工作空间失败 —— 租约不能留在那儿，否则它会占着一个不存在的名字。"""

    def test_the_lease_is_rolled_back(self, manager: WorkspaceManager, git: FakeGitClient) -> None:
        git.fail_next_add = True
        with pytest.raises(GitError, match="模拟的失败"):
            manager.acquire("task-1", holder="executor-a")
        assert manager.lease_of("task-1") is None
        assert not manager.lease_store.path_for("task-1").exists()
