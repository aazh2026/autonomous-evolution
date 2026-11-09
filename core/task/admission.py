"""验收契约准入（领域契约）。

设计依据：docs/design.md §3.5B（"每个任务在开始执行前，必须先定义怎样才算
完成"）、§6（``READY → RUNNING`` 的准入闸门）；docs/tasks.md TASK-P1-062、
R-051。

**准入闸门不是事后报告。** 这是本模块存在的全部理由。design.md §3.5B 说
"每个任务在开始执行前，必须先定义怎样才算完成" —— 如果契约可以在执行完之后
补上，那它就退化成一份事后报告，而事后报告是**没有约束力**的：执行完之后再写
"覆盖率 >= 0.85"和写"覆盖率 >= 0.01"，效果完全一样。

**"存在且绑定"是两个条件，缺一不可。**

- **存在** —— 没有契约就没有验收标准，没有验收标准的执行是"做完了"而不是
  "做对了"。
- **绑定** —— 契约必须写明它属于哪个任务（:attr:`AcceptanceContract.task_id`）。
  一份"谁的覆盖率"答不出来的契约，可以被拿到任何一个任务上去放行 ——
  拿 A 任务的契约放行 B 任务，等于没有契约。

**绑定错了与没有契约要分开说。** 两种情况的补救完全不同：没有契约要**补**
一份；绑错了要**改**回去。把它们合并成一句"没有验收契约"，读日志的人就会
去补一份新的，而真正绑错的那份还躺在库里 —— 于是任务绑上第二份契约，而第一
份仍然指向别的任务。

**契约的"版本"就是契约的内容。** 库里没有单独的版本号列，契约文档本身就是
全部事实。所以 :func:`contract_fingerprint` 给出内容指纹：同一份内容 → 同一个
版本；内容变了 → 版本变了。指纹是**内容**的哈希，不是行号也不是时间戳 ——
所以它可复算：今天算出来的和明天算出来的是同一个值。

**为什么这一层只有"绑定"与"版本"，没有契约的完整校验。** 契约的结构校验
（``required`` 不得为空、阈值可比、不得重复声明）在
:class:`harness.verification.contract.AcceptanceContract` 里 —— 那份模型住在
``harness/``，而 ``core/`` 不依赖 ``harness/``（方向是反的）。编排层
(:mod:`orchestration.gates`) 拿得到那份模型，于是**完整校验在那里做**；这里
留下的是不依赖任何模型的、随时能算的那部分 —— 仓储层也要用它。

**仓储层为什么也要查绑定。** 状态机那道闸门（:class:`core.task.state_machine.
AcceptanceContractRequiredError`）只看一个布尔值，于是"绑定错了"在那一层
看不出来。把 :func:`is_bound_to` 给仓储层用，它就能在引擎级把住这一条 ——
否则一条绑错的契约足以让任务开工，而事后没人说得清"它当时验的是谁的标准"。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from core.task.state_machine import TransitionGateError

#: 准入闸门的名字。与 :class:`core.task.state_machine.AcceptanceContractRequiredError`
#: 用的是同一个 —— 它们把守的是同一件事，只是站的位置不同（一个是引擎级强制，
#: 一个是编排级判定）。
CONTRACT_GATE = "acceptance_contract"


class AdmissionOutcome(StrEnum):
    """准入结论。两种，不多不少。

    **没有"有条件通过"。** 有条件通过听起来像"先跑起来再说"，而验收标准是
    执行**之前**定下的东西：还没定下来就开工，等于先干活再定标准。
    """

    ADMITTED = "admitted"
    REFUSED = "refused"


class RefusalReason(StrEnum):
    """拒绝的理由 —— 每一类指向不同的补救动作。

    三类而不是一类，是因为"没有契约"、"契约绑错了"、"契约坏了"要做的补救
    完全不同：补一份、改回去、重新写。合并成一句话，读日志的人就只能靠猜。
    """

    #: 压根没有契约。
    NO_CONTRACT = "no_contract"
    #: 契约在，但绑定的是别的任务。
    BOUND_ELSEWHERE = "bound_elsewhere"
    #: 契约在，但读不出来（坏数据）。
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """准入结论。

    :attr:`kind` 是**机器可读**的理由，:attr:`reason` 是给人看的那句话。
    两者都要有：只有人话，程序没法据此分支；只有机器码，日志里读不出"到底
    怎么了"。

    :attr:`bound_to` 与 :attr:`detail` 是**细节，不从人话里抠** —— 把
    "绑在哪个任务上"、"哪一句读不出来"塞进一句话里再想办法解析回去，是把
    结构化的东西退化成文本的一种典型做法：改一次措辞，解析就跟着坏。

    :attr:`contract_version` 是被放行的那份契约的版本指纹 —— 拒绝时为空。
    """

    outcome: AdmissionOutcome
    reason: str
    kind: RefusalReason | None = None
    contract_version: str = ""
    #: 契约绑定的那个任务（``BOUND_ELSEWHERE`` 时有值）。
    bound_to: str = ""
    #: 读不出来的那句原因（``UNREADABLE`` 时有值）。
    detail: str = ""

    @property
    def admitted(self) -> bool:
        """能不能开工。"""
        return self.outcome is AdmissionOutcome.ADMITTED

    @classmethod
    def admit(cls, *, reason: str, contract_version: str) -> AdmissionDecision:
        """放行。"""
        return cls(
            outcome=AdmissionOutcome.ADMITTED,
            reason=reason,
            kind=None,
            contract_version=contract_version,
        )

    @classmethod
    def refuse(
        cls,
        *,
        reason: str,
        kind: RefusalReason,
        bound_to: str = "",
        detail: str = "",
    ) -> AdmissionDecision:
        """拒绝。"""
        return cls(
            outcome=AdmissionOutcome.REFUSED,
            reason=reason,
            kind=kind,
            bound_to=bound_to,
            detail=detail,
        )


class ContractBindingMismatchError(TransitionGateError):
    """契约绑在别的任务上（R-051 的"绑定"那一条）。

    **与"没有契约"分开。** 两者都是拒绝，但补救动作不同 —— 一个要补，一个
    要改回去。
    """

    def __init__(self, task_id: str, bound_to: str) -> None:
        super().__init__(
            f"任务 {task_id} 的验收契约绑定的是任务 {bound_to}，不能拿它开工",
            gate=CONTRACT_GATE,
        )
        self.task_id = task_id
        self.bound_to = bound_to


class ContractUnreadableError(TransitionGateError):
    """契约存在，但读不出来。

    **坏数据按"没有契约"处理，但理由要如实说。** 悄悄放行一份读不出来的
    契约等于没有契约；悄悄把它当成"没绑定"又等于放行了一个来路不明的东西。
    """

    def __init__(self, task_id: str, detail: str) -> None:
        super().__init__(
            f"任务 {task_id} 的验收契约读不出来：{detail}",
            gate=CONTRACT_GATE,
        )
        self.task_id = task_id
        self.detail = detail


class ContractAuditError(RuntimeError):
    """契约改动没能留下审计 —— 改动已被回滚。

    **写不下来的审计等于这件事没发生过**（design.md §3.5D）。契约改了而没人
    能证明它改过，下次再有人改一次标准就查不出"到底是谁把门槛放低了"。

    所以门在这里**回滚**：改回去了、审计没写成，两件事都还是真的。留下的
    痕迹是这条异常 —— 它说"有一次改动没能留痕"。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


@runtime_checkable
class ContractStore(Protocol):
    """验收契约的存取口。

    **为什么是端口而不是直接写库。** 领域层不该知道数据库存在（分层：
    ``infrastructure → core``）；而"契约存在且绑定"这条规则是领域规则 ——
    所以规则写在领域层，写库的实现放在基础设施层。

    **三条方法对应三种状态。** 没有契约、绑着一份契约、撤回一份契约 ——
    少一条，闸门就少一种能站得住的立场。
    """

    async def contract_of(self, task_id: str) -> Mapping[str, object] | None:
        """取这个任务当前绑定的契约；没有契约返回 ``None``。

        **返回 ``None`` 与抛错是两件事。** 任务不存在是错误；任务存在但没绑
        契约才是"没有契约"。把前者当成后者，闸门就会对不存在的任务说"补一份
        契约"，而真正的问题（任务压根没有）被掩盖了。
        """
        ...

    async def bind_contract(self, task_id: str, document: Mapping[str, object]) -> None:
        """把契约绑到这个任务上（覆盖已绑的那份）。"""
        ...

    async def unbind_contract(self, task_id: str) -> None:
        """撤回契约。

        **存在的理由是回滚。** 审计写失败时要把刚才那次改动退回去 —— 而
        "退回去"对一个本来就没有契约的任务来说就是撤回。
        """
        ...


def bound_task_of(document: Mapping[str, object] | None) -> str | None:
    """契约绑定的任务 id。

    没有契约、契约里没写 ``task_id``、写的是空白 —— 都返回 ``None``：
    **这三种情形都答不出"这是谁的契约"**，而在准入这件事上答不出就是不行。
    """
    if document is None:
        return None
    raw = document.get("task_id")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def is_bound_to(document: Mapping[str, object] | None, task_id: str) -> bool:
    """契约存在，且绑定的正是这个任务。

    **这是"准入"最便宜的那一版。** 它只查绑定，不查契约本身结构完好 —— 结构
    校验要那份模型，而模型住在 ``harness/``。编排层拿得到模型，做完整校验；
    仓储层用得上这一版，把住"绑定"这一条。

    **空任务 id 匹配不上任何契约。** 这条守卫单看是多余的 ——
    :func:`bound_task_of` 永远不返回空串，所以空 id 天然匹配不上。但它防的是
    另一个方向：万一哪天有人把"空白 task_id 归成没写"那条规则改掉，空 id 与
    空 id 就撞上了。守卫把这条不变量写在了它生效的地方，而不是指望下游那条
    规则一直不变。
    """
    if not task_id:
        return False
    return bound_task_of(document) == task_id


def contract_fingerprint(document: Mapping[str, object]) -> str:
    """契约的版本指纹。

    **算的是内容，不是行号也不是时间戳。** 所以同一份契约在任何机器、任何
    时候算出来的都是同一个值 —— 这条性质让"改动前后各记一条审计"能对得上，
    也让回放（TASK-P1-065）能复现同一串版本。

    **键序不影响结果。** 序列化时按键排序，所以 JSON 列里键的排列顺序（数据库
    可能重排）不参与计算。

    文档里若有不认识的值（不是 JSON 能表达的），这里会抛 :class:`TypeError`
    —— 那说明存进去的东西本身有问题，不该被悄悄当成"另一个版本"。
    """
    payload = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "CONTRACT_GATE",
    "AdmissionDecision",
    "AdmissionOutcome",
    "ContractAuditError",
    "ContractBindingMismatchError",
    "ContractStore",
    "ContractUnreadableError",
    "RefusalReason",
    "bound_task_of",
    "contract_fingerprint",
    "is_bound_to",
]
