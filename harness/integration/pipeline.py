"""最小集成路径 —— 单任务从任务分支走到 main。

设计依据：docs/design.md §3.5C（Task Branch → Local Validation → Integration
Branch → … → Merge Approval → Main；"单任务通过测试，不代表多个任务集成后也
通过测试"）、docs/tasks.md TASK-P1-047（R-056 集成流水线顺序）、TASK-P1-046
（人工审批门）、待裁决 I-12（审批应是信号/事件而非轮询）。

**这条路径只走一件事：把一个任务分支合进 main，并且保证合进去的东西是好的。**

阶段一的边界（任务书写得很清楚）：**只做单任务集成路径**。跨任务集成、
Integration Branch 与冲突自动求解留阶段二（TASK-P2-017 / TASK-P2-030）——
所以本模块里没有"集成队列"、没有"多任务按依赖顺序排队合并"。那些要等
"合并的顺序本身也需要被验证"这件事有答案之后再说。

**六个阶段，每一步都能单独停住：**

    ① 任务分支上的本地验证   ── 不过 → 停（返工 / 阻塞）
    ② 发起或复用审批请求     ── 只有 ① 过了才有这一步
    ③ 策略层判定能不能合并   ── 还在等人 → 停（等审批）
    ④ 暂存合并（不提交）      ── 冲突 → 撤销合并，停（返工）
    ⑤ 在 main 上再验一次      ── 不过 → 撤销合并，停（返工）
    ⑥ 提交合并

**为什么 ④⑤ 要"先暂存再验证"。** 这是本模块最要紧的一处设计。

朴素的做法是"先合并，再在 main 上验"。那样的话，合并之后 main 可能是坏的，
而"要不要回滚"这件事就落到了人身上 —— 而人未必知道 main 刚被合坏了。
R-057 说的"单任务通过 ≠ 集成后通过"正是这个：任务分支上验过了，合进 main
之后可能就不过（main 上还有别的改动，两边合起来可能就不对了）。

所以这里用 git 自己的机制把这件事做对：**``git merge --no-ff --no-commit``**
把合并结果放进索引与工作区但**不提交**。此时 main 的工作区里已经是"合并后
的内容"，可以在上面跑验证；通过了才 ``git commit``，不通过就 ``git merge
--abort`` —— **main 一个提交都没有前进过**。合并是原子的：要么整份进来，
要么一点没进来。

**审批绑在具体的提交上。** 人批的是"他看到的那份改动" —— 分支头变了，那份
批准就不再成立。所以审批请求的 diff 摘要第一行写明被批准的提交号
（``commit:<sha>``），路径只认覆盖当前提交的那条请求。分支在等人期间又动了，
路径会**撤回**那条请求（I-12 的撤回语义：diff 改了，这条请求作废，重新提）
并发起一条新的 —— 于是"批的是哪一份代码"这件事不会因为等待而失效。

**审批之前不动 main。** design.md §3.5C 的顺序是"… → Merge Approval → Main"，
而阶段一没有 Integration Branch，所以"集成后的内容"只能落在 main 的工作区
里。如果先暂存再等人，main 会在人吃饭的这几个小时里一直停在"合并进行中" ——
而这正是要避免的：**main 的工作区在等人期间必须保持干净**。所以这里的顺序
是"审批 → 暂存 → 验证 → 提交"。代价是：人批的是任务分支上的那一份改动，
"合进 main 之后还过不过"是在他批完之后才验的 —— 验不过就撤销合并、main 不
动、任务回去返工。阶段二有了 Integration Branch 之后，这个顺序会回到
design.md 写的那样（审批在跨任务回归之后），因为那时候"脏"的是专用分支而
不是 main。

**路径推一步，不跑到底。** 审批要等人，而等待属于编排层（阶段三是 Temporal
Signal）。停在 ``AWAITING_APPROVAL`` 时编排层去等信号，人表态之后再调一次
:meth:`IntegrationPipeline.run`。这样"等待"的形状从第一天起就是信号的形状
（I-12），而不是一个藏在函数里的轮询循环。

**阶段一不做的事。** 不做跨任务依赖调度、不做冲突自动求解、不做发布与回滚、
不做定时触发与夜间批量。本模块只保证一件事：**一个任务分支进 main 的这条
路，坏的东西进不去。**
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from core.policy.approval import ApprovalOutcome, ApprovalRequest, ApprovalStatus
from core.policy.autonomy import Decision, Operation, PolicyManager, TaskContext
from core.task.outcome import VerificationVerdict
from core.task.state_machine import TaskAction, TaskState, TransitionContext, apply_action
from harness.approval.gate import ApprovalGate
from harness.verification.contract import AcceptanceContract
from harness.verification.engine import (
    VerificationEngine,
    VerificationPlan,
    VerificationRequest,
    VerificationRun,
)

#: 审批请求 diff 摘要的**第一行**前缀 —— 机器可读的提交号。
#:
#: 摘要分两行：第一行 ``commit:<sha>`` 是给路径自己看的，下面是给人看的
#: 说明。人看到的摘要不能被机器可读的那一行挤掉，所以提交号单独一行。
COMMIT_PREFIX: Final[str] = "commit:"

#: 同一份内容有多条审批记录时，选哪一条。
#:
#: **按"能不能用"排，不按时间排。** 按时间排会把"人拒绝了"排在"人批准了"
#: 后面 —— 后发起的请求时间更晚 —— 而那正好是最不该发生的一种排序。同一
#: 份内容被拒了，就该停在被拒那里，不该因为后来又发了一条就被冲掉。
_STATUS_SCORE: Final[Mapping[ApprovalStatus, int]] = MappingProxyType(
    {
        ApprovalStatus.APPROVED: 3,
        ApprovalStatus.PENDING: 2,
        ApprovalStatus.REJECTED: 1,
        ApprovalStatus.EXPIRED: 0,
        ApprovalStatus.WITHDRAWN: 0,
    }
)


class MergeError(RuntimeError):
    """合并这一步没成。

    **不是"合并被拒"，是"合并没能完成"。** 两者的处置完全不同：被拒是人的
    决定（任务回去返工），没成是工具或环境的事（任务阻塞，让人看一眼）。
    """


class MergeConflictError(MergeError):
    """合并撞上了冲突。"""


class PipelineStage(StrEnum):
    """路径停在了哪一步。"""

    #: 本地验证没通过 —— 该返工。
    LOCAL_VALIDATION_FAILED = "local_validation_failed"
    #: 本地验证**没验成**（环境问题、进程挂了）—— 该阻塞，不该返工。
    LOCAL_VALIDATION_ERRORED = "local_validation_errored"
    #: 在等人工审批。
    AWAITING_APPROVAL = "awaiting_approval"
    #: 审批被拒 / 过期 / 撤回 —— 任务阻塞。
    APPROVAL_REFUSED = "approval_refused"
    #: 合并撞上冲突 —— 合并已撤销，该返工。
    MERGE_CONFLICTED = "merge_conflicted"
    #: 合并这一步工具没干成 —— 合并已撤销，该阻塞。
    MERGE_FAILED = "merge_failed"
    #: 合并后的内容没通过复验 —— 合并已撤销，该返工。
    MERGED_CONTENT_FAILED = "merged_content_failed"
    #: 合并后的内容**没验成** —— 合并已撤销，该阻塞。
    MERGED_CONTENT_ERRORED = "merged_content_errored"
    #: 合完了。
    MERGED = "merged"


@dataclass(frozen=True, slots=True)
class StagedMerge:
    """一次已暂存、尚未提交的合并。

    :attr:`changed` 为 ``False`` 表示**没什么可合的** —— 分支的内容已经在
    main 里了。那不是错误，但也谈不上"合了一次"。
    """

    changed: bool
    detail: str = ""


@runtime_checkable
class BranchMerger(Protocol):
    """把任务分支合进 main 需要的那些 git 操作。

    **端口而不是实现。** 合并的语义（暂存、提交、撤销）要用假实现测 —— 比如
    "暂存成功但复验不过，必须撤销"这条规则，用真 git 测要搭一个仓库，而用假
    实现测只要记一笔调用序列。
    """

    def head_commit(self, workspace: Path) -> str:
        """这个工作区当前 HEAD 的提交号。"""

    def stage_merge(self, *, main_workspace: Path, branch: str) -> StagedMerge:
        """把 ``branch`` 暂存进 ``main_workspace``，**但不提交**。"""

    def commit_staged_merge(self, *, main_workspace: Path, message: str) -> str:
        """把暂存的合并提交掉，返回新提交的提交号。"""

    def abort_staged_merge(self, *, main_workspace: Path) -> None:
        """撤销暂存的合并，让 main 回到合并前的样子。"""


@dataclass(frozen=True, slots=True)
class SubprocessBranchMerger:
    """真的去调 ``git`` 的合并口。

    **只做 git 真正支持的事，而且只做 git 真正需要的四件。** 别的 git 操作
    （建分支、切分支、查日志）不在这个口的职责里 —— 它们属于工作空间管理
    （:mod:`harness.workspace.git`）。这里多一个方法，就多一处"合并口到底
    还能干多少别的事"的糊涂。
    """

    git_binary: str = "git"
    timeout_seconds: int = 120

    def _run(self, args: tuple[str, ...], *, cwd: Path) -> tuple[int, str, str]:
        """跑一条 git 命令，返回 ``(退出码, 标准输出, 标准错误)``。

        **不抛错，返回退出码。** 合并这一步需要看失败长什么样 —— 冲突与
        "工作区不干净"都是非零退出码，但处置完全不同。这里抛出去的话，两种
        情况就都被压成了同一个异常。
        """
        command = (self.git_binary, *args)
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise MergeError(
                f"git 命令超时（{self.timeout_seconds}s）：{' '.join(command)}"
            ) from exc
        return proc.returncode, proc.stdout, proc.stderr

    def head_commit(self, workspace: Path) -> str:
        code, out, err = self._run(("rev-parse", "HEAD"), cwd=workspace)
        if code != 0:
            raise MergeError(f"读 HEAD 失败：{err.strip() or out.strip()}")
        return out.strip()

    def stage_merge(self, *, main_workspace: Path, branch: str) -> StagedMerge:
        """``git merge --no-ff --no-commit <branch>``。

        **``--no-commit`` 是这一整段设计的支点。** 没有它，合并会当场提交，
        main 就前进到了还没验过的内容上；有了它，合并结果停在索引与工作区
        里，可以先验再提交。

        ``--no-ff`` 保证合并一定产生一个合并提交 —— 哪怕分支是 main 的直系
        后代。没有它，"合了一次"这件事在历史里可能什么都不剩。
        """
        code, out, err = self._run(("merge", "--no-ff", "--no-commit", branch), cwd=main_workspace)
        if code != 0:
            detail = err.strip() or out.strip()
            # 冲突时合并是**半完成**的：MERGE_HEAD 已经写下了，索引里也留着
            # 冲突标记。不撤销的话，main 会一直停在"合并进行中"。
            self.abort_staged_merge(main_workspace=main_workspace)
            if "CONFLICT" in out or "CONFLICT" in err:
                raise MergeConflictError(f"合并 {branch} 撞上冲突：{detail}")
            raise MergeError(f"合并 {branch} 失败：{detail}")
        if self._merge_in_progress(main_workspace):
            return StagedMerge(changed=True, detail=out.strip())
        return StagedMerge(changed=False, detail=out.strip() or "分支内容已经在 main 里")

    def commit_staged_merge(self, *, main_workspace: Path, message: str) -> str:
        """``git commit`` 把暂存的合并提交掉。

        **不跑任何钩子（``-c core.hooksPath=/dev/null``）。** 钩子可以**改
        文件** —— 而合并进来的内容必须与验证时逐字一致。钩子改过的东西没有
        被验过，那么"验过的就是合进去的"这条就不成立了。要卡质量，走验收
        契约里的 ``required`` 检查 —— 那些已经在验证阶段跑过了。

        **为什么不是 ``--no-verify``：** 那个开关只跳过 ``pre-commit`` 与
        ``commit-msg``，``prepare-commit-msg`` 照样跑。承诺"不跑钩子"却只
        挡住一半，剩下那一半迟早有人踩 —— 而踩到的时候没人会想到跟钩子
        有关。``core.hooksPath`` 指到 ``/dev/null`` 才是真的一个都不跑。

        提交失败（例如仓库没配 git 身份）同样要撤销合并：不能把 main 留在
        "合并进行中"的状态里，那比失败本身更糟。
        """
        code, out, err = self._run(
            ("-c", "core.hooksPath=/dev/null", "commit", "-m", message), cwd=main_workspace
        )
        if code != 0:
            detail = err.strip() or out.strip()
            self.abort_staged_merge(main_workspace=main_workspace)
            raise MergeError(f"提交合并失败：{detail}")
        return self.head_commit(main_workspace)

    def abort_staged_merge(self, *, main_workspace: Path) -> None:
        """``git merge --abort`` —— 撤销暂存的合并。

        **没有合并在进行时，失败是可以接受的** —— 撤销一个不存在的合并，
        不该是一件事。真正的不行是"要撤销却撤销不掉"，那要报出来。
        """
        code, out, err = self._run(("merge", "--abort"), cwd=main_workspace)
        if code != 0 and "no merge" not in (out + err).lower():
            raise MergeError(f"撤销合并失败：{err.strip() or out.strip()}")

    def _merge_in_progress(self, workspace: Path) -> bool:
        """当前工作区里有没有一个进行中的合并。

        靠 ``MERGE_HEAD`` 判断 —— 那是 git 自己写的"我正在合并"标记，
        比解析命令输出可靠。
        """
        code, _, _ = self._run(("rev-parse", "-q", "--verify", "MERGE_HEAD"), cwd=workspace)
        return code == 0


@dataclass(frozen=True, slots=True)
class PipelineRequest:
    """一次集成请求 —— 路径需要的全部输入。

    **没有"要不要合并"这个开关。** 路径的意义就是合并；要合并就得给齐这几
    样东西。少一样就说明调用方还没想清楚要合什么。
    """

    task_id: str
    agent_id: str
    project_id: str
    branch: str
    task_workspace: Path
    main_workspace: Path
    contract: AcceptanceContract
    plan: VerificationPlan
    #: **人**看的改动说明。提交号由路径自己写进摘要的第一行。
    diff_summary: str
    requested_by: str
    #: 验证证据引用。留空时用默认的"这次提交上的本地验证"。
    evidence_ref: str = ""
    #: 任务现在停在哪。阶段一只接受 ``VERIFYING`` 与 ``PASSED``。
    task_state: TaskState = TaskState.VERIFYING
    #: 合并提交的信息。留空时用一句默认的。
    merge_message: str = ""
    #: 要授权的操作。默认是合并到 main。
    operation: Operation = Operation.MERGE_TO_MAIN

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("集成请求必须带任务 id")
        if not self.branch.strip():
            raise ValueError("集成请求必须带分支名")
        if not self.diff_summary.strip():
            raise ValueError("集成请求必须带改动说明 —— 人批的是他看到的那份改动")
        if not self.requested_by.strip():
            raise ValueError("集成请求必须记下请求人")
        if self.contract.task_id != self.task_id:
            raise ValueError(
                f"验收契约绑定的是任务 {self.contract.task_id}，与请求里的 "
                f"{self.task_id} 不是同一个 —— 拿错契约，验的就不是这件事"
            )
        if self.task_state not in (TaskState.VERIFYING, TaskState.PASSED):
            raise ValueError(f"集成路径从 VERIFYING 或 PASSED 起步，收到 {self.task_state.value}")


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """一次推进的结果 —— 停在哪、为什么停。"""

    task_id: str
    stage: PipelineStage
    task_state: TaskState
    #: 任务分支上的本地验证结果。
    local_verification: VerificationRun | None = None
    #: 合并后 main 上的复验结果。
    main_verification: VerificationRun | None = None
    #: 本次用的那条审批请求。
    approval_request_id: str | None = None
    #: 策略层的判定结果。
    decision: Decision | None = None
    #: 合并提交号（只有 ``MERGED`` 时有）。
    merged_commit: str | None = None
    #: 被批准的提交号。
    head_commit: str = ""
    #: 停下来的理由。
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Approved:
    """已经拿到批准的这次推进。"""

    head: str
    approval: ApprovalRequest
    decision: Decision


@dataclass(frozen=True, slots=True)
class IntegrationPipeline:
    """最小集成路径。

    :param engine: 验证引擎 —— 本地验证与合并后的复验都用它。
    :param gate: 人工审批门。
    :param merger: git 合并口。
    :param policy: 权限策略。默认是 design.md §7 的初始基线（合并需审批）。
    """

    engine: VerificationEngine
    gate: ApprovalGate
    merger: BranchMerger
    policy: PolicyManager = field(default_factory=PolicyManager.default)

    async def run(self, request: PipelineRequest) -> PipelineResult:
        """把这条路径往前推一步。

        **推一步，不是跑到底。** 审批要等人 —— 路径不会在这里阻塞着等。停在
        ``AWAITING_APPROVAL`` 时，编排层去等信号（阶段三是 Temporal Signal），
        人表态之后再调一次 :meth:`run`。
        """
        head = self.merger.head_commit(request.task_workspace)

        local = await self._verify(request, workspace=request.task_workspace)
        verdict = local.to_report().verdict
        if verdict is not VerificationVerdict.PASSED:
            return self._validation_failed(request, local, verdict)

        state = self._advance(request, request.task_state, TaskAction.PASS)
        approval = await self._approval_for(request, head)
        decision = await self._decide(request, approval)
        approved = _Approved(head=head, approval=approval, decision=decision)
        if decision.needs_approval:
            return self._awaiting(request, state, approved)
        if not decision.is_allowed:
            return self._refused(request, state, approved)
        state = self._advance(request, state, TaskAction.INTEGRATE)
        return await self._merge(request, state, approved)

    # -- ① 本地验证 --------------------------------------------------------

    async def _verify(self, request: PipelineRequest, *, workspace: Path) -> VerificationRun:
        """在某个工作区上跑一遍验收契约。"""
        return await self.engine.verify(
            VerificationRequest(
                task_id=request.task_id,
                workspace=workspace,
                contract=request.contract,
                plan=request.plan,
            )
        )

    def _validation_failed(
        self, request: PipelineRequest, local: VerificationRun, verdict: VerificationVerdict
    ) -> PipelineResult:
        """本地验证没过 —— 停。

        **没验成与验了没过，走向不同。** 验了没过是代码不对 → 返工；没验成
        （进程挂了、装不上依赖）是环境不对 → 阻塞。去修一个没坏的东西，
        比什么都不做更糟。
        """
        errored = verdict is VerificationVerdict.ERROR
        # 从 PASSED 起步时状态机没有 PASSED→REPAIRING 这条边 —— 复验没过说明
        # "当时通过的那份计划已经不成立"，走 REPLAN 回去重做，而不是硬造
        # 一条边出来。
        if errored:
            action = TaskAction.BLOCK
        elif request.task_state is TaskState.VERIFYING:
            action = TaskAction.REPAIR
        else:
            action = TaskAction.REPLAN
        state = self._advance(request, request.task_state, action)
        return PipelineResult(
            task_id=request.task_id,
            stage=(
                PipelineStage.LOCAL_VALIDATION_ERRORED
                if errored
                else PipelineStage.LOCAL_VALIDATION_FAILED
            ),
            task_state=state,
            local_verification=local,
            reasons=tuple(local.to_report().reasons),
        )

    # -- ②③ 审批 ----------------------------------------------------------

    async def _approval_for(self, request: PipelineRequest, head: str) -> ApprovalRequest:
        """拿到这次要用的审批请求。

        **复用的前提是"它批的就是现在这个提交"。** 覆盖不到当前提交的请求
        不能拿来放行 —— 分支头变了，人当初看到的那份改动就不是现在这份了。

        三种情况：

        - 有覆盖当前提交、且状态可用的请求（已批准 / 等待中）→ 复用；
        - 有覆盖当前提交、且被拒的请求 → 复用（并因此停下）。**不重提同一
          份内容**：人已经说过不，机器再提一次只是刷屏；
        - 覆盖不到当前提交 → **撤回**还挂着的旧请求（diff 改了，它作废了），
          重新发起一条。
        """
        covering = [
            candidate for candidate in await self._requests_for(request) if _covers(candidate, head)
        ]
        if covering:
            best = max(
                covering,
                key=lambda candidate: (
                    _STATUS_SCORE[candidate.status],
                    _epoch(candidate.requested_at),
                    candidate.request_id,
                ),
            )
            if best.status is not ApprovalStatus.EXPIRED and best.status is not (
                ApprovalStatus.WITHDRAWN
            ):
                return best

        await self._withdraw_stale(request, head)
        return await self.gate.request(
            task_id=request.task_id,
            operation=request.operation,
            diff_summary=_compose_diff_summary(head, request.diff_summary),
            evidence_ref=self._evidence_ref(request, head),
            requested_by=request.requested_by,
        )

    async def _requests_for(self, request: PipelineRequest) -> tuple[ApprovalRequest, ...]:
        """这个任务在这个操作上的全部审批请求。

        **取全部而不是"最近一条"。** "最近"是个有歧义的排序 —— 两条请求
        时间戳相同时，谁更新取决于 id 的大小，那不是"最近"。这里按"覆盖哪
        个提交 + 能不能用"来选，与时间无关。
        """
        rows = await self.gate.store.list_for_task(request.task_id)
        return tuple(row for row in rows if row.operation == request.operation.value)

    async def _withdraw_stale(self, request: PipelineRequest, head: str) -> None:
        """把覆盖不到当前提交、还挂着的请求撤掉。

        **撤回正是为这件事准备的**（I-12）：diff 改了，这条请求作废，重新
        提一条。留着它的话，同一个任务上会同时挂两张未决的审批单 —— 而
        "一个任务同时有两张未决的审批单，说明流程出了岔子"。
        """
        for candidate in await self._requests_for(request):
            if candidate.status is not ApprovalStatus.PENDING:
                continue
            if _covers(candidate, head):
                continue
            await self.gate.withdraw(
                request_id=candidate.request_id,
                by=request.requested_by,
                comment=f"任务分支已变（当前提交 {head}），这份请求作废，重新提",
            )

    def _evidence_ref(self, request: PipelineRequest, head: str) -> str:
        """这次审批的验证证据引用。

        默认指向"当前提交上的那次本地验证"。**它是一个引用，不是产物** ——
        真正的产物落库与取回是 TASK-P1-048 的事；在那之前，这里给的是
        "哪一次验证"而不是"哪一份文件"。
        """
        if request.evidence_ref.strip():
            return request.evidence_ref
        return f"local-verification:{request.task_id}@{head}"

    async def _decide(self, request: PipelineRequest, approval: ApprovalRequest) -> Decision:
        """问策略层：这次合并能不能做。

        **判据是"这一条请求"，不是"这个任务最近的一条请求"。** 路径批的是
        具体的某个提交，判定也得落在同一条上 —— 否则"最近那条"一变，结论
        就跟着变，而人批的东西没变。
        """
        return await self.policy.authorize(
            request.operation,
            TaskContext(
                agent_id=request.agent_id,
                task_id=request.task_id,
                project_id=request.project_id,
            ),
            approval=_RequestBoundChecker(self.gate, approval.request_id),
        )

    def _awaiting(
        self, request: PipelineRequest, state: TaskState, approved: _Approved
    ) -> PipelineResult:
        """在等人工审批 —— 停。

        **任务留在 ``PASSED``。** 代码验过了，还没进 main —— "通过了但没合"
        就是这个状态。往 ``INTEGRATING`` 走是等到真的要开始合并的时候。
        """
        return PipelineResult(
            task_id=request.task_id,
            stage=PipelineStage.AWAITING_APPROVAL,
            task_state=state,
            approval_request_id=approved.approval.request_id,
            decision=approved.decision,
            head_commit=approved.head,
            reasons=(approved.decision.reason,),
        )

    def _refused(
        self, request: PipelineRequest, state: TaskState, approved: _Approved
    ) -> PipelineResult:
        """审批没过 —— 停，任务阻塞。

        **拒绝、过期、撤回都落在这里，但理由各不相同** —— 判定结果里带着
        ``approval_outcome``，调用方据此能分出"人明确说不"与"请求作废了"。
        """
        return PipelineResult(
            task_id=request.task_id,
            stage=PipelineStage.APPROVAL_REFUSED,
            task_state=self._advance(request, state, TaskAction.BLOCK),
            approval_request_id=approved.approval.request_id,
            decision=approved.decision,
            head_commit=approved.head,
            reasons=(approved.decision.reason,),
        )

    # -- ④⑤⑥ 合并 --------------------------------------------------------

    async def _merge(
        self, request: PipelineRequest, state: TaskState, approved: _Approved
    ) -> PipelineResult:
        """暂存合并 → 在 main 上复验 → 提交。"""
        try:
            staged = self.merger.stage_merge(
                main_workspace=request.main_workspace, branch=request.branch
            )
        except MergeConflictError as error:
            return self._merge_conflicted(request, state, approved, error)
        except MergeError as error:
            return self._merge_failed(request, state, approved, error)

        if not staged.changed:
            return self._already_merged(request, state, approved)

        main_run = await self._verify(request, workspace=request.main_workspace)
        main_verdict = main_run.to_report().verdict
        if main_verdict is not VerificationVerdict.PASSED:
            return await self._merged_content_failed(
                request, state, approved, main_run, main_verdict
            )

        try:
            commit = self.merger.commit_staged_merge(
                main_workspace=request.main_workspace,
                message=self._merge_message(request),
            )
        except MergeError as error:
            return self._merge_failed(request, state, approved, error)

        merged_state = self._advance(request, state, TaskAction.MERGE)
        return PipelineResult(
            task_id=request.task_id,
            stage=PipelineStage.MERGED,
            task_state=self._advance(request, merged_state, TaskAction.COMPLETE),
            local_verification=None,
            main_verification=main_run,
            approval_request_id=approved.approval.request_id,
            decision=approved.decision,
            merged_commit=commit,
            head_commit=approved.head,
            reasons=("已合并进 main",),
        )

    def _merge_conflicted(
        self,
        request: PipelineRequest,
        state: TaskState,
        approved: _Approved,
        error: MergeError,
    ) -> PipelineResult:
        """撞上冲突 —— 合并已撤销，任务回去返工。"""
        return PipelineResult(
            task_id=request.task_id,
            stage=PipelineStage.MERGE_CONFLICTED,
            task_state=self._advance(request, state, TaskAction.REPAIR),
            approval_request_id=approved.approval.request_id,
            decision=approved.decision,
            head_commit=approved.head,
            reasons=(str(error),),
        )

    def _merge_failed(
        self,
        request: PipelineRequest,
        state: TaskState,
        approved: _Approved,
        error: MergeError,
    ) -> PipelineResult:
        """合并这一步工具没干成 —— 合并已撤销，任务阻塞。

        **阻塞而不是返工**：git 自己没干成（工作区不干净、没配身份、仓库
        坏了），这不是代码的问题。让人看一眼比让 Agent 去改代码靠谱。
        """
        return PipelineResult(
            task_id=request.task_id,
            stage=PipelineStage.MERGE_FAILED,
            task_state=self._advance(request, state, TaskAction.BLOCK),
            approval_request_id=approved.approval.request_id,
            decision=approved.decision,
            head_commit=approved.head,
            reasons=(str(error),),
        )

    async def _merged_content_failed(
        self,
        request: PipelineRequest,
        state: TaskState,
        approved: _Approved,
        main_run: VerificationRun,
        verdict: VerificationVerdict,
    ) -> PipelineResult:
        """合并后的内容没通过复验 —— 撤销合并，main 不动。

        **这是 R-057 真正落地的地方。** 任务分支上验过了，合进 main 之后
        没过 —— 那么这次合并就不该成立。撤销之后 main 一个提交都没前进，
        坏的东西进不去。
        """
        aborted, abort_error = await self._abort(request)
        errored = verdict is VerificationVerdict.ERROR
        reasons = list(main_run.to_report().reasons)
        reasons.extend(aborted)
        if abort_error is not None:
            reasons.append(abort_error)
        return PipelineResult(
            task_id=request.task_id,
            stage=(
                PipelineStage.MERGED_CONTENT_ERRORED
                if errored
                else PipelineStage.MERGED_CONTENT_FAILED
            ),
            task_state=self._advance(
                request, state, TaskAction.BLOCK if errored else TaskAction.REPAIR
            ),
            main_verification=main_run,
            approval_request_id=approved.approval.request_id,
            decision=approved.decision,
            head_commit=approved.head,
            reasons=tuple(reasons),
        )

    def _already_merged(
        self, request: PipelineRequest, state: TaskState, approved: _Approved
    ) -> PipelineResult:
        """分支内容已经在 main 里 —— 没什么可合的，任务照常完成。"""
        merged_state = self._advance(request, state, TaskAction.MERGE)
        return PipelineResult(
            task_id=request.task_id,
            stage=PipelineStage.MERGED,
            task_state=self._advance(request, merged_state, TaskAction.COMPLETE),
            approval_request_id=approved.approval.request_id,
            decision=approved.decision,
            merged_commit=self.merger.head_commit(request.main_workspace),
            head_commit=approved.head,
            reasons=("分支内容已经在 main 里，没有需要合并的改动",),
        )

    async def _abort(self, request: PipelineRequest) -> tuple[tuple[str, ...], str | None]:
        """撤销暂存的合并。

        **撤销失败必须报出来。** 撤销不掉意味着 main 停在"合并进行中" ——
        那比这次合并失败严重得多，不能被一句"合并失败"盖过去。
        """
        try:
            self.merger.abort_staged_merge(main_workspace=request.main_workspace)
        except MergeError as error:
            return (("合并已撤销失败 —— main 仍停在合并进行中，需要人工处理",), str(error))
        return (("合并已撤销，main 没有前进",), None)

    def _merge_message(self, request: PipelineRequest) -> str:
        """合并提交的信息。"""
        if request.merge_message.strip():
            return request.merge_message
        return f"合并 {request.branch}（任务 {request.task_id}，已获人工审批）"

    def _advance(self, request: PipelineRequest, state: TaskState, action: TaskAction) -> TaskState:
        """走一步状态迁移。

        迁移非法时抛 :class:`IllegalTransitionError` —— 状态机不替人做主
        （design.md §6 第一条硬约束）。这条路径上的每一次迁移都是按表来的，
        真抛出来说明表变了而这里没跟上，那正是要立刻炸掉的事。
        """
        return apply_action(state, action, TransitionContext(task_id=request.task_id)).current


@dataclass(frozen=True, slots=True)
class _RequestBoundChecker:
    """只认**这一条**请求的审批查询口。

    **策略层问的是"批过没有"，这里答的是"这一条批过没有"。** 门自己的
    :meth:`~harness.approval.gate.ApprovalGate.verdict` 按 (任务, 操作) 找
    最近一条 —— 那对一般用途是对的，但合并路径批的是**具体的某个提交**，
    判定必须落在同一条上。
    """

    gate: ApprovalGate
    request_id: str

    async def approval_outcome(self, *, task_id: str, operation: Operation) -> ApprovalOutcome:
        verdict = await self.gate.verdict_for(self.request_id)
        request = verdict.request
        if request is None or request.task_id != task_id or request.operation != operation.value:
            return ApprovalOutcome.MISSING
        return verdict.outcome


def _compose_diff_summary(head: str, text: str) -> str:
    """把提交号写进摘要的第一行。

    第一行是机器读的（``commit:<sha>``），下面是**人**读的说明 —— 人看到的
    摘要不能被机器可读的那一行挤掉，所以提交号单独一行、正文照旧。
    """
    return f"{COMMIT_PREFIX}{head}\n{text.strip()}"


def _covers(request: ApprovalRequest, head: str) -> bool:
    """这条审批批的是不是当前这个提交。

    **只认自己写的那种摘要。** 没有 ``commit:`` 行的请求（人手工在别处发
    的）不算覆盖任何提交 —— 路径不认它，会自己发一条新的。这是不留余地的
    做法：认不出批的是哪一份代码，就不能拿它放行。
    """
    first_line, _, _ = request.diff_summary.partition("\n")
    if not first_line.startswith(COMMIT_PREFIX):
        return False
    return first_line[len(COMMIT_PREFIX) :].strip() == head


def _epoch(moment: datetime) -> float:
    """把时间戳成秒 —— 只用来排序。

    naive 时间按 UTC 处理，与 :mod:`core.policy.approval` 里的 ``_as_utc``
    同一套规矩：混用本地时区会让"哪条更新"在不同机器上得到不同答案。
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


__all__ = [
    "COMMIT_PREFIX",
    "BranchMerger",
    "IntegrationPipeline",
    "MergeConflictError",
    "MergeError",
    "PipelineRequest",
    "PipelineResult",
    "PipelineStage",
    "StagedMerge",
    "SubprocessBranchMerger",
]
