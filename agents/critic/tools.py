"""事实核验工具 —— Critic 凭什么说"我查过了"。

**Critic 不能只给一个 LLM 评分。** design.md §5 写死了这句话：独立 Critic
"应该调用代码仓库、Git 历史、测试报告和项目状态进行事实核验"。这一模块把那
四类核验加上**任务表查询**（"这个问题是不是已经有别的任务在做了"）与**产物
登记处查询**（"这条证据指向的产物真的在那儿吗"），抽象成统一的工具接口，
并**记录每一次调用**。

**为什么记录调用。** 一份说"我查过了"的报告，如果拿不出查过什么，就跟没查
一样 —— 而且分不清"查过没找到"与"压根没查"。调用记录让这两件事在报告里
**分得开**：工具不可用 → 结论是"未核实"；查了没找到 → 结论是"查过，没有"。

**工具不可用时检查不能瞎猜。** :meth:`FactChecker.try_call` 返回 ``None``
表示"没有这个工具"，检查据此把结论写成"未核实"，而不是"没问题"。这是整个
模块最重要的一条：**没有工具就没有结论。**
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from core.project.models import Constraint, KeyResult, NonGoal


class ToolKind(StrEnum):
    """事实核验工具的类别。"""

    #: 仓库搜索 —— 在代码树里按文本找线索。
    REPOSITORY_SEARCH = "repository_search"
    #: Git 历史查询 —— 提交信息里有没有提到过这件事。
    GIT_HISTORY = "git_history"
    #: 测试报告读取 —— 有没有失败的用例能证明问题存在。
    TEST_REPORT = "test_report"
    #: 项目状态查询 —— 停止条件、目标达成度、约束当前状态。
    PROJECT_STATUS = "project_status"
    #: 任务表查询 —— 是不是已经有别的任务在做了。
    TASK_TABLE = "task_table"
    #: 产物登记处查询 —— 证据指向的产物是否真的登记过。
    ARTIFACT_STORE = "artifact_store"


class ProjectStatusFact(BaseModel):
    """项目当前状态的事实。

    **"不知道"要能表达出来。** ``all_key_results_met`` 是三态的：``None``
    表示"这次没查出来"，与 ``True``（查了，都达成了）、``False``（查了，没全
    达成）是三件不同的事。把它压成布尔，"没查"就会变成"没达成"。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 已触发的停止条件 id。
    stop_conditions_triggered: tuple[str, ...] = ()
    #: 项目当前已违反的约束 id。
    violated_constraints: tuple[str, ...] = ()
    #: 尚未达成的关键结果 id。
    unmet_key_results: tuple[str, ...] = ()
    #: 关键结果是否**全部**达成。``None`` = 不知道。
    all_key_results_met: bool | None = None
    #: 项目**声明**的关键结果。
    key_results: tuple[KeyResult, ...] = ()
    #: 项目**声明**的约束 —— 判定"是否违反约束"时要看的是这一份。
    constraints: tuple[Constraint, ...] = ()
    #: 项目**声明**的非目标 —— "明确不做什么"。
    non_goals: tuple[NonGoal, ...] = ()
    #: 补充说明。
    notes: str = ""


class ToolResult(BaseModel):
    """一次工具调用的结果。

    ``found`` 是"查到了吗"，不是"有没有问题" —— 工具只负责事实，判定留给检查。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 是否查到了东西。
    found: bool
    #: 一句话说明这次查到了什么 / 没查到什么。
    summary: str = Field(min_length=1)
    #: 具体指向 —— 路径、提交号、用例 id、任务 id。
    references: tuple[str, ...] = ()
    #: 只有项目状态工具会填它。
    status: ProjectStatusFact | None = None


class TaskSummary(BaseModel):
    """任务表里的一条任务 —— Critic 只看得见它需要的那几个字段。

    **不给整张任务行。** 任务行里带着分支名、执行器、验收契约那些字段 ——
    Critic 一个都用不上，多给只会让"任务表"变成"任务表的全量转储"。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 任务 id。
    id: str = Field(min_length=1)
    #: 任务标题。
    title: str = Field(min_length=1)
    #: 任务当前状态（如 ``APPROVED``）。空串表示"不知道"。
    status: str = ""


class TaskLookup(Protocol):
    """任务表的查询端口。

    **这是端口，不是实现。** 任务表在数据库里，但 Critic 不该知道数据库 ——
    它只需要"给我标题里像这一条的任务"。仓储层负责把查询翻译成这个协议。

    **端口自己不做相似度判断。** 怎么算"像"是实现的事（标题前缀、关键词、
    全文检索都行），端口只承诺"像的都给"。判"是不是同一件事"是检查的事 ——
    端口自作主张地筛掉几条，Critic 就再也看不见它们了。
    """

    async def find_similar(self, text: str, *, limit: int = 20) -> tuple[TaskSummary, ...]:
        """按文本找可能相关的任务。"""
        ...


class FactTool(Protocol):
    """一个事实核验工具。

    **工具只做查询，不做判定。** 返回"查到了什么"，不返回"这意味着什么" ——
    判定归检查，理由归报告。
    """

    kind: ToolKind

    async def query(self, argument: str) -> ToolResult:
        """按 ``argument`` 查询。"""
        ...


class ToolCall(BaseModel):
    """一次工具调用的记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ToolKind
    argument: str
    found: bool
    summary: str


class ToolUnavailableError(RuntimeError):
    """请求了一个没有注册的工具。"""


class FactChecker:
    """工具的注册表 + 调用记录。

    **记录的是"真的调用过"，不是"打算调用"。** 没注册的工具不产生调用记录 ——
    否则报告里会出现"查过仓库"而实际根本没查的行，那比不记还坏。
    """

    def __init__(self, tools: Mapping[ToolKind, FactTool] | None = None) -> None:
        self._tools: dict[ToolKind, FactTool] = dict(tools or {})
        self._calls: list[ToolCall] = []

    def register(self, tool: FactTool) -> None:
        """注册一个工具。同类工具后注册的覆盖先注册的。"""
        self._tools[tool.kind] = tool

    def has(self, kind: ToolKind) -> bool:
        """有没有这个工具。"""
        return kind in self._tools

    async def try_call(self, kind: ToolKind, argument: str) -> ToolResult | None:
        """调用工具。**没有这个工具时返回 ``None``** —— 不抛错。

        检查据此把结论写成"未核实"。抛错会让"工具没配"这件事变成一次崩溃，
        而工具没配是**部署**问题，不是**候选**的问题 —— 不该让它把整轮审查看挂。
        """
        tool = self._tools.get(kind)
        if tool is None:
            return None
        result = await tool.query(argument)
        self._calls.append(
            ToolCall(kind=kind, argument=argument, found=result.found, summary=result.summary)
        )
        return result

    async def call(self, kind: ToolKind, argument: str) -> ToolResult:
        """调用工具。工具没注册就抛 :class:`ToolUnavailableError`。"""
        result = await self.try_call(kind, argument)
        if result is None:
            raise ToolUnavailableError(f"没有注册 {kind.value} 工具")
        return result

    @property
    def calls(self) -> tuple[ToolCall, ...]:
        """全部调用记录，按调用顺序。"""
        return tuple(self._calls)

    @property
    def called_kinds(self) -> frozenset[ToolKind]:
        """被用过的工具类别。"""
        return frozenset(call.kind for call in self._calls)


__all__ = [
    "FactChecker",
    "FactTool",
    "ProjectStatusFact",
    "TaskLookup",
    "TaskSummary",
    "ToolCall",
    "ToolKind",
    "ToolResult",
    "ToolUnavailableError",
]
