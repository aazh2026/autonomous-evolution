"""架构决策的领域模型与存取端口。

设计依据：docs/tasks.md TASK-P2-016（交付物：ADR 存储）、R-067（不用
对话历史当事实）、R-068（十二个核心实体）；docs/design.md §3.6；core/
entities/architecture.py（``ArchitectureDecision`` 实体的设计说明）。

**端口放在 ``core/``，因为它有两头。** 架构决策要存两处：Git 里的
Markdown（给人读）与 PostgreSQL 里的行（给系统读）。两个实现分处两个
层（``knowledge/`` 与 ``infrastructure/``），如果端口跟着其中一个走，
另一个就得反过来依赖它 —— 而 ``infrastructure`` 依赖 ``knowledge`` 是
反的。所以端口与它的数据类型留在最底下的 ``core/``：两头都往上指它。

**为什么决策要存两份而不是一份。** ``core/entities/architecture.py`` 里
写明了：Git 里的 ADR 是**给人读的文档**，数据库里的那条是**结构化的事
实**。少任何一半都不行 —— 只有文档，Agent 读不到"这个模块当初为什么
这么设计"，于是它会一次又一次地推翻前人已经推翻过的决策；只有数据库
行，人打开仓库看不到任何决策依据，而**人看不到的决策不算决策**。

**决策不可变，只能被取代。** 新决策用 ``supersedes`` 指向旧的，旧的
**不删不改** —— 历史不能被抹掉。"已被取代"这件事是**推导**出来的
（谁被指向了谁就是被取代了），不需要回头改旧行，而"回头改旧行"正是
那种会在并发或中断下留下半截状态的操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol, runtime_checkable

#: 决策状态：已提出。
STATUS_PROPOSED: Final[str] = "proposed"

#: 决策状态：已采纳。
STATUS_ACCEPTED: Final[str] = "accepted"

#: 决策状态：已否决。
STATUS_REJECTED: Final[str] = "rejected"

#: 决策状态：已被取代。
STATUS_SUPERSEDED: Final[str] = "superseded"

#: 全部合法状态。
DECISION_STATUSES: Final[tuple[str, ...]] = (
    STATUS_PROPOSED,
    STATUS_ACCEPTED,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
)


class ArchitectureDecisionError(RuntimeError):
    """架构决策的存取不成立。"""


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """一条架构决策。

    :attr:`id` 是**这套存储里的标识** —— 文件存储里是 ``adr-0001``，
    数据库存储里是 UUID。两条决策能不能接上（谁取代谁），取决于它们在
    **同一套存储**里。

    :attr:`created_at` 是**决策被记下的时刻**，不是"决策描述的那件事发生
    的时刻"。这两个时刻混在一起，事后就没法回答"当时的代码是什么样"。
    """

    id: str
    project_id: str
    context: str
    decision: str
    tradeoff: str | None = None
    status: str = STATUS_ACCEPTED
    supersedes_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))

    @property
    def is_superseded(self) -> bool:
        """这条决策的状态是不是"已被取代"。

        **光看状态字段是不够的** —— 谁取代了谁是从**别的决策指向了它**
        推出来的，不是每条决策自己写着的。
        """
        return self.status == STATUS_SUPERSEDED


@runtime_checkable
class DecisionStore(Protocol):
    """架构决策的存取端口。

    **两个实现**：文件（Git + Markdown/YAML，给人读）与数据库（结构化
    事实，给系统读）。两者的语义必须一致 —— 否则"Git 里写的决策"与
    "库里那条决策"会各说各话，而**各说各话的事实比没有事实更坏**。

    **端口是异步的。** 数据库那一份的会话是异步的，而端口由**存系统的
    那一头**定义 —— 两边都照它实现，文件那一边用 ``asyncio.to_thread``
    把同步的文件读写挪出事件循环。
    """

    async def record(  # noqa: PLR0913 - 参数就是决策本身的字段，打包成 dict 只会把结构藏起来
        self,
        *,
        project_id: str,
        context: str,
        decision: str,
        tradeoff: str | None = None,
        status: str = STATUS_ACCEPTED,
        supersedes_id: str | None = None,
    ) -> DecisionRecord:
        """记一条决策。**已经记过的决策不会被改写** —— 要改就再记一条，
        让它取代旧的。
        """
        ...

    async def recall(self, project_id: str) -> tuple[DecisionRecord, ...]:
        """把这个项目的决策全取出来（按时间排序）。"""
        ...

    async def get(self, decision_id: str) -> DecisionRecord | None:
        """按 id 取一条决策。取不到返回 ``None``。"""
        ...


__all__ = [
    "DECISION_STATUSES",
    "STATUS_ACCEPTED",
    "STATUS_PROPOSED",
    "STATUS_REJECTED",
    "STATUS_SUPERSEDED",
    "ArchitectureDecisionError",
    "DecisionRecord",
    "DecisionStore",
]
