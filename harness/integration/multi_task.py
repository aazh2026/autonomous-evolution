"""多任务集成引擎 —— 一堆任务分支汇成一条 Integration Branch。

设计依据：docs/design.md §3.5C（Task Branch → Local Validation → Integration
Branch → Cross-task Integration Test → Architecture & Regression Test → Merge
Approval → Main；"单任务通过测试，不代表多个任务集成后也通过测试"）、
docs/tasks.md TASK-P2-017（R-056 集成流水线顺序、R-057 必须做跨任务集成测试、
R-058 独立 Integrator 解决冲突并回归）、TASK-P1-047（单任务集成路径）。

**这一层解决的是"合在一起"这件事。** TASK-P1-047 的集成路径一次只走一个任务
分支；这一层一次走一批 —— 因为"合在一起才出的问题"只在合在一起时才出得来。

**三样东西要在集成阶段被挡住：**

- **文本冲突** —— git 自己会报。git 挡得住的它挡得住，挡不住的它也挡不住。
- **符号冲突** —— git **挡不住**。两个任务各自新增了同一个函数，git 合得干
  干净净，Python 也照跑不误（后一个定义把前一个盖掉），而合出来的代码已经
  不是人写的那份了。**静默的错误比报出来的错误危险。**
- **联合回归失败** —— 两边各自都绿，合在一起不绿。这是最难自己发现的一类，
  因为每个任务单独看都好好的。

**集成分支是草稿，不是产物。** 产物是 main。所以这一层任何一步没过，都要把
集成分支拨回 main 原来的位置 —— 半截的集成没有意义，还会让下一轮跑同一条
分支时被上一轮的残留污染。

**两段，不是一段。** :meth:`~MultiTaskIntegrationEngine.integrate` 把一批任务
汇到集成分支上并验过；:meth:`~MultiTaskIntegrationEngine.merge_to_main` 才把
集成分支推进 main。分成两段是因为**审批要等人** —— 等人的那段时间里，引擎
不该占着工作区，更不该把半截结果往前推。人批了之后再调第二段。
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from core.policy.approval import ApprovalOutcome, ApprovalRequest, ApprovalStatus
from core.policy.autonomy import Decision, Operation, PolicyManager, TaskContext
from core.task.outcome import VerificationVerdict
from harness.approval.gate import ApprovalGate
from harness.integration.pipeline import (
    COMMIT_PREFIX,
    MergeConflictError,
    MergeError,
    StagedMerge,
    SubprocessBranchMerger,
)
from harness.verification.contract import AcceptanceContract, ContractEvaluation
from harness.verification.engine import (
    VerificationEngine,
    VerificationPlan,
    VerificationRequest,
    VerificationRun,
)
from harness.workspace.naming import validate_task_id

#: 集成分支的前缀。**引擎只复位带这个前缀的分支** —— 别的分支不是草稿，
#: 复位它们就是在毁掉别人的东西。
INTEGRATION_BRANCH_PREFIX: Final[str] = "integration/"

#: 同一个符号要被几个任务声明才算冲突。**一个不算** —— 一个任务声明一个
#: 符号不叫冲突。
MIN_CONFLICTING_TASKS: Final[int] = 2

#: 同一份内容有多条审批记录时，选哪一条。**按"能不能用"排，不按时间排。**
#:
#: 与 ``harness.integration.pipeline`` 里那一份同义，但这里是另一件事 ——
#: 这一处判的是"这一批集成的审批"，不是"一个任务分支的审批"。
_STATUS_SCORE: Final[Mapping[ApprovalStatus, int]] = MappingProxyType(
    {
        ApprovalStatus.APPROVED: 3,
        ApprovalStatus.PENDING: 2,
        ApprovalStatus.REJECTED: 1,
        ApprovalStatus.EXPIRED: 0,
        ApprovalStatus.WITHDRAWN: 0,
    }
)


class IntegrationError(RuntimeError):
    """集成这一步没成。

    **不是"合不上去"，是"这一步工具没干成"。** 两者处置完全不同：合不上去
    是代码的事（回去改），工具没干成是环境的事（人看一眼）。
    """


class IntegrationStage(StrEnum):
    """集成停在了哪一步（第一段：汇入 Integration Branch）。"""

    #: 某个任务在自己的分支上就没验过 —— 该返工。
    LOCAL_VALIDATION_FAILED = "local_validation_failed"
    #: 某个任务的本地验证**没验成**（环境问题、进程挂了）—— 该阻塞。
    LOCAL_VALIDATION_ERRORED = "local_validation_errored"
    #: 任务分支合进集成分支时撞上文本冲突 —— 该返工。
    TASK_CONFLICTED = "task_conflicted"
    #: 两个任务定义了同一个符号 —— 该返工，而且要指得出是谁。
    SYMBOL_CONFLICTED = "symbol_conflicted"
    #: 联合回归没通过 —— 该返工。
    REGRESSION_FAILED = "regression_failed"
    #: 联合回归**没验成** —— 该阻塞。
    REGRESSION_ERRORED = "regression_errored"
    #: 汇完了，验过了，等着合进 main。
    INTEGRATED = "integrated"


class MergeStage(StrEnum):
    """合并停在了哪一步（第二段：Integration Branch → Main）。"""

    #: 在等人工审批。
    AWAITING_APPROVAL = "awaiting_approval"
    #: 审批被拒 / 过期 / 撤回 —— 阻塞。
    APPROVAL_REFUSED = "approval_refused"
    #: 集成分支合进 main 时撞上冲突 —— 该返工。
    MAIN_CONFLICTED = "main_conflicted"
    #: 这一步工具没干成 —— 该阻塞。
    MAIN_MERGE_FAILED = "main_merge_failed"
    #: main 上的联合回归没通过 —— 合并已撤销，该返工。
    MAIN_REGRESSION_FAILED = "main_regression_failed"
    #: main 上的联合回归**没验成** —— 合并已撤销，该阻塞。
    MAIN_REGRESSION_ERRORED = "main_regression_errored"
    #: 合完了。
    MERGED = "merged"


@dataclass(frozen=True, slots=True)
class ModuleSymbol:
    """一个源文件里的一个顶层定义。

    :attr:`path` 是**文件路径**，而文件路径就是模块的身份 —— 同一个模块就是
    同一个文件。
    """

    path: str
    name: str
    source: str


@dataclass(frozen=True, slots=True)
class SymbolClaim:
    """一个任务声称拥有某个符号。

    **只记"新定义"。** 改一个已有的函数不算声明 —— 那种情况归 git 的文本
    冲突管，而 git 管得住。
    """

    task_id: str
    path: str
    name: str
    source: str


@dataclass(frozen=True, slots=True)
class SymbolConflict:
    """两个任务定义了同一个符号，而且内容不一样。"""

    path: str
    name: str
    #: 涉及的任务。**要指得出是谁** —— 只说"有冲突"的话，人还得自己去查。
    tasks: tuple[str, ...]
    detail: str = ""

    def describe(self) -> str:
        """一句话说明。"""
        return f"{self.path} 里的 {self.name}：{self.detail}"


def module_symbols(path: str, source: str) -> tuple[ModuleSymbol, ...]:
    """一个源文件里定义了哪些符号。

    **只看顶层定义。** 嵌套在函数里的东西不构成命名空间冲突 —— 两个任务各
    在自己的函数里起同名局部变量，谁也碰不到谁。

    **只认 Python。** 别的语言的文件一律不解析 —— 按猜出来的规则去判一种
    语言，比不判更糟：不判只是漏报，猜错会误报，而误报会把好的一批挡下来。

    **解析不了就当没有符号。** 语法错误是别的事（编译检查会报），这一层不
    越权替它下结论。
    """
    if not path.endswith(".py"):
        return ()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return ()
    found: list[ModuleSymbol] = []
    for node in tree.body:
        name = _defined_name(node)
        if name is None:
            continue
        segment = ast.get_source_segment(source, node) or ""
        found.append(ModuleSymbol(path=path, name=name, source=_normalized(segment)))
    return tuple(found)


def find_symbol_conflicts(claims: Sequence[SymbolClaim]) -> tuple[SymbolConflict, ...]:
    """找出被两个任务同时定义的符号。

    **内容一样不算冲突。** 两个任务写了同一个定义 —— 那不是"打架"，那是
    "做了同一件事"，合出来还是那一份。这类可以自动消解，所以先在这里消掉，
    不该让它挡住合并。

    **只有两个以上任务才可能是冲突。** 一个任务声明一个符号不叫冲突。
    """
    grouped: dict[tuple[str, str], list[SymbolClaim]] = {}
    for claim in claims:
        grouped.setdefault((claim.path, claim.name), []).append(claim)
    conflicts: list[SymbolConflict] = []
    for (path, name), group in sorted(grouped.items()):
        tasks = tuple(dict.fromkeys(claim.task_id for claim in group))
        if len(tasks) < MIN_CONFLICTING_TASKS:
            continue
        if len({claim.source for claim in group}) == 1:
            continue
        detail = "任务 {} 各自定义了一份，内容不同".format("、".join(tasks))
        conflicts.append(SymbolConflict(path=path, name=name, tasks=tasks, detail=detail))
    return tuple(conflicts)


@runtime_checkable
class IntegrationGit(Protocol):
    """集成引擎需要的 git 操作。

    **端口而不是实现。** 合并的次序与"失败要撤销"要用假实现测 —— 用真 git
    测它，每条断言都要先搭一个仓库、造一次冲突，再去看 main 到底动没动。

    **"复位"这一件只对集成分支有意义。** 引擎只会复位
    ``integration/`` 前缀的分支 —— 别的分支不是草稿。
    """

    def head_commit(self, workspace: Path) -> str:
        """这个工作区当前 HEAD 的提交号。"""

    def merge_base(self, *, workspace: Path, ancestor: str, descendant: str) -> str:
        """两条线的共同祖先。"""

    def changed_files(self, *, workspace: Path, base: str, branch: str) -> tuple[str, ...]:
        """``base..branch`` 之间改过的文件（相对仓库根的路径）。"""

    def show_file(self, *, workspace: Path, ref: str, path: str) -> str | None:
        """某个版本上某个文件的内容；那个版本里没有它就返回 ``None``。"""

    def open_branch(self, *, workspace: Path, branch: str, start_point: str) -> str:
        """把集成分支拨到 ``start_point`` 并检出来，返回 HEAD。"""

    def reset_branch(self, *, workspace: Path, branch: str, target: str) -> None:
        """把集成分支拨回 ``target`` —— 失败时清场。"""

    def stage_merge(self, *, main_workspace: Path, branch: str) -> StagedMerge:
        """把 ``branch`` 暂存进 ``main_workspace``，**但不提交**。"""

    def commit_staged_merge(self, *, main_workspace: Path, message: str) -> str:
        """把暂存的合并提交掉，返回新提交的提交号。"""

    def abort_staged_merge(self, *, main_workspace: Path) -> None:
        """撤销暂存的合并。"""


@dataclass(frozen=True, slots=True)
class SubprocessIntegrationGit(SubprocessBranchMerger):
    """真的去调 ``git`` 的集成口。

    **合并那四件直接继承 TASK-P1-047 的那一份**（``SubprocessBranchMerger``）
    —— "暂存合并 / 提交 / 撤销"这几件事的语义（``--no-ff --no-commit``、不跑
    钩子、失败要撤销）已经被那份代码踩过坑了，不该再写一遍。

    **新增的那几件都是只读的或只碰集成分支。** 除了 ``open_branch`` 与
    ``reset_branch``，这个口不改任何东西 —— 而那两个只动 ``integration/``
    前缀的分支。
    """

    def open_branch(self, *, workspace: Path, branch: str, start_point: str) -> str:
        """把集成分支拨到 ``start_point`` 并检出来。

        **分支已存在就复位，不存在就新建。** 复位的理由是"每一批都从 main
        起" —— 上一批留下的东西不该悄悄混进这一批。留着旧内容看着"省事"，
        实际上是把别的批次的半成品带进了这一批。

        **``git reset --hard`` 是重操作，所以只碰集成分支。** 引擎在请求里
        就把分支名前缀钉死了（见 :data:`INTEGRATION_BRANCH_PREFIX`）。
        """
        code, _, _ = self._run(("rev-parse", "--verify", f"refs/heads/{branch}"), cwd=workspace)
        if code == 0:
            code, out, err = self._run(("checkout", branch), cwd=workspace)
            if code != 0:
                raise IntegrationError(f"检出集成分支 {branch} 失败：{_detail(out, err)}")
            code, out, err = self._run(("reset", "--hard", start_point), cwd=workspace)
        else:
            code, out, err = self._run(("checkout", "-b", branch, start_point), cwd=workspace)
        if code != 0:
            raise IntegrationError(f"准备集成分支 {branch} 失败：{_detail(out, err)}")
        return self.head_commit(workspace)

    def reset_branch(self, *, workspace: Path, branch: str, target: str) -> None:
        """把集成分支拨回 ``target``。

        **先确认工作区就在那条分支上。** ``git reset --hard`` 动的是**当前**
        分支 —— 工作区不在集成分支上时，那一下动的是别的分支。
        """
        current = self._current_branch(workspace)
        if current != branch:
            code, out, err = self._run(("checkout", branch), cwd=workspace)
            if code != 0:
                raise IntegrationError(f"检出集成分支 {branch} 失败：{_detail(out, err)}")
        code, out, err = self._run(("reset", "--hard", target), cwd=workspace)
        if code != 0:
            raise IntegrationError(f"复位集成分支 {branch} 失败：{_detail(out, err)}")

    def merge_base(self, *, workspace: Path, ancestor: str, descendant: str) -> str:
        code, out, err = self._run(("merge-base", ancestor, descendant), cwd=workspace)
        if code != 0:
            raise IntegrationError(f"求共同祖先失败：{_detail(out, err)}")
        return out.strip()

    def changed_files(self, *, workspace: Path, base: str, branch: str) -> tuple[str, ...]:
        code, out, err = self._run(("diff", "--name-only", f"{base}..{branch}"), cwd=workspace)
        if code != 0:
            raise IntegrationError(f"读改动文件列表失败：{_detail(out, err)}")
        return tuple(line.strip() for line in out.splitlines() if line.strip())

    def show_file(self, *, workspace: Path, ref: str, path: str) -> str | None:
        code, out, _err = self._run(("show", f"{ref}:{path}"), cwd=workspace)
        if code != 0:
            return None
        return out

    def _current_branch(self, workspace: Path) -> str:
        code, out, err = self._run(("rev-parse", "--abbrev-ref", "HEAD"), cwd=workspace)
        if code != 0:
            raise IntegrationError(f"读当前分支失败：{_detail(out, err)}")
        return out.strip()


@dataclass(frozen=True, slots=True)
class TaskBranch:
    """一个待集成的任务分支。

    :attr:`workspace` 是这个任务自己的工作区 —— **本地验证在那儿跑**。
    """

    task_id: str
    branch: str
    contract: AcceptanceContract
    plan: VerificationPlan
    workspace: Path

    def __post_init__(self) -> None:
        validate_task_id(self.task_id)
        if not self.branch.strip():
            raise ValueError("任务分支必须有分支名")
        if self.contract.task_id != self.task_id:
            raise ValueError(
                f"任务 {self.task_id} 的验收契约绑定的是 {self.contract.task_id} —— "
                "拿错契约，验的就不是这件事"
            )


@dataclass(frozen=True, slots=True)
class MultiTaskRequest:
    """一次多任务集成请求 —— 引擎需要的全部输入。"""

    #: 这一批的标识。联合契约绑在它身上。
    batch_id: str
    project_id: str
    agent_id: str
    #: 集成分支名。**必须带 ``integration/`` 前缀。**
    integration_branch: str
    #: 集成分支自己的工作区。**不能与 main 的工作区是同一个。**
    integration_workspace: Path
    #: main 的工作区。
    main_workspace: Path
    #: 待集成的任务分支。**顺序就是合并顺序** —— 上游在前。
    tasks: tuple[TaskBranch, ...]
    #: 联合回归用的契约。绑在 ``batch_id`` 上。
    joint_contract: AcceptanceContract
    joint_plan: VerificationPlan
    requested_by: str
    #: **人**看的改动说明。
    diff_summary: str

    def __post_init__(self) -> None:
        validate_task_id(self.batch_id)
        if not self.integration_branch.startswith(INTEGRATION_BRANCH_PREFIX):
            raise ValueError(
                f"集成分支必须带 {INTEGRATION_BRANCH_PREFIX} 前缀，"
                f"收到 {self.integration_branch!r} —— 引擎只复位带这个前缀的分支"
            )
        if self.integration_workspace == self.main_workspace:
            raise ValueError(
                "集成分支的工作区不能与 main 的工作区是同一个 —— 集成失败时要清场，"
                "清的就是那个工作区"
            )
        if not self.tasks:
            raise ValueError("一次集成至少要有一个任务分支")
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise ValueError("同一个任务不能在一批里出现两次")
        if self.joint_contract.task_id != self.batch_id:
            raise ValueError(
                f"联合契约绑定的是 {self.joint_contract.task_id}，与这一批 "
                f"{self.batch_id} 不是同一个"
            )
        if not self.requested_by.strip():
            raise ValueError("集成请求必须记下请求人")
        if not self.diff_summary.strip():
            raise ValueError("集成请求必须带改动说明 —— 人批的是他看到的那份改动")


@dataclass(frozen=True, slots=True)
class IntegrationOutcome:
    """第一段的结果 —— 停在哪、为什么停。"""

    batch_id: str
    stage: IntegrationStage
    #: 已经汇进集成分支的任务。
    integrated_tasks: tuple[str, ...] = ()
    #: 卡住的是哪个任务。
    blocking_task: str | None = None
    conflicts: tuple[SymbolConflict, ...] = ()
    verification: VerificationRun | None = None
    #: 集成分支上的提交号（只有 ``INTEGRATED`` 时有）。
    integration_commit: str | None = None
    reasons: tuple[str, ...] = ()

    @property
    def integrated(self) -> bool:
        """这一批有没有汇完并验过。"""
        return self.stage is IntegrationStage.INTEGRATED


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """第二段的结果 —— 停在哪、为什么停。"""

    batch_id: str
    stage: MergeStage
    approval_request_id: str | None = None
    merged_commit: str | None = None
    verification: VerificationRun | None = None
    reasons: tuple[str, ...] = ()

    @property
    def merged(self) -> bool:
        """有没有合进 main。"""
        return self.stage is MergeStage.MERGED


@dataclass(frozen=True, slots=True)
class MultiTaskIntegrationEngine:
    """多任务集成引擎。

    :param git: git 口。
    :param engine: 验证引擎 —— 本地验证与联合回归都用它。
    :param gate: 人工审批门。
    :param policy: 权限策略。默认是 design.md §7 的初始基线（合并需审批）。
    """

    git: IntegrationGit
    engine: VerificationEngine
    gate: ApprovalGate
    policy: PolicyManager = field(default_factory=PolicyManager.default)

    # -- 第一段：汇入 Integration Branch -----------------------------------

    async def integrate(self, request: MultiTaskRequest) -> IntegrationOutcome:
        """把一批任务分支汇到集成分支上，并验过。

        **顺序是死的：本地验证 → 汇入 → 符号检查 → 联合回归。** 每一道都在
        下一道前面，因为后一道的结论依赖前一道的前提 —— 没验过的分支不该被
        汇进来，而符号冲突要在合并之后才看得出来。

        **任何一步没过，这一批就归零。** 集成分支被拨回 main 原处，半截的
        集成不留下来。
        """
        base = self.git.head_commit(request.main_workspace)
        self.git.open_branch(
            workspace=request.integration_workspace,
            branch=request.integration_branch,
            start_point=base,
        )
        integrated: list[str] = []
        for task in request.tasks:
            local = await self._local_validation(task)
            if local is not None:
                stage = (
                    IntegrationStage.LOCAL_VALIDATION_ERRORED
                    if local.to_report().verdict is VerificationVerdict.ERROR
                    else IntegrationStage.LOCAL_VALIDATION_FAILED
                )
                return self._fail(
                    request,
                    IntegrationOutcome(
                        batch_id=request.batch_id,
                        stage=stage,
                        integrated_tasks=tuple(integrated),
                        blocking_task=task.task_id,
                        verification=local,
                        reasons=local.to_report().reasons,
                    ),
                )
            problem = self._merge_task(task, request)
            if problem is not None:
                return self._fail(
                    request,
                    IntegrationOutcome(
                        batch_id=request.batch_id,
                        stage=IntegrationStage.TASK_CONFLICTED,
                        integrated_tasks=tuple(integrated),
                        blocking_task=task.task_id,
                        reasons=(problem,),
                    ),
                )
            integrated.append(task.task_id)

        conflicts = self._symbol_conflicts(request, base=base)
        if conflicts:
            return self._fail(
                request,
                IntegrationOutcome(
                    batch_id=request.batch_id,
                    stage=IntegrationStage.SYMBOL_CONFLICTED,
                    integrated_tasks=tuple(integrated),
                    conflicts=conflicts,
                    reasons=tuple(conflict.describe() for conflict in conflicts),
                ),
            )

        joint = await self._joint_regression(request)
        verdict = joint.to_report().verdict
        if verdict is not VerificationVerdict.PASSED:
            stage = (
                IntegrationStage.REGRESSION_ERRORED
                if verdict is VerificationVerdict.ERROR
                else IntegrationStage.REGRESSION_FAILED
            )
            return self._fail(
                request,
                IntegrationOutcome(
                    batch_id=request.batch_id,
                    stage=stage,
                    integrated_tasks=tuple(integrated),
                    verification=joint,
                    reasons=joint.to_report().reasons,
                ),
            )

        return IntegrationOutcome(
            batch_id=request.batch_id,
            stage=IntegrationStage.INTEGRATED,
            integrated_tasks=tuple(integrated),
            verification=joint,
            integration_commit=self.git.head_commit(request.integration_workspace),
            reasons=("已集成到 Integration Branch，等待合并审批",),
        )

    async def _local_validation(self, task: TaskBranch) -> VerificationRun | None:
        """在任务自己的工作区上跑一遍它自己的契约。

        **没有结论就算不过。** 工作区没了、验证引擎跑不起来 —— 那不是"过了"，
        那是"没验"。这一层分三态：过了、没过、没验成。
        """
        try:
            run = await self.engine.verify(
                VerificationRequest(
                    task_id=task.task_id,
                    workspace=task.workspace,
                    contract=task.contract,
                    plan=task.plan,
                )
            )
        except Exception as error:
            return VerificationRun(
                task_id=task.task_id,
                required=task.contract.required,
                evaluation=_errored_evaluation(f"本地验证没跑成：{type(error).__name__}: {error}"),
            )
        if run.to_report().verdict is VerificationVerdict.PASSED:
            return None
        return run

    def _merge_task(self, task: TaskBranch, request: MultiTaskRequest) -> str | None:
        """把一个任务分支合进集成分支。**没问题就返回 ``None``。**

        **一个任务一个合并提交。** 分支的来路在历史里看得见 —— "合过一次"
        这件事不留痕迹的话，出事时没人知道它从哪来。

        **合不上去与合不上是两回事。** 撞上冲突是代码的事；git 自己没干成
        （仓库坏了、身份没配）是环境的事。两者的理由要分开写。
        """
        try:
            staged = self.git.stage_merge(
                main_workspace=request.integration_workspace, branch=task.branch
            )
        except MergeConflictError as error:
            return f"任务 {task.task_id} 合进集成分支时撞上文本冲突：{error}"
        except MergeError as error:
            return f"任务 {task.task_id} 合进集成分支时合并没成：{error}"
        if not staged.changed:
            return None
        message = f"集成任务 {task.task_id}（分支 {task.branch}）"
        try:
            self.git.commit_staged_merge(
                main_workspace=request.integration_workspace, message=message
            )
        except MergeError as error:
            return f"任务 {task.task_id} 的合并提交没成：{error}"
        return None

    def _symbol_conflicts(
        self, request: MultiTaskRequest, *, base: str
    ) -> tuple[SymbolConflict, ...]:
        """这一批里有没有两个任务定义了同一个符号。"""
        claims: list[SymbolClaim] = []
        for task in request.tasks:
            claims.extend(self._claims_of(task, request, base=base))
        return find_symbol_conflicts(claims)

    def _claims_of(
        self, task: TaskBranch, request: MultiTaskRequest, *, base: str
    ) -> tuple[SymbolClaim, ...]:
        """这个任务声明了哪些符号。

        **基线取的是与 main 的共同祖先，不是 main 现在这个头。** 任务是
        从某个更早的点拉出来的分支 —— 用 main 现在的头当基线，会把"任务
        开始之前就有的符号"算成"任务新加的"，那是误报。

        **只声明"新"符号。** 已有符号被改动不算声明 —— 那种情况 git 的文本
        冲突管，而且 git 管得住。
        """
        workspace = request.integration_workspace
        common = self.git.merge_base(workspace=workspace, ancestor=base, descendant=task.branch)
        claims: list[SymbolClaim] = []
        for path in self.git.changed_files(workspace=workspace, base=common, branch=task.branch):
            tip = self.git.show_file(workspace=workspace, ref=task.branch, path=path)
            if tip is None:
                continue
            before = self.git.show_file(workspace=workspace, ref=common, path=path)
            existed = {symbol.name for symbol in module_symbols(path, before or "")}
            for symbol in module_symbols(path, tip):
                if symbol.name in existed:
                    continue
                claims.append(
                    SymbolClaim(
                        task_id=task.task_id,
                        path=symbol.path,
                        name=symbol.name,
                        source=symbol.source,
                    )
                )
        return tuple(claims)

    async def _joint_regression(self, request: MultiTaskRequest) -> VerificationRun:
        """在集成分支上跑一遍联合契约。

        **这是 R-057 真正落地的地方。** 两个任务各自都绿，合在一起不绿 ——
        联合回归就是专门看这一件事的。
        """
        return await self.engine.verify(
            VerificationRequest(
                task_id=request.batch_id,
                workspace=request.integration_workspace,
                contract=request.joint_contract,
                plan=request.joint_plan,
            )
        )

    def _fail(self, request: MultiTaskRequest, outcome: IntegrationOutcome) -> IntegrationOutcome:
        """这一批没成 —— 集成分支拨回原处，然后报出为什么。"""
        cleanup = self._clear(request)
        return IntegrationOutcome(
            batch_id=outcome.batch_id,
            stage=outcome.stage,
            integrated_tasks=outcome.integrated_tasks,
            blocking_task=outcome.blocking_task,
            conflicts=outcome.conflicts,
            verification=outcome.verification,
            reasons=outcome.reasons + cleanup,
        )

    def _clear(self, request: MultiTaskRequest) -> tuple[str, ...]:
        """把集成分支拨回 main —— 失败不留半截。

        **清场失败不许盖掉原来的结论。** 集成分支收不干净是运维的事，这一批
        为什么没过是另一回事 —— 后者才是调用方要拿去做决定的。
        """
        try:
            base = self.git.head_commit(request.main_workspace)
            self.git.reset_branch(
                workspace=request.integration_workspace,
                branch=request.integration_branch,
                target=base,
            )
        except Exception as error:
            return (f"集成分支清场失败：{type(error).__name__}: {error}",)
        return ()

    # -- 第二段：Integration Branch → Main --------------------------------

    async def merge_to_main(
        self, request: MultiTaskRequest, *, integration_commit: str
    ) -> MergeOutcome:
        """把集成分支推进 main。

        **审批绑在集成分支的那个提交上。** 人批的是他看到的那份内容 —— 集成分
        支变了，批准就不再成立。

        **审批要等人，所以这一段也只推一步。** 停在 ``AWAITING_APPROVAL`` 时
        编排层去等信号，人表态之后再调一次这一段。
        """
        approval = await self._approval_for(request, integration_commit)
        decision = await self._decide(request, approval)
        if decision.needs_approval:
            return MergeOutcome(
                batch_id=request.batch_id,
                stage=MergeStage.AWAITING_APPROVAL,
                approval_request_id=approval.request_id,
                reasons=(decision.reason,),
            )
        if not decision.is_allowed:
            return MergeOutcome(
                batch_id=request.batch_id,
                stage=MergeStage.APPROVAL_REFUSED,
                approval_request_id=approval.request_id,
                reasons=(decision.reason,),
            )
        return await self._merge(request, integration_commit=integration_commit)

    async def _merge(self, request: MultiTaskRequest, *, integration_commit: str) -> MergeOutcome:
        """暂存合并 → 在 main 上复验 → 提交。

        **先暂存再验证。** 与 TASK-P1-047 同一套规矩：合并结果停在索引与工作
        区里，在 main 上复验通过了才提交。main 一个提交都没有前进过，直到
        验过为止。
        """
        try:
            staged = self.git.stage_merge(
                main_workspace=request.main_workspace, branch=request.integration_branch
            )
        except MergeConflictError as error:
            return MergeOutcome(
                batch_id=request.batch_id,
                stage=MergeStage.MAIN_CONFLICTED,
                reasons=(f"集成分支合进 main 时撞上冲突：{error}",),
            )
        except MergeError as error:
            return MergeOutcome(
                batch_id=request.batch_id,
                stage=MergeStage.MAIN_MERGE_FAILED,
                reasons=(f"集成分支合进 main 时合并没成：{error}",),
            )
        if not staged.changed:
            return MergeOutcome(
                batch_id=request.batch_id,
                stage=MergeStage.MERGED,
                merged_commit=self.git.head_commit(request.main_workspace),
                reasons=("集成分支的内容已经在 main 里",),
            )

        run = await self.engine.verify(
            VerificationRequest(
                task_id=request.batch_id,
                workspace=request.main_workspace,
                contract=request.joint_contract,
                plan=request.joint_plan,
            )
        )
        verdict = run.to_report().verdict
        if verdict is not VerificationVerdict.PASSED:
            aborted = self._abort(request)
            stage = (
                MergeStage.MAIN_REGRESSION_ERRORED
                if verdict is VerificationVerdict.ERROR
                else MergeStage.MAIN_REGRESSION_FAILED
            )
            reasons = tuple(run.to_report().reasons) + aborted
            return MergeOutcome(
                batch_id=request.batch_id,
                stage=stage,
                verification=run,
                reasons=reasons,
            )

        message = request.diff_summary.strip() or (
            f"集成批次 {request.batch_id}（集成分支提交 {integration_commit[:12]}）"
        )
        try:
            commit = self.git.commit_staged_merge(
                main_workspace=request.main_workspace, message=message
            )
        except MergeError as error:
            return MergeOutcome(
                batch_id=request.batch_id,
                stage=MergeStage.MAIN_MERGE_FAILED,
                reasons=(f"合并提交没成：{error}",),
            )
        return MergeOutcome(
            batch_id=request.batch_id,
            stage=MergeStage.MERGED,
            merged_commit=commit,
            verification=run,
            reasons=("已合进 main",),
        )

    def _abort(self, request: MultiTaskRequest) -> tuple[str, ...]:
        """撤销暂存的合并。

        **撤销失败必须报出来。** 撤销不掉意味着 main 停在"合并进行中" ——
        那比这次合并失败严重得多，不能被一句"合并失败"盖过去。
        """
        try:
            self.git.abort_staged_merge(main_workspace=request.main_workspace)
        except MergeError as error:
            return (f"合并已撤销失败 —— main 仍停在合并进行中，需要人工处理：{error}",)
        return ("合并已撤销，main 没有前进",)

    async def _approval_for(self, request: MultiTaskRequest, head: str) -> ApprovalRequest:
        """拿到这次要用的审批请求。

        **复用的前提是"它批的就是现在这个提交"。** 覆盖不到当前提交的请求
        不能拿来放行 —— 集成分支变了，人当初看到的那份内容就不是现在这份了。
        """
        rows = await self.gate.store.list_for_task(request.batch_id)
        covering = [
            row
            for row in rows
            if row.operation == Operation.MERGE_TO_MAIN.value and _covers(row, head)
        ]
        if covering:
            best = max(
                covering,
                key=lambda row: (
                    _STATUS_SCORE[row.status],
                    row.requested_at,
                    row.request_id,
                ),
            )
            if best.status is not ApprovalStatus.EXPIRED and best.status is not (
                ApprovalStatus.WITHDRAWN
            ):
                return best
        return await self.gate.request(
            task_id=request.batch_id,
            operation=Operation.MERGE_TO_MAIN,
            diff_summary=f"{COMMIT_PREFIX}{head}\n{request.diff_summary.strip()}",
            evidence_ref=f"joint-regression:{request.batch_id}@{head}",
            requested_by=request.requested_by,
        )

    async def _decide(self, request: MultiTaskRequest, approval: ApprovalRequest) -> Decision:
        """问策略层：这次合并能不能做。

        **判据是"这一条请求"。** 判定落在具体那条上，而不是"这个任务最近
        的一条" —— 后者一变结论就跟着变，而人批的东西没变。
        """
        return await self.policy.authorize(
            Operation.MERGE_TO_MAIN,
            TaskContext(
                agent_id=request.agent_id,
                task_id=request.batch_id,
                project_id=request.project_id,
            ),
            approval=_BoundChecker(self.gate, approval.request_id),
        )


@dataclass(frozen=True, slots=True)
class _BoundChecker:
    """只认**这一条**请求的审批查询口。"""

    gate: ApprovalGate
    request_id: str

    async def approval_outcome(self, *, task_id: str, operation: Operation) -> ApprovalOutcome:
        try:
            verdict = await self.gate.verdict_for(self.request_id)
        except Exception:
            return ApprovalOutcome.MISSING
        request = verdict.request
        if request is None or request.task_id != task_id or request.operation != operation.value:
            return ApprovalOutcome.MISSING
        return verdict.outcome


def _defined_name(node: ast.stmt) -> str | None:
    """这个顶层语句定义了什么名字。"""
    if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef)):
        return node.name
    if isinstance(node, ast.Assign):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id
        return None
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _normalized(source: str) -> str:
    """把源码压成一行 —— **只为了比"是不是同一份"**。

    两个任务写了同一个定义但缩进不同，那还是同一个定义。用原文比会把它
    判成冲突，然后白白挡下一批好活。
    """
    return " ".join(source.split())


def _covers(request: ApprovalRequest, head: str) -> bool:
    """这条审批批的是不是当前这个提交。

    **只认自己写的那种摘要。** 没有 ``commit:`` 行的请求不算覆盖任何提交 ——
    认不出批的是哪一份内容，就不能拿它放行。
    """
    first_line, _, _ = request.diff_summary.partition("\n")
    if not first_line.startswith(COMMIT_PREFIX):
        return False
    return first_line[len(COMMIT_PREFIX) :].strip() == head


def _errored_evaluation(reason: str) -> ContractEvaluation:
    """一个"没验成"的契约判定。

    **结论是"没得出结论"，不是"没通过"。** 两者的处置不同：前者该重跑或
    让人看一眼，后者该返工。
    """
    return ContractEvaluation(passed=False, failures=(reason,), missing=())


def _detail(out: str, err: str) -> str:
    """git 报错时给一句人话。"""
    return err.strip() or out.strip()


__all__ = [
    "INTEGRATION_BRANCH_PREFIX",
    "IntegrationError",
    "IntegrationGit",
    "IntegrationOutcome",
    "IntegrationStage",
    "MergeOutcome",
    "MergeStage",
    "ModuleSymbol",
    "MultiTaskIntegrationEngine",
    "MultiTaskRequest",
    "SubprocessIntegrationGit",
    "SymbolClaim",
    "SymbolConflict",
    "TaskBranch",
    "find_symbol_conflicts",
    "module_symbols",
]
