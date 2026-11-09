"""受控并发执行引擎 —— 多 Agent 并行跑，各跑各的。

设计依据：docs/design.md §1.2（Event Collector 收集代码 / 测试 / 缺陷 / 依赖 /
运行时五类事件；Budget 含并发上限）、§3.5A（每任务独立工作空间；Git Worktree
必选）；docs/tasks.md TASK-P2-013（交付物 ``orchestration/parallel_runner.py``；
验收"并行任务的工作空间互不干扰；并发数不超过 ``max_parallel_agents``"）、
R-013、R-017、R-045。

**这一层回答的是"并行"那两个字里的两件事：并得起，并不串。**

- **并得起** —— 并发数严格受 ``max_parallel_agents`` 约束；
- **并不串** —— 工作区、事件流、成本三样，**并行跑的任务之间互不干扰**。

**上限只有一个出处。** 并发上限来自 :class:`core.policy.budget.Budget` 的
``max_parallel_agents``（R-013：Budget 含日成本与并发上限，可配）——
编排层不另设一个"最大并发"的参数，因为两处各有一个上限时，
"现在到底是几"这个问题就永远答不准。

**并行不干扰，靠的是结构而不是约定。**

- **工作区**：一任务一目录一分支（:mod:`harness.workspace.naming` 的约定），
  而 git 自己就不许一条分支被两个 worktree 同时检出 —— 分支冲突在结构上
  不可能发生，不需要谁记得"别用同一个目录"；
- **临时目录**：每个任务一个 ``mkdtemp``，名字由系统保证唯一；
- **事件流**：**一个任务一个 NDJSON 文件** —— 串流在结构上就不可能发生；
- **成本**：按任务与会话分别记账，**并且按币种分桶归集** —— 两种币种的
  数字加在一起是一个没有意义的数。

**它只做四件事**：按上限并发跑、给每个任务一块互不干扰的地盘、把事件分流
到各自的文件、把成本按任务归集。**它不落状态** —— 与
:mod:`orchestration.gates` 同一套做法：算结论、给迁移，写状态的是调用方。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, cast, runtime_checkable
from uuid import uuid4

from core.metrics.cost import CostAmount
from core.policy.budget import Budget
from core.task.execution_loop import WorkspaceInfo
from core.task.outcome import ExecutionStatus
from harness.workspace.manager import WorkspaceManager
from orchestration.cost_accounting import ExecutionCostAccountant

#: 临时目录名前缀。**每个任务一个目录**，系统保证唯一。
_SCRATCH_PREFIX: Final[str] = "ases-parallel-"

#: 任务号里能安全进文件名的部分。别的字符一律换成横线。
_UNSAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_-]+")


class EventCategory(StrEnum):
    """五类事件 —— design.md §1.2 的 Event Collector 那一行。

    **分类是为了能按类查。** "这一轮改了什么"如果只是一串自由文本，就得
    全文搜；分成代码 / 测试 / 缺陷 / 依赖 / 运行时五类，查询才是索引而不是
    猜测。
    """

    #: 代码事件 —— 改了哪个文件、哪一行。
    CODE = "code"
    #: 测试事件 —— 跑了哪条用例、过了还是没过。
    TEST = "test"
    #: 缺陷事件 —— 发现了什么问题。
    DEFECT = "defect"
    #: 依赖事件 —— 动了哪条依赖。
    DEPENDENCY = "dependency"
    #: 运行时事件 —— 执行器本身发生了什么。
    RUNTIME = "runtime"


class EventStreamError(RuntimeError):
    """事件流出了岔子。"""


@dataclass(frozen=True, slots=True)
class ParallelEvent:
    """一条事件 —— **它属于一个任务，且只属于那个任务**。

    :attr:`sequence` 是**任务内**的顺序号，不是全局的 —— 两个任务的第一条
    事件都是序号 1，它们互不相干。
    """

    task_id: str
    session_id: str
    sequence: int
    category: EventCategory
    payload: Mapping[str, object] = field(default_factory=dict)

    def to_json(self) -> str:
        """一行 NDJSON。"""
        return json.dumps(
            {
                "task_id": self.task_id,
                "session_id": self.session_id,
                "sequence": self.sequence,
                "category": self.category.value,
                "payload": dict(self.payload),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )


class TaskEventSink:
    """一个任务的事件出口。

    **出口是按任务发的，不是按会话发的。** 任务拿到出口之后只管喊
    ``emit``；出口自己盖着 ``task_id`` 与 ``session_id`` —— 想写别人的事件
    写不成，因为那两个字不是它能定的。

    :param store: 事件库。
    :param task_id: 这个出口属于谁。
    :param session_id: 这一轮执行的会话。
    """

    __slots__ = ("_emitted", "_sequence", "_session_id", "_store", "_task_id")

    def __init__(self, store: NdjsonEventStore, *, task_id: str, session_id: str) -> None:
        self._store = store
        self._task_id = task_id
        self._session_id = session_id
        self._sequence = 0
        self._emitted = 0

    @property
    def task_id(self) -> str:
        """这个出口属于谁。"""
        return self._task_id

    @property
    def emitted(self) -> int:
        """这个出口已经写出去多少条。"""
        return self._emitted

    async def emit(
        self,
        category: EventCategory,
        payload: Mapping[str, object] | None = None,
    ) -> ParallelEvent:
        """写一条事件。

        :param category: 哪一类。
        :param payload: 事件的细节。**值写不进 JSON 的按字符串记** ——
            事件是给人看的日志，不是要能被反序列化回来的数据。
        """
        event = ParallelEvent(
            task_id=self._task_id,
            session_id=self._session_id,
            sequence=self._sequence,
            category=category,
            payload=dict(payload or {}),
        )
        self._sequence += 1
        self._emitted += 1
        await self._store.write(event)
        return event


class NdjsonEventStore:
    """按任务分文件的 NDJSON 事件库。

    **一个任务一个文件。** 串流在结构上就不可能发生 —— 没有任何一个文件
    装着两个任务的事件。

    :param root: 事件库的根目录。
    """

    __slots__ = ("_lock", "_root")

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        """事件库的根目录。"""
        return self._root

    def path_of(self, task_id: str) -> Path:
        """这个任务的事件文件。"""
        return self._root / f"{_safe(task_id)}.ndjson"

    def stream_for(self, task_id: str, *, session_id: str) -> TaskEventSink:
        """给一个任务发一个出口。"""
        return TaskEventSink(self, task_id=task_id, session_id=session_id)

    async def write(self, event: ParallelEvent) -> None:
        """写一条事件。

        **写在锁里。** 同一个文件里的事件按喊出来的顺序落盘 —— 顺序乱了，
        "先失败后修复"这类因果就说不清了。
        """
        line = event.to_json()
        async with self._lock:
            path = self.path_of(event.task_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()

    def read(self, task_id: str) -> tuple[ParallelEvent, ...]:
        """读回这个任务的事件。"""
        path = self.path_of(task_id)
        if not path.exists():
            return ()
        events: list[ParallelEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            events.append(_event_from_json(text))
        return tuple(events)

    def audit(self) -> tuple[str, ...]:
        """查一遍有没有串流 —— 返回串了流的任务号（**没串就是空**）。

        **串流是查得出来的。** 每个文件里的事件都带着 ``task_id``；某个
        文件里出现了别的任务的事件，就是串了。串流在结构上不会发生，但
        "不会发生"与"发生了能查出来"是两回事 —— 后者才让 bug 有地方冒头。
        """
        problems: list[str] = []
        if not self._root.exists():
            return ()
        for path in sorted(self._root.glob("*.ndjson")):
            expected = path.stem
            for line in path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text:
                    continue
                raw = cast(object, json.loads(text))
                if not isinstance(raw, dict):
                    continue
                found = _text_of(raw.get("task_id"))
                if found != expected:
                    problems.append(f"{path.name} 里混进了「{found}」的事件")
        return tuple(problems)


@dataclass(frozen=True, slots=True)
class TaskWorkspace:
    """一个任务并行时拿到的那一份地盘。

    :attr:`path` 是 git worktree 的检出位置，:attr:`scratch` 是这个任务
    专属的临时目录。**两样都不是共享的** —— 共享的目录在并行下就是等着被
    踩的那一块。
    """

    task_id: str
    workspace_id: str
    branch: str
    path: Path
    scratch: Path
    session_id: str


@dataclass(frozen=True, slots=True)
class ParallelTaskResult:
    """一个任务跑完之后的结论。"""

    status: ExecutionStatus
    cost: CostAmount
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """并行跑完一个任务的结论 —— **成本挂在它自己名下**。

    :attr:`events` 是这个任务写出去的事件条数。
    """

    task_id: str
    status: ExecutionStatus
    cost: CostAmount
    events: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ParallelReport:
    """一轮并发跑完之后的总账。

    :attr:`totals` **按币种分桶** —— 两种币种的数字加在一起是个没有意义
    的数，而"把 CNY 当 USD 加"这种错查不出来：加起来仍然是个像样的金额。
    """

    outcomes: tuple[TaskOutcome, ...]
    limit: int
    peak_concurrency: int
    totals: Mapping[str, Decimal] = field(default_factory=dict)

    def outcome_of(self, task_id: str) -> TaskOutcome | None:
        """某个任务的结论。"""
        for outcome in self.outcomes:
            if outcome.task_id == task_id:
                return outcome
        return None

    def total_of(self, currency: str) -> Decimal:
        """某个币种的总花费。"""
        return self.totals.get(currency.upper(), Decimal(0))

    @property
    def failed(self) -> tuple[TaskOutcome, ...]:
        """没跑成的那些。"""
        return tuple(
            outcome for outcome in self.outcomes if outcome.status is not ExecutionStatus.COMPLETED
        )


class ParallelTaskError(RuntimeError):
    """任务失败了 —— **但它已经花的钱要说得清**。

    **失败不等于没花钱。** 任务跑了半个钟头然后炸了，那半个钟头的钱已经
    花出去了；把它记成零，账本就比 reality 好看，而账本好看是查不出来的
    那种错。
    """

    def __init__(self, message: str, *, cost: CostAmount) -> None:
        super().__init__(message)
        self.cost = cost


@runtime_checkable
class ParallelTaskPort(Protocol):
    """并行任务要做的事。

    **端口只有两样**：这个任务是谁、怎么把它跑起来。编排层不该知道任务
    内部怎么跑 —— 那是执行器的事。
    """

    @property
    def task_id(self) -> str:
        """这个任务的标识。"""

    async def run(
        self,
        workspace: TaskWorkspace,
        events: TaskEventSink,
    ) -> ParallelTaskResult:
        """在给定的地盘上把这件事干完。"""


@runtime_checkable
class WorkspacePort(Protocol):
    """工作空间的分配与释放。

    **端口是同步的** —— 分配工作空间要真的调 git，而 git 是阻塞的。挪到
    线程里跑是调用方的事（:class:`ParallelRunner` 负责挪）。
    """

    def acquire(self, task_id: str, *, holder: str) -> WorkspaceInfo:
        """给这个任务分配工作空间。"""

    def release(self, task_id: str) -> None:
        """释放工作空间。**分支留着** —— 分支是产出。"""


class ManagerWorkspacePort:
    """:class:`~harness.workspace.manager.WorkspaceManager` 的适配。

    **编排层不该知道工作空间怎么建。** 这一层只把 manager 的返回值翻译成
    领域里的 :class:`~core.task.execution_loop.WorkspaceInfo`。
    """

    __slots__ = ("_manager",)

    def __init__(self, manager: WorkspaceManager) -> None:
        self._manager = manager

    def acquire(self, task_id: str, *, holder: str) -> WorkspaceInfo:
        workspace = self._manager.acquire(task_id, holder=holder)
        return WorkspaceInfo(
            workspace_id=workspace.workspace_id,
            path=str(workspace.path),
            branch=workspace.branch,
        )

    def release(self, task_id: str) -> None:
        self._manager.release(task_id)


class ParallelRunner:
    """受控并发执行引擎。

    :param budget: 预算配置 —— **并发上限从这儿来**
        （``max_parallel_agents``，R-013）。别处再设一个上限，就会有两个
        答案。
    :param workspaces: 工作空间的分配面。
    :param events: 事件库。
    :param cost_accountant: 成本记账口。没给就只归集不记账。
    :param scratch_root: 临时目录的根。没给就用系统临时目录。

    **它不落状态。** 与 :mod:`orchestration.gates` 同一套做法：算结论、
    给迁移，写状态的是调用方。
    """

    __slots__ = (
        "_accountant",
        "_budget",
        "_events",
        "_holder",
        "_live",
        "_peak",
        "_scratch_root",
        "_workspace_lock",
        "_workspaces",
    )

    def __init__(
        self,
        *,
        budget: Budget,
        workspaces: WorkspacePort,
        events: NdjsonEventStore,
        cost_accountant: ExecutionCostAccountant | None = None,
        scratch_root: Path | None = None,
    ) -> None:
        self._budget = budget
        self._workspaces = workspaces
        self._events = events
        self._accountant = cost_accountant
        self._scratch_root = scratch_root
        self._holder = f"parallel-runner-{uuid4().hex[:8]}"
        self._workspace_lock = asyncio.Lock()
        self._peak = 0
        self._live = 0

    @property
    def limit(self) -> int:
        """并发上限。"""
        return self._budget.max_parallel_agents

    @property
    def peak_concurrency(self) -> int:
        """最近一轮真正同时跑着的任务数。"""
        return self._peak

    async def run(self, tasks: Sequence[ParallelTaskPort]) -> ParallelReport:
        """并发跑这些任务。

        **一个任务失败不影响其余任务。** 失败的那个记成 ``FAILED``，其余的
        照跑 —— 并行的意义就在这儿：一个任务炸了就把整批掀了，等于白并行。

        **返回的结论按输入的顺序排。** 并行的完成顺序是随机的，而"这次跑
        的第几个任务失败了"不该取决于谁跑得快。
        """
        if not tasks:
            return ParallelReport(
                outcomes=(),
                limit=self.limit,
                peak_concurrency=0,
                totals={},
            )
        semaphore = asyncio.Semaphore(self.limit)
        outcomes: list[TaskOutcome | None] = [None] * len(tasks)
        self._peak = 0
        self._live = 0
        async with asyncio.TaskGroup() as group:
            for index, task in enumerate(tasks):
                group.create_task(self._run_one(task, semaphore, outcomes, index))
        finished = tuple(outcome for outcome in outcomes if outcome is not None)
        return ParallelReport(
            outcomes=finished,
            limit=self.limit,
            peak_concurrency=self._peak,
            totals=_totals_of(finished),
        )

    async def _run_one(
        self,
        task: ParallelTaskPort,
        semaphore: asyncio.Semaphore,
        outcomes: list[TaskOutcome | None],
        index: int,
    ) -> None:
        """一个任务的完整生命周期 —— **从拿到号牌到交回号牌**。

        **信号量在这里面。** 拿了号牌才开始干活，所以"同时跑着几个"的上限
        就是配置里的那个数，不会因为多了几个任务而多。
        """
        async with semaphore:
            self._live += 1
            self._peak = max(self._peak, self._live)
            try:
                outcomes[index] = await self._execute(task)
            finally:
                self._live -= 1

    async def _execute(self, task: ParallelTaskPort) -> TaskOutcome:
        """跑一个任务：拿工作区 → 给地盘 → 干活 → 记账 → 收摊。"""
        task_id = task.task_id
        session_id = f"{task_id}:{uuid4().hex[:12]}"
        sink = self._events.stream_for(task_id, session_id=session_id)
        workspace: WorkspaceInfo | None = None
        scratch: Path | None = None
        result: ParallelTaskResult | None = None
        try:
            workspace = await self._acquire(task_id)
            scratch = self._make_scratch()
            context = TaskWorkspace(
                task_id=task_id,
                workspace_id=workspace.workspace_id,
                branch=workspace.branch,
                path=Path(workspace.path),
                scratch=scratch,
                session_id=session_id,
            )
            result = await task.run(context, sink)
            await self._record_cost(task_id, session_id, result.cost)
            return TaskOutcome(
                task_id=task_id,
                status=result.status,
                cost=result.cost,
                events=sink.emitted,
                detail=result.detail,
            )
        except Exception as error:
            return TaskOutcome(
                task_id=task_id,
                status=ExecutionStatus.FAILED,
                cost=result.cost if result is not None else _spent_by(error),
                events=sink.emitted,
                detail=f"{type(error).__name__}: {error}",
            )
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            if workspace is not None:
                await self._release(task_id)

    async def _acquire(self, task_id: str) -> WorkspaceInfo:
        """拿工作空间。

        **分配工作空间要真的调 git，而 git 是阻塞的** —— 所以挪到线程里跑，
        再用一把锁串起来。git 本身就串行化 worktree 的增删，所以这里串行
        不吃亏；而这把锁还顺带保证了"查租约 → 建租约 → 建 worktree"这串
        动作不会在中间被打断 —— 打断了就可能留下一个"目录在、租约不在"
        的工作空间。
        """
        async with self._workspace_lock:
            return await asyncio.to_thread(
                self._workspaces.acquire,
                task_id,
                holder=self._holder,
            )

    async def _release(self, task_id: str) -> None:
        """释放工作空间。

        **释放失败不许盖掉任务本身的结论。** 工作空间收不干净是运维的事，
        任务跑成什么样是另一回事 —— 两件事混在一起，就分不清"这次失败是
        因为活没干成，还是因为收摊没收干净"。
        """
        async with self._workspace_lock:
            try:
                await asyncio.to_thread(self._workspaces.release, task_id)
            except Exception:
                return

    def _make_scratch(self) -> Path:
        """这个任务专属的临时目录。

        **每个任务一个。** 共用一个目录的话，两个任务往里写同名文件就会
        互相踩 —— 而那正是"并行不干扰"要防的事。
        """
        if self._scratch_root is not None:
            self._scratch_root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=self._scratch_root))

    async def _record_cost(
        self,
        task_id: str,
        session_id: str,
        cost: CostAmount,
    ) -> None:
        """把这次执行的成本记到账本与观测里。

        **记的是"谁花的"。** 账本按任务与会话记 —— 两个并行任务的钱落到
        同一个会话上，就说不清哪笔是谁的。
        """
        if self._accountant is None:
            return
        await self._accountant.record(cost, task_id=task_id, session_id=session_id)


# --------------------------------------------------------------------------
# 内部小工具
# --------------------------------------------------------------------------


def _safe(task_id: str) -> str:
    """任务号里能安全进文件名的部分。"""
    return _UNSAFE.sub("-", task_id) or "task"


def _totals_of(outcomes: Sequence[TaskOutcome]) -> dict[str, Decimal]:
    """按币种分桶归集。"""
    totals: dict[str, Decimal] = {}
    for outcome in outcomes:
        currency = outcome.cost.currency
        totals[currency] = totals.get(currency, Decimal(0)) + outcome.cost.amount
    return totals


def _spent_by(error: Exception) -> CostAmount:
    """失败之前已经花掉的钱。"""
    if isinstance(error, ParallelTaskError):
        return error.cost
    return CostAmount(Decimal(0), "USD")


def _event_from_json(text: str) -> ParallelEvent:
    """读回一条事件。"""
    raw = cast(object, json.loads(text))
    if not isinstance(raw, dict):
        raise EventStreamError(f"事件不是 JSON 对象：{text[:80]}")
    payload = raw.get("payload")
    clean: dict[str, object] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(key, str):
                clean[key] = value
    return ParallelEvent(
        task_id=_text_of(raw.get("task_id")),
        session_id=_text_of(raw.get("session_id")),
        sequence=_int_of(raw.get("sequence")),
        category=_category_of(raw.get("category")),
        payload=clean,
    )


def _text_of(value: object) -> str:
    """取字符串。"""
    return value if isinstance(value, str) else ""


def _int_of(value: object) -> int:
    """取整数。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _category_of(value: object) -> EventCategory:
    """取事件类别。**认不出来就说认不出来。**"""
    if isinstance(value, str):
        try:
            return EventCategory(value)
        except ValueError:
            return EventCategory.RUNTIME
    return EventCategory.RUNTIME


__all__ = [
    "EventCategory",
    "EventStreamError",
    "ManagerWorkspacePort",
    "NdjsonEventStore",
    "ParallelEvent",
    "ParallelReport",
    "ParallelRunner",
    "ParallelTaskError",
    "ParallelTaskPort",
    "ParallelTaskResult",
    "TaskEventSink",
    "TaskOutcome",
    "TaskWorkspace",
    "WorkspacePort",
]
