"""观察层契约 —— 六类数据源，一份观察长什么样。

设计依据：docs/design.md §3 第一层（项目状态观察：数据来源包括 Git Commit
/ PR / 代码变更、单元与集成与 E2E 测试、静态代码分析与依赖漏洞与复杂度、
运行时性能与日志与异常与业务指标、产品需求与 Issue 与用户反馈、技术文档
与架构决策与设计规范）；docs/tasks.md TASK-P3-013（六类数据源各接入 ≥1 个
真实来源；候选的 evidence 指向真实可解析的证据；R-024 五类演进机会分类法；
R-028 候选要有真实证据）。

**观察不是事件。** 事件是"某件事发生了"——一次提交、一条用例没过；观察是
"现在是什么样子"——哪个文件改得最频繁、这个套件现在有几条没过。**同一件
事可以被反复观察到。** 所以观察**没有去重窗口**：昨天观察到的事实今天再
观察到一次，那是同一条事实，不是两件事。

**观察必须有证据。** 一条没有证据的观察，等于一句"我觉得这里有问题" ——
读的人没法核验，也没法在事后判断当初的判断准不准（R-033）。所以
:class:`Observation` 的证据**至少一条**，且每一条都必须指向**可解析的
引用**（:mod:`core.evolution.evidence`）。

**每一类观察只能带它该带的那种证据。** 这张表
（:data:`OBSERVATION_EVIDENCE_KINDS`）不是装饰：一条"代码改动"的观察如果
带着一个 ``issue:`` 引用，说明生产者把两件事混成了一件 —— 那时候没人说得
清这条观察到底在说什么。

**"这一路读到了没有"是三种事，不是一种。** :class:`SourceOutcome` 把它们
分开：**读到了东西**、**读到了但是空的**、**压根没读成**。第三种最容易被
当成第二种 —— 一个跑不起来的测试套件看起来和一个全绿的套件一模一样。
**缺实测值是失败，不是 0。**
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from core.evolution.evidence import Evidence, EvidenceKind


class ObservationKind(StrEnum):
    """六类观察来源 —— **与 design.md §3 第一层那六条一一对应**。

    **六类不能少也不能多。** 少一类，"六类数据源各接入 ≥1 个真实来源"就是
    空话；多一类，那一类就有自己的一套规则，词表对不上。
    """

    CHANGES = "changes"
    TESTS = "tests"
    STATIC = "static"
    RUNTIME = "runtime"
    FEEDBACK = "feedback"
    DOCS = "docs"


#: 每一类观察允许带哪几种证据。
#:
#: **这张表是承重的。** 它挡住的是"把两件事混成一件"：一条代码改动的观察
#: 带着问题单编号，说明生产者没想清楚自己在说什么。
OBSERVATION_EVIDENCE_KINDS: Final[dict[ObservationKind, frozenset[EvidenceKind]]] = {
    ObservationKind.CHANGES: frozenset({EvidenceKind.FILE}),
    ObservationKind.TESTS: frozenset({EvidenceKind.FILE, EvidenceKind.TEST_REPORT}),
    ObservationKind.STATIC: frozenset({EvidenceKind.FILE}),
    ObservationKind.RUNTIME: frozenset({EvidenceKind.FILE}),
    ObservationKind.FEEDBACK: frozenset({EvidenceKind.ISSUE, EvidenceKind.FILE}),
    ObservationKind.DOCS: frozenset({EvidenceKind.FILE}),
}


class ObservationContractError(Exception):
    """观察与契约不相符。

    **名字带 ``Error`` 是仓库的硬规矩**（ruff 的 N818）—— 异常的名字要能
    在 traceback 里一眼看出是"哪一类问题"。
    """


@dataclass(frozen=True, slots=True)
class Observation:
    """一条观察 —— **一件事的当前状态**。

    :param kind: 这一类观察。
    :param subject: **主体**：这条观察说的是哪个东西。主体是后续定位与去
        重的粒度 —— 没有主体，所有观察都挤成一团。
    :param fact: **事实**：一句话说清观察到了什么。
    :param evidence: 支撑这条观察的证据。**至少一条**。
    :param detail: 补充说明。自由文本，**不能替代** ``fact`` 与 ``evidence``。

    **"事实"与"判断"要分开。** 这一层只说"这里有 7 个模块依赖它"，
    不说"这是个耦合枢纽"—— 后者是发现层的事。混在一起，阈值就再也调不动
    了。
    """

    kind: ObservationKind
    subject: str
    fact: str
    evidence: tuple[Evidence, ...]
    detail: str = ""

    def __post_init__(self) -> None:
        """检查观察本身站不站得住。

        :raises ObservationContractError: 主体或事实是空白、证据是空的、
            或者证据的形态与这一类观察对不上。
        """
        for name, value in (("subject", self.subject), ("fact", self.fact)):
            if not value.strip():
                raise ObservationContractError(f"观察的 {name} 不能是空白")
        if not self.evidence:
            raise ObservationContractError("观察必须带至少一条证据 —— 没有证据的不是观察，是说法")
        allowed = OBSERVATION_EVIDENCE_KINDS[self.kind]
        for item in self.evidence:
            if item.kind not in allowed:
                raise ObservationContractError(
                    f"{self.kind.value} 这一类观察不能带 {item.kind.value} 的证据"
                    f"（只认 {'、'.join(sorted(kind.value for kind in allowed))}）"
                )

    @property
    def fingerprint(self) -> tuple[str, str]:
        """这条观察的身份 —— **种类 + 主体**。

        **同一条观察反复被观察到，身份不变。** 这是观察与事件最不一样的地方：
        事件是"发生了一次"，观察是"它是这样"。所以这里没有时间戳、没有计数。
        """
        return (self.kind.value, self.subject)


class SourceOutcome(StrEnum):
    """一路数据源的结果 —— **三态**。

    **"读到了但是空的"与"压根没读成"必须分开。** 一个跑不起来的测试套件、
    一个格式变了的台账、一个不存在的目录 —— 它们都产出不了观察，但原因完
    全不同，补救动作也不同（前者要修工具，后者要补数据）。
    """

    OBSERVED = "observed"
    EMPTY = "empty"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """一路数据源的状态 —— 读到了什么，或者没读到什么。

    :param kind: 这一路是哪一类。
    :param outcome: 三态之一。
    :param detail: 说清为什么。**"没读成"必须说得清原因** —— 不然读的人
        分不清"这一路坏了"与"这一路今天没东西"。
    """

    kind: ObservationKind
    outcome: SourceOutcome
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ObservationSnapshot:
    """一次观察的结果 —— **项目此刻的样子**。

    :param project_id: 这是哪个项目。
    :param observed_at: 什么时候看的。
    :param observations: 观察到的全部事实。
    :param statuses: 每一路数据源的状态 —— **六路都要在，一个都不能少**。
        少一路就看不出"没读到"与"读到了但是空的"。
    """

    project_id: str
    observed_at: datetime
    observations: tuple[Observation, ...] = ()
    statuses: tuple[SourceStatus, ...] = ()

    def by_kind(self, kind: ObservationKind) -> tuple[Observation, ...]:
        """某一类观察的全部。"""
        return tuple(item for item in self.observations if item.kind is kind)

    def status_of(self, kind: ObservationKind) -> SourceStatus | None:
        """某一路数据源的状态。"""
        for item in self.statuses:
            if item.kind is kind:
                return item
        return None

    @property
    def kinds_covered(self) -> tuple[ObservationKind, ...]:
        """读到了东西的那几类。"""
        return tuple(item.kind for item in self.statuses if item.outcome is SourceOutcome.OBSERVED)

    @property
    def kinds_missing(self) -> tuple[ObservationKind, ...]:
        """没读到东西的那几类 —— **"空"与"失败"都算**。

        **缺的那几类必须看得见。** 六路里少了一路而没人知道，"六类数据源各
        接入 ≥1 个真实来源"这句话就随时会不成立 —— 而且不会有人发现。
        """
        return tuple(
            item.kind for item in self.statuses if item.outcome is not SourceOutcome.OBSERVED
        )

    @property
    def failed_kinds(self) -> tuple[ObservationKind, ...]:
        """压根没读成的那几类。"""
        return tuple(item.kind for item in self.statuses if item.outcome is SourceOutcome.FAILED)


@runtime_checkable
class ObservationSource(Protocol):
    """一类观察来源。

    **它只负责读。** 它不判断这是不是机会，也不决定要不要开一轮 —— 那是
    发现层与派发层的事。

    **"读不到"用异常说。** 一路数据源读不成时**抛异常**，而不是返回空 ——
    空是"读到了，没有东西"，异常是"压根没读成"。观察编排那一侧接得住这个
    区别，所以它才敢把失败也记进快照。
    """

    @property
    def kind(self) -> ObservationKind:
        """这一路是哪一类观察来源。"""
        ...

    def observe(self) -> tuple[Observation, ...]:
        """现在能观察到什么。"""
        ...


def group_by_kind(
    observations: Iterable[Observation],
) -> dict[ObservationKind, tuple[Observation, ...]]:
    """按种类把观察分桶 —— 顺序按种类在词表里出现的次序排。

    **分桶的结果要是确定的。** 同样的观察集合分两次，桶的顺序不一样，
    "这轮看到了什么"就没法逐项比较。
    """
    buckets: dict[ObservationKind, list[Observation]] = {kind: [] for kind in ObservationKind}
    for item in observations:
        buckets[item.kind].append(item)
    return {kind: tuple(items) for kind, items in buckets.items()}


__all__ = [
    "OBSERVATION_EVIDENCE_KINDS",
    "Observation",
    "ObservationContractError",
    "ObservationKind",
    "ObservationSnapshot",
    "ObservationSource",
    "SourceOutcome",
    "SourceStatus",
    "group_by_kind",
]
