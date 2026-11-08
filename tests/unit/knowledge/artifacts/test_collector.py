"""TASK-P1-048 —— 从工作区里取三类证据。

**三类证据的来源完全不同，所以取法完全不同**：

- ``git_diff`` 是**算**出来的（跑一条 ``git diff``）；
- ``test_report`` 是**放着**的（测试框架写出来的文件）；
- ``execution_trace`` **不在工作区里**（它由验证引擎给出）。

这里最要紧的两条边界：

1. **"没产出"与"产出是空的"是两回事。** git 跑不起来 → ``None``（取不到）；
   跑起来了但输出为空 → **空字节**（"当时确实没有改动"本身也是证据）。
2. **测试报告的路径是配置，不是约定。** 各种框架写在哪都不一样，猜一个
   路径等于制造一个"证据在但找不到"的假故障。

**git 命令不开 shell。** 工作区路径来自外部输入，``shell=True`` 等于把执行权
交给一份外部数据 —— 所以这里断言的是"路径里有分号也不会被当成两条命令"。
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from core.evidence.artifacts import ArtifactKind
from knowledge.artifacts.archiver import EvidenceSource
from knowledge.artifacts.collector import WorkspaceCollector


def _git(workspace: Path, *arguments: str) -> None:
    """在仓库里跑一条 git 命令（建仓库用，不走被测代码）。"""
    subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """一个有一个提交、并且有一处未提交改动的仓库。"""
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "verifier@example.invalid")
    _git(repo, "config", "user.name", "verifier")
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-m", "基线")
    return repo


def _hung_git(tmp_path: Path) -> Path:
    """一条会卡住的假 git。

    **它必须真的派生出子进程**（``sleep 37 &`` + ``wait``），否则这条用例
    考不到"孤儿"这件事：``#!/bin/sh`` 里只有一条命令时，shell 会**把自己
    exec 成那条命令** —— 进程还是只有一个，杀掉它就什么都没剩，"只杀父
    进程"与"杀整组进程"的效果完全一样，用例是绿的，实现的洞还在。

    真 ``git diff`` 会派生子进程（外部 diff 驱动、凭证助手），所以这里得
    像真 git 那样有子进程可留。

    **孙进程不能攥着管道。** 它的输出与错误都引到 ``/dev/null`` 去 ——
    否则父进程一死、孙进程还活着，管道的写端就还攥在它手里，收尾的
    ``communicate()`` 会一直等 EOF 等到孙进程自然退出（三十七秒），等它
    退出时"孤儿"早就不在了 —— 用例是绿的，实现的洞还在。
    """
    script = tmp_path / "hung-git"
    script.write_text("#!/bin/sh\nsleep 37 >/dev/null 2>&1 &\nwait\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _survivors() -> list[str]:
    """还活着的 ``sleep 37``（**僵尸不算** —— 它已经被收掉了，只是还没被回收）。

    父进程先死的话，被收掉的那个进程会留下一小会儿的僵尸（它被 init 收养
    之后才会被回收）—— 那不是"还占着工作区"。
    """
    pids = subprocess.run(
        ["pgrep", "-f", "sleep 37"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    alive: list[str] = []
    for pid in pids:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", pid],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if state and not state.startswith("Z"):
            alive.append(f"{pid}（{state}）")
    return alive


async def _wait_for_survivors(timeout: float = 3.0) -> list[str]:
    """等一会儿，看还有没有活着的 ``sleep 37``。

    **要等**：被 SIGKILL 掉的那个进程有一小会儿是**僵尸**（父进程已经死了，
    它被 init 收养之后才会被回收）—— 僵尸不算活着，但它确实还在进程表里。
    真孤儿会一直活到三十七秒自然退出，所以三秒钟足够把两者分开。
    """
    deadline = time.monotonic() + timeout
    while True:
        survivors = _survivors()
        if not survivors or time.monotonic() >= deadline:
            return survivors
        await asyncio.sleep(0.1)


class TestGitDiff:
    """``git_diff`` —— 工作区里算出来的改动。"""

    async def test_the_change_is_collected(self, workspace: Path) -> None:
        (workspace / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        collector = WorkspaceCollector()

        data = await collector.collect(
            ArtifactKind.GIT_DIFF, source=EvidenceSource(workspace=workspace)
        )

        assert data is not None
        assert b"VALUE = 2" in data
        assert b"-VALUE = 1" in data

    async def test_no_change_is_empty_content_not_missing(self, workspace: Path) -> None:
        """ "确实没有改动"是**一份内容为空的证据**，不是"取不到"。"""
        collector = WorkspaceCollector()

        data = await collector.collect(
            ArtifactKind.GIT_DIFF, source=EvidenceSource(workspace=workspace)
        )

        assert data == b""

    async def test_a_non_repository_is_not_retrievable(self, tmp_path: Path) -> None:
        """不是仓库 → 取不到（``None``），不是"没有改动"。"""
        collector = WorkspaceCollector()

        data = await collector.collect(
            ArtifactKind.GIT_DIFF, source=EvidenceSource(workspace=tmp_path)
        )

        assert data is None

    async def test_a_missing_git_binary_is_not_retrievable(self, workspace: Path) -> None:
        collector = WorkspaceCollector(git_binary="ases-no-such-git-binary")

        data = await collector.collect(
            ArtifactKind.GIT_DIFF, source=EvidenceSource(workspace=workspace)
        )

        assert data is None

    async def test_the_diff_can_be_taken_against_a_base(self, workspace: Path) -> None:
        (workspace / "module.py").write_text("VALUE = 3\n", encoding="utf-8")
        collector = WorkspaceCollector(diff_base="HEAD")

        data = await collector.collect(
            ArtifactKind.GIT_DIFF, source=EvidenceSource(workspace=workspace)
        )

        assert data is not None
        assert b"VALUE = 3" in data

    async def test_the_workspace_path_is_not_run_through_a_shell(self, tmp_path: Path) -> None:
        """工作区路径来自外部输入 —— 不能让它被 shell 解释。"""
        hostile = tmp_path / "a; touch pwned.txt"
        hostile.mkdir()
        collector = WorkspaceCollector()

        data = await collector.collect(
            ArtifactKind.GIT_DIFF, source=EvidenceSource(workspace=hostile)
        )

        assert data is None
        assert not (tmp_path / "pwned.txt").exists()

    async def test_the_diff_base_is_not_run_through_a_shell(self, tmp_path: Path) -> None:
        """基线 ref 也是外部输入 —— 同样不能被 shell 解释。

        ``diff_base`` 直接进了命令行，所以这一条比路径那条更要紧：路径是
        传给子进程的 ``cwd``，而基线是**命令的一部分**。
        """
        collector = WorkspaceCollector(diff_base="HEAD; touch pwned.txt")

        data = await collector.collect(
            ArtifactKind.GIT_DIFF, source=EvidenceSource(workspace=tmp_path)
        )

        assert data is None
        assert not (tmp_path / "pwned.txt").exists()

    async def test_a_hung_git_is_not_retrievable(self, tmp_path: Path) -> None:
        """一条卡住的 git 不该把整个归档流程挂死。

        超时按"取不到"处理 —— 不是"没有改动"：那条 git 压根没跑完，
        它看到了什么谁也不知道。
        """
        collector = WorkspaceCollector(git_binary=str(_hung_git(tmp_path)), timeout_seconds=0.5)

        try:
            data = await collector.collect(
                ArtifactKind.GIT_DIFF, source=EvidenceSource(workspace=tmp_path)
            )

            assert data is None
        finally:
            subprocess.run(["pkill", "-f", "sleep 37"], capture_output=True, check=False)

    async def test_a_hung_git_leaves_no_orphan(self, tmp_path: Path) -> None:
        """被收掉的 git 那一组进程都得跟着走。

        只杀父进程会留下一堆孤儿继续占着工作区 —— 而归档是在验收之后跑的，
        那些孤儿会一直跑到自然退出。
        """
        collector = WorkspaceCollector(git_binary=str(_hung_git(tmp_path)), timeout_seconds=0.5)

        try:
            await collector.collect(
                ArtifactKind.GIT_DIFF, source=EvidenceSource(workspace=tmp_path)
            )

            # 收掉之后不该再有那一组进程 —— 脚本派生的 sleep 用一个冷僻的
            # 时长，靠命令行认得出来（父进程死了，子进程还活着就是孤儿）。
            survivors = await _wait_for_survivors()
            assert survivors == [], f"收掉 git 之后留下了孤儿进程：{survivors}"
        finally:
            # 用例失败时也别把孤儿留在机器上。
            subprocess.run(["pkill", "-f", "sleep 37"], capture_output=True, check=False)


class TestTestReport:
    """``test_report`` —— 测试框架写出来的文件。"""

    async def test_the_report_is_read(self, workspace: Path) -> None:
        (workspace / "junit.xml").write_bytes(b"<testsuite/>")
        collector = WorkspaceCollector(test_report_path=Path("junit.xml"))

        data = await collector.collect(
            ArtifactKind.TEST_REPORT, source=EvidenceSource(workspace=workspace)
        )

        assert data == b"<testsuite/>"

    async def test_an_absolute_path_is_used_as_is(self, workspace: Path, tmp_path: Path) -> None:
        report = tmp_path / "report.xml"
        report.write_bytes(b"<testsuite/>")
        collector = WorkspaceCollector(test_report_path=report)

        data = await collector.collect(
            ArtifactKind.TEST_REPORT, source=EvidenceSource(workspace=workspace)
        )

        assert data == b"<testsuite/>"

    async def test_a_missing_report_is_not_retrievable(self, workspace: Path) -> None:
        collector = WorkspaceCollector(test_report_path=Path("absent.xml"))

        data = await collector.collect(
            ArtifactKind.TEST_REPORT, source=EvidenceSource(workspace=workspace)
        )

        assert data is None

    async def test_no_configured_path_means_no_report(self, workspace: Path) -> None:
        """路径是**配置** —— 没配就没有，不去猜。"""
        (workspace / "junit.xml").write_bytes(b"<testsuite/>")
        collector = WorkspaceCollector()

        data = await collector.collect(
            ArtifactKind.TEST_REPORT, source=EvidenceSource(workspace=workspace)
        )

        assert data is None

    async def test_an_empty_report_is_empty_content(self, workspace: Path) -> None:
        """报告文件在但内容为空 —— 那是**一份空的证据**，不是取不到。"""
        (workspace / "junit.xml").write_bytes(b"")
        collector = WorkspaceCollector(test_report_path=Path("junit.xml"))

        data = await collector.collect(
            ArtifactKind.TEST_REPORT, source=EvidenceSource(workspace=workspace)
        )

        assert data == b""


class TestExecutionTrace:
    """``execution_trace`` —— 验证引擎给的那几行。"""

    async def test_the_traces_are_joined(self, workspace: Path) -> None:
        collector = WorkspaceCollector()
        source = EvidenceSource(
            workspace=workspace,
            traces=("unit_tests_pass: exit=0", "lint_pass: exit=0"),
        )

        data = await collector.collect(ArtifactKind.EXECUTION_TRACE, source=source)

        assert data == b"unit_tests_pass: exit=0\nlint_pass: exit=0\n"

    async def test_no_trace_is_empty_content(self, workspace: Path) -> None:
        """空轨迹说明"这次一项检查都没跑" —— 那是一条结论，不是证据的缺失。"""
        collector = WorkspaceCollector()
        source = EvidenceSource(workspace=workspace, traces=())

        data = await collector.collect(ArtifactKind.EXECUTION_TRACE, source=source)

        assert data == b""
