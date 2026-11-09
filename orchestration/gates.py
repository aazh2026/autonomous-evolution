"""验收契约准入闸门（编排层）。

设计依据：docs/design.md §3.5B（"每个任务在开始执行前，必须先定义怎样才算
完成"）、§6（``READY → RUNNING`` 的准入闸门）；docs/tasks.md TASK-P1-062、
T1 ①（无契约的任务无法从 READY 迁移到 RUNNING）、T1 ②（补上契约后迁移成功）、
T1 ③（契约在任务开始后被修改 → 下一次迁移用新版本，且修改留审计）；R-051。

**这道门只回答一件事：这个任务现在能不能开工。** 它不写任务状态 —— 状态归
状态机（:mod:`core.task.state_machine`）与仓储层管 —— 也不执行任务。门把
"该进 RUNNING"这件事算清楚，剩下的是调用方的事；两边都写状态就会有一边写丢
（与 :mod:`harness.approval.gate` 同一套做法）。

**三道拒绝，理由各不相同。**

1. **没有契约** —— 拒绝（:class:`core.task.state_machine.
   AcceptanceContractRequiredError`）。补救是补一份。
2. **契约绑在别的任务上** —— 拒绝（:class:`core.task.admission.
   ContractBindingMismatchError`）。补救是改回去；**不是**再补一份。
3. **契约读不出来** —— 拒绝（:class:`core.task.admission.
   ContractUnreadableError`）。补救是重写一份。

**为什么第三种要单独说。** 契约在库里但内容坏了 —— 这时候"当它不存在"与
"当它存在"都不对：当成不存在，闸门会说"补一份"，而坏的那份还躺在库里；当成
存在，一份读不出来的契约就放行了任务。所以它有自己的名字。

**契约改了要留审计，而且改不了就退回去。** 契约是**验收标准**：把它改松了再
跑一遍，等于用降低标准换取"通过"（design.md §3.5B 的脚注）。所以每一次改动
都留下一条审计记录（主体、任务、资源、结果四要素齐全），而**审计写不下来时
这次改动就不算数** —— 门会把刚才那次改动退回去（:class:`core.task.admission.
ContractAuditError`）。写不下来的审计等于这件事没发生过（design.md §3.5D）。

**"下一次迁移用新版本"靠的是不缓存。** 门每次判定都**重新读**契约，没有
"上次看到的版本"这种状态。所以契约改了之后，下一次判定的就是新内容 ——
版本指纹（:func:`core.task.admission.contract_fingerprint`）随之变化，而
改动前后各有一条审计，于是"什么时候换的标准、换成了什么"都查得到。

**为什么幂等重投不再问一遍契约。** 任务已经在 RUNNING 时又收到一次 START，
要的是"别重复执行副作用"，不是"再审一遍准入"。所以门先看目标态是不是当前
态 —— 是的话照常交给状态机（它返回空操作），不去打扰存储层。状态机的那条
"幂等判定先于允许集合判定"（:mod:`core.task.state_machine`）也是同一条道理。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn

from core.task.admission import (
    AdmissionDecision,
    ContractAuditError,
    ContractBindingMismatchError,
    ContractStore,
    ContractUnreadableError,
    RefusalReason,
    contract_fingerprint,
)
from core.task.state_machine import (
    AcceptanceContractRequiredError,
    TaskAction,
    TaskState,
    TransitionContext,
    TransitionResult,
    apply_action,
    target_of,
)
from harness.security.audit import (
    NOT_APPLICABLE,
    AuditDecision,
    AuditRecorder,
    AuditResult,
)
from harness.verification.contract import AcceptanceContract

#: 首次绑定契约的审计操作名。
BIND_OPERATION = "bind_acceptance_contract"

#: 改写已有契约的审计操作名。**与首次绑定分开记** —— "补一份"与"把标准改松
#: 了"在事后复盘时是两件事。
UPDATE_OPERATION = "update_acceptance_contract"


@dataclass(frozen=True, slots=True)
class ContractBinding:
    """契约绑定之后的结果。

    :attr:`replaced_version` 是**被换掉的那一版**的指纹；首次绑定时为空。
    留着它，审计里才能说出"从哪一版换到哪一版" —— 只说"现在是这一版"，
    就查不出标准是什么时候被放低的。
    """

    task_id: str
    document: Mapping[str, object]
    version: str
    replaced_version: str = ""


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """准入 + 迁移的结果。

    :attr:`transition` 是**授权**去做这次迁移，不是"已经迁移了"。状态由调用
    方落库（仓储层），门不写状态 —— 两边都写就会有一边写丢。
    """

    decision: AdmissionDecision
    transition: TransitionResult


class AdmissionGate:
    """``READY → RUNNING`` 的准入闸门（R-051）。

    :param store: 契约的存取口（:class:`core.task.admission.ContractStore`）。
    :param audit: 审计的写入口（:class:`harness.security.audit.AuditRecorder`）。
        **它是必填的** —— 契约的每一次改动都必须留痕，而"可不带审计"意味着
        有人会不带。

    **门只算迁移，不落任务状态** —— 与 :mod:`harness.approval.gate` 同一套
    做法：门说"该进 RUNNING"，仓储层写状态。
    """

    __slots__ = ("_audit", "_store")

    def __init__(self, store: ContractStore, *, audit: AuditRecorder) -> None:
        self._store = store
        self._audit = audit

    async def check(self, *, task_id: str) -> AdmissionDecision:
        """这个任务现在能不能开工。**只判定，不改任何东西。**

        **每次都重新读契约。** 门不缓存"上次看到的版本" —— 契约改了之后，
        下一次判定读到的就是新内容。缓存会让"改松了标准"这件事在判定上看不
        出来，而那正是最需要被看见的一种改动。
        """
        document = await self._store.contract_of(task_id)
        if document is None:
            return AdmissionDecision.refuse(
                reason=f"任务 {task_id} 没有验收契约",
                kind=RefusalReason.NO_CONTRACT,
            )

        try:
            contract = AcceptanceContract.from_document(dict(document))
        except ValueError as error:
            return AdmissionDecision.refuse(
                reason=f"任务 {task_id} 的验收契约读不出来：{error}",
                kind=RefusalReason.UNREADABLE,
                detail=str(error),
            )

        if contract.task_id != task_id:
            return AdmissionDecision.refuse(
                reason=f"任务 {task_id} 的验收契约绑定的是任务 {contract.task_id}",
                kind=RefusalReason.BOUND_ELSEWHERE,
                bound_to=contract.task_id,
            )

        version = contract_fingerprint(dict(document))
        return AdmissionDecision.admit(
            reason=f"任务 {task_id} 的验收契约版本 {version} 可以开工",
            contract_version=version,
        )

    async def admit_start(
        self,
        *,
        task_id: str,
        current_state: TaskState,
    ) -> AdmissionResult:
        """判定 + 迁移。**拒绝时抛错，状态不变。**

        :param current_state: 任务当前的状态。**门不自己去读** —— 状态归状态
            机与仓储层管，调用方知道就传进来。

        拒绝时抛的三种错都是 :class:`core.task.state_machine.
        TransitionGateError` 的子类，调用方一把抓住即可；而**状态没有被改动**
        —— 门在迁移之前就抛了出来。
        """
        decision = await self.check(task_id=task_id)

        # 幂等重投：任务已经在 RUNNING，又收到一次 START。那次重投要的是
        # "别重复执行副作用"，不是"再审一遍准入" —— 所以不去打扰存储层，
        # 照常交给状态机（它会返回空操作）。
        idempotent = current_state is target_of(TaskAction.START)
        if not decision.admitted and not idempotent:
            raise _refusal_error(decision, task_id=task_id)

        transition = apply_action(
            current_state,
            TaskAction.START,
            TransitionContext(task_id=task_id, acceptance_contract_bound=decision.admitted),
        )
        return AdmissionResult(decision=decision, transition=transition)

    async def bind_contract(
        self,
        *,
        task_id: str,
        project_id: str,
        agent_id: str,
        contract: AcceptanceContract | Mapping[str, object],
    ) -> ContractBinding:
        """绑定或改写契约 —— 并留审计。

        :param contract: 要绑的契约。可以给模型，也可以给落库形态的字典；
            两种都过一遍**同一份校验**，所以"从哪来"不影响"能不能绑"。

        **绑错了当场报错。** 契约写明的任务与 ``task_id`` 不符时，绑上去就
        是给别的任务发了一张假通行证。

        **审计写不下来，这次改动就不算数** —— 门把改动退回去并抛
        :class:`core.task.admission.ContractAuditError`。
        """
        normalized = _normalize(contract, task_id=task_id)

        previous = await self._store.contract_of(task_id)
        previous_version = contract_fingerprint(dict(previous)) if previous is not None else ""

        await self._store.bind_contract(task_id, normalized)

        version = contract_fingerprint(dict(normalized))
        try:
            self._audit.record_tool_call(
                agent_id=agent_id,
                task_id=task_id,
                project_id=project_id,
                # 契约不是工作空间里的东西 —— 没有工作空间。用占位值而不是
                # 空字符串：审计记录的元素不许为空，空的会让读的人以为"漏填了"。
                workspace_id=NOT_APPLICABLE,
                resource=f"acceptance_contract:{task_id}",
                operation=BIND_OPERATION if previous is None else UPDATE_OPERATION,
                result=AuditResult.SUCCESS,
                decision=AuditDecision.ALLOW,
                reason=_binding_reason(task_id=task_id, version=version, replaced=previous_version),
            )
        except Exception as error:
            await self._rollback(task_id=task_id, previous=previous, error=error)

        return ContractBinding(
            task_id=task_id,
            document=normalized,
            version=version,
            replaced_version=previous_version,
        )

    async def _rollback(
        self,
        *,
        task_id: str,
        previous: Mapping[str, object] | None,
        error: Exception,
    ) -> NoReturn:
        """审计写失败 → 把刚才那次改动退回去。

        **退不回去比改不掉更糟**，所以退不回去时抛的是另一条错（把退不回去
        这件事也说出来），而不是把原来那条审计错误吞掉。

        两条路都抛 —— 所以标了 ``NoReturn``：调用方不该以为它可能正常返回。
        """
        try:
            if previous is None:
                await self._store.unbind_contract(task_id)
            else:
                await self._store.bind_contract(task_id, previous)
        except Exception as rollback_error:
            raise ContractAuditError(
                f"任务 {task_id} 的验收契约改了但审计写不下来，回滚也失败了：{rollback_error}"
            ) from error
        raise ContractAuditError(
            f"任务 {task_id} 的验收契约改动没有留下审计，已回滚：{error}"
        ) from error


def _normalize(
    contract: AcceptanceContract | Mapping[str, object],
    *,
    task_id: str,
) -> Mapping[str, object]:
    """把契约归一成落库形态，并检查它绑的是这个任务。

    **两种入参走同一条路。** 给模型还是给字典，不该影响"能不能绑" ——
    两边各走一套校验，早晚会对不上（一边放行、一边拒绝同一份契约）。
    """
    if isinstance(contract, AcceptanceContract):
        parsed = contract
    else:
        parsed = AcceptanceContract.from_document(dict(contract))

    if parsed.task_id != task_id:
        raise ContractBindingMismatchError(task_id=task_id, bound_to=parsed.task_id)
    return parsed.to_document()


def _refusal_error(decision: AdmissionDecision, *, task_id: str) -> Exception:
    """把拒绝结论翻译成对应的错。

    **理由不同，错就不同** —— "没有契约"与"绑错了"要做的补救不一样，调用方
    拿到同一个错就没法分别处理。
    """
    match decision.kind:
        case RefusalReason.BOUND_ELSEWHERE:
            return ContractBindingMismatchError(task_id=task_id, bound_to=decision.bound_to)
        case RefusalReason.UNREADABLE:
            return ContractUnreadableError(task_id=task_id, detail=decision.detail)
        case _:
            return AcceptanceContractRequiredError(task_id)


def _binding_reason(*, task_id: str, version: str, replaced: str) -> str:
    """审计里那句话 —— 说清"换成哪一版、从哪一版换的"。"""
    if replaced:
        return f"任务 {task_id} 的验收契约换成了版本 {version}（原版本 {replaced}）"
    return f"任务 {task_id} 的验收契约绑定为版本 {version}"


__all__ = [
    "BIND_OPERATION",
    "UPDATE_OPERATION",
    "AdmissionGate",
    "AdmissionResult",
    "ContractBinding",
]
