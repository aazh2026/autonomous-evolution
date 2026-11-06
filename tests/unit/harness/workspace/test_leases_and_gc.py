"""TASK-P1-032 —— 租约与 GC。

覆盖任务书的 T1 ③（租约过期后 workspace 被 GC 回收）与 ④（崩溃遗留的
workspace 可被 GC 识别并清理）。

**GC 的判据只有两条，而且都必须来自磁盘。** GC 常常是在系统重启之后才跑的
—— 那时内存里那份租约早没了。所以这两条判据（租约过期 / 没有租约）都只
看磁盘上的记录，测试里也就必须真的落盘，而不是只在内存里造一个租约对象。

**"读不出来"与"没有记录"是同一件事。** 租约记录是"这个工作空间有主人"的
唯一证据；证据读不出来，等于没有证据。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness.workspace.gc import WorkspaceGC
from harness.workspace.leases import LeaseStore
from harness.workspace.manager import WorkspaceManager
from harness.workspace.models import ReclaimReason, WorkspaceLease
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
def store(workspace_root: Path) -> LeaseStore:
    return LeaseStore(workspace_root)


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


@pytest.fixture
def gc(repo: Path, workspace_root: Path, git: FakeGitClient, clock: FakeClock) -> WorkspaceGC:
    return WorkspaceGC(
        repo_path=repo,
        workspace_root=workspace_root,
        git=git,
        clock=clock,
    )


def _lease(workspace_id: str, path: Path, *, expires_at: datetime) -> WorkspaceLease:
    return WorkspaceLease(
        workspace_id=workspace_id,
        task_id=workspace_id,
        branch=f"agent/{workspace_id}",
        path=path,
        repo_path=Path("/repo"),
        holder="executor-a",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=expires_at,
    )


class TestTheLeaseIsStoredOnDisk:
    def test_a_lease_round_trips(self, store: LeaseStore, tmp_path: Path) -> None:
        expires = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        store.save(_lease("task-1", tmp_path / "task-1", expires_at=expires))
        loaded = store.load("task-1")
        assert loaded is not None
        assert loaded.workspace_id == "task-1"
        assert loaded.holder == "executor-a"
        assert loaded.expires_at == expires

    def test_a_missing_lease_is_none(self, store: LeaseStore) -> None:
        assert store.load("task-unknown") is None

    def test_a_corrupted_lease_is_treated_as_absent(self, store: LeaseStore) -> None:
        """证据读不出来 = 没有证据。"""
        store.root.mkdir(parents=True, exist_ok=True)
        store.path_for("task-1").write_text("{ 这不是 JSON", encoding="utf-8")
        assert store.load("task-1") is None

    def test_a_lease_with_missing_fields_is_treated_as_absent(self, store: LeaseStore) -> None:
        store.root.mkdir(parents=True, exist_ok=True)
        store.path_for("task-1").write_text('{"workspace_id": "task-1"}', encoding="utf-8")
        assert store.load("task-1") is None

    def test_all_lists_every_lease(self, store: LeaseStore, tmp_path: Path) -> None:
        expires = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        store.save(_lease("task-1", tmp_path / "task-1", expires_at=expires))
        store.save(_lease("task-2", tmp_path / "task-2", expires_at=expires))
        assert {lease.workspace_id for lease in store.all()} == {"task-1", "task-2"}

    def test_removing_reports_whether_something_was_removed(
        self, store: LeaseStore, tmp_path: Path
    ) -> None:
        expires = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        store.save(_lease("task-1", tmp_path / "task-1", expires_at=expires))
        assert store.remove("task-1") is True
        assert store.remove("task-1") is False


class TestTheExpiredLeaseIsReclaimed:
    """T1 ③ —— 租约过期后 workspace 被 GC 回收。"""

    def test_an_expired_lease_becomes_a_candidate(
        self, manager: WorkspaceManager, gc: WorkspaceGC, clock: FakeClock
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL + 1)
        candidates = gc.candidates()
        assert [c.workspace_id for c in candidates] == ["task-1"]
        assert candidates[0].reason is ReclaimReason.LEASE_EXPIRED

    def test_a_live_lease_is_not_a_candidate(
        self, manager: WorkspaceManager, gc: WorkspaceGC
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        assert gc.candidates() == ()

    def test_collecting_removes_the_directory(
        self, manager: WorkspaceManager, gc: WorkspaceGC, clock: FakeClock
    ) -> None:
        workspace = manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL + 1)
        reclaimed = gc.collect()
        assert [item.workspace_id for item in reclaimed] == ["task-1"]
        assert not workspace.path.exists()

    def test_collecting_removes_the_worktree_registration(
        self, manager: WorkspaceManager, gc: WorkspaceGC, git: FakeGitClient, clock: FakeClock
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL + 1)
        gc.collect()
        assert git.list_worktrees(manager.repo_path) == ()

    def test_collecting_keeps_the_branch(
        self, manager: WorkspaceManager, gc: WorkspaceGC, git: FakeGitClient, clock: FakeClock
    ) -> None:
        """回收的是"临时地盘"，不是"产出"。"""
        manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL + 1)
        gc.collect()
        assert git.branch_exists(manager.repo_path, "agent/task-1")

    def test_collecting_removes_the_lease_record(
        self, manager: WorkspaceManager, gc: WorkspaceGC, clock: FakeClock
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL + 1)
        gc.collect()
        assert not manager.lease_store.path_for("task-1").exists()

    def test_the_reclaim_reason_is_recorded(
        self, manager: WorkspaceManager, gc: WorkspaceGC, clock: FakeClock
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL + 1)
        reclaimed = gc.collect()
        assert reclaimed[0].reason is ReclaimReason.LEASE_EXPIRED


class TestTheCrashLeftoverIsReclaimed:
    """T1 ④ —— 崩溃遗留的 workspace 可被 GC 识别并清理。"""

    def test_a_directory_without_a_lease_is_orphaned(
        self, gc: WorkspaceGC, manager: WorkspaceManager
    ) -> None:
        manager.path_of("task-1").mkdir(parents=True)
        candidates = gc.candidates()
        assert [c.workspace_id for c in candidates] == ["task-1"]
        assert candidates[0].reason is ReclaimReason.ORPHANED

    def test_an_orphaned_workspace_is_cleaned(
        self, gc: WorkspaceGC, manager: WorkspaceManager
    ) -> None:
        leftover = manager.path_of("task-1")
        leftover.mkdir(parents=True)
        (leftover / "半路写的文件.txt").write_text("崩了", encoding="utf-8")
        reclaimed = gc.collect()
        assert [item.workspace_id for item in reclaimed] == ["task-1"]
        assert not leftover.exists()

    def test_a_corrupted_lease_also_counts_as_orphaned(
        self, gc: WorkspaceGC, manager: WorkspaceManager
    ) -> None:
        """证据读不出来 = 没有主人 = 孤儿。"""
        manager.path_of("task-1").mkdir(parents=True)
        manager.lease_store.root.mkdir(parents=True, exist_ok=True)
        manager.lease_store.path_for("task-1").write_text("坏了", encoding="utf-8")
        candidates = gc.candidates()
        assert candidates[0].reason is ReclaimReason.ORPHANED

    def test_the_metadata_directory_is_never_reclaimed(
        self, gc: WorkspaceGC, manager: WorkspaceManager
    ) -> None:
        """``.ases`` 是租约记录的存放处 —— 把它回收掉等于删掉所有租约。"""
        manager.lease_store.root.mkdir(parents=True, exist_ok=True)
        assert gc.candidates() == ()


class TestTheStaleRecordsArePruned:
    """租约先落盘、worktree 后建 —— 崩在中间会留下一份指向空处的记录。"""

    def test_a_record_without_a_directory_is_pruned(
        self, store: LeaseStore, gc: WorkspaceGC, tmp_path: Path
    ) -> None:
        expires = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        store.save(_lease("task-1", tmp_path / "never-created", expires_at=expires))
        assert gc.prune_stale_records() == ("task-1",)
        assert not store.path_for("task-1").exists()

    def test_a_live_record_is_kept(
        self, store: LeaseStore, gc: WorkspaceGC, tmp_path: Path
    ) -> None:
        expires = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        store.save(_lease("task-1", tmp_path / "task-1", expires_at=expires))
        (tmp_path / "task-1").mkdir()
        assert gc.prune_stale_records() == ()
        assert store.path_for("task-1").exists()

    def test_run_does_both(self, store: LeaseStore, gc: WorkspaceGC, tmp_path: Path) -> None:
        expires = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        store.save(_lease("task-1", tmp_path / "never-created", expires_at=expires))
        store.save(_lease("task-2", tmp_path / "task-2", expires_at=expires))
        (tmp_path / "task-2").mkdir()
        assert gc.run() == ()
        assert store.path_for("task-1").exists() is False
        assert store.path_for("task-2").exists() is True


class TestTheGCWorksOnAFreshProcess:
    """GC 常常在系统重启之后才跑 —— 那时内存里那份租约早没了。"""

    def test_a_fresh_gc_still_sees_the_expired_lease(
        self,
        manager: WorkspaceManager,
        repo: Path,
        workspace_root: Path,
        git: FakeGitClient,
        clock: FakeClock,
    ) -> None:
        manager.acquire("task-1", holder="executor-a")
        clock.advance(LEASE_TTL + 1)
        fresh = WorkspaceGC(
            repo_path=repo,
            workspace_root=workspace_root,
            git=git,
            clock=clock,
        )
        assert [c.workspace_id for c in fresh.candidates()] == ["task-1"]


class TestTheLeaseGuards:
    def test_the_ttl_must_be_positive(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        lease = _lease("task-1", Path("/tmp/task-1"), expires_at=now)
        with pytest.raises(ValueError, match="租约时长"):
            lease.renew(now, 0)

    def test_renewing_returns_a_new_lease(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        lease = _lease("task-1", Path("/tmp/task-1"), expires_at=now)
        renewed = lease.renew(now, 60)
        assert renewed is not lease
        assert renewed.expires_at == now + timedelta(seconds=60)
        assert lease.expires_at == now

    def test_remaining_is_negative_once_expired(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        lease = _lease("task-1", Path("/tmp/task-1"), expires_at=now)
        assert lease.remaining(now + timedelta(seconds=5)).total_seconds() == -5
