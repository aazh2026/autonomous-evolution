"""六类触发源的领域契约 —— 什么算一个触发事件。

设计依据：docs/tasks.md TASK-P3-011（交付物：定时 / 代码 / 质量 / 运行 / 外部
/ 知识 六类触发器；验收条件"六类事件各能触发一轮演进；同一事件去重后只触发
一次"；验证 T1 + T2）、R-079、R-080、I-11；docs/design.md §4（六个触发源）。

**六个触发源不是六个枚举值，是六种不同形状的事件。** ``TriggerSource`` 早就
存在（在 :mod:`core.evolution.evolution_loop` 里），但那只是一份**声明** ——
声明"应该有六路"，不等于真有六路能产出事件。这一层把"事件"定成形状：

- 每类源有**自己的事件类型**（``code.commit`` 与 ``quality.failure`` 不是
  同一件事）；
- 每条事件有**自己的主体标识**（``aggregate_id``）—— 一次提交、一个失败的
  检查、一条事故、一份报告、一条决策记录。主体标识决定了去重的粒度：同一
  次提交报十遍是一件事，两次不同的提交是两件事；
- 每条事件带着**它说的是什么事**（``summary``）—— 事后翻周期记录的人看的
  就是这句话。

**为什么要有"事件类型 × 触发源"这张对照表。** 没有它，"这条事件的触发源写
对了吗"没人管 —— 一个生产者把代码事件标成定时事件，周期记录里写的就是
"定时触发"，而实际上它是被一次提交触发的。**事后没人能查出真相。** 这张表
把"哪一类源可以发哪一种事件"写成数据，于是配错是**一条错误**，不是一个
看不出来的事实。

**这一层不做 IO。** 它只定义形状与校验；去读真实来源、去派发、去开轮，都在
更高的层（:mod:`orchestration.triggers` 与 :mod:`apps.triggers`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from core.events.dedup import EventFingerprint
from core.evolution.evolution_loop import TriggerSource


class TriggerEventType(StrEnum):
    """触发事件的事件类型。

    **每类触发源一个类型 —— 不是六个源共用一个类型。** 共用一个类型的话，
    去重指纹就只看主体，"一次提交"与"一次质量失败"只要主体撞上就互相挡掉。
    """

    #: 定时：定期扫描发现了新问题。
    SCHEDULE_SCAN = "scan.found"
    #: 代码：一次提交 / 推送。
    CODE_COMMIT = "code.commit"
    #: 质量：一项检查没过（用例失败、覆盖率下降）。
    QUALITY_FAILURE = "quality.failure"
    #: 运行：跑起来之后出的事故（崩溃、超时、退出码非零）。
    RUNTIME_INCIDENT = "runtime.incident"
    #: 外部：人报上来的问题 / 需求。
    EXTERNAL_REPORT = "external.report"
    #: 知识：知识库里多了新东西（架构决策、依赖漏洞、技术升级）。
    KNOWLEDGE_UPDATE = "knowledge.update"


#: **每类触发源可以发哪些事件类型。**
#:
#: 这是"配错了就是错"的依据 —— :class:`TriggerEvent` 在构造时就查这张表。
SOURCE_EVENT_TYPES: Final[dict[TriggerSource, tuple[TriggerEventType, ...]]] = {
    TriggerSource.SCHEDULE: (TriggerEventType.SCHEDULE_SCAN,),
    TriggerSource.CODE: (TriggerEventType.CODE_COMMIT,),
    TriggerSource.QUALITY: (TriggerEventType.QUALITY_FAILURE,),
    TriggerSource.RUNTIME: (TriggerEventType.RUNTIME_INCIDENT,),
    TriggerSource.EXTERNAL: (TriggerEventType.EXTERNAL_REPORT,),
    TriggerSource.KNOWLEDGE: (TriggerEventType.KNOWLEDGE_UPDATE,),
}


class TriggerContractError(Exception):
    """触发事件与契约不符。

    **名字带 ``Error`` 是仓库的硬规矩**（ruff 的 N818）—— 异常的名字要能
    在 traceback 里一眼看出是"哪一类问题"。
    """


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    """一条归一化后的触发事件。

    :param source: 这条事件来自哪一类触发源。
    :param event_type: 事件类型。**必须与** :attr:`source` 相配。
    :param aggregate_id: 事件的主体 —— **去重的粒度**。
    :param project_id: 属于哪个项目。
    :param summary: 人话：这件事是什么。
    :param causation_id: 因果链 id。**定时与外部触发没有** —— 它们不是任何
        事的后果。

    **主体标识为什么这么重要。** 去重窗口按 ``(event_type, aggregate_id)``
    去重（P1-064）。主体选得粗，"整个项目"就是一条事件，于是项目里任何一
    次提交都被当成同一件事挡掉；主体选得细，一次提交拆成两次推送就绕过了
    去重。**主体是"这件事本身"** —— 一次提交、失败的某一条检查、某一次
    崩溃。
    """

    #: 这条事件来自哪一类触发源。
    source: TriggerSource
    #: 事件类型。
    event_type: TriggerEventType
    #: 事件的主体 —— 去重的粒度。
    aggregate_id: str
    #: 属于哪个项目。
    project_id: str
    #: 人话：这件事是什么。
    summary: str
    #: 因果链 id。没有就是外部触发。
    causation_id: str | None = None

    def __post_init__(self) -> None:
        """校验事件与契约相不相符。

        **校验在这里做，不在派发的时候做。** 派发时校验已经晚了 —— 事件
        已经被接受、可能已经开了轮。
        """
        allowed = SOURCE_EVENT_TYPES[self.source]
        if self.event_type not in allowed:
            raise TriggerContractError(
                f"触发源 {self.source.value} 不发 {self.event_type.value} 事件"
                f"（它只能发 {', '.join(item.value for item in allowed)}）"
            )
        for name, value in (
            ("aggregate_id", self.aggregate_id),
            ("project_id", self.project_id),
            ("summary", self.summary),
        ):
            if not value.strip():
                raise TriggerContractError(f"触发事件的 {name} 不能是空白")

    @property
    def fingerprint(self) -> EventFingerprint:
        """去重用的指纹 —— (事件类型, 主体)。"""
        return EventFingerprint(event_type=self.event_type.value, aggregate_id=self.aggregate_id)


__all__ = [
    "SOURCE_EVENT_TYPES",
    "TriggerContractError",
    "TriggerEvent",
    "TriggerEventType",
]
