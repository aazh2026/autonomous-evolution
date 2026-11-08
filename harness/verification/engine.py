"""独立验证引擎 —— 验收结论由执行器之外的人给出。

设计依据：docs/design.md §3.5B（"不要让 Coding Agent 自己决定是否验收通过"；
验证引擎应当独立于执行器）、§3.5A（Docker / worktree 隔离）；docs/tasks.md
TASK-P1-041、R-049（验证引擎独立于执行器）、I-22（验证所用的模型须与被测代码
生成模型解耦）。

**这个模块的立场只有一句：被测者不能给自己打分。**

Coding Agent 跑完活会说"我做完了"（``self_assessment.completed = true``）。
那句话是**被验收对象**对验收结论的预判 —— 让它参与判定，等于让考生自己判卷。
所以这个模块做三件事，且每一件都在把执行者往外推：

1. **引擎看不见执行结果。** :class:`VerificationRequest` 的字段里没有
   ``ExecutionResult``、没有 ``self_assessment``、没有 session id ——
   不是"约定不用"，是**没有地方可以传进来**。想要"参考一下 Agent 怎么说"
   的调用方会发现这个参数不存在。
2. **验证进程看不到执行环境。** 检查跑在一个**独立进程**里，环境变量按白名单
   重新造，``HOME`` 指向一次性临时目录而不是宿主机家目录 ——
   否则 ``~/.claude/projects/<session>.jsonl``（Agent 的会话记录）就在验证
   进程的可达范围内，那不是隔离，那是同屋。
3. **验证端模型与生成端模型分开配置。** I-22：验证用的模型与写代码用的模型
   必须是两个旋钮。用写代码的同一个模型去验证它自己写的东西，等于让同一个人
   出题又判卷 —— 而模型自评的倾向性是**系统性的**，不是随机噪声。

**结论有三个取值，不是两个。** ``passed`` / ``failed`` / ``error`` 的区别是
本模块最要紧的一处：``failed`` 是"验了，没过"（可以走自动返工），``error``
是"没验成"（环境问题、进程被杀掉、检查压根没跑起来）。把两者压成一个布尔值，
"pytest 装不上依赖"会被记成"测试没过"，然后系统会去修一个根本没坏的东西。

**fail-closed 落在三个地方**：证据不齐（必检项没产出结果）判 ``error`` 而不判
``passed``；计划里缺一项必检 → 契约判定记 ``missing`` → 整体不通过；阈值没有
实测值 → 判失败而不是"没达标"。三处的共同点是 —— **"不知道"永远不等于
"没问题"**。

**这个模块不负责判定"任务算不算成功"。** 那是 :mod:`core.task.outcome` 的
事，它拿 :class:`~core.task.outcome.VerificationReport` 判。方向必须是
"引擎 → 领域"；反过来就等于让验证引擎既出题又定最终结论。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from core.project.models import TargetValue, ValueKind
from core.task.outcome import VerificationReport, VerificationVerdict
from harness.verification.contract import (
    AcceptanceContract,
    ArtifactKind,
    CheckOutcome,
    CheckResult,
    ContractEvaluation,
    ContractEvidence,
    ForbiddenRule,
    RequiredCheck,
    evaluate_contract,
)

#: 验证进程环境变量的白名单。
#:
#: **不是继承宿主机环境，是重新造。** 白名单之外的一个都不进来 ——
#: ``ANTHROPIC_API_KEY``、``AWS_SECRET_ACCESS_KEY``、``HTTPS_PROXY``、
#: 以及各家的 session 句柄，一律进不来。
#:
#: 刻意包含 ``PATH`` 与 ``HOME``：前者让工具找得到，后者是很多工具的缓存
#: 落点。但 ``HOME`` 指向的是一次性临时目录，不是宿主的家目录 —— 原因见
#: :func:`build_verification_env`。
VERIFICATION_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "CI",
        "TERM",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "PIP_DISABLE_PIP_VERSION_CHECK",
        "PYTEST_ADDOPTS",
        "COVERAGE_RCFILE",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
        "SOURCE_DATE_EPOCH",
    }
)

#: 环境变量名里出现这些**词**，就判定它装的是凭据。
#:
#: 与 :data:`harness.sandbox.policy.SECRET_NAME_TOKENS` 同源，但**不复用**
#: 那个常量：沙箱策略是给"跑 Agent 的沙箱"用的，验证引擎是给"跑测试的进程"
#: 用的，两者被刻意解耦（待裁决 13 的同一条原则：包之间不互相引用，靠数据
#: 耦合）。这里各留一份，改一边不影响另一边。
SECRET_NAME_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "SECRET",
        "SECRETS",
        "TOKEN",
        "TOKENS",
        "PASSWORD",
        "PASSWD",
        "PASSPHRASE",
        "CREDENTIAL",
        "CREDENTIALS",
        "KEY",
        "APIKEY",
        "SESSION",
    }
)

#: 默认的单项检查超时（秒）。
DEFAULT_CHECK_TIMEOUT_SECONDS: Final[int] = 1800

#: 杀进程组时，先给 SIGTERM 的宽限期（秒）。
_TERMINATION_GRACE_SECONDS: Final[int] = 5


class VerificationSetupError(RuntimeError):
    """验证请求本身不成立 —— 配置错误，不是"验证没通过"。

    这条异常只在"请求没法执行"时抛出（工作区不存在、契约与计划对不上）。
    **运行期**的问题（检查跑挂了、测试没过）不进这里 —— 那些要变成结论，
    而不是异常。一个把"环境坏了"报成异常的引擎，会让编排层把环境故障
    误判成任务失败，然后去返工一个没坏的代码。
    """


class VerificationEnvError(RuntimeError):
    """验证进程的环境变量不合法。

    单独成一条，因为它和"配置错误"不同：这条说明**有人试图把不该进去的东西
    放进去**（凭据、session 句柄）。是拒绝，不是修补。
    """


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """从一个检查的输出里读出一个指标。

    :param name: 指标名 —— 与契约 ``thresholds`` 里的键对应。
    :param pattern: 正则。必须有**一个**捕获组，捕获到的就是数值。
    :param group: 用第几个捕获组。默认第 1 个。
    :param scale: 读出来后乘的系数。覆盖率常以百分数出现（``85.3``），
        而契约里写的是比例（``0.85``），差的就是这个系数。
    """

    name: str
    pattern: str
    group: int = 1
    scale: Decimal = Decimal(1)

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern)

    def read(self, output: str) -> TargetValue | None:
        """从输出里读指标。读不到返回 ``None`` —— **不是**读成 0。

        读不到而返回 0 是这里最容易犯的错：契约写着"覆盖率 >= 0.85"，
        而检查因为某种原因没输出覆盖率，读成 0 会得到"未达标"；
        可 0 这个数是**编出来的**，它会让"没测到"看起来像"测到个位数"。
        返回 ``None`` 才老实 —— 契约判定会把"缺实测值"记成失败并归到
        ``missing``，那里的处置是"重跑"，而不是"覆盖率不够，去补测试"。
        """
        match = self.compiled().search(output)
        if match is None:
            return None
        try:
            captured = match.group(self.group)
        except (IndexError, re.error):
            return None
        text = captured.strip().rstrip("%")
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None
        if not value.is_finite():
            return None
        return TargetValue(kind=ValueKind.NUMBER, number=value * self.scale)


@dataclass(frozen=True, slots=True)
class CheckSpec:
    """一项必检检查怎么跑。

    :param check: 对应契约 ``required`` 里的哪一项。
    :param command: 命令与参数。**不开 shell** —— 计划是外部输入，
        ``shell=True`` 等于把执行权交给一份配置文件。
    :param timeout_seconds: 超时。超时判 ``error``，不判 ``failed``。
    :param metric: 从这次输出里读哪个指标。
    :param produces: 这次检查跑完会产出哪些证据。**跑成了就有** ——
        测试没过也会产出测试报告，所以"没产出"只对应"压根没跑成"。
    """

    check: RequiredCheck
    command: tuple[str, ...]
    timeout_seconds: int = DEFAULT_CHECK_TIMEOUT_SECONDS
    metric: MetricSpec | None = None
    produces: tuple[ArtifactKind, ...] = ()

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("检查命令不得为空")
        if self.timeout_seconds <= 0:
            raise ValueError(f"超时必须为正，得到 {self.timeout_seconds}")


@dataclass(frozen=True, slots=True)
class ScanSpec:
    """一项禁止项扫描。

    约定很简单：**退出码 0 表示干净，非 0 表示命中了这条禁止项。**

    之所以用退出码而不是解析输出 —— 输出格式各家的扫描器不一样
    （detect-secrets、gitleaks、ruff 的输出结构都不同），而退出码是所有
    工具都有的共同语言。真要拿到"命中了哪一行"，那属于安全门
    （TASK-P1-043）的报告内容，不属于契约判定。

    :param produces: 这次扫描顺带产出的证据（比如 ``git_diff``）。
    """

    rule: ForbiddenRule
    command: tuple[str, ...]
    timeout_seconds: int = DEFAULT_CHECK_TIMEOUT_SECONDS
    produces: tuple[ArtifactKind, ...] = ()

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("扫描命令不得为空")
        if self.timeout_seconds <= 0:
            raise ValueError(f"超时必须为正，得到 {self.timeout_seconds}")


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    """验证计划 —— 每项检查怎么跑。

    :param checks: 每项必检检查的命令。契约要求了但计划里没有的，
        那一项就**没有结果** —— 契约判定会记成 ``missing``，整体不通过。
        这是刻意的：计划不全时正确的反应是"没验成"，不是"验过了"。
    :param scans: 每条禁止项的扫描命令。同理，契约没声明的禁止项不扫 ——
        契约没要的检查不该产生副作用。
    :param model: **验证端**用的模型（I-22）。

    :param model 与写代码用的模型是**两个旋钮**，这是 I-22 的全部要求。
    这个模块压根不知道"生成端的模型"是什么 —— 它没地方知道。所以解耦
    不是靠约定，是靠**引擎的输入里没有那个字段**。
    """

    checks: tuple[CheckSpec, ...] = ()
    scans: tuple[ScanSpec, ...] = ()
    model: str | None = None

    def check_for(self, check: RequiredCheck) -> CheckSpec | None:
        for spec in self.checks:
            if spec.check is check:
                return spec
        return None

    def scan_for(self, rule: ForbiddenRule) -> ScanSpec | None:
        for spec in self.scans:
            if spec.rule is rule:
                return spec
        return None


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    """一次验证请求 —— 引擎能看到的**全部**输入。

    **这个类里没有执行结果。** 没有 ``ExecutionResult``、没有
    ``self_assessment``、没有 session id、没有"Agent 说自己做完了"这句话。

    这不是省略，是**设计**：R-049 说"验证引擎独立于执行器"，而"独立"要有
    硬边界才守得住 —— 靠"大家记得别用它"守不住，因为总有一天有人觉得
    "参考一下 Agent 的说法总没坏处"。让这个字段**不存在**，那次"没坏处"
    就写不成代码。
    """

    task_id: str
    workspace: Path
    contract: AcceptanceContract
    plan: VerificationPlan

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id 不得为空")
        if not self.workspace.is_dir():
            raise VerificationSetupError(f"工作区 {self.workspace} 不存在或不是目录")


@dataclass(frozen=True, slots=True)
class CheckExecution:
    """一项检查跑完之后的原始结果。

    :attr:`started` 为 ``False`` 表示**压根没起来**（命令不存在、没权限）。
    :attr:`timed_out` 表示跑起来了但没跑完。
    :attr:`signalled` 表示被信号打死（OOM、被人 kill）—— 同样没跑完。
    """

    exit_code: int
    stdout: str
    stderr: str
    started: bool = True
    timed_out: bool = False
    signalled: bool = False

    @property
    def completed(self) -> bool:
        """是不是跑完了（并且是正常退出）。"""
        return self.started and not self.timed_out and not self.signalled


@runtime_checkable
class CheckRunner(Protocol):
    """跑一项检查的办法。

    协议而不是具体类，是为了让"独立进程"这件事可以被替换成"独立容器"：
    :class:`SubprocessCheckRunner` 是阶段一的实现，容器实现挂上来时编排层
    一行都不用改。
    """

    async def run(
        self,
        command: Sequence[str],
        *,
        workspace: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> CheckExecution: ...


@dataclass(frozen=True, slots=True)
class SubprocessCheckRunner:
    """真的去起一个独立进程的运行器。

    **独立进程，不是 shell 管道。** ``create_subprocess_exec`` 不经过
    shell，所以计划里的命令**不能**带管道与重定向 —— 那是刻意的：计划是
    外部输入，``shell=True`` 等于把执行权交给一份配置文件。要跑管道，
    写一个脚本进工作区，让命令去调那个脚本。

    **新会话（``start_new_session=True``）。** 超时时必须能把整组进程
    一起杀掉 —— ``pytest`` 会派生子进程，只杀父进程会留下一堆孤儿继续
    占着 CPU 与端口。
    """

    async def run(
        self,
        command: Sequence[str],
        *,
        workspace: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> CheckExecution:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace),
                env=dict(env),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            # 命令不存在、没权限、工作区没了 —— 压根没起来。
            return CheckExecution(exit_code=-1, stdout="", stderr=str(exc), started=False)

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            await _terminate_process_group(process)
            return CheckExecution(
                exit_code=-1,
                stdout="",
                stderr=f"检查超时（{timeout_seconds}s）",
                timed_out=True,
            )

        return_code = process.returncode if process.returncode is not None else -1
        signalled = return_code < 0
        return CheckExecution(
            exit_code=return_code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            signalled=signalled,
        )


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """把一个进程组收干净。

    先 ``SIGTERM`` 给它一点时间自己收尾，宽限期过了再 ``SIGKILL``。
    ``SIGKILL`` 直接上会把临时文件、数据库文件留在半截状态 —— 而验证跑在
    工作区里，半截文件会污染下一次验证看到的 diff。
    """
    try:
        group = os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        return
    for sig, grace in ((signal.SIGTERM, _TERMINATION_GRACE_SECONDS), (signal.SIGKILL, 0)):
        try:
            os.killpg(group, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        if grace == 0:
            break
        try:
            await asyncio.wait_for(process.wait(), timeout=grace)
        except TimeoutError:
            continue
        return
    with contextlib.suppress(TimeoutError):  # pragma: no cover - SIGKILL 之后极少发生
        await process.wait()


def _name_looks_like_secret(name: str) -> bool:
    """变量名是不是装凭据或 session 句柄的。

    按 ``_`` 切成词再比对，而不是子串匹配 —— 否则 ``TOKENIZERS_PARALLELISM``
    这种无害变量会被误伤。
    """
    tokens = {part for part in re.split(r"[._]", name.upper()) if part}
    return bool(tokens & SECRET_NAME_TOKENS)


def build_verification_env(
    host_env: Mapping[str, str] | None = None,
    *,
    model: str | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    """造验证进程的环境变量。

    **白名单造，不继承。** 宿主机的环境变量只被读取白名单里那几个名字，
    其余的一个都不进来 —— 包括 ``ANTHROPIC_API_KEY``、``AWS_*``、
    ``HTTPS_PROXY``，以及各家的 session 句柄。

    :param home: 验证进程的 ``HOME``。默认是一次性临时目录。

    **为什么 ``HOME`` 不能是宿主机的家目录**：``~/.claude/projects/<会话>.jsonl``
    存着 Agent 的完整会话记录，``~/.ssh``、``~/.aws``、``~/.pgpass`` 存着长期
    凭据。把宿主机的 ``HOME`` 给验证进程，等于把这些目录的读取权一起给了它
    —— 而验证进程跑的是**被验收项目自己的代码**，那份代码是 Agent 写的，
    也就是说它可能顺手就读了。
    """
    source = host_env if host_env is not None else dict(os.environ)
    env: dict[str, str] = {}
    for name in sorted(VERIFICATION_ENV_NAMES):
        value = source.get(name)
        if value is not None:
            env[name] = value
    if home is not None:
        env["HOME"] = str(home)
    if model is not None:
        env["ASES_VERIFIER_MODEL"] = model
    assert_verification_env_is_safe(env)
    return env


def assert_verification_env_is_safe(env: Mapping[str, str]) -> None:
    """环境变量里不许有凭据或 session 句柄。

    这是**第二道**防线。第一道是白名单 —— 只放行固定那几个名字。第二道
    防的是"有人往白名单里加了一个名字"：``ASES_SESSION_ID`` 这种名字不会
    自己长出来，但它长出来时应该被挡下，而不是被放进去。
    """
    for name in env:
        if _name_looks_like_secret(name):
            raise VerificationEnvError(
                f"环境变量 {name!r} 看起来装的是凭据或会话句柄 —— "
                "验证进程不得接触这些（验证端模型请走 ASES_VERIFIER_MODEL）"
            )


@dataclass(frozen=True, slots=True)
class VerificationRun:
    """一次验证的完整结论。

    :attr:`checks` 是每项必检的结果。:attr:`evaluation` 是按契约的判定。

    **"没得出结论"与"结论是不通过"是分开的两件事**：前者是
    :attr:`missing` 与 ``ERROR`` 的检查结果，后者是 ``FAILED`` 的检查结果。
    前者该重跑，后者该返工。
    """

    task_id: str
    checks: Mapping[RequiredCheck, CheckResult] = field(default_factory=dict)
    metrics: Mapping[str, TargetValue] = field(default_factory=dict)
    artifacts: frozenset[ArtifactKind] = frozenset()
    violations: frozenset[ForbiddenRule] = frozenset()
    evaluation: ContractEvaluation = field(
        default_factory=lambda: ContractEvaluation(passed=False, failures=("没有产生验证结论",))
    )
    traces: tuple[str, ...] = ()
    #: 这次验证**本该**覆盖哪些必检项。缺了它就分不清"这项没跑"与
    #: "这项本来就不用跑"。
    required: tuple[RequiredCheck, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    @property
    def evidence_is_complete(self) -> bool:
        """证据齐不齐 —— 齐了才有资格给 ``passed`` / ``failed``。

        **只看检查，不看阈值与产物。** 这一条划得很准：

        - 某项必检**没有结果**（计划里没有、没跑起来）→ 不齐；
        - 某项必检报 ``ERROR``（进程挂了、超时了）→ 不齐；
        - 某项必检**明确失败**（测试没过）→ **齐**。

        最后一条最要紧。测试没过是一个**判定**，不是一个事故 —— 契约没通过
        就是没通过，判 ``FAILED``（该返工）。而"进程挂了"是事故，判
        ``ERROR``（该重跑或让人看一眼）。把两者混成"没验成"，会让一次
        普通的测试失败被当成环境故障，然后去修一个根本不存在的问题。

        阈值实测值读不到**不**算"不齐"：检查跑完了、有明确结论，只是那个
        数字没抠出来 —— 那是"验收口径没被证明满足"，判 ``FAILED``，
        理由里会写明是哪条阈值没有实测值。
        """
        for check in self.required:
            result = self.checks.get(check)
            if result is None or result.outcome is CheckOutcome.ERROR:
                return False
        return True

    def to_report(self) -> VerificationReport:
        """转成领域层的结论。

        三个取值的分界：

        - 证据不齐（缺结果 / 有 ERROR）→ ``ERROR``："没得出结论"。
        - 证据齐了但契约没过 → ``FAILED``："验了，没过"。
        - 证据齐了且契约过了 → ``PASSED``。

        **为什么 ``ERROR`` 不能当成 ``FAILED``**：下游对两者的处置不同 ——
        ``FAILED`` 可以走有界修复，``ERROR`` 得先让人看一眼。验证引擎自己
        崩了的时候，"自动修复"多半是在修一个不存在的问题。

        注意"证据不齐"只看**检查**（见 :attr:`evidence_is_complete`）：
        阈值没测到、产物没产出，都归 ``FAILED`` —— 那是"验收口径没被证明
        满足"，而不是"验证没跑成"。
        """
        if not self.evidence_is_complete:
            verdict = VerificationVerdict.ERROR
        elif self.evaluation.passed:
            verdict = VerificationVerdict.PASSED
        else:
            verdict = VerificationVerdict.FAILED

        reasons: list[str] = list(self.evaluation.failures)
        if verdict is VerificationVerdict.ERROR and not reasons:
            reasons.append("验证证据不齐 —— 结论不可得")
        return VerificationReport(
            verdict=verdict,
            reasons=tuple(reasons),
            checks=tuple(
                (check.value, result.outcome is CheckOutcome.PASSED)
                for check, result in self.checks.items()
            ),
        )

    def to_evidence(self) -> ContractEvidence:
        """转成契约判定用的证据。

        单独一个方法，是为了让"证据长什么样"只有一处定义 —— 判定函数
        (``evaluate_contract``）与结论构造 (``to_report``) 读的是同一份东西，
        不可能一个按通过算、另一个按失败算。
        """
        return ContractEvidence(
            task_id=self.task_id,
            checks=self.checks,
            metrics=self.metrics,
            violations=self.violations,
            produced_artifacts=self.artifacts,
        )


@dataclass(frozen=True, slots=True)
class VerificationEngine:
    """独立验证引擎。

    :param runner: 怎么跑一项检查。默认是独立进程
        （:class:`SubprocessCheckRunner`）。
    :param host_env: 环境变量的来源。默认是当前进程的环境变量 ——
        但只有白名单里的名字会被读取。
    """

    runner: CheckRunner = field(default_factory=SubprocessCheckRunner)
    host_env: Mapping[str, str] | None = None

    async def verify(self, request: VerificationRequest) -> VerificationRun:
        """跑一遍验证，给出结论。

        **不会抛异常表示"验证没过"** —— 验证没过是结论，不是错误。
        只有请求本身不成立（工作区没了）才抛
        :class:`VerificationSetupError`。
        """
        contract = request.contract
        plan = request.plan

        specs = [spec for spec in plan.checks if spec.check in contract.required]
        scans = [spec for spec in plan.scans if spec.rule in contract.forbidden]

        home = Path(tempfile.mkdtemp(prefix="ases-verify-"))
        try:
            env = build_verification_env(self.host_env, model=plan.model, home=home)

            check_results, executions = await self._run_checks(specs, request=request, env=env)
            metrics = self._read_metrics(specs, executions)
            violations, scan_artifacts, scan_traces = await self._run_scans(
                scans, request=request, env=env
            )
            artifacts = self._collect_artifacts(specs, check_results, scan_artifacts)
            traces = _check_traces(specs, executions) + scan_traces

            evidence = ContractEvidence(
                task_id=request.task_id,
                checks=check_results,
                metrics=metrics,
                violations=violations,
                produced_artifacts=artifacts,
            )
            evaluation = evaluate_contract(contract, evidence)

            return VerificationRun(
                task_id=request.task_id,
                checks=check_results,
                metrics=metrics,
                artifacts=artifacts,
                violations=violations,
                evaluation=evaluation,
                traces=traces,
                required=contract.required,
            )
        finally:
            # 临时 HOME 用完就删 —— 它只是一次性缓存目录，留着只会慢慢攒出
            # 一堆没人认领的垃圾。
            shutil.rmtree(home, ignore_errors=True)

    async def _run_checks(
        self,
        specs: Sequence[CheckSpec],
        *,
        request: VerificationRequest,
        env: Mapping[str, str],
    ) -> tuple[dict[RequiredCheck, CheckResult], dict[RequiredCheck, CheckExecution]]:
        """跑各项检查。**并发跑** —— 四项检查互不依赖，串行只是白等。

        结果按**计划里的顺序**装进字典 —— 并发完成顺序是不确定的，
        而"报告里哪一项排在前面"会被人拿来读。

        同时返回**原始执行结果**：指标要从原始输出里读，不能从折好的
        ``detail`` 里读 —— 通过的检查其 ``detail`` 是"通过"两个字，
        覆盖率根本没地方读。
        """
        if not specs:
            return {}, {}

        raw = await asyncio.gather(
            *(
                self.runner.run(
                    spec.command,
                    workspace=request.workspace,
                    env=env,
                    timeout_seconds=spec.timeout_seconds,
                )
                for spec in specs
            )
        )
        results: dict[RequiredCheck, CheckResult] = {}
        executions: dict[RequiredCheck, CheckExecution] = {}
        for spec, execution in zip(specs, raw, strict=True):
            results[spec.check] = _to_check_result(spec, execution)
            executions[spec.check] = execution
        return results, executions

    def _read_metrics(
        self,
        specs: Sequence[CheckSpec],
        executions: Mapping[RequiredCheck, CheckExecution],
    ) -> dict[str, TargetValue]:
        """读指标。

        **只从跑成了的检查里读。** 报 ``ERROR`` 的检查没有可信输出 ——
        从一段被中断的输出里正则抠出来的数字，可能是半个数字。
        """
        metrics: dict[str, TargetValue] = {}
        for spec in specs:
            if spec.metric is None:
                continue
            execution = executions.get(spec.check)
            if execution is None or not execution.completed:
                continue
            value = spec.metric.read(f"{execution.stdout}\n{execution.stderr}")
            if value is not None:
                metrics[spec.metric.name] = value
        return metrics

    async def _run_scans(
        self,
        scans: Sequence[ScanSpec],
        *,
        request: VerificationRequest,
        env: Mapping[str, str],
    ) -> tuple[frozenset[ForbiddenRule], frozenset[ArtifactKind], tuple[str, ...]]:
        """跑禁止项扫描。

        扫描**失败**（命令不存在、超时）**不算命中禁止项** —— 那是"没扫成"，
        不是"扫干净了"。把两者混起来会让一次工具缺失变成一次安全事件。
        但它也不会被当成"干净"：契约判定里，这一项既没有产物也没有结论，
        缺产物会记成 ``missing``，整体不通过。
        """
        if not scans:
            return frozenset(), frozenset(), ()

        executions = await asyncio.gather(
            *(
                self.runner.run(
                    spec.command,
                    workspace=request.workspace,
                    env=env,
                    timeout_seconds=spec.timeout_seconds,
                )
                for spec in scans
            )
        )
        violations: set[ForbiddenRule] = set()
        artifacts: set[ArtifactKind] = set()
        traces: list[str] = []
        for spec, execution in zip(scans, executions, strict=True):
            traces.append(f"{spec.rule.value}: exit={execution.exit_code}")
            if not execution.completed:
                continue
            if execution.exit_code != 0:
                violations.add(spec.rule)
            artifacts.update(spec.produces)
        return frozenset(violations), frozenset(artifacts), tuple(traces)

    def _collect_artifacts(
        self,
        specs: Sequence[CheckSpec],
        results: Mapping[RequiredCheck, CheckResult],
        scan_artifacts: frozenset[ArtifactKind],
    ) -> frozenset[ArtifactKind]:
        """收证据。

        **检查跑成了就有它的产物** —— 测试没过也会产出测试报告。所以
        "没产出"只对应"压根没跑成"，不对应"没过"。

        :attr:`ArtifactKind.EXECUTION_TRACE` 由引擎自己产出：它手里有每项
        检查的退出码与输出，那就是执行轨迹。
        """
        artifacts: set[ArtifactKind] = {ArtifactKind.EXECUTION_TRACE}
        for spec in specs:
            result = results.get(spec.check)
            if result is None or result.outcome is CheckOutcome.ERROR:
                continue
            artifacts.update(spec.produces)
        artifacts.update(scan_artifacts)
        return frozenset(artifacts)


def _check_traces(
    specs: Sequence[CheckSpec],
    executions: Mapping[RequiredCheck, CheckExecution],
) -> tuple[str, ...]:
    """执行轨迹 —— 每项检查一行。

    轨迹是**给人翻的**：哪一项跑了、退出码几、被没被杀。所以每行都带
    检查名与退出码，而不只是"成功"。
    """
    traces: list[str] = []
    for spec in specs:
        execution = executions.get(spec.check)
        if execution is None:
            continue
        traces.append(f"{spec.check.value}: exit={execution.exit_code}")
    return tuple(traces)


def _to_check_result(spec: CheckSpec, execution: CheckExecution) -> CheckResult:
    """把一次原始执行折成一项检查的结果。

    **三态在这里落地**：没起来 / 超时 / 被信号打死 → ``ERROR``；正常退出但
    非 0 → ``FAILED``；退出 0 → ``PASSED``。

    ``detail`` 里带上输出 —— 判失败时人要看的是"哪几个用例没过"，不是
    "退出码 1"。
    """
    if not execution.started:
        return CheckResult(
            spec.check,
            CheckOutcome.ERROR,
            f"检查没能启动：{execution.stderr.strip() or '未知原因'}",
        )
    if execution.timed_out:
        return CheckResult(spec.check, CheckOutcome.ERROR, execution.stderr.strip() or "检查超时")
    if execution.signalled:
        return CheckResult(
            spec.check,
            CheckOutcome.ERROR,
            f"检查被信号 {execution.exit_code} 终止 —— 没有跑完",
        )
    if execution.exit_code != 0:
        return CheckResult(
            spec.check,
            CheckOutcome.FAILED,
            _failure_detail(execution),
        )
    return CheckResult(spec.check, CheckOutcome.PASSED, "通过")


def _failure_detail(execution: CheckExecution) -> str:
    """失败详情 —— 优先取输出里真的有信息的那部分。

    先看 stdout 再看 stderr：测试框架（pytest、jest）把用例明细打在 stdout，
    而真正的错误信息常在 stderr。**两段都给** —— 只给一段，看的人得自己
    去猜另一段藏了什么。
    """
    parts: list[str] = []
    for stream in (execution.stdout, execution.stderr):
        text = stream.strip()
        if text:
            parts.append(text)
    if not parts:
        return f"退出码 {execution.exit_code}"
    return " | ".join(parts)


__all__ = [
    "DEFAULT_CHECK_TIMEOUT_SECONDS",
    "SECRET_NAME_TOKENS",
    "VERIFICATION_ENV_NAMES",
    "CheckExecution",
    "CheckRunner",
    "CheckSpec",
    "MetricSpec",
    "ScanSpec",
    "SubprocessCheckRunner",
    "VerificationEngine",
    "VerificationEnvError",
    "VerificationPlan",
    "VerificationRequest",
    "VerificationRun",
    "VerificationSetupError",
    "assert_verification_env_is_safe",
    "build_verification_env",
]
