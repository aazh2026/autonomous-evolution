"""验收证据归档 —— 验证结束后把证据落到对象存储与数据库里。

设计依据：docs/design.md §3.6（执行产物库保存不可丢失的证据）；docs/tasks.md
TASK-P1-048（验证结束后把 git diff / test report / execution trace 上传对象
存储并登记 ``Artifact``，且与 ``VerificationRun`` 绑定；**归档失败的验证不计
为通过**）、TASK-P1-004（内容寻址 + 读时校验）、TASK-P1-024（append-only、
证据不可丢失）；R-065 / R-071 / R-099。

**归档是"两件事"，不是一件。** 一件是把内容放进对象存储（``put``），另一件
是把"哪份内容在哪"登记进数据库（``Artifact`` 行）。两件事必须**一起**成立
才算归档成功：只 put 不登记，等于把证据扔进了没人知道地址的池子；只登记
不 put，登记的是一个指向空处的指针。

**为什么验收记录与产物写在同一个事务里。** ``artifact.verification_run_id``
是外键，指向 ``verification_run``；而"这次验收到底算不算通过"，只有在证据
都落了盘之后才知道。所以验收记录与它的产物必须**要么一起写进去，要么一样
都不写** —— 分开写会留下"验收记录写着通过、产物只有两样"这种中间态，而那种
中间态在事后看与"证据丢了"完全一样。

**fail-closed 落在结论上。** 归档失败时这次验收**不算通过**。两种失败的
**理由不同，结论也不同**：

- **证据没取到**（该有的那份没有）→ ``FAILED``：验收口径没被满足，该返工；
- **过程出错**（对象存储或数据库不可用）→ ``ERROR``：结论不可得，该让人
  看一眼。

这条区分与 :mod:`harness.verification.engine` 里"没跑成 vs 跑了没过"的区分
是同一条 —— 把两者压成一个"失败"，就会派去修一个根本没坏的东西。

**归档失败不抛异常，但绝不静默。** 抛异常的话，调用方一句
``except Exception: pass`` 就能把 fail-closed 吃掉；返回结果的话，"通过"这个
结论只能从结果里读，而结果里没有它 —— 结论就丢不掉。

**这个模块不认识数据库，也不认识对象存储。** 它只认两个端口
（:class:`~core.evidence.artifacts.ArtifactStorage` 与
:class:`~core.evidence.artifacts.ArtifactRegistry`）和一个取证据的端口
（:class:`ArtifactCollector`）。实现都在外面：存储后端在
``infrastructure/storage/``、登记处在 ``infrastructure/database/``、取证据在
:mod:`knowledge.artifacts.collector`。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from core.evidence.artifact_hash import ArtifactHashMismatchError, compute_hash
from core.evidence.artifacts import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactRegistry,
    ArtifactStorage,
    VerificationRunRecord,
    key_for,
)
from core.task.outcome import VerificationVerdict


class ArchiveError(RuntimeError):
    """归档请求本身不成立 —— 配置错误，不是"归档没成功"。

    这条异常只在"请求没法执行"时抛出（任务 id 为空、检查结果为空）。
    **运行期**的问题（对象存储挂了、证据没取到）不进这里 —— 那些要变成
    结论，而不是异常。一个把"环境坏了"报成异常的归档器，会让编排层把环境
    故障误判成验收失败，然后去修一个根本没坏的代码。
    """


class ArtifactNotFoundError(LookupError):
    """按 URI 找不到对应的登记。"""


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """证据从哪儿取。

    :attr:`workspace` 是被验收的工作区 —— git diff 与测试报告都在那里。
    :attr:`traces` 是验证引擎给的执行轨迹（每行一条：检查名与退出码）。它
    不在工作区里 —— 工作区里有的是"这次验出了什么"，轨迹是"这次怎么验的"。
    """

    workspace: Path
    traces: tuple[str, ...] = ()


@runtime_checkable
class ArtifactCollector(Protocol):
    """把一类证据变成字节。

    **取不到就返回 ``None``** —— "没产出"与"产出是空的"是两回事：前者说明
    那项检查压根没跑成（或者压根没跑），后者是一份**内容为空**的证据，
    而"当时确实没有改动"本身也是证据。
    """

    async def collect(self, kind: ArtifactKind, *, source: EvidenceSource) -> bytes | None:
        """取出这一类证据的内容。取不到返回 ``None``。"""
        ...


@dataclass(frozen=True, slots=True)
class ArchiveRequest:
    """一次归档的输入。

    :attr:`required` 是**契约要求**的产物，:attr:`produced` 是**这次验证
    产出**的产物。两个不是一回事：契约要求三样而这次只产出两样，那缺的那
    一样就是缺 —— 不能因为"产出里有两样"就说齐了。

    :attr:`run_id` 标识**一次验收尝试**。重跑归档就是新的一次尝试，用新的
    ``run_id``；用同一个 ``run_id`` 再归档一遍是幂等的（不会重复登记）。
    """

    run_id: str
    task_id: str
    verdict: VerificationVerdict
    verified_by: str
    checks: tuple[Mapping[str, object], ...]
    source: EvidenceSource
    required: tuple[ArtifactKind, ...] = ()
    produced: frozenset[ArtifactKind] = frozenset()
    #: 验证引擎自己给的理由（哪几项没过）。归档结果里要带上 —— 一个不带
    #: 理由的"没过"和"环境坏了"在下游看起来一模一样。
    verdict_reasons: tuple[str, ...] = ()
    duration_ms: int | None = None
    cycle_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """一次归档的结果。

    :attr:`verdict` 是**合起来的结论**：验证的结论与归档的成败**取更严的
    那个**。它是这次验收能不能算通过的**唯一**依据 —— 拿它去判"通过了"
    的时候，"通过"只可能来自两个都成立的前提：验证过了，且证据都在。

    :attr:`archived` 说的是另一件事：归档这一步本身成没成。验证没过时
    归档照样可以成（失败的验收也要留证据），所以这两个不是一回事。
    """

    run_id: str
    verdict: VerificationVerdict
    archived: bool
    artifacts: tuple[ArtifactRecord, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """这次验收能不能算通过。"""
        return self.verdict is VerificationVerdict.PASSED


class EvidenceArchiver:
    """验收证据的归档器。

    :param storage: 内容寻址的产物存储。
    :param registry: 产物登记处（验收记录与产物一起写）。
    :param collector: 从工作区里取证据。
    """

    def __init__(
        self,
        *,
        storage: ArtifactStorage,
        registry: ArtifactRegistry,
        collector: ArtifactCollector,
    ) -> None:
        self._storage = storage
        self._registry = registry
        self._collector = collector

    async def archive(self, request: ArchiveRequest) -> ArchiveResult:
        """把这次验证的证据归档，并给出**合起来的结论**。

        **不抛运行期的异常。** 对象存储挂了、证据没取到、登记写不进去 ——
        这些都变成结果里的"不是通过"与理由。抛异常的话，调用方一句
        ``except Exception: pass`` 就能把 fail-closed 吃掉；而"通过"这个
        结论只能从结果里读，结果里没有它，结论就丢不掉。
        """
        _check_request(request)

        records: list[ArtifactRecord] = []
        failures: list[str] = []
        #: 有没有"过程出错"。过程出错比"证据没取到"更严重 —— 前者连事实都
        #: 不确定（存储是不是把内容写坏了？登记是不是没落库？），后者只是
        #: 少了一份证据。
        broken = False

        for kind in _in_order(request.produced):
            try:
                data = await self._collector.collect(kind, source=request.source)
            except Exception as error:
                failures.append(f"{kind.value}：取证据时出错 —— {error}")
                broken = True
                continue

            if data is None:
                failures.append(f"{kind.value}：这次验证没有产出这一类证据")
                continue

            try:
                stored = await self._storage.put_bytes(data)
            except Exception as error:
                failures.append(f"{kind.value}：上传对象存储失败 —— {error}")
                broken = True
                continue

            # **写完必须自己核对一遍。** 内容寻址的写是幂等的：key 已存在时
            # 不重写（包括内容已被损坏的对象 —— 篡改是要保留的信号）。所以
            # put 完不能假设"key 处的内容 == 刚写进去的内容"。
            try:
                intact = await self._storage.verify(stored.key)
            except Exception as error:
                failures.append(f"{kind.value}：核对写入内容时出错 —— {error}")
                broken = True
                continue

            if not intact:
                failures.append(f"{kind.value}：写进去的内容与它的摘要对不上")
                broken = True
                continue

            records.append(
                ArtifactRecord(
                    artifact_id=str(uuid.uuid4()),
                    task_id=request.task_id,
                    verification_run_id=request.run_id,
                    uri=stored.uri,
                    hash=stored.digest,
                    type=kind,
                    size_bytes=stored.size_bytes,
                    project_id=request.project_id,
                )
            )

        # 契约要的证据有没有齐。**"产出了但没归档成功"的原因上面已经报过
        # 了**，这里只补"压根没产出"的那一类 —— 同一个问题报两遍只会让人
        # 以为出了两个问题。
        archived_kinds = {record.type for record in records}
        for kind in request.required:
            if kind in request.produced:
                continue
            if kind not in archived_kinds:
                failures.append(f"{kind.value}：契约要求的证据这次验证没有产出")

        verdict = _combine(request.verdict, failures, broken=broken)

        run = VerificationRunRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            checks=request.checks,
            result=verdict.value,
            verified_by=request.verified_by,
            duration_ms=request.duration_ms,
            cycle_id=request.cycle_id,
        )
        try:
            registered = await self._registry.record_archive(run, records)
        except Exception as error:
            # **落库失败是最严重的一种**：验收记录与产物**都没写进去** ——
            # 数据库里既没有结论也没有证据。这次尝试在库里不留痕，调用方
            # 拿到的是"不是通过"与失败原因。
            failures.append(f"证据登记失败 —— {error}")
            broken = True
            verdict = _combine(request.verdict, failures, broken=broken)
            registered = ()

        reasons = (*request.verdict_reasons, *failures)
        return ArchiveResult(
            run_id=request.run_id,
            verdict=verdict,
            archived=not failures,
            artifacts=registered,
            reasons=reasons,
        )

    async def retrieve(self, uri: str) -> bytes:
        """按 URI 取回证据，并核对它与登记时的摘要一致。

        **取回来的内容必须与登记值对得上** —— 对不上就抛
        :class:`ArtifactHashMismatchError`：证据被换掉了，调用方必须拒绝
        采信。

        **为什么 key 能从摘要算出来。** 存储是内容寻址的，key 形如
        ``sha256:<摘要>``，**是内容的函数** —— 所以登记里存的摘要就足以
        定位对象，不需要另存一个 key。这不是巧合，是内容寻址这条性质的
        直接后果；哪天存储改成按时间命名，这条就不成立了。
        """
        record = await self._registry.get_by_uri(uri)
        if record is None:
            raise ArtifactNotFoundError(f"没有登记过这个地址的证据：{uri}")

        try:
            data = await self._storage.get_bytes(key_for(record.hash))
        except ArtifactHashMismatchError:
            # 篡改的信号要原样传出去 —— 它与"东西不在了"是两回事。
            raise
        except Exception as error:
            raise ArtifactNotFoundError(f"按 URI 取不到证据：{uri} —— {error}") from error

        actual = compute_hash(data)
        if actual != record.hash:
            raise ArtifactHashMismatchError(record.hash, actual, uri=uri)
        return data


def _in_order(kinds: Sequence[ArtifactKind] | frozenset[ArtifactKind]) -> tuple[ArtifactKind, ...]:
    """归档的次序 —— 按种类名排定。

    **次序必须是确定的。** 报告里"先归档了 diff 还是先归档了报告"会被人
    拿来读；而 dict 的迭代序在内容一样时也一样，在内容不同时就不一样 ——
    那正是最需要确定的时候。
    """
    return tuple(sorted(kinds, key=lambda kind: kind.value))


def _combine(
    verdict: VerificationVerdict,
    failures: Sequence[str],
    *,
    broken: bool,
) -> VerificationVerdict:
    """把验证的结论与归档的成败合成一个结论。

    **取更严的那个。** 验证已经判了"没过"，就不要因为归档也失败而改成
    "没验成" —— 前者是个结论（该返工），后者是"不知道"（该让人看一眼）；
    一个已经知道答案的事，不该被说成不知道。

    验证通过时才看归档：出了过程错误记 ``ERROR``（结论不可得），只是证据
    没齐记 ``FAILED``（验收口径没被满足）。
    """
    if verdict is VerificationVerdict.ERROR:
        return VerificationVerdict.ERROR
    if verdict is VerificationVerdict.FAILED:
        return VerificationVerdict.FAILED
    if not failures:
        return VerificationVerdict.PASSED
    return VerificationVerdict.ERROR if broken else VerificationVerdict.FAILED


def _check_request(request: ArchiveRequest) -> None:
    """请求本身成不成立。

    **这里抛异常，别处不抛。** 请求不成立是调用方的错（少传了 id、空的
    检查结果），当场炸掉比归档出一份"看起来成功"的记录要好。
    """
    if not request.run_id.strip():
        raise ArchiveError("归档必须带 run_id —— 证据要绑在一次验收上")
    if not request.task_id.strip():
        raise ArchiveError("归档必须带 task_id —— 证据要挂在任务下")
    if not request.verified_by.strip():
        raise ArchiveError("归档必须带 verified_by —— '谁验的'要可追溯")
    if not request.checks:
        raise ArchiveError("归档必须带检查结果 —— 没跑检查的验收不算验收")


__all__ = [
    "ArchiveError",
    "ArchiveRequest",
    "ArchiveResult",
    "ArtifactCollector",
    "ArtifactNotFoundError",
    "EvidenceArchiver",
    "EvidenceSource",
]
