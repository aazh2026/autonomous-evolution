"""Coding Agent Adapter 抽象基类与统一执行结果契约。

设计依据：docs/design.md §3.4（七个方法的核心接口、统一结果 schema、
"实际工程中还需要补充认证、超时、能力声明、取消确认、幂等键和错误分类"）、
§3.4 的执行器协议选择（ACP / CLI / SDK / OpenHands Server 的边界）；
docs/tasks.md TASK-P1-030。

**这个模块是执行器可替换性的唯一客观依据。** design.md §3.4 说"统一执行器
的核心接口**应该是**这七个方法" —— 但"应该是"不等于"就是"。任何执行器只要
实现这七个方法，编排层就能拿来用；只要多写一个自己的私有接口，编排层就得为
它改代码 —— 那时候"可替换"就成了口号。所以这里把契约钉死，并由
``tests/unit/executors/test_adapter_contract.py`` 用**同一套契约测试**跑过两个
不同的实现：契约测试全绿，是"可替换"的证据；少实现一个方法，红灯。

**七方法之外补的六件事**（design.md §3.4 明说要补）：

1. **认证** —— :attr:`SessionConfig.credential_ref` 只放**凭证引用**，不是凭证
   本身。长生命周期的密钥不进这个对象、更不进 Prompt；真正的取用发生在执行器
   进程内，用的是短时令牌。
2. **超时** —— :attr:`ExecutionTask.timeout_seconds` 必须显式声明。超时与失败
   是**两个不同的状态**（:class:`ExecutionStatus.TIMED_OUT`），因为超时之后要
   先探测会话能不能恢复，直接判失败会留下僵尸会话（R-097 / I-03；处置逻辑见
   TASK-P1-034）。
3. **能力声明** —— :class:`Capability`。编排层据此决定某个执行器能不能干某件
   活。**声明了就得兑现** —— 契约测试里有一条专门盯这件事。
4. **取消确认** —— :meth:`CodingAgentAdapter.interrupt` 返回
   :class:`CancellationAck`，区分"已请求"与"已确认停"。取消传播是四层链
   （R-096 / I-02），每层**确认收到**才推进下一层 —— 没有确认就没有推进。
5. **幂等键** —— :func:`build_idempotency_key`。规则是 ``task_id + attempt``
   （I-01），语义是"同一幂等键提交两次返回同一结果"。键由基类派生，所以任何
   两个实现算出来的键都相同 —— 否则跨执行器的去重无从谈起。
6. **错误分类** —— :class:`ErrorCategory` 分可重试 / 不可重试 / 需人工。没有
   分类，编排层只有两种选择：一律重试（撞上不可重试的错就一直撞）或一律不
   重试（网络抖动就报废一次执行）。**需人工**是第三类：授权、预算、口径这类
   事系统不该自己拍板。

**统一结果 schema 为什么这么严**（``strict=True`` / ``extra="forbid"``）：
执行器报上来的东西必须**是**它声称的类型。把 ``changed_files`` 写成字符串
``"12"``、把 ``cost`` 写成浮点数 0.1，都会被当场拒绝 —— 前者是执行器没把
schema 当回事，后者是浮点数丢精度的那个地方（``Decimal(0.1)`` 是
0.1000000000000000055511151231257827，而作者写的是 0.1）。多出来的字段说明
执行器与契约已经分叉，而分叉在这里不会被发现 —— 字段被默默接受，然后没人
知道它意味着什么。

**判定不在这里。** 这个模块只产出"执行器报了什么"，不产出"任务算不算
成功" —— 后者由 :mod:`core.task.outcome` 依据独立验证结论决定。方向必须是
"执行器 → 领域"；反过来就等于让执行器给自己打分（design.md §3.4：
"self_assessment 只是 Agent 的自我报告，不是验收结果"）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.task.outcome import ExecutionStatus

#: 空的、只读的环境变量表。共享同一个实例是安全的 —— 它不可变。
_EMPTY_ENV: Final[Mapping[str, str]] = MappingProxyType({})

#: 空的、只读的事件载荷。
_EMPTY_PAYLOAD: Final[Mapping[str, object]] = MappingProxyType({})


class Capability(StrEnum):
    """执行器的能力声明。

    编排层据此决定能不能用某个执行器干某件活。**声明了就得兑现** ——
    声明了 ``INTERRUPT`` 却不支持中断，比不声明更糟：调用方会据此以为
    "请求了就能停"，而实际上那个请求会被丢掉。
    """

    #: 支持实时事件流（:meth:`CodingAgentAdapter.stream_events`）。
    STREAM_EVENTS = "stream_events"
    #: 支持协作式中断，并能给出确认（:meth:`CodingAgentAdapter.interrupt`）。
    INTERRUPT = "interrupt"
    #: 同一执行器可同时跑多个会话。
    PARALLEL_SESSIONS = "parallel_sessions"
    #: 能直接返回结构化结果，不必从文本里猜。
    STRUCTURED_OUTPUT = "structured_output"


class ErrorCategory(StrEnum):
    """错误的可重试性分类。

    三类而不是两类，是因为"不可重试"和"该由人来看"是两种不同的后续动作：
    前者把任务判失败，后者把任务挂起等审批。混在一起，就只能把授权问题
    当成技术失败重试三次 —— 或者反过来，把网络抖动当成需要人工。
    """

    #: 瞬时故障，同样的输入重试可能成功（网络抖动、进程尚未就绪）。
    RETRYABLE = "retryable"
    #: 输入或配置不对，重试只会得到同样的失败。
    NON_RETRYABLE = "non_retryable"
    #: 需要人来决定（授权、预算、口径）—— 系统不该自己拍板。
    NEEDS_HUMAN = "needs_human"


class ExecutorError(Exception):
    """执行器抛出的错误 —— 必须带分类。

    没有 :attr:`category`，编排层就不知道该重试、该判失败、还是该挂起等
    人。所以分类是必填的，而不是"可选的补充信息"。
    """

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory,
        executor: str,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.executor = executor
        self.task_id = task_id
        self.session_id = session_id


class UnsupportedCapabilityError(ExecutorError):
    """调用了执行器没有声明的能力。

    这通常是编排层的错（该先看 ``capabilities``），所以归为不可重试 ——
    重试一次不会让没声明的能力冒出来。
    """

    def __init__(self, executor: str, capability: Capability) -> None:
        super().__init__(
            f"执行器 {executor!r} 不支持能力 {capability.value!r}",
            category=ErrorCategory.NON_RETRYABLE,
            executor=executor,
        )
        self.capability = capability


class UnknownSessionError(ExecutorError):
    """会话不存在。

    报错的理由和"报一个假状态"一样实在：给一个不存在的会话编一个
    ``RUNNING``，调用方就会一直等一个永远不会来的终态 —— 而任务是**真的**
    在跑（只是这个适配器不知道它）。所以这里必须炸出来。
    """

    def __init__(self, executor: str, session_id: str) -> None:
        super().__init__(
            f"执行器 {executor!r} 没有会话 {session_id!r}",
            category=ErrorCategory.NON_RETRYABLE,
            executor=executor,
            session_id=session_id,
        )
        self.session_id = session_id


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """创建执行会话的配置。

    :attr:`credential_ref` 是**凭证引用**而不是凭证本身。长生命周期的密钥
    不进这个对象、更不进 Prompt（design.md 安全约束）；真正的取用发生在
    执行器进程内，用的是短时令牌。

    :attr:`env` 只放非敏感的环境变量。放不放密钥不由这里判断 —— 那是安全
    门的事（TASK-P1-044）—— 但这里的注释要写在前面，免得它变成默认做法。
    """

    workspace: str
    branch: str | None = None
    model: str | None = None
    env: Mapping[str, str] = _EMPTY_ENV
    credential_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionTask:
    """交给执行器的任务。

    :attr:`session_id` 是本模块对 design.md §3.4 的一处**补全**：设计里
    ``execute(task)`` 不带会话参数，可"任务落在哪个会话上"就没有地方说。
    适配器若只记"最近一个会话"，并发跑两个任务时必然张冠李戴 —— 所以会话
    由任务自己带过来。七个方法的签名一个没动。

    :attr:`timeout_seconds` **必须显式声明**，不设默认值：超时的处置与失败
    不同（R-097 / I-03），而一个"忘了设"的默认值会让这个区别在真正出事时
    才暴露出来 —— 那时已经晚了。

    验收契约**不在**这里：它由 TASK-P1-040 定义并绑定到 ``task_id``，在那个
    任务落地之前，这里不猜它的形状。
    """

    session_id: str
    task_id: str
    title: str
    instruction: str
    timeout_seconds: int
    #: 第几次尝试。幂等键由 ``task_id + attempt`` 构成（I-01）——
    #: 重试必须换键，否则重放去重会把新的一次尝试当成旧的。
    attempt: int = 1

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError(f"attempt 必须从 1 起算，得到 {self.attempt}")
        if self.timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds 必须为正，得到 {self.timeout_seconds}")


@dataclass(frozen=True, slots=True)
class AgentSession:
    """一次执行会话。"""

    session_id: str
    executor: str
    workspace: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionHandle:
    """一次任务提交的执行凭据。"""

    session_id: str
    task_id: str
    idempotency_key: str
    attempt: int
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """执行过程中的一条事件。

    ``seq`` 在会话内单调递增 —— 事件是乱序到达的，靠它才能把顺序还原回去。
    """

    session_id: str
    task_id: str
    seq: int
    kind: str
    occurred_at: datetime
    payload: Mapping[str, object] = _EMPTY_PAYLOAD


class CancellationState(StrEnum):
    """取消请求的状态。

    三态而不是布尔值，是因为"请求发出去了"与"确认停下来了"是两件事 ——
    取消传播链（I-02）要靠后者才推进下一层。只有一个布尔值，调用方分不清
    "正在停"和"已经停了"，于是要么提前判死（留下僵尸会话），要么干等。
    """

    #: 请求已发出，尚未确认。
    REQUESTED = "requested"
    #: 执行器确认已停止。
    CONFIRMED = "confirmed"
    #: 执行器无法取消（不支持中断、进程已不可控）。
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class CancellationAck:
    """取消请求的回执。"""

    session_id: str
    state: CancellationState
    detail: str = ""


class ResultSchema(BaseModel):
    """统一结果里所有模型的共同基类。

    ``strict=True`` 是这里最要紧的一条：执行器报上来的东西必须**是**它声称
    的类型。``extra="forbid"`` 同理 —— 多出来的字段说明执行器与契约已经
    分叉，而分叉在这里不会被发现。
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class ExecutionArtifacts(ResultSchema):
    """执行产出的代码变更。

    ``branch`` / ``commit`` 允许为 ``None`` —— 有些任务确实不产出提交（纯
    分析、纯提问）。但**必须是 ``None`` 而不是空字符串**：空串看起来像
    "有个名字是空的"，``None`` 才老实地说"没有"。
    """

    branch: str | None = None
    commit: str | None = None
    changed_files: int = Field(default=0, ge=0)


class ExecutionMetrics(ResultSchema):
    """执行的量化指标。

    :attr:`duration` 单位是**秒**。用 ``Decimal`` 而不是浮点数：时长与金额
    一样会被累加，而浮点数的累加误差不会自己显形。

    :attr:`currency` 必填 —— 成本没有币种就没法和预算比（R-039 要求三处
    成本同源同币种）。``core.policy.budget.Budget.currency`` 上的默认值是
    **记账侧**的默认值，不是执行器实际计价的币种；两者混用会让成本被换算
    两次。
    """

    duration: Decimal = Field(ge=0)
    tool_calls: int = Field(ge=0)
    cost: Decimal = Field(ge=0)
    currency: str = Field(min_length=1, max_length=16)

    @field_validator("duration", "cost", mode="before")
    @classmethod
    def _decimal_from_text(cls, value: object) -> object:
        return _to_decimal(value)


class SelfAssessment(ResultSchema):
    """Agent 的自我报告。

    **这不是验收结果。** 它只是"Agent 认为自己做完了没有、有什么局限"，
    只能作为给人看的线索，不能参与判定（design.md §3.4）。判定见
    :mod:`core.task.outcome`。
    """

    completed: bool
    limitations: tuple[str, ...] = ()

    @field_validator("limitations", mode="before")
    @classmethod
    def _limitations_to_tuple(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value


class ExecutionResult(ResultSchema):
    """执行器的统一结果。

    字段与 design.md §3.4 的示例一一对应，另加两处：

    - :attr:`idempotency_key` —— 没有它就无法核对"同一幂等键返回同一结果"
      （I-01），也无法区分这是第几次尝试的产出；
    - :attr:`execution.currency` —— 见 :class:`ExecutionMetrics`。
    """

    task_id: str
    executor: str
    status: ExecutionStatus
    artifacts: ExecutionArtifacts
    execution: ExecutionMetrics
    self_assessment: SelfAssessment
    idempotency_key: str

    @field_validator("status", mode="before")
    @classmethod
    def _status_from_text(cls, value: object) -> object:
        if isinstance(value, ExecutionStatus):
            return value
        if isinstance(value, str):
            return ExecutionStatus(value)
        return value


def _to_decimal(value: object) -> object:
    """把 JSON 边界上的数字字面量转成 ``Decimal``。

    浮点数经由它的**十进制字面量**转换（``Decimal(str(value))``），而不是
    二进制精确值 —— 后者会把作者写的 0.1 变成
    0.1000000000000000055511151231257827。整数字面量是精确的，直接转。

    布尔值拒绝：``True`` 不是金额，也不是时长。

    这一整段存在的原因，是 ``strict=True`` 下 Pydantic 只认 ``Decimal``
    实例 —— 而 JSON 边界上根本没有 ``Decimal``，只有数字与数字的字面量。
    """
    if isinstance(value, bool):
        raise ValueError("布尔值不能当作金额或时长")
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{value!r} 不是合法的十进制字面量") from exc
    return value


def build_idempotency_key(task_id: str, attempt: int) -> str:
    """幂等键 —— ``task_id + attempt``（I-01）。

    规则来自 design.md §6 与 §3.4 的两处表述，合成一条：**同一任务的不同
    尝试必须算出不同的键**，否则重试会被重放去重当成"同一次提交"而丢掉。

    语义是"同一幂等键提交两次返回同一结果" —— 所以键必须由**输入**派生，
    不能由时间、随机数或会话状态派生，否则同一个请求算两次会得到两个键。
    """
    if attempt < 1:
        raise ValueError(f"attempt 必须从 1 起算，得到 {attempt}")
    return f"{task_id}:{attempt}"


class CodingAgentAdapter(ABC):
    """Coding Agent 执行器适配器。

    七个方法（design.md §3.4）加上**能力声明**与**幂等键**。实现者只填
    七个方法；:meth:`idempotency_key` 由基类提供，所以任何两个实现算出来
    的键都相同。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """执行器标识，如 ``"claude-code"``。会写进结果记录。"""

    @property
    @abstractmethod
    def capabilities(self) -> frozenset[Capability]:
        """能力声明。声明了就得兑现 —— 契约测试会逐条盯。"""

    @abstractmethod
    async def create_session(self, config: SessionConfig) -> AgentSession:
        """创建执行会话。"""

    @abstractmethod
    async def execute(self, task: ExecutionTask) -> ExecutionHandle:
        """提交开发任务。"""

    @abstractmethod
    async def get_status(self, session_id: str) -> ExecutionStatus:
        """查询执行状态。"""

    @abstractmethod
    async def stream_events(self, session_id: str) -> AsyncIterator[ExecutionEvent]:
        """获取实时执行事件。

        调用约定：``async for event in await adapter.stream_events(session_id)``。
        """

    @abstractmethod
    async def interrupt(self, session_id: str) -> CancellationAck:
        """中断执行。

        必须给出确认（:class:`CancellationAck`）："请求发出去了"与"确认停了"
        是两件事，取消传播链靠后者才推进下一层（I-02）。
        """

    @abstractmethod
    async def collect_result(self, session_id: str) -> ExecutionResult:
        """收集代码变更和执行结果。"""

    @abstractmethod
    async def terminate(self, session_id: str) -> None:
        """结束会话。"""

    def supports(self, capability: Capability) -> bool:
        """是否支持某项能力。"""
        return capability in self.capabilities

    def idempotency_key(self, task: ExecutionTask) -> str:
        """这次执行的幂等键。

        由基类派生 —— 所以任何两个实现算出来的键都相同。这是"执行器可
        替换"在数据层面的一环：换了执行器，重放去重仍然认得同一件事。
        """
        return build_idempotency_key(task.task_id, task.attempt)

    def require_capability(self, capability: Capability) -> None:
        """没有这项能力就抛错。

        实现者用它守住自己没声明的能力：调用方误用了没声明的能力时，
        得到的应该是明确的错误，而不是一个看起来正常的空结果。
        """
        if not self.supports(capability):
            raise UnsupportedCapabilityError(self.name, capability)


__all__ = [
    "AgentSession",
    "CancellationAck",
    "CancellationState",
    "Capability",
    "CodingAgentAdapter",
    "ErrorCategory",
    "ExecutionArtifacts",
    "ExecutionEvent",
    "ExecutionHandle",
    "ExecutionMetrics",
    "ExecutionResult",
    "ExecutionTask",
    "ExecutorError",
    "ResultSchema",
    "SelfAssessment",
    "SessionConfig",
    "UnknownSessionError",
    "UnsupportedCapabilityError",
    "build_idempotency_key",
]
