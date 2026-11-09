"""循环 A 的测试构造器。

设计依据：docs/tasks.md TASK-P1-060（"pytest（打桩 Agent + 打桩验证引擎）"）。

**打桩件是"真的能跑"的对象，不是随手捏的假货。** 执行器报上来的是一份
符合 ``executors.base.ExecutionResult`` 契约的结果（``strict=True`` 会当场
拒绝类型不对的字段）；验证引擎报上来的是一份真的
:class:`core.task.outcome.VerificationReport`。

**打桩执行器会报"我做完了"。** 这正是要验的东西 —— 执行器的自我报告不参与
判定（design.md §3.4）：打桩报完成而打桩验证判失败时，任务仍应走失败路径。

**打桩落库口会记下每一次会话与事件。** "每步都留痕"这条验收重点靠它验 ——
而它同时是**还原轨迹**的手段：节点顺序与任务状态迁移序列都能从事件里读出来。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from core.task.execution_loop import (
    DEFAULT_MAX_REPAIRS,
    CollectedArtifacts,
    EventRecord,
    ExecutionPlan,
    FailureCategory,
    FailureDiagnosis,
    LoopAState,
    MergeOutcome,
    SessionRecord,
    WorkspaceInfo,
)
from core.task.outcome import ExecutionStatus, VerificationReport, VerificationVerdict
from core.task.repair import InMemoryRepairLedger, RepairLimits
from core.task.state_machine import TaskState
from executors.base import (
    AgentSession,
    ExecutionArtifacts,
    ExecutionHandle,
    ExecutionMetrics,
    ExecutionResult,
    ExecutionTask,
    SelfAssessment,
    SessionConfig,
    build_idempotency_key,
)
from orchestration.loop_a import LoopAHandlers
from orchestration.repair import RepairCoordinator

#: 固定的时间戳 —— 打桩件里不取墙钟时间。
#:
#: **为什么。** 状态与轨迹都要能"回放两次完全一样"（TASK-P1-065）。一个
#: ``datetime.now()`` 就会让两次回放产出不同的记录，那条验收重点就永远做不到。
FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)

#: 打桩任务用的 id —— **UUID 字符串形式**，因为 ``agent_session.task_id`` 是
#: 指向 ``task.id`` 的外键，而任务表的主键是 UUID。
TASK_ID = "3f1b2c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


@dataclass
class RecordingJournal:
    """把执行会话与事件记在内存里的假落库口。"""

    sessions: list[SessionRecord] = field(default_factory=list)
    #: ``open_session`` 返回的行 id —— 事件挂的是**行 id**，不是执行器侧的
    #: 会话标识。这两样东西不一样，混起来就分不清"事件属于哪一次执行"。
    session_refs: list[str] = field(default_factory=list)
    events: list[EventRecord] = field(default_factory=list)

    async def open_session(self, record: SessionRecord) -> str:
        self.sessions.append(record)
        ref = f"session-row-{len(self.sessions)}"
        self.session_refs.append(ref)
        return ref

    async def record_event(self, event: EventRecord) -> None:
        self.events.append(event)

    @property
    def nodes(self) -> tuple[str, ...]:
        """走过的节点（按事件顺序）。"""
        return tuple(event.event_type for event in self.events)

    @property
    def task_states(self) -> tuple[TaskState, ...]:
        """任务状态迁移序列 —— 把每个事件带过的状态摊平。

        **这是"全链路状态迁移序列"的还原方式。** 合并闸门一步走过两个状态
        （``INTEGRATING`` 与 ``MERGED``），所以一个事件可能带多个状态。
        """
        states: list[TaskState] = []
        for event in self.events:
            raw = event.payload.get("task_states", ())
            if not isinstance(raw, (list, tuple)):
                continue
            for value in raw:
                states.append(TaskState(str(value)))
        return tuple(states)


class StubExecutor:
    """打桩执行器 —— 只实现编排层用到的四个方法。

    **它报"我做完了"。** 打桩验证判失败时任务仍要走失败路径 —— 自我报告不
    参与判定。
    """

    def __init__(
        self,
        name: str = "stub-executor",
        status: ExecutionStatus = ExecutionStatus.COMPLETED,
    ) -> None:
        self._name = name
        self.status = status
        self.tasks: list[ExecutionTask] = []
        self.session_configs: list[SessionConfig] = []
        self._task_id = ""

    @property
    def name(self) -> str:
        return self._name

    async def create_session(self, config: SessionConfig) -> AgentSession:
        self.session_configs.append(config)
        return AgentSession(
            session_id=f"{self._name}-session-{len(self.session_configs)}",
            executor=self._name,
            workspace=config.workspace,
            created_at=FIXED_TIME,
        )

    async def execute(self, task: ExecutionTask) -> ExecutionHandle:
        self.tasks.append(task)
        self._task_id = task.task_id
        return ExecutionHandle(
            session_id=task.session_id,
            task_id=task.task_id,
            idempotency_key=build_idempotency_key(task.task_id, task.attempt),
            attempt=task.attempt,
            accepted_at=FIXED_TIME,
        )

    async def collect_result(self, session_id: str) -> ExecutionResult:
        return ExecutionResult(
            task_id=self._task_id,
            executor=self._name,
            status=self.status,
            artifacts=ExecutionArtifacts(branch="feature/x", commit="c0ffee", changed_files=2),
            execution=ExecutionMetrics(
                duration=Decimal("1"),
                tool_calls=1,
                cost=Decimal("0.5"),
                currency="CNY",
            ),
            self_assessment=SelfAssessment(completed=True),
            idempotency_key=self._task_id,
        )


@dataclass
class StubHandlers:
    """打桩的各步处理器。

    **一个对象实现八个端口。** 它们的方法名互不冲突，合成一个更省事；而
    "哪个端口归谁"由 :class:`orchestration.loop_a.LoopAHandlers` 的字段名
    决定，不靠这里。

    **每个处理器都会记下自己被调用了几次。** "返修只跑了几轮"这类断言盯的
    是**次数**，不是最终状态 —— 状态看着一样的那两轮，代价可能完全不同。
    """

    journal: RecordingJournal = field(default_factory=RecordingJournal)
    executor: StubExecutor = field(default_factory=StubExecutor)
    #: 打桩验证引擎按顺序给出的结论；用完了就重复最后一个。
    verdicts: list[VerificationVerdict] = field(default_factory=list)
    diagnosis_category: FailureCategory = FailureCategory.AUTO_REPAIRABLE
    repair_hint: str = "修好那个失败的用例"
    merged: bool = True
    calls: dict[str, int] = field(default_factory=dict)
    #: 修复额度的账（TASK-P1-063）。**默认给一份宽裕的** —— 打桩件要能跑完
    #: 整条返修链，而"全局上限挡住了"这种断言要自己把上限调小。
    repair_limits: RepairLimits = field(
        default_factory=lambda: RepairLimits(max_attempts=DEFAULT_MAX_REPAIRS)
    )
    #: 额度账本。**默认新建一本** —— 每个打桩件自己一本账，断言"两个任务共用
    #: 一本"时要显式把那本传进来。
    repair_ledger: InMemoryRepairLedger = field(default_factory=InMemoryRepairLedger)

    def _count(self, name: str) -> int:
        self.calls[name] = self.calls.get(name, 0) + 1
        return self.calls[name]

    @property
    def handlers(self) -> LoopAHandlers:
        return LoopAHandlers(
            planner=self,
            selector=self,
            workspace=self,
            collector=self,
            verifier=self,
            diagnoser=self,
            merge_gate=self,
            project_state=self,
            journal=self.journal,
            executors={self.executor.name: self.executor},
            repair_coordinator=RepairCoordinator(
                self.repair_ledger,
                limits=self.repair_limits,
            ),
        )

    # -- 各步 ---------------------------------------------------------------

    async def plan(self, state: LoopAState) -> ExecutionPlan:
        self._count("plan")
        return ExecutionPlan(
            task_id=state.task_id,
            instruction="把功能做完",
            task_type=state.task_type,
        )

    async def select(self, state: LoopAState, candidates: Sequence[str]) -> str:
        self._count("select")
        return candidates[0]

    async def create(self, state: LoopAState) -> WorkspaceInfo:
        self._count("create")
        return WorkspaceInfo(workspace_id="ws-1", path="/tmp/ases-ws-1", branch="feature/x")

    async def collect(self, state: LoopAState, result: ExecutionResult) -> CollectedArtifacts:
        self._count("collect")
        return CollectedArtifacts(
            branch=result.artifacts.branch or "",
            commit=result.artifacts.commit,
            changed_files=result.artifacts.changed_files,
        )

    async def verify(self, state: LoopAState) -> VerificationReport:
        index = self._count("verify") - 1
        verdict = VerificationVerdict.PASSED
        if self.verdicts:
            verdict = self.verdicts[min(index, len(self.verdicts) - 1)]
        reasons = () if verdict is VerificationVerdict.PASSED else ("单元测试没通过",)
        checks = (("unit_tests", verdict is VerificationVerdict.PASSED),)
        return VerificationReport(verdict=verdict, reasons=reasons, checks=checks)

    async def diagnose(self, state: LoopAState) -> FailureDiagnosis:
        self._count("diagnose")
        return FailureDiagnosis(
            reason="单元测试没通过",
            category=self.diagnosis_category,
            repair_hint=self.repair_hint,
        )

    async def merge(self, state: LoopAState) -> MergeOutcome:
        self._count("merge")
        if self.merged:
            return MergeOutcome(merged=True, commit="c0ffee")
        return MergeOutcome(merged=False, reason="合并冲突")

    async def update(self, state: LoopAState) -> None:
        self._count("update")


def ready_state(**overrides: object) -> LoopAState:
    """一份就绪的循环 A 状态。"""
    state = LoopAState(
        task_id=TASK_ID,
        project_id="p-demo",
        agent_id="agent-1",
        title="把功能做完",
        contract_bound=True,
    )
    if not overrides:
        return state
    return state.model_copy(update=overrides)


__all__ = [
    "FIXED_TIME",
    "TASK_ID",
    "RecordingJournal",
    "StubExecutor",
    "StubHandlers",
    "ready_state",
]
