"""工作区里的证据怎么取出来 —— git diff / test report / execution trace。

设计依据：docs/tasks.md TASK-P1-048（归档三类产物）、TASK-P1-041（验证引擎
产出 ``VerificationRun``）、TASK-P1-024（证据不可丢失）。

**取证据与存证据是分开的两件事。** :mod:`knowledge.artifacts.archiver` 负责
"存"（上传 + 登记 + 绑验收 + fail-closed），这个模块负责"取"。

**三类证据的取法完全不同，因为它们的来源完全不同**：

``git_diff``
    工作区里**算**出来的 —— 跑一条 ``git diff``。它是"改动"的唯一客观
    描述：验收契约点名要它，就是为了让"到底改了哪几行"有据可查。

``test_report``
    工作区里**放着**的 —— 测试框架写出来的文件（junit xml、coverage 报告、
    重定向的输出……）。**它的路径是配置，不是约定。** 各种测试框架写在哪
    都不一样，猜一个路径等于制造一个"证据在但找不到"的假故障；所以路径
    由调用方给，给不出来就取不到（返回 ``None``），而不是去猜。

``execution_trace``
    **不在工作区里** —— 它是验证引擎给的（哪几项检查、退出码几）。工作区里
    有的是"这次验出了什么"，轨迹是"这次怎么验的"。

**git 命令不开 shell。** ``create_subprocess_exec`` 不经过 shell，所以
diff 的参数与工作区路径**不能**带管道与重定向 —— 工作区路径来自外部输入，
``shell=True`` 等于把执行权交给一份外部数据。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

from core.evidence.artifacts import ArtifactKind
from knowledge.artifacts.archiver import EvidenceSource

#: 取 diff 的超时（秒）。超时按"取不到"处理 —— 一条卡住的 git 不该把整个
#: 归档流程挂死。
GIT_TIMEOUT_SECONDS = 60

#: 收掉卡住进程时的宽限期（秒）—— ``SIGTERM`` 之后给它自己收尾的时间。
_TERMINATION_GRACE_SECONDS = 5


class WorkspaceCollector:
    """从工作区里取三类证据。

    :param test_report_path: 测试报告文件的位置。**相对路径按工作区解析。**
        给 ``None`` 表示"这次没有测试报告可取"。
    :param diff_base: ``git diff`` 的基线。给 ``None`` 时比的是 ``HEAD``
        （含未提交的改动）；给了一个 ref（比如 ``main``）时比的是那个 ref。
    :param timeout_seconds: 跑 ``git diff`` 的超时。一条卡住的 git 不该把
        整个归档流程挂死 —— 超时按"取不到"处理。
    """

    def __init__(
        self,
        *,
        test_report_path: Path | None = None,
        diff_base: str | None = None,
        git_binary: str = "git",
        timeout_seconds: float = GIT_TIMEOUT_SECONDS,
    ) -> None:
        self._test_report_path = test_report_path
        self._diff_base = diff_base
        self._git_binary = git_binary
        self._timeout_seconds = timeout_seconds

    async def collect(self, kind: ArtifactKind, *, source: EvidenceSource) -> bytes | None:
        """取出这一类证据。取不到返回 ``None``。

        **按种类穷举，不留兜底分支。** 多出来的那种证据不该被"取不到"轻轻
        带过 —— 那会让一份没人取过的证据看起来像"本来就没有"。加了新种类
        就该在这里显式接上（类型检查会逼着改）。
        """
        match kind:
            case ArtifactKind.GIT_DIFF:
                return await self._collect_diff(source.workspace)
            case ArtifactKind.TEST_REPORT:
                return self._collect_test_report(source.workspace)
            case ArtifactKind.EXECUTION_TRACE:
                return _collect_execution_trace(source.traces)

    async def _collect_diff(self, workspace: Path) -> bytes | None:
        """跑一条 ``git diff``。

        **失败与"没有改动"要分得开。** git 跑不起来（不是仓库、没有 git）
        是"取不到"，返回 ``None``；跑起来了但输出为空是"确实没有改动"，
        返回空字节 —— 那本身也是证据（"当时真的没有改动"）。
        """
        arguments = ["diff"]
        if self._diff_base is not None:
            arguments.append(self._diff_base)
        else:
            arguments.append("HEAD")

        try:
            process = await asyncio.create_subprocess_exec(
                self._git_binary,
                *arguments,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            # git 不存在 —— 取不到，不是"没有改动"。
            return None

        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self._timeout_seconds)
        except TimeoutError:
            await _kill(process)
            return None

        if process.returncode != 0:
            return None
        return stdout

    def _collect_test_report(self, workspace: Path) -> bytes | None:
        """读测试报告文件。

        **报告不在就是取不到**，返回 ``None`` —— 让归档器知道"这份证据压根
        没有"，而不是"有一份空的"。
        """
        if self._test_report_path is None:
            return None
        path = self._test_report_path
        if not path.is_absolute():
            path = workspace / path
        if not path.is_file():
            return None
        return path.read_bytes()


def _collect_execution_trace(traces: tuple[str, ...]) -> bytes | None:
    """执行轨迹 —— 验证引擎给的那几行。

    **空轨迹不是"没有轨迹"。** 它说明"这次一项检查都没跑" —— 那是一条
    结论，不是证据的缺失。所以返回空字节而不是 ``None``。
    """
    if not traces:
        return b""
    return ("\n".join(traces) + "\n").encode("utf-8")


async def _kill(process: asyncio.subprocess.Process) -> None:
    """把卡住的 git 收掉。

    连同它那一组进程一起杀 —— ``git diff`` 会派生子进程（外部 diff 驱动、
    凭证助手），只杀父进程会留下一堆孤儿占着工作区。

    **先礼后兵**：``SIGTERM`` 给它一点时间自己收尾，宽限期过了再
    ``SIGKILL``。``git diff`` 是只读的，被打断也不会留下半截文件。

    **整组都要补一发 ``SIGKILL``** —— 不只是直系进程还活着的时候：孙进程
    可能**忽略** ``SIGTERM``，而它只要还活着就攥着管道的写端，管子不关，
    下面的 ``communicate()`` 就永远等不到 EOF —— "超时"于是变成了"永久
    卡住"，一条卡住的 git 会把整个归档流程拖死。

    **管道也要收干净。** 被取消的 ``communicate()`` 不会关管道；进程都死了
    管道自然到头，再收一次就把它们收了 —— 否则解释器会在垃圾回收时抱怨
    "unclosed transport"，而那种警告会落在**别的**测试头上（谁都说不清
    是谁留下的）。
    """
    group = _process_group(process)
    if group is not None:
        _signal_group(group, signal.SIGTERM)

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=_TERMINATION_GRACE_SECONDS)

    # **整组补刀**（直系进程退了也一样）—— 见上面那段说明：孙进程可能
    # 不听话，而它攥着管道不放。
    if group is not None:
        _signal_group(group, signal.SIGKILL)

    # 进程都没了，管道就到头了 —— 再收一次，顺手把管道关掉。
    with contextlib.suppress(Exception):
        await process.communicate()


def _process_group(process: asyncio.subprocess.Process) -> int | None:
    """这组进程的 pgid。取不到就返回 ``None``（只能杀进程本身）。"""
    try:
        return os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        return None


def _signal_group(group: int, sig: int) -> None:
    """给一整组进程发信号。发不出去就算了 —— 进程可能已经自己退了。"""
    try:
        os.killpg(group, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return


__all__ = ["GIT_TIMEOUT_SECONDS", "WorkspaceCollector"]
