"""读世界的地方 —— 只有这一处。

设计依据：docs/tasks.md TASK-P3-011（六路触发源）、TASK-P3-013（Observe 层
六类数据源各接入 ≥1 个真实来源）。

**为什么要有这一层。** 触发层与观察层读的是**同一个世界的六个角落** ——
git 历史、测试结果、跑一条命令、人写的台账、决策记录。两层问的问题不同
（"出了什么事"还是"现在什么样"），但**读法是一样的**：都是起一个子进程、
都是解析一种文本格式。

**读法一样就该有一处读法。** 各写各的，迟早有一处改了另一处没改 ——
于是"git log 的输出长什么样"在系统里有两个答案，而触发源与观察源给出的
提交列表对不上，**没有人会发现**。

**这一层只负责读，不负责解释。** 它把外面世界的原样搬进来：跑一条命令
就跑，把 stdout 原样带回，解析失败就说解析失败。**"这说明什么"是上面
那一层的事。**

**跑不起来也是一种结果。** :class:`ProcessOutcome` 分得清三件事：**跑成了
（退出码可能是非零）**、**超时了**、**压根起不来**。把它们合成一个
``None``，上层就分不清"命令失败了"与"工具坏了" —— 而这两件事该报的
内容完全不同。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: 跑一条命令的默认超时（秒）。
DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0

#: 跑一个测试套件的默认超时（秒）。**给上限** —— 套件挂住了要报错，不能
#: 一直等着。
DEFAULT_SUITE_TIMEOUT_SECONDS: Final[float] = 120.0

#: 人写台账里，条目标题长什么样。
#:
#: **只认一种形状。** 认得越宽，解析错的越多 —— 一份解析错的台账会产出
#: 主体标识不对的事件，去重窗口于是把两件不同的事当成同一件挡掉。
REPORT_HEADING: Final[re.Pattern[str]] = re.compile(
    r"^###\s+(?P<id>[A-Za-z0-9][A-Za-z0-9._-]*)\s+——\s*(?P<title>.+?)\s*$"
)

#: pytest 的失败摘要行长什么样。
#:
#: ``-rf`` 让 pytest 打出 ``FAILED <nodeid> - <理由>``。
FAILED_LINE: Final[re.Pattern[str]] = re.compile(r"^FAILED\s+(?P<nodeid>\S+)")

#: pytest 摘要行里的"多少条过了 / 多少条没过"。
#:
#: **只认整数计数。** 摘要行的形状随 pytest 版本变，认不准就宁可不给数 ——
#: 缺实测值是失败，不是 0。
PYTEST_COUNT: Final[re.Pattern[str]] = re.compile(
    r"(?P<count>\d+)\s+(?P<label>passed|failed|error|errors|skipped|xfailed|xpassed)"
)


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """跑一条命令的结果 —— **三态，别合成一个**。

    :param argv: 跑的是什么。
    :param returncode: 退出码。**起不来或超时时是 ``None``** —— 那不是"退出
        码 0"，是压根没有退出码。
    :param stdout: 标准输出。
    :param stderr: 标准错误。
    :param started: **进程有没有起来。**
    :param timed_out: 有没有超时。
    :param duration_seconds: 跑了多久。
    :param error: 起不来时的原因（操作系统给的）。

    **"起不来"与"退出码非零"必须分开。** 前者是工具坏了，后者是被测的东西
    报错了 —— 报给上层的说法不一样，补救动作也不一样。
    """

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    started: bool
    timed_out: bool
    duration_seconds: float
    error: str = ""

    @property
    def ok(self) -> bool:
        """跑成了，而且退出码是 0。"""
        return self.started and not self.timed_out and self.returncode == 0


def run_process(
    argv: tuple[str, ...] | list[str],
    *,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> ProcessOutcome:
    """跑一条命令。

    **跑不起来、超时、失败，全都返回一个结果，不抛。** 这一层的定位是"如实
    说外面发生了什么"，不是"因为工具坏了就把整个系统带崩"。

    :param argv: 要跑的命令。
    :param cwd: 在哪跑。
    :param timeout: 跑多久算超时。
    :param env: **额外加进去的环境变量**（不是替换整个环境）。
    """
    command = tuple(argv)
    if not command:
        return ProcessOutcome(
            argv=command,
            returncode=None,
            stdout="",
            stderr="",
            started=False,
            timed_out=False,
            duration_seconds=0.0,
            error="什么命令都没给",
        )
    merged = {**os.environ, **env} if env else None
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=merged,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ProcessOutcome(
            argv=command,
            returncode=None,
            stdout="",
            stderr="",
            started=True,
            timed_out=True,
            duration_seconds=time.monotonic() - started_at,
            error=f"超过 {timeout} 秒没跑完",
        )
    except OSError as error:
        return ProcessOutcome(
            argv=command,
            returncode=None,
            stdout="",
            stderr="",
            started=False,
            timed_out=False,
            duration_seconds=time.monotonic() - started_at,
            error=str(error),
        )
    return ProcessOutcome(
        argv=command,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        started=True,
        timed_out=False,
        duration_seconds=time.monotonic() - started_at,
    )


@dataclass(frozen=True, slots=True)
class RuntimeProbe:
    """一条要看着的命令。

    :param id: 探针的身份 —— **事件的主体**。同一条命令看两次，出的事是
        同一件。
    :param argv: 要跑的命令。
    :param cwd: 在哪跑。
    :param timeout: 跑多久算超时。
    :param env: **额外加进去的环境变量。** 跑一个装在 ``src/`` 下的包要
        把 ``src`` 放进 ``PYTHONPATH`` —— 这一条就是给这件事用的。
    :param evidence_path: **这条命令看的是哪个文件。** 触发层不看它（触发
        事件的主体是命令本身）；观察层拿它当证据 —— **证据是硬要求**，没有
        落点的观察不产出。

    **放在这一层是因为"一条要跑的命令"就是一条命令。** 触发层与观察层问
    的问题不同，但跑的是同一个东西 —— 两个形状迟早对不上。
    """

    #: 探针的身份。
    id: str
    #: 要跑的命令。
    argv: tuple[str, ...]
    #: 在哪跑。
    cwd: Path | None = None
    #: 跑多久算超时。
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    #: 额外加进去的环境变量。
    env: tuple[tuple[str, str], ...] = ()
    #: 这条命令看的是哪个文件（相对仓库根目录）。
    evidence_path: str = ""


@dataclass(frozen=True, slots=True)
class CommitEntry:
    """一次提交。

    :param sha: 提交的完整 sha（40 位）。
    :param subject: 提交标题。
    """

    sha: str
    subject: str


def read_commits(
    repo: Path,
    *,
    limit: int = 20,
    since: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[CommitEntry, ...]:
    """读一个 git 仓库的提交历史。

    **不是 git 仓库就什么也不产出**，而不是报错 —— 这一层读的是"一个可能
    存在的仓库"，没有就当没有。

    :param repo: 仓库根目录。
    :param limit: 最多读多少条。
    :param since: 只看这个提交之后的。
    """
    argv = [
        "git",
        "-C",
        str(repo),
        "log",
        "--format=%H%x1f%s",
        f"--max-count={limit}",
    ]
    if since:
        argv.append(f"{since}..HEAD")
    outcome = run_process(tuple(argv), cwd=repo, timeout=timeout)
    if not outcome.ok:
        return ()
    entries: list[CommitEntry] = []
    for line in outcome.stdout.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition("\x1f")
        entries.append(CommitEntry(sha=sha.strip(), subject=subject.strip()))
    return tuple(entries)


def read_changed_files(
    repo: Path,
    *,
    limit: int = 20,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[tuple[str, int], ...]:
    """数一数最近几次提交里，**每个文件被改过多少次**。

    **这是"状态"，不是"事件"。** 触发层读的是"新来了哪几次提交"（每次
    提交是一件事）；这里读的是"哪个文件改得最频繁"（那是一件**关于这个
    文件**的事，反复成立）。

    **路径是相对 ``repo`` 的，不是相对 git 仓库根的。** ``--relative`` 就是
    为这件事加的 —— 没有它，一个仓库里的子目录会拿到 ``子目录/文件.py``
    这种路径，而证据核验器是拿它去**那个子目录**底下找的，一找就找不到。
    **"读出来的东西指的不是同一个地方"是最难查的一类错。**

    :param repo: 仓库根目录。
    :param limit: 看最近多少次提交。
    :return: (文件路径, 被改过的次数)，按次数从多到少排。
    """
    argv = [
        "git",
        "-C",
        str(repo),
        "log",
        "--format=%x1e",
        f"--max-count={limit}",
        "--name-only",
        "--relative",
    ]
    outcome = run_process(tuple(argv), cwd=repo, timeout=timeout)
    if not outcome.ok:
        return ()
    counts: dict[str, int] = {}
    for line in outcome.stdout.splitlines():
        path = line.strip()
        if not path or path.startswith("\x1e"):
            continue
        counts[path] = counts.get(path, 0) + 1
    return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """人写台账里的一条。

    :param id: 条目的编号。
    :param title: 条目的标题。
    """

    id: str
    title: str


def read_ledger(path: Path) -> tuple[LedgerEntry, ...]:
    """读人写的台账（Markdown）。

    **只认一种形状**（``### <编号> —— <标题>``），认不出来的那条就跳过 ——
    **宁可漏报，不要报错**：台账是人写的，格式变一点不该让整个系统罢工。

    :param path: 台账文件。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    entries: list[LedgerEntry] = []
    for line in text.splitlines():
        matched = REPORT_HEADING.match(line.strip())
        if matched is None:
            continue
        entries.append(LedgerEntry(id=matched.group("id"), title=matched.group("title")))
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class RecordEntry:
    """一份记录（决策记录之类）。

    :param stem: 记录的文件名（不含扩展名）。
    :param title: 记录的一级标题。
    """

    stem: str
    title: str


def read_records(directory: Path) -> tuple[RecordEntry, ...]:
    """读一个目录里的记录（``*.md``）。

    :param directory: 记录放在哪。
    """
    if not directory.is_dir():
        return ()
    entries: list[RecordEntry] = []
    for path in sorted(directory.glob("*.md")):
        entries.append(RecordEntry(stem=path.stem, title=read_first_heading(path)))
    return tuple(entries)


def read_first_heading(path: Path) -> str:
    """文件里的一级标题。读不出来就是空的。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


@dataclass(frozen=True, slots=True)
class PytestOutcome:
    """跑一遍 pytest 的结果。

    :param returncode: pytest 的退出码。
    :param failed_node_ids: 没过的那几条用例的 id。
    :param counts: 摘要里的计数（``{"passed": 34, "failed": 9}``）。
    :param duration_seconds: 跑了多久。

    **退出码 1 是"跑完了，有用例没过"；其余非零退出码都是"没跑成"。**
    调用方要自己分清这两种情况 —— 本仓库的规矩是**缺实测值是失败，不是 0**。
    """

    returncode: int
    failed_node_ids: tuple[str, ...]
    counts: Mapping[str, int]
    duration_seconds: float


def run_pytest(
    repo: Path,
    targets: tuple[str, ...],
    *,
    run_xfail: bool = True,
    timeout: float = DEFAULT_SUITE_TIMEOUT_SECONDS,
) -> PytestOutcome:
    """真的跑一遍 pytest。

    :param repo: 仓库根目录。
    :param targets: **跑哪些东西。必须说清楚。**
    :param run_xfail: 把标记为 xfail 的用例当成真失败跑。

    **跑不起来这里会抛 ``RuntimeError``** —— 因为"没跑成"与"没有失败"是
    两件事，调用方必须自己决定怎么报。
    """
    argv = [sys.executable, "-m", "pytest", "-q", "--tb=no", "-rf"]
    if run_xfail:
        argv.append("--runxfail")
    argv.extend(targets)
    outcome = run_process(tuple(argv), cwd=repo, timeout=timeout)
    if not outcome.started or outcome.timed_out:
        raise RuntimeError(f"在 {repo} 里跑 pytest 失败 —— {outcome.error}")
    failed: list[str] = []
    for line in outcome.stdout.splitlines():
        matched = FAILED_LINE.match(line.strip())
        if matched is not None:
            failed.append(matched.group("nodeid"))
    return PytestOutcome(
        returncode=outcome.returncode if outcome.returncode is not None else -1,
        failed_node_ids=tuple(failed),
        counts=read_pytest_counts(outcome.stdout),
        duration_seconds=outcome.duration_seconds,
    )


def read_pytest_counts(stdout: str) -> Mapping[str, int]:
    """从 pytest 的输出里读出摘要计数。

    **只认整数计数。** 认不出来就少几个键 —— **不要填 0**，"没读到"与
    "读到是 0"是两件事。
    """
    counts: dict[str, int] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if " passed" not in stripped and " failed" not in stripped:
            continue
        for matched in PYTEST_COUNT.finditer(stripped):
            label = matched.group("label")
            key = "error" if label == "errors" else label
            counts[key] = counts.get(key, 0) + int(matched.group("count"))
    return counts


__all__ = [
    "DEFAULT_SUITE_TIMEOUT_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "FAILED_LINE",
    "PYTEST_COUNT",
    "REPORT_HEADING",
    "CommitEntry",
    "LedgerEntry",
    "ProcessOutcome",
    "PytestOutcome",
    "RecordEntry",
    "RuntimeProbe",
    "read_changed_files",
    "read_commits",
    "read_first_heading",
    "read_ledger",
    "read_pytest_counts",
    "read_records",
    "run_process",
    "run_pytest",
]
