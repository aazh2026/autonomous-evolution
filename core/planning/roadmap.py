"""演进路线图 —— 把已批准的决策拆成可执行的任务。

设计依据：docs/design.md §5（规划阶段：已批准的 Decisions → Evolution
Roadmap）、§4 循环 B（"Generate Evolution Plan → Decompose into Tasks →
Dependency & Resource Scheduling"）、§3.2（"不能所有任务都走相同的验收流程"：
重构任务需要强调行为保持和回归测试、性能优化任务需要比较优化前后的基准、
安全修复任务需要漏洞复测）；docs/tasks.md TASK-P1-056（R-022 规划阶段产出
路线图、R-023 不只是遍历待办、R-076 七种任务类型、R-081 五阶段决策链）。

**这一层是纯领域模型。** 它描述"一份路线图长什么样"与"哪些一致性必须成立"
（依赖无环、任务可追溯到决策、没放行就不拆解），一行 IO 都没有 —— 落库是
仓储层的事（``TaskRepository.land_roadmap``），审查是角色层的事。

**放行凭据为什么由调用方给。** 领域层不认识 Critic 的报告类型 —— 那属于角色
层（``agents/``），领域层不该反向依赖它。所以这里定义的是一个**中性凭据**
:class:`DecisionApproval`：一条决策被放行，还是被否决。角色层负责把审查
报告翻译成凭据（``agents.critic.approval``），领域层只认凭据。

**没有凭据就不拆解。** 缺凭据与被否决一样，都不产生任务 —— 这是"关不上就
不开"：一条没被放行的决策派生出任务，等于把审查这一层绕过去了，而绕过的
方式还是最容易发生的那种 —— "忘了"。

**七种任务类型不是七套流程，是七种验收重点。** design.md §3.2 原话："不能
所有任务都走相同的验收流程"。类型决定**看什么**：重构看行为是否保持、性能
看前后基准、缺陷修复看复现与回归。同一个验收流程套到所有类型上，等于没有
验收。

**类型是系统的词汇表，不是数据库的约束。** ``core.entities.Task.type`` 那一
列刻意不加 CHECK 约束 —— 任务种类随项目而变，系统不该把它锁死。这里的七类
是**规划层自己用的词**，项目要加自己的类型，加就是了。

**同一个决策可以拆出多个任务。** 一个候选说"重构权限模块并更新架构文档"，
那是两件事、两种验收重点、可能还有先后依赖。所以任务与决策是**一对多**，
任务自己的标识是 ``key``，依赖边指向的是 ``key`` 而不是决策。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.evolution.decision import DecisionRound, EvolutionDecision


class PlanningError(Exception):
    """规划层错误的基类。"""


class UnknownDecisionError(PlanningError):
    """拆解引用了一条不存在的决策。"""

    def __init__(self, decision_id: str) -> None:
        super().__init__(f"拆解引用了不存在的决策 {decision_id!r}")
        self.decision_id = decision_id


class DuplicateTaskKeyError(PlanningError):
    """同一个任务标识出现了两次。"""

    def __init__(self, keys: Sequence[str]) -> None:
        super().__init__(f"任务标识重复了：{'、'.join(keys)}")
        self.keys = tuple(keys)


class UnknownDependencyError(PlanningError):
    """依赖指向了一个不存在的任务标识。"""

    def __init__(self, keys: Sequence[str]) -> None:
        super().__init__(f"依赖指向了不存在的任务：{'、'.join(keys)}")
        self.keys = tuple(keys)


class DependencyCycleError(PlanningError):
    """依赖图有环。"""

    def __init__(self, cycle: Sequence[str]) -> None:
        super().__init__(f"依赖图有环：{' → '.join(cycle)}")
        self.cycle = tuple(cycle)


class ConflictingApprovalError(PlanningError):
    """同一条决策给了两份互相矛盾的放行结论。"""

    def __init__(self, decision_id: str) -> None:
        super().__init__(f"决策 {decision_id!r} 给了两份互相矛盾的放行结论")
        self.decision_id = decision_id


class TaskType(StrEnum):
    """七种任务类型（R-076）。

    取值与 design.md §3.2 列的七种一致：Feature Development / Bug Fix /
    Refactoring / Test Improvement / Performance Optimization / Dependency
    Upgrade / Documentation Update。
    """

    FEATURE = "feature"
    BUGFIX = "bugfix"
    REFACTORING = "refactoring"
    TEST = "test"
    PERFORMANCE = "performance"
    DEPENDENCY = "dependency"
    DOCS = "docs"


#: 每种类型的**验收重点** —— 这个类型要拿什么来证明它做成了。
#:
#: **为什么按类型分。** design.md §3.2 明写"不能所有任务都走相同的验收流程"，
#: 并举了三个例子：重构强调行为保持和回归测试，性能优化比较优化前后的基准，
#: 安全修复需要漏洞复测。安全修复落在 ``bugfix`` 这一类里，所以那一类的验收
#: 重点要同时覆盖"复现"与"回归"两件事。
ACCEPTANCE_FOCUS: Final[MappingProxyType[TaskType, str]] = MappingProxyType(
    {
        TaskType.FEATURE: "功能验收标准与端到端行为",
        TaskType.BUGFIX: "缺陷复现与修复后的回归验证（安全修复需漏洞复测）",
        TaskType.REFACTORING: "行为保持与回归测试",
        TaskType.TEST: "新增用例的有效性与覆盖率变化",
        TaskType.PERFORMANCE: "优化前后的基准对比",
        TaskType.DEPENDENCY: "依赖变更的影响面与安全扫描",
        TaskType.DOCS: "文档与实现的一致性",
    }
)


class DependencyType(StrEnum):
    """依赖类型。

    与 ``core.entities.TaskDependency.dependency_type`` 那一列的取值一致
    （finish_to_start / start_to_start / finish_to_finish）。
    """

    FINISH_TO_START = "finish_to_start"
    START_TO_START = "start_to_start"
    FINISH_TO_FINISH = "finish_to_finish"


class TaskDependency(BaseModel):
    """任务依赖（领域模型）。

    **同名不同层。** 这里说的是"路线图里 A 要排在 B 前面"；持久化的那张表
    （``core.entities.TaskDependency``）说的是"数据库里两条任务行之间的边"。
    两件事的端点一个是**路线图内的任务标识**，一个是**数据库里的任务 id** ——
    落库时才把前者换成后者。

    **自环是非法的。** 自己依赖自己是死锁，不是依赖。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 前置任务的标识（路线图内唯一）。
    predecessor_key: str = Field(min_length=1)
    #: 后继任务的标识（路线图内唯一）。
    successor_key: str = Field(min_length=1)
    dependency_type: DependencyType = DependencyType.FINISH_TO_START

    @model_validator(mode="after")
    def _not_self_dependent(self) -> Self:
        if self.predecessor_key == self.successor_key:
            raise ValueError(f"任务 {self.predecessor_key!r} 不能依赖自己 —— 那是死锁")
        return self


class DecisionApproval(BaseModel):
    """一条决策的放行凭据。

    **领域层只认这个，不认审查报告。** 审查报告是角色层的东西（``agents/``），
    领域层反向依赖它就把层次搞反了。角色层负责翻译：把"这份报告否不否决这条
    决策"变成"放行 / 不放行 + 理由"。

    **``approved=False`` 时 ``reason`` 必填。** 否决也要说清楚为什么 ——
    否则读的人只看到"没做"，看不到"为什么没做"（与决策三态同一套理由）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    approved: bool
    #: 放行依据，或不通过的理由。
    reason: str = ""

    @model_validator(mode="after")
    def _rejection_needs_a_reason(self) -> Self:
        if not self.approved and not self.reason.strip():
            raise ValueError("不放行必须写明理由 —— 否决也要说清楚为什么")
        return self


class TaskDraft(BaseModel):
    """拆解的输入 —— 规划层对"这件事要做成哪些任务"的判断。

    **拆解的内容由规划层给，不由拆解器猜。** 标题写什么、拆成几件事、哪件
    排在前面 —— 这些是 Director 的判断（阶段五），拆解器只负责**守住边界**：
    没放行的不拆、依赖不成环、任务都挂得住决策。

    **``priority`` 留空就取决策自己的分数。** 优先级是决策那一层算出来的
    （:attr:`core.evolution.decision.EvolutionDecision.score`），另起一套数
    就等于有两个"这条候选有多重要"的口径。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 路线图内的稳定标识。依赖边指向的是它。
    key: str = Field(min_length=1)
    #: 来源决策。**一条任务只有一个来源** —— 这是三表分离的最后一环。
    decision_id: str = Field(min_length=1)
    task_type: TaskType
    title: str = Field(min_length=1)
    description: str = ""
    #: 优先级。留空（``None``）则取决策的分数。
    priority: Decimal | None = Field(default=None, ge=0, le=1)
    #: 前置任务标识。可以指向同一决策拆出来的另一个任务。
    depends_on: tuple[str, ...] = ()
    #: 为什么拆出这一件。
    rationale: str = ""


class PlannedTask(BaseModel):
    """路线图里的一个任务。

    **它带着它的决策 id。** 没有这一条，"这个任务是从哪来的"就只能靠猜 ——
    而三表分离（TASK-P1-022）要的正是"每个任务都能追溯到一条决策"。

    **验收重点由类型决定，不逐任务改。** 逐任务改就意味着七种类型又退化成了
    一种 —— 那正是 design.md §3.2 反对的"所有任务走同一套验收"。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    #: 来源决策（领域 id）。
    decision_id: str = Field(min_length=1)
    #: 来源候选。
    candidate_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    task_type: TaskType
    description: str = ""
    priority: Decimal = Field(default=Decimal(0), ge=0, le=1)
    #: 前置任务标识（只含留在路线图里的那些）。
    depends_on: tuple[str, ...] = ()
    #: 为什么拆出这一件。
    rationale: str = ""

    @property
    def acceptance_focus(self) -> str:
        """这一类任务的验收重点。"""
        return ACCEPTANCE_FOCUS[self.task_type]


class SkippedDecision(BaseModel):
    """没进路线图的一条决策 —— 连同原因。

    **不做也要说清楚为什么。** 与决策三态同一套理由：什么都不留的话，读的人
    分不清"判断为不值得"与"压根没看见"，而这两种情况的补救动作完全相反。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ScheduleSlot(BaseModel):
    """排期建议里的一个位置。

    **``wave`` 是"可以并行"的那一组。** 同一波里的任务彼此没有依赖，可以同
    时开工；跨波必须等前面那波做完。给的是**建议**不是排班 —— 真正的资源
    调度在编排层（TASK-P1-060 / P1-061）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 1 起的顺序号。
    position: int = Field(ge=1)
    #: 任务标识。
    key: str = Field(min_length=1)
    #: 第几波。同波可并行。
    wave: int = Field(ge=1)


class EvolutionRoadmap(BaseModel):
    """一份演进路线图 —— 周期、计划任务、依赖关系与排期建议。

    **四样东西缺一不可。** 只有任务没有依赖，排出来的顺序是假的；只有依赖
    没有排期，没人知道先动哪一件。

    **依赖图必须无环。** 有环的依赖图没有"先做哪个"的答案 —— 编排层拿着它
    排期只会排出一圈互相等待的任务，而且**不会报错**（每一件都在等别人）。
    所以这里在模型层就挡掉，而不是等到编排时才发现。

    **路线图里的每个任务都要挂着一条决策。** 没有决策来源的任务，就是"发现
    问题后直接改代码" —— 那条路径一旦打通，后面所有的审批门、预算门、审计
    都形同虚设（TASK-P1-022）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 周期 id —— 这一轮演进是哪一轮。
    cycle_id: str = Field(min_length=1)
    #: 计划任务。
    tasks: tuple[PlannedTask, ...] = ()
    #: 依赖关系。
    dependencies: tuple[TaskDependency, ...] = ()
    #: 排期建议。
    schedule: tuple[ScheduleSlot, ...] = ()
    #: 没进路线图的决策 —— 连同原因。
    skipped: tuple[SkippedDecision, ...] = ()
    #: 因为前置任务没进路线图而被摘掉的依赖边。
    #:
    #: **摘掉而不是保留。** 前置任务不做了，后继还在等它，等的是一个永远不会
    #: 到的东西 —— 那是死等，不是依赖。但摘掉这件事必须留痕：读的人要能看
    #: 出"这里原本有一条边，现在没了"。
    detached_dependencies: tuple[TaskDependency, ...] = ()

    @field_validator("cycle_id")
    @classmethod
    def _cycle_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("周期 id 不能是空白")
        return value.strip()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        keys = [task.key for task in self.tasks]
        if len(keys) != len(set(keys)):
            duplicates = sorted({key for key in keys if keys.count(key) > 1})
            raise DuplicateTaskKeyError(duplicates)

        known = set(keys)
        # 只有**留下的**依赖边要指向路线图里的任务；被摘掉的那些边，端点本来
        # 就不在路线图里（那正是它们被摘掉的原因）。
        for edge in self.dependencies:
            if edge.predecessor_key not in known or edge.successor_key not in known:
                missing = sorted(
                    key for key in (edge.predecessor_key, edge.successor_key) if key not in known
                )
                raise UnknownDependencyError(missing)

        cycle = _find_cycle(self.tasks, self.dependencies)
        if cycle is not None:
            raise DependencyCycleError(cycle)

        scheduled = [slot.key for slot in self.schedule]
        if sorted(scheduled) != sorted(keys):
            raise ValueError("排期建议必须恰好覆盖每一个计划任务")
        positions = sorted(slot.position for slot in self.schedule)
        if positions != list(range(1, len(self.schedule) + 1)):
            raise ValueError("排期建议的位置必须是从 1 起不重不漏的连续序号")
        return self

    @property
    def total(self) -> int:
        """计划任务总数。"""
        return len(self.tasks)

    @property
    def execution_order(self) -> tuple[str, ...]:
        """按排期建议给出的执行顺序（任务标识）。"""
        return tuple(slot.key for slot in sorted(self.schedule, key=lambda item: item.position))

    @property
    def by_type(self) -> Mapping[TaskType, tuple[PlannedTask, ...]]:
        """按任务类型分组。"""
        grouped: dict[TaskType, list[PlannedTask]] = {}
        for task in self.tasks:
            grouped.setdefault(task.task_type, []).append(task)
        return MappingProxyType({key: tuple(value) for key, value in grouped.items()})

    @property
    def decision_ids(self) -> frozenset[str]:
        """这条路线图覆盖了哪些决策。"""
        return frozenset(task.decision_id for task in self.tasks)

    def task_by_key(self, key: str) -> PlannedTask:
        """按标识取任务。取不到抛 :class:`KeyError`。"""
        for task in self.tasks:
            if task.key == key:
                return task
        raise KeyError(key)

    def predecessors_of(self, key: str) -> tuple[str, ...]:
        """某个任务的前置任务标识。"""
        return tuple(
            edge.predecessor_key for edge in self.dependencies if edge.successor_key == key
        )

    def successors_of(self, key: str) -> tuple[str, ...]:
        """某个任务的后继任务标识。"""
        return tuple(
            edge.successor_key for edge in self.dependencies if edge.predecessor_key == key
        )


def _find_cycle(
    tasks: Sequence[PlannedTask],
    dependencies: Sequence[TaskDependency],
) -> tuple[str, ...] | None:
    """依赖图里有环吗？有的话给出环上的那条路径。

    **用 DFS 记颜色，而不是 Kahn 的"排不完就是有环"。** 后者只能回答"有没有
    环"，答不出"环在哪" —— 而排期失败时读的人要看的正是后者：哪几个任务在
    互相等。
    """
    successors: dict[str, list[str]] = {task.key: [] for task in tasks}
    for edge in dependencies:
        successors.setdefault(edge.predecessor_key, []).append(edge.successor_key)

    unvisited, on_path, done = 0, 1, 2
    color = dict.fromkeys(successors, unvisited)
    path: list[str] = []

    def visit(node: str) -> tuple[str, ...] | None:
        color[node] = on_path
        path.append(node)
        for nxt in successors.get(node, ()):
            if color[nxt] == on_path:
                start = path.index(nxt)
                return (*path[start:], nxt)
            if color[nxt] == unvisited:
                found = visit(nxt)
                if found is not None:
                    return found
        path.pop()
        color[node] = done
        return None

    for node in list(successors):
        if color[node] == unvisited:
            found = visit(node)
            if found is not None:
                return found
    return None


class TaskDecomposer:
    """任务拆解器 —— 把已批准的决策拆成一份路线图。

    **它守三道边界。**

    1. **只有被放行的 ``record`` 决策才拆。** ``record`` 是决策那一层的门槛，
       放行是审查那一层的门槛 —— 两道都要过。少一道，任务就会从没有决策
       来源的地方冒出来（T1 ④）。
    2. **依赖图不成环。** 有环就没有"先做哪个"的答案。
    3. **每个任务都挂着决策。** 挂不住的拆解结果直接报错，不放行。

    **它不猜内容。** 标题、拆成几件事、哪件排在前面 —— 这些是规划层的判断，
    拆解器只做筛选、校验与排期。
    """

    def __init__(self, *, dependency_type: DependencyType = DependencyType.FINISH_TO_START) -> None:
        self._dependency_type = dependency_type

    def decompose(
        self,
        *,
        cycle_id: str,
        decision_round: DecisionRound,
        approvals: Sequence[DecisionApproval],
        drafts: Sequence[TaskDraft],
    ) -> EvolutionRoadmap:
        """把一轮的决策拆成路线图。

        ``approvals`` 是审查层给出的放行凭据。**没有凭据的决策不拆** ——
        缺凭据与被否决一样，都不产生任务。
        """
        approvals_by_decision = _index_approvals(approvals)
        decisions_by_id = {item.id: item for item in decision_round.decisions}

        surviving: list[tuple[TaskDraft, EvolutionDecision]] = []
        skipped: list[SkippedDecision] = []
        for draft in drafts:
            decision = decisions_by_id.get(draft.decision_id)
            if decision is None:
                raise UnknownDecisionError(draft.decision_id)
            reason = _why_not_planned(decision, approvals_by_decision.get(decision.id))
            if reason is not None:
                skipped.append(SkippedDecision(decision_id=decision.id, reason=reason))
                continue
            surviving.append((draft, decision))

        planned_keys = {draft.key for draft, _ in surviving}
        all_draft_keys = {draft.key for draft in drafts}
        dependencies, detached = _split_dependencies(
            drafts=[draft for draft, _ in surviving],
            planned_keys=planned_keys,
            all_draft_keys=all_draft_keys,
            dependency_type=self._dependency_type,
        )

        # 任务自己记的前置任务，只记**留下的那些** —— 前置任务没进路线图的，
        # 那条边已经被摘掉了，任务身上不该还挂着它。
        planned = [
            _to_planned(
                draft,
                decision,
                depends_on=tuple(
                    edge.predecessor_key for edge in dependencies if edge.successor_key == draft.key
                ),
            )
            for draft, decision in surviving
        ]

        return EvolutionRoadmap(
            cycle_id=cycle_id,
            tasks=tuple(planned),
            dependencies=dependencies,
            schedule=_schedule(planned, dependencies),
            skipped=tuple(skipped),
            detached_dependencies=detached,
        )


def _index_approvals(approvals: Sequence[DecisionApproval]) -> dict[str, DecisionApproval]:
    """按决策 id 索引放行凭据。

    **两份矛盾的结论直接报错。** 同一个决策既"放行"又"不放行" —— 这时候
    取哪一份都是猜，而猜错的方向是"放行"，那等于把审查绕过去了。
    """
    indexed: dict[str, DecisionApproval] = {}
    for approval in approvals:
        existing = indexed.get(approval.decision_id)
        if existing is not None and existing.approved != approval.approved:
            raise ConflictingApprovalError(approval.decision_id)
        indexed[approval.decision_id] = approval
    return indexed


def _why_not_planned(
    decision: EvolutionDecision,
    approval: DecisionApproval | None,
) -> str | None:
    """这条决策为什么不进路线图。返回 ``None`` 表示进。

    **理由按门槛的顺序说。** 先看决策本身（不是 ``record`` 就没戏），再看审查
    —— 顺序反过来的话，读的人会以为"被否决"是"没做决策"。
    """
    if not decision.produces_task:
        return f"决策是 {decision.decision.value} —— 只有 record 才派生任务"
    if approval is None:
        return "没有放行凭据 —— 未经审查放行的决策不派生任务"
    if not approval.approved:
        return approval.reason.strip() or "审查层未放行"
    return None


def _to_planned(
    draft: TaskDraft,
    decision: EvolutionDecision,
    *,
    depends_on: tuple[str, ...],
) -> PlannedTask:
    """把一份拆解草稿变成路线图里的任务。"""
    return PlannedTask(
        key=draft.key,
        decision_id=decision.id,
        candidate_id=decision.candidate_id,
        title=draft.title,
        task_type=draft.task_type,
        description=draft.description,
        priority=decision.score if draft.priority is None else draft.priority,
        depends_on=depends_on,
        rationale=draft.rationale,
    )


def _split_dependencies(
    *,
    drafts: Sequence[TaskDraft],
    planned_keys: set[str],
    all_draft_keys: set[str],
    dependency_type: DependencyType,
) -> tuple[tuple[TaskDependency, ...], tuple[TaskDependency, ...]]:
    """把依赖边分成"留下的"与"被摘掉的"。

    **指向不存在的标识是错误，不是忽略。** 那多半是拼错了 —— 拼错却当成
    "没有依赖"放过去，任务就会在一件还没做完的事上开工，而没人会发现。
    """
    kept: list[TaskDependency] = []
    detached: list[TaskDependency] = []
    seen: set[tuple[str, str]] = set()

    for draft in drafts:
        for predecessor in draft.depends_on:
            if predecessor not in all_draft_keys:
                raise UnknownDependencyError([predecessor])
            edge = TaskDependency(
                predecessor_key=predecessor,
                successor_key=draft.key,
                dependency_type=dependency_type,
            )
            if (edge.predecessor_key, edge.successor_key) in seen:
                continue
            seen.add((edge.predecessor_key, edge.successor_key))
            if predecessor in planned_keys:
                kept.append(edge)
            else:
                detached.append(edge)
    return tuple(kept), tuple(detached)


def _schedule(
    tasks: Sequence[PlannedTask],
    dependencies: Sequence[TaskDependency],
) -> tuple[ScheduleSlot, ...]:
    """排期建议 —— 拓扑序，同波可并行。

    **同波内按优先级再按标识排。** 没有这个次序，同一波里的顺序会随字典
    顺序漂移 —— 而"这个顺序是稳定的"是读排期表的人默认会假设的事。

    **波次就是并行的批次。** 依赖只在波与波之间生效；同一波里的任务彼此
    没有依赖，可以同时开工。
    """
    priority_by_key = {task.key: task.priority for task in tasks}
    successors: dict[str, list[str]] = {task.key: [] for task in tasks}
    indegree: dict[str, int] = {task.key: 0 for task in tasks}
    for edge in dependencies:
        successors[edge.predecessor_key].append(edge.successor_key)
        indegree[edge.successor_key] += 1

    def order_key(key: str) -> tuple[Decimal, str]:
        return (-priority_by_key.get(key, Decimal(0)), key)

    remaining = dict(indegree)
    slots: list[ScheduleSlot] = []
    position = 1
    wave = 1
    pending = sorted((key for key, degree in remaining.items() if degree == 0), key=order_key)
    while pending:
        for key in pending:
            slots.append(ScheduleSlot(position=position, key=key, wave=wave))
            position += 1
        next_wave: list[str] = []
        for key in pending:
            for nxt in successors.get(key, ()):
                remaining[nxt] -= 1
                if remaining[nxt] == 0:
                    next_wave.append(nxt)
        wave += 1
        pending = sorted(next_wave, key=order_key)
    return tuple(slots)


__all__ = [
    "ACCEPTANCE_FOCUS",
    "ConflictingApprovalError",
    "DecisionApproval",
    "DependencyCycleError",
    "DependencyType",
    "DuplicateTaskKeyError",
    "EvolutionRoadmap",
    "PlannedTask",
    "PlanningError",
    "ScheduleSlot",
    "SkippedDecision",
    "TaskDecomposer",
    "TaskDependency",
    "TaskDraft",
    "TaskType",
    "UnknownDecisionError",
    "UnknownDependencyError",
]
