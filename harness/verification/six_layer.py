"""六层独立验证 —— 功能 / 代码 / 架构 / 安全 / 性能 / 产品。

设计依据：docs/design.md §3.5B（六层验证表；"不要让 Coding Agent 自己决定
是否验收通过"）、§3.2（"不能所有任务都走相同的验收流程"）；docs/tasks.md
TASK-P2-014（六层独立 Runner 与独立判定器）、R-050、R-077、降级 G（性能基线
未建立前不判定性能层）。

**这一层解决的是"结论只有一个布尔值"这件事。** 阶段一的验证引擎把一次
验收折成三态（通过 / 未通过 / 没验成）。这个三态对"四项必检"够用 ——
四项都过了就是过了，没过就是没过。design.md §3.5B 的六层验证不是这个
形状：**六层各有各的问题、各有各的失败方式**。功能层挂在一条用例上，
安全层挂在一条密钥上，性能层挂在一次基准对比上，产品层挂在一次人工评估
上。把它们压成一个布尔值，压掉的正是"这次到底是哪一层坏了"这个信息 ——
而那恰恰是返工时唯一有用的信息。

所以这里的核心不是"多加五个检查"，而是三条：

1. **每层有自己的执行器与判定器。** 一层的失败不许由另一层代答 ——
   代码层看到 lint 报错，它给的诊断必须说的是 lint 怎么修，而不是
   "测试没过"。
2. **六层的结果独立记录，不合并。** 报告里要能同时看到"功能层过了、
   性能层判不了、安全层命中一条密钥"。
3. **每层自己给修复诊断。** 诊断不是从结论反推的，是这一层在判定时
   一起给出来的 —— 因为只有它知道细节。

**三种"没结论"要分开。** 除了通过与不通过，还有两种拿不到结论：

- ``ERROR`` —— 没跑成（命令不存在、超时、被杀）。该重跑。
- ``NOT_JUDGED`` —— 判定依据不具备（性能层没有基线、产品层没有人评）。
  按 docs/tasks.md 降级 G，性能层在基线建立前**不判定** —— 报"基线未
  建立"，而不是报 0，也不是报通过。

**为什么"判不了"不能算"通过"。** "没量过"与"量过且达标"是两件事：把
前者算成后者，等于在没有证据的情况下放行 —— 那与 design.md §3.5B 开头
禁止的"自己给自己打分"是同一种毛病，只是换了个方向。降级 G 写得很直白：
**报"基线未建立"，不是返回 0**。

**这一层不替代 :mod:`harness.verification.engine`。** 阶段一的引擎是四项
必检的最小实现，仍然在用；这里做的是它的**上位形态** —— 同一套"没验成 ≠
没过"的语义、同一个契约判定函数，但结论是六维的而不是一维的。

**为什么架构层可以读仓库索引。** 架构验证问的是"有没有违反依赖规则"，
而依赖关系是 :mod:`knowledge.repository_index` 的产物。重扫一遍仓库来答
同一个问题，等于把 R-064（不每轮重读仓库重建认知）又违反一次。所以这里
直接用它 —— 层次上是 harness → knowledge，同级依赖，不是反向。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from core.evidence.artifacts import ArtifactKind
from core.planning.roadmap import TaskType
from core.project.models import TargetValue
from core.task.outcome import VerificationReport, VerificationVerdict
from harness.verification.contract import (
    AcceptanceContract,
    CheckOutcome,
    CheckResult,
    ContractEvaluation,
    ContractEvidence,
    ForbiddenRule,
    RequiredCheck,
    Threshold,
    evaluate_contract,
)
from harness.verification.engine import (
    CheckExecution,
    CheckRunner,
    MetricSpec,
    SubprocessCheckRunner,
)
from knowledge.architecture.model import build_architecture
from knowledge.repository_index.index import build_index

#: 一层里一条命令的默认超时（秒）。
DEFAULT_LAYER_TIMEOUT_SECONDS: Final[int] = 1800

#: 诊断里最多列几条具体问题 —— 列全了没人读，列一条又看不出规模。
MAX_DIAGNOSIS_ITEMS: Final[int] = 5

#: 紧凑格式的一行：``path:line:col: CODE 消息``（flake8 / ruff ``--output-format=concise``）。
_LINT_COMPACT: Final[re.Pattern[str]] = re.compile(
    r"^(?P<position>[^\s:]+:\d+:\d+):\s+(?P<code>[A-Za-z]{1,5}\d{1,4})\b"
)

#: 完整格式里的规则行：行首就是规则名（ruff 默认格式先给规则名，再给位置）。
_RULE_CODE: Final[re.Pattern[str]] = re.compile(r"^(?P<code>[A-Za-z]{1,5}\d{1,4})\b")

#: 完整格式里的位置行：``--> path:line:col``。
#:
#: **位置是一整段**（``path:line:col``）—— 只留路径就把"第几行"丢了，
#: 而行号正是这一层诊断里最有用的一半。
_LINT_POSITION: Final[re.Pattern[str]] = re.compile(r"(?P<position>[^\s:]+:\d+:\d+)")


class VerificationLayer(StrEnum):
    """六层验证（design.md §3.5B 的六层表）。

    取值与那张表一一对应：功能 / 代码 / 架构 / 安全 / 性能 / 产品。
    """

    FUNCTIONAL = "functional"
    CODE = "code"
    ARCHITECTURE = "architecture"
    SECURITY = "security"
    PERFORMANCE = "performance"
    PRODUCT = "product"


class LayerOutcome(StrEnum):
    """一层的结论。

    四态而不是两态。三态（通过 / 不通过 / 没验成）对四项必检够用；六层
    之后多出来的那一态是 ``NOT_JUDGED`` —— **判定依据不具备**，与"没跑成"
    不是一回事。

    两者的处置不同：``ERROR`` 该重跑（环境问题），``NOT_JUDGED`` 该先把
    依据补上（基线没建、没人评）。混在一起就只剩"再试一次"，而重试解决
    不了缺基线这件事。
    """

    PASSED = "passed"
    FAILED = "failed"
    #: 没跑成 —— 命令不存在、超时、被信号打死。
    ERROR = "error"
    #: 判定依据不具备 —— 性能层没有基线、产品层没有人工评估。
    NOT_JUDGED = "not_judged"


class SixLayerVerdict(StrEnum):
    """六层合起来的整体结论。

    **这一层是派生的，不是存出来的** —— 与"已被取代"是推导出来的同一个
    原则。报告里存的永远是六份独立的层结果，整体结论由它们算出来。
    """

    PASSED = "passed"
    FAILED = "failed"
    #: 有一层没跑成。
    ERROR = "error"
    #: 有一层判不了（依据不具备）。
    INCOMPLETE = "incomplete"


class FindingSeverity(StrEnum):
    """一条发现的严重程度。

    ``ERROR`` 的发现会让这一层判不过去；``WARNING`` 与 ``INFO`` 只记录，
    不影响判定。分这一档是因为"记下来"与"判不过去"是两种不同的诉求 ——
    架构层看到一条没人引用的公开符号，是**记下来**；看到产品代码 import
    测试代码，是**判不过去**。
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class EvidenceState(StrEnum):
    """一层跑完之后，证据处于什么状态。

    状态决定判定器怎么走：``READY`` 看发现，``BROKEN`` 直接判"没跑成"，
    ``UNDETERMINED`` 直接判"判不了"。三种状态各有各的处置，把它们塞进
    一"detail 里靠解析文本区分，就迟早有人解析错。
    """

    READY = "ready"
    BROKEN = "broken"
    UNDETERMINED = "undetermined"


@dataclass(frozen=True, slots=True)
class Finding:
    """一条发现 —— 某一层自己的话。

    :attr:`code` 在安全层里是**禁止项的名字**（``ForbiddenRule`` 的取值）。
    契约判定靠这个把发现与禁止项对上，所以那一层的发现必须用它自己的
    规则名，不能自己编一个近义词。
    """

    code: str
    message: str
    location: str = ""
    severity: FindingSeverity = FindingSeverity.ERROR


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """修复诊断 —— 这一层自己给的。

    **诊断不能由别的层代答。** 代码层看到的是 lint 报错，它给的诊断必须
    说的是 lint 怎么修；如果六层共用一个"通用诊断"，那它就只会说"检查
    没通过" —— 而那句话对返工没有任何用处。
    """

    summary: str
    location: str = ""
    repair: str = ""


@dataclass(frozen=True, slots=True)
class LayerEvidence:
    """一层跑完之后的原始证据。

    证据是**这一层的事实**，还没经过判定。判定器读它、给出结论与诊断。
    """

    layer: VerificationLayer
    state: EvidenceState = EvidenceState.READY
    findings: tuple[Finding, ...] = ()
    metrics: Mapping[str, TargetValue] = field(default_factory=dict)
    artifacts: frozenset[ArtifactKind] = frozenset()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class LayerResult:
    """一层的结论。

    :attr:`required` 说这一层在本次验收里**算不算数**。不算数的层照样
    跑、照样记结果 —— 但它不参与整体结论。理由见
    :data:`REQUIRED_LAYERS_BY_TASK_TYPE`：不同类型的任务走不同的验收
    流程（design.md §3.2、R-077）。
    """

    layer: VerificationLayer
    outcome: LayerOutcome
    required: bool = True
    findings: tuple[Finding, ...] = ()
    diagnosis: Diagnosis | None = None
    metrics: Mapping[str, TargetValue] = field(default_factory=dict)
    artifacts: frozenset[ArtifactKind] = frozenset()
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


@dataclass(frozen=True, slots=True)
class LayerContext:
    """一层能看到的输入。

    **这里没有执行结果。** 与 :class:`harness.verification.engine.
    VerificationRequest` 同一个立场：验证端看不到"Agent 说自己做完了"。
    """

    task_id: str
    workspace: Path
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int = DEFAULT_LAYER_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """一层里的一条命令。

    :param name: 这条命令叫什么 —— 会进诊断里，所以要能让人看懂。
    :param command: 命令与参数。**不开 shell** —— 与阶段一的引擎同一条
        规矩：命令来自外部输入，``shell=True`` 等于把执行权交给它。
    :param metric: 从这次输出里读哪个指标。
    :param timeout_seconds: 超时。超时判 ``ERROR``，不判 ``FAILED``。
    :param produces: 这次跑完产出哪些证据。
    """

    name: str
    command: tuple[str, ...]
    metric: MetricSpec | None = None
    timeout_seconds: int = DEFAULT_LAYER_TIMEOUT_SECONDS
    produces: tuple[ArtifactKind, ...] = ()

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("命令不得为空")
        if self.timeout_seconds <= 0:
            raise ValueError(f"超时必须为正，得到 {self.timeout_seconds}")


@runtime_checkable
class LayerRunner(Protocol):
    """一层的执行器。

    **协议而不是具体类**，是为了让每一层的实现可以被替换 —— 架构层现在
    读仓库索引，将来可以换成跑架构测试；性能层现在跑基准命令，将来可以
    换成读一次基准结果。

    :attr:`layer` 是**这一份执行器属于哪一层**。一份执行器只能属于一层 ——
    两层共用一份执行器，那它们的判定也就必然共用，"独立"就成了空话。
    """

    @property
    def layer(self) -> VerificationLayer: ...

    async def run(self, context: LayerContext) -> LayerEvidence: ...

    def judge(self, evidence: LayerEvidence) -> LayerResult: ...


#: 契约里的必检项各归哪一层。
#:
#: **每一项恰好归一层。** 一个必检项归两层，等于有两个层对它有发言权，
#: 而层与层之间并不知道对方判了什么 —— 那就等于没有人对它负责。
CHECK_LAYERS: Final[Mapping[RequiredCheck, VerificationLayer]] = MappingProxyType(
    {
        RequiredCheck.UNIT_TESTS_PASS: VerificationLayer.FUNCTIONAL,
        RequiredCheck.INTEGRATION_TESTS_PASS: VerificationLayer.FUNCTIONAL,
        RequiredCheck.LINT_PASS: VerificationLayer.CODE,
        RequiredCheck.ARCHITECTURE_RULES_PASS: VerificationLayer.ARCHITECTURE,
        RequiredCheck.SECURITY_SCANS_PASS: VerificationLayer.SECURITY,
        RequiredCheck.PERFORMANCE_BASELINE_PASS: VerificationLayer.PERFORMANCE,
        RequiredCheck.GOAL_ATTAINMENT_MET: VerificationLayer.PRODUCT,
    }
)

#: 每类任务默认要过哪几层 —— R-077「不同任务类型走不同验收流程」。
#:
#: **这是"默认"，不是"强制"。** 契约里写明的 ``required`` 说了算（见
#: :meth:`SixLayerVerifier.required_layers`）；这份表只是"没有别的说法时
#: 按类型给一套"。把七种类型写成七套不同的层组合，就是 design.md §3.2
#: 那句"不能所有任务都走相同的验收流程"的具体落法。
REQUIRED_LAYERS_BY_TASK_TYPE: Final[Mapping[TaskType, frozenset[VerificationLayer]]] = (
    MappingProxyType(
        {
            TaskType.FEATURE: frozenset(
                {
                    VerificationLayer.FUNCTIONAL,
                    VerificationLayer.CODE,
                    VerificationLayer.ARCHITECTURE,
                    VerificationLayer.PRODUCT,
                }
            ),
            TaskType.BUGFIX: frozenset(
                {
                    VerificationLayer.FUNCTIONAL,
                    VerificationLayer.CODE,
                    VerificationLayer.ARCHITECTURE,
                    VerificationLayer.SECURITY,
                }
            ),
            TaskType.REFACTORING: frozenset(
                {
                    VerificationLayer.FUNCTIONAL,
                    VerificationLayer.CODE,
                    VerificationLayer.ARCHITECTURE,
                }
            ),
            TaskType.TEST: frozenset(
                {
                    VerificationLayer.FUNCTIONAL,
                    VerificationLayer.CODE,
                }
            ),
            TaskType.PERFORMANCE: frozenset(
                {
                    VerificationLayer.FUNCTIONAL,
                    VerificationLayer.CODE,
                    VerificationLayer.PERFORMANCE,
                }
            ),
            TaskType.DEPENDENCY: frozenset(
                {
                    VerificationLayer.FUNCTIONAL,
                    VerificationLayer.CODE,
                    VerificationLayer.ARCHITECTURE,
                    VerificationLayer.SECURITY,
                }
            ),
            TaskType.DOCS: frozenset(
                {
                    VerificationLayer.CODE,
                    VerificationLayer.PRODUCT,
                }
            ),
        }
    )
)


@dataclass(frozen=True, slots=True)
class PerformanceBaseline:
    """性能基线 —— "这个指标本来是多少"。

    :attr:`tolerance` 是**允许退化的比例**（``0.05`` = 允许退化 5%）。
    :attr:`higher_is_better` 说这个指标是越大越好（覆盖率）还是越小越好
    （延迟）—— 方向错了，"退化"就会被判成"提升"。
    """

    metric: str
    value: Decimal
    tolerance: Decimal = Decimal(0)
    higher_is_better: bool = True

    def accepts(self, measured: Decimal) -> bool:
        """实测值是否还在基线允许的范围内。"""
        if self.higher_is_better:
            floor = self.value * (Decimal(1) - self.tolerance)
            return measured >= floor
        ceiling = self.value * (Decimal(1) + self.tolerance)
        return measured <= ceiling


@dataclass(frozen=True, slots=True)
class GoalTarget:
    """产品层的一个目标 —— "这件事要做到什么程度"。

    :attr:`measured` 是 ``None`` 时，这一项目标**判不了** —— 但按契约判
    定的那条老规矩（"缺实测值的阈值算失败"），它算**没达标**而不是
    "没测过"。理由与阈值一样：无法判断不能被当成没问题。
    """

    name: str
    threshold: Threshold
    measured: TargetValue | None = None


@dataclass(frozen=True, slots=True)
class HumanReview:
    """人工评估 —— 产品层要求的"场景测试 + 人工评估"里的那一半。"""

    reviewer: str
    accepted: bool
    comment: str = ""


@dataclass(frozen=True, slots=True)
class SixLayerRequest:
    """一次六层验证的请求。

    :attr:`contract` 可选。给了就按契约判（必检项、阈值、禁止项、产物）；
    不给就只有六层自己的结论 —— 那是一个**更少但更真**的结论。
    """

    task_id: str
    workspace: Path
    contract: AcceptanceContract | None = None
    task_type: TaskType | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int = DEFAULT_LAYER_TIMEOUT_SECONDS


# --------------------------------------------------------------------------
# 共用的执行零件
# --------------------------------------------------------------------------


async def _execute(
    commands: Sequence[CommandSpec],
    context: LayerContext,
    runner: CheckRunner,
) -> tuple[tuple[CommandSpec, CheckExecution], ...]:
    """并发跑一组命令，**按命令顺序**返回结果。

    顺序要固定：并发完成顺序是不确定的，而"报告里哪条命令排在前面"会
    被人拿来读。
    """
    if not commands:
        return ()
    executions = await asyncio.gather(
        *(
            runner.run(
                spec.command,
                workspace=context.workspace,
                env=context.env,
                timeout_seconds=spec.timeout_seconds,
            )
            for spec in commands
        )
    )
    return tuple(zip(commands, executions, strict=True))


@dataclass(frozen=True, slots=True)
class _Batch:
    """一组命令跑完之后的汇总。"""

    findings: tuple[Finding, ...]
    metrics: Mapping[str, TargetValue]
    artifacts: frozenset[ArtifactKind]
    passed: int
    total: int
    broken: bool


def _summarize(raw: Sequence[tuple[CommandSpec, CheckExecution]]) -> _Batch:
    """把一组执行结果折成发现、指标与产物。

    **"没跑完"与"跑了没过"分开。** 没跑完的命令不产出指标也不产出证据
    —— 从一段被中断的输出里读出来的数字，可能是半个数字。
    """
    findings: list[Finding] = []
    metrics: dict[str, TargetValue] = {}
    artifacts: set[ArtifactKind] = set()
    passed = 0
    broken = False
    for spec, execution in raw:
        if not execution.completed:
            broken = True
            findings.append(_broken_finding(spec, execution))
            continue
        artifacts.update(spec.produces)
        if spec.metric is not None:
            value = spec.metric.read(f"{execution.stdout}\n{execution.stderr}")
            if value is not None:
                metrics[spec.metric.name] = value
        if execution.exit_code == 0:
            passed += 1
        else:
            findings.append(Finding(spec.name, _failure_detail(execution)))
    return _Batch(
        findings=tuple(findings),
        metrics=metrics,
        artifacts=frozenset(artifacts),
        passed=passed,
        total=len(raw),
        broken=broken,
    )


def _broken_finding(spec: CommandSpec, execution: CheckExecution) -> Finding:
    """一条**没跑完**的命令 —— 这是"没验成"，不是"没过"。"""
    if not execution.started:
        reason = execution.stderr.strip() or "未知原因"
        return Finding(spec.name, f"命令没能启动：{reason}")
    if execution.timed_out:
        return Finding(spec.name, f"命令超时（{spec.timeout_seconds}s）")
    return Finding(spec.name, f"命令被信号 {execution.exit_code} 终止 —— 没有跑完")


def _failure_detail(execution: CheckExecution) -> str:
    """失败详情 —— stdout 与 stderr 都给。

    测试框架把用例明细打在 stdout，而真正的错误信息常在 stderr。只给一段
    的话，看的人得自己猜另一段藏了什么。
    """
    parts: list[str] = []
    for stream in (execution.stdout, execution.stderr):
        text = stream.strip()
        if text:
            parts.append(text)
    if not parts:
        return f"退出码 {execution.exit_code}"
    return " | ".join(parts)


def _items(messages: Sequence[str]) -> str:
    """把若干条消息拼成一句能读的话（最多列几条）。"""
    shown = list(messages[:MAX_DIAGNOSIS_ITEMS])
    if len(messages) > MAX_DIAGNOSIS_ITEMS:
        shown.append(f"……另有 {len(messages) - MAX_DIAGNOSIS_ITEMS} 条")
    return "；".join(shown)


# --------------------------------------------------------------------------
# 六层的执行器与判定器
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FunctionalLayerRunner:
    """功能层 —— "需求是否实现"（单元、集成、E2E）。

    判定看的是**用例**：挂了就是没实现，不是"环境不对"。
    """

    commands: tuple[CommandSpec, ...]
    runner: CheckRunner = field(default_factory=SubprocessCheckRunner)
    layer: VerificationLayer = VerificationLayer.FUNCTIONAL

    async def run(self, context: LayerContext) -> LayerEvidence:
        raw = await _execute(self.commands, context, self.runner)
        batch = _summarize(raw)
        return LayerEvidence(
            layer=self.layer,
            state=EvidenceState.BROKEN if batch.broken else EvidenceState.READY,
            findings=batch.findings,
            metrics=batch.metrics,
            artifacts=batch.artifacts | {ArtifactKind.TEST_REPORT},
            detail=f"{batch.passed}/{batch.total} 条命令通过",
        )

    def judge(self, evidence: LayerEvidence) -> LayerResult:
        if evidence.state is EvidenceState.BROKEN:
            return LayerResult(
                self.layer,
                LayerOutcome.ERROR,
                findings=evidence.findings,
                detail=evidence.detail,
                diagnosis=Diagnosis(
                    "功能层没跑完",
                    repair="先把测试命令跑通（命令不存在 / 超时 / 被杀），再谈验收",
                ),
            )
        failures = [item for item in evidence.findings if item.severity is FindingSeverity.ERROR]
        if failures:
            return LayerResult(
                self.layer,
                LayerOutcome.FAILED,
                findings=evidence.findings,
                diagnosis=_test_diagnosis(failures),
                detail=evidence.detail,
            )
        return LayerResult(
            self.layer,
            LayerOutcome.PASSED,
            findings=evidence.findings,
            metrics=evidence.metrics,
            artifacts=evidence.artifacts,
            detail=evidence.detail,
        )


def _test_diagnosis(failures: Sequence[Finding]) -> Diagnosis:
    """功能层的修复诊断 —— 说清"哪几条用例没过"。"""
    names: list[str] = []
    for item in failures:
        names.extend(_failed_cases(item.message))
    if names:
        return Diagnosis(
            f"有 {len(names)} 条用例没过",
            location=_items(names),
            repair="让这些用例重新通过；用例本身不对就先改用例，别改断言迁就实现",
        )
    return Diagnosis(
        f"有 {len(failures)} 条测试命令没通过",
        repair="看命令输出里的失败明细；命令本身失败要先修命令",
    )


def _failed_cases(message: str) -> list[str]:
    """从测试输出里挑出失败的用例名。

    **认得两种写法**：``FAILED tests/test_x.py::test_y``（pytest）与
    ``tests/test_x.py::test_y FAILED``（unittest 风格）。认不出就返回空
    —— 那时诊断里就只说"命令没过"，而不是编一个用例名出来。
    """
    found: list[str] = []
    for line in message.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            found.append(stripped.removeprefix("FAILED ").split(" ")[0])
        elif stripped.endswith(" FAILED") and "::" in stripped:
            found.append(stripped.removesuffix(" FAILED").strip())
    return found


@dataclass(frozen=True, slots=True)
class CodeLayerRunner:
    """代码层 —— "代码是否符合规范"（Lint、静态分析）。

    判定看的是**规范**：lint 报错与类型错误都算这一层的事。
    """

    commands: tuple[CommandSpec, ...]
    runner: CheckRunner = field(default_factory=SubprocessCheckRunner)
    layer: VerificationLayer = VerificationLayer.CODE

    async def run(self, context: LayerContext) -> LayerEvidence:
        raw = await _execute(self.commands, context, self.runner)
        batch = _summarize(raw)
        return LayerEvidence(
            layer=self.layer,
            state=EvidenceState.BROKEN if batch.broken else EvidenceState.READY,
            findings=batch.findings,
            metrics=batch.metrics,
            artifacts=batch.artifacts,
            detail=f"{batch.passed}/{batch.total} 条命令通过",
        )

    def judge(self, evidence: LayerEvidence) -> LayerResult:
        if evidence.state is EvidenceState.BROKEN:
            return LayerResult(
                self.layer,
                LayerOutcome.ERROR,
                findings=evidence.findings,
                detail=evidence.detail,
                diagnosis=Diagnosis(
                    "代码层没跑完",
                    repair="先把 lint / 类型检查命令跑通，再谈验收",
                ),
            )
        failures = [item for item in evidence.findings if item.severity is FindingSeverity.ERROR]
        if failures:
            return LayerResult(
                self.layer,
                LayerOutcome.FAILED,
                findings=evidence.findings,
                diagnosis=_lint_diagnosis(failures),
                detail=evidence.detail,
            )
        return LayerResult(
            self.layer,
            LayerOutcome.PASSED,
            findings=evidence.findings,
            metrics=evidence.metrics,
            artifacts=evidence.artifacts,
            detail=evidence.detail,
        )


def _lint_diagnosis(failures: Sequence[Finding]) -> Diagnosis:
    """代码层的修复诊断 —— 说清"哪几处、哪一行"。"""
    places: list[str] = []
    for item in failures:
        places.extend(_lint_locations(item.message))
    if places:
        return Diagnosis(
            f"有 {len(places)} 处规范问题",
            location=_items(places),
            repair="按报告改这几处；不要靠加 noqa 或关规则来让它过",
        )
    return Diagnosis(
        f"有 {len(failures)} 条规范检查没通过",
        repair="看命令输出里的报错位置；规则本身不合理就改规则，但要在 ADR 里说清",
    )


def _lint_locations(message: str) -> list[str]:
    """从 lint 输出里挑出"文件:行:列"的位置（连同规则名）。

    **两种输出格式都要认**：紧凑格式（``path:line:col: CODE 消息``）与
    ruff 默认的完整格式（先一行规则名，再一行 ``--> path:line:col``）。
    只认一种的话，另一半的 lint 报错就只剩"检查没通过"这句话了。

    认不出就返回空 —— 诊断里宁可少说，也不要报一个编出来的位置。
    """
    places: list[str] = []
    pending_code = ""
    for raw in message.splitlines():
        line = raw.strip()
        if not line:
            continue
        compact = _LINT_COMPACT.match(line)
        if compact is not None:
            places.append(f"{compact.group('position')} {compact.group('code')}")
            pending_code = ""
            continue
        code_match = _RULE_CODE.match(line)
        if code_match is not None:
            pending_code = code_match.group("code")
            continue
        match = _LINT_POSITION.search(line.lstrip("-> "))
        if match is None:
            continue
        places.append(f"{match.group('position')} {pending_code}".strip())
        pending_code = ""
    return places


@dataclass(frozen=True, slots=True)
class SecurityLayerRunner:
    """安全层 —— "是否存在安全问题"（SAST、依赖漏洞、密钥扫描）。

    **退出码的约定与阶段一一致**：0 表示干净，非 0 表示命中了这一条。
    各家扫描器的输出格式不一样，而退出码是所有工具都有的共同语言。

    命中的那条发现，``code`` 用的是**禁止项的名字** —— 契约判定靠它把
    发现与 ``forbidden`` 里的规则对上。
    """

    commands: tuple[CommandSpec, ...]
    runner: CheckRunner = field(default_factory=SubprocessCheckRunner)
    layer: VerificationLayer = VerificationLayer.SECURITY

    async def run(self, context: LayerContext) -> LayerEvidence:
        raw = await _execute(self.commands, context, self.runner)
        findings: list[Finding] = []
        artifacts: set[ArtifactKind] = set()
        passed = 0
        broken = False
        for spec, execution in raw:
            if not execution.completed:
                broken = True
                findings.append(_broken_finding(spec, execution))
                continue
            artifacts.update(spec.produces)
            if execution.exit_code == 0:
                passed += 1
                continue
            findings.append(
                Finding(
                    code=spec.name,
                    message=_failure_detail(execution),
                    severity=FindingSeverity.ERROR,
                )
            )
        return LayerEvidence(
            layer=self.layer,
            state=EvidenceState.BROKEN if broken else EvidenceState.READY,
            findings=tuple(findings),
            artifacts=frozenset(artifacts),
            detail=f"{passed}/{len(raw)} 条扫描干净",
        )

    def judge(self, evidence: LayerEvidence) -> LayerResult:
        if evidence.state is EvidenceState.BROKEN:
            return LayerResult(
                self.layer,
                LayerOutcome.ERROR,
                findings=evidence.findings,
                detail=evidence.detail,
                diagnosis=Diagnosis(
                    "安全层没跑完",
                    repair="扫描没跑成不等于扫干净了 —— 先把扫描命令跑通",
                ),
            )
        hits = [item for item in evidence.findings if item.severity is FindingSeverity.ERROR]
        if hits:
            return LayerResult(
                self.layer,
                LayerOutcome.FAILED,
                findings=evidence.findings,
                diagnosis=_security_diagnosis(hits),
                detail=evidence.detail,
            )
        return LayerResult(
            self.layer,
            LayerOutcome.PASSED,
            findings=evidence.findings,
            artifacts=evidence.artifacts,
            detail=evidence.detail,
        )


def _security_diagnosis(hits: Sequence[Finding]) -> Diagnosis:
    """安全层的修复诊断 —— 说清"命中了哪一条"。"""
    rules = [item.code for item in hits]
    return Diagnosis(
        f"命中了 {len(rules)} 条禁止项",
        location=_items(rules),
        repair="按禁止项逐条处理：密钥撤掉并轮换、依赖升到无漏洞版本、公开接口改回兼容",
    )


@dataclass(frozen=True, slots=True)
class ArchitectureLayerRunner:
    """架构层 —— "是否违反架构约束"（依赖规则、架构测试）。

    **这一层不跑命令。** 依赖关系是 :mod:`knowledge.repository_index` 的
    产物，重扫一遍仓库来答同一个问题，等于把 R-064 又违反一次 —— 所以
    这里直接读索引。

    **解析失败的文件记成警告，不判不过去。** 一个语法坏掉的文件意味着
    依赖图不完整，但"不完整"不等于"有违规"；把它判成失败，就成了拿一个
    猜出来的结论去卡人。真正会因为语法坏掉而判不过去的是代码层。
    """

    layer: VerificationLayer = VerificationLayer.ARCHITECTURE

    async def run(self, context: LayerContext) -> LayerEvidence:
        index = build_index(context.workspace)
        model = build_architecture(index)
        findings: list[Finding] = []
        for violation in model.violations():
            findings.append(
                Finding(
                    code=violation.kind.value,
                    message=violation.message,
                    location=f"{violation.importer} → {violation.imported}",
                )
            )
        for cycle in model.cycles():
            findings.append(
                Finding(
                    code="cyclic_dependency",
                    message=f"模块形成了循环依赖：{cycle.describe()}",
                    location=cycle.describe(),
                )
            )
        for module in index.modules:
            if module.parse_error is not None:
                findings.append(
                    Finding(
                        code="parse_error",
                        message=f"{module.path} 解析失败：{module.parse_error}",
                        location=module.path,
                        severity=FindingSeverity.WARNING,
                    )
                )
        return LayerEvidence(
            layer=self.layer,
            state=EvidenceState.READY,
            findings=tuple(findings),
            detail=f"{len(index.modules)} 个模块，{len(findings)} 条发现",
        )

    def judge(self, evidence: LayerEvidence) -> LayerResult:
        errors = [item for item in evidence.findings if item.severity is FindingSeverity.ERROR]
        if errors:
            return LayerResult(
                self.layer,
                LayerOutcome.FAILED,
                findings=evidence.findings,
                diagnosis=_architecture_diagnosis(errors),
                detail=evidence.detail,
            )
        return LayerResult(
            self.layer,
            LayerOutcome.PASSED,
            findings=evidence.findings,
            detail=evidence.detail,
        )


def _architecture_diagnosis(errors: Sequence[Finding]) -> Diagnosis:
    """架构层的修复诊断 —— 说清"哪条依赖不该存在"。"""
    places = [item.location or item.message for item in errors]
    return Diagnosis(
        f"有 {len(errors)} 条依赖违反了架构约束",
        location=_items(places),
        repair="拆掉不该有的那条依赖（把被依赖的部分下沉，或把依赖方向倒过来）",
    )


@dataclass(frozen=True, slots=True)
class PerformanceLayerRunner:
    """性能层 —— "是否出现性能退化"（Benchmark、负载测试）。

    **没有基线就判不了，而不是判通过。** docs/tasks.md 降级 G 写得很
    直白：性能退化没有基线就没法判定，而"没法判定"不能被当成"没问题"。
    所以没基线时这一层是 ``NOT_JUDGED``，不是 ``PASSED``。
    """

    commands: tuple[CommandSpec, ...]
    baseline: PerformanceBaseline | None = None
    runner: CheckRunner = field(default_factory=SubprocessCheckRunner)
    layer: VerificationLayer = VerificationLayer.PERFORMANCE

    async def run(self, context: LayerContext) -> LayerEvidence:
        raw = await _execute(self.commands, context, self.runner)
        batch = _summarize(raw)
        if batch.broken:
            state = EvidenceState.BROKEN
        elif self.baseline is None:
            state = EvidenceState.UNDETERMINED
        else:
            state = EvidenceState.READY
        return LayerEvidence(
            layer=self.layer,
            state=state,
            findings=batch.findings,
            metrics=batch.metrics,
            artifacts=batch.artifacts,
            detail=f"{batch.passed}/{batch.total} 条命令通过",
        )

    def judge(self, evidence: LayerEvidence) -> LayerResult:
        if evidence.state is EvidenceState.BROKEN:
            return LayerResult(
                self.layer,
                LayerOutcome.ERROR,
                findings=evidence.findings,
                detail=evidence.detail,
                diagnosis=Diagnosis(
                    "性能层没跑完",
                    repair="先把基准命令跑通；没跑出来的数字不能拿来比基线",
                ),
            )
        if self.baseline is None:
            return LayerResult(
                self.layer,
                LayerOutcome.NOT_JUDGED,
                findings=evidence.findings,
                metrics=evidence.metrics,
                artifacts=evidence.artifacts,
                detail="性能基线未建立 —— 按降级 G，性能层在基线建立前不判定",
                diagnosis=Diagnosis(
                    "性能基线未建立",
                    repair="先采集一次基准并落成基线；在此之前性能层只报数、不下结论",
                ),
            )
        regression = self._regression(evidence)
        if regression is not None:
            return LayerResult(
                self.layer,
                LayerOutcome.FAILED,
                findings=evidence.findings,
                metrics=evidence.metrics,
                artifacts=evidence.artifacts,
                detail=regression,
                diagnosis=Diagnosis(
                    "出现了性能退化",
                    location=self.baseline.metric if self.baseline is not None else "",
                    repair="对照基线找出退化的那部分；基线本身不合理就先改基线（要留审计）",
                ),
            )
        return LayerResult(
            self.layer,
            LayerOutcome.PASSED,
            findings=evidence.findings,
            metrics=evidence.metrics,
            artifacts=evidence.artifacts,
            detail=evidence.detail,
        )

    def _regression(self, evidence: LayerEvidence) -> str | None:
        """退化了吗 —— 返回一句说明，没退化返回 ``None``。"""
        if self.baseline is None:
            return None
        measured = evidence.metrics.get(self.baseline.metric)
        if measured is None or measured.number is None:
            return f"指标 {self.baseline.metric} 没有实测值 —— 判不了退化"
        if self.baseline.accepts(measured.number):
            return None
        return (
            f"指标 {self.baseline.metric} 实测 {measured.number}，"
            f"基线 {self.baseline.value}（允许退化 {self.baseline.tolerance}）"
        )


@dataclass(frozen=True, slots=True)
class ProductLayerRunner:
    """产品层 —— "是否达到预期体验"（场景测试、人工评估）。

    **目标没达到是确定的失败，人没评是判不了。** 两者分开：前者要返工，
    后者要先补一次人工评估。合成一个"没过"，就会让人去改那些本来没错的
    东西。
    """

    commands: tuple[CommandSpec, ...]
    targets: tuple[GoalTarget, ...] = ()
    review: HumanReview | None = None
    runner: CheckRunner = field(default_factory=SubprocessCheckRunner)
    layer: VerificationLayer = VerificationLayer.PRODUCT

    async def run(self, context: LayerContext) -> LayerEvidence:
        raw = await _execute(self.commands, context, self.runner)
        batch = _summarize(raw)
        findings: list[Finding] = []
        if batch.broken:
            state = EvidenceState.BROKEN
        else:
            state = EvidenceState.READY
            findings.extend(self._target_findings())
        unmet = sum(1 for item in findings if item.severity is FindingSeverity.ERROR)
        detail = f"{batch.passed}/{batch.total} 条场景命令通过"
        if unmet:
            # 目标没达到时只说"命令通过"会误导：场景命令全过，正是目标没达到
            # 的常见样子（功能对了，用起来不是那么回事）。
            detail = f"{detail}；{unmet} 项产品目标未达到"
        return LayerEvidence(
            layer=self.layer,
            state=state,
            findings=tuple(findings),
            metrics=batch.metrics,
            artifacts=batch.artifacts,
            detail=detail,
        )

    def judge(self, evidence: LayerEvidence) -> LayerResult:
        if evidence.state is EvidenceState.BROKEN:
            return LayerResult(
                self.layer,
                LayerOutcome.ERROR,
                findings=evidence.findings,
                detail=evidence.detail,
                diagnosis=Diagnosis(
                    "产品层没跑完",
                    repair="先把场景命令跑通，再谈是否达到预期",
                ),
            )
        unmet = [item for item in evidence.findings if item.severity is FindingSeverity.ERROR]
        if unmet:
            return LayerResult(
                self.layer,
                LayerOutcome.FAILED,
                findings=evidence.findings,
                detail=evidence.detail,
                diagnosis=_goal_diagnosis(unmet),
            )
        if self.review is None:
            return LayerResult(
                self.layer,
                LayerOutcome.NOT_JUDGED,
                findings=evidence.findings,
                detail="产品层还缺人工评估 —— 场景测试过了不等于达到预期体验",
                diagnosis=Diagnosis(
                    "没有人工评估",
                    repair="安排一次人工评估并把结论记下来；没有它这一层只能报数",
                ),
            )
        if not self.review.accepted:
            return LayerResult(
                self.layer,
                LayerOutcome.FAILED,
                findings=evidence.findings,
                detail=evidence.detail,
                diagnosis=Diagnosis(
                    f"人工评估没有通过（{self.review.reviewer}）",
                    location=self.review.comment,
                    repair=self.review.comment or "按评审意见改，改完重新提交评估",
                ),
            )
        return LayerResult(
            self.layer,
            LayerOutcome.PASSED,
            findings=evidence.findings,
            metrics=evidence.metrics,
            artifacts=evidence.artifacts,
            detail=evidence.detail,
        )

    def _target_findings(self) -> list[Finding]:
        """目标达没达到 —— **缺实测值算没达标**，不算"没测过"。"""
        found: list[Finding] = []
        for target in self.targets:
            if target.measured is None:
                found.append(
                    Finding(
                        code="goal_not_measured",
                        message=f"目标 {target.name} 没有实测值",
                        location=target.name,
                    )
                )
                continue
            try:
                satisfied = target.threshold.evaluate(target.measured)
            except ValueError as exc:
                found.append(
                    Finding(
                        code="goal_not_comparable",
                        message=f"目标 {target.name} 未能判定：{exc}",
                        location=target.name,
                    )
                )
                continue
            if not satisfied:
                found.append(
                    Finding(
                        code="goal_not_met",
                        message=(
                            f"目标 {target.name} 未达标（要求 {target.threshold}，"
                            f"实测 {target.measured}）"
                        ),
                        location=target.name,
                    )
                )
        return found


def _goal_diagnosis(unmet: Sequence[Finding]) -> Diagnosis:
    """产品层的修复诊断 —— 说清"哪个目标没达到"。"""
    return Diagnosis(
        f"有 {len(unmet)} 项产品目标没达到",
        location=_items([item.location or item.message for item in unmet]),
        repair="按目标逐条补；实测值缺失的那几项先把测量补上",
    )


# --------------------------------------------------------------------------
# 报告与验证器
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SixLayerReport:
    """六层验证的**多维报告**。

    **这是这一层的主要产物。** 六份层结果各自独立地待在这里，没有被合并
    成一个布尔值 —— 读的人要能同时看到"功能层过了、性能层判不了、安全层
    命中一条密钥"，而这三件事对返工来说缺一不可。

    :attr:`verdict` 是**派生**的（见 :attr:`verdict` 属性），不是存下来
    的：它由六份层结果算出来，随时可以重算。
    """

    task_id: str
    layers: Mapping[VerificationLayer, LayerResult] = field(default_factory=dict)
    contract_evaluation: ContractEvaluation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "layers", MappingProxyType(dict(self.layers)))

    def result(self, layer: VerificationLayer) -> LayerResult | None:
        """某一层的结论。没有返回 ``None``。"""
        return self.layers.get(layer)

    @property
    def required_layers(self) -> tuple[VerificationLayer, ...]:
        """本次验收里算数的那些层。"""
        return tuple(
            layer
            for layer in VerificationLayer
            if layer in self.layers and self.layers[layer].required
        )

    @property
    def failed_layers(self) -> tuple[VerificationLayer, ...]:
        """判不过去的层。"""
        return self._layers_with(LayerOutcome.FAILED)

    @property
    def errored_layers(self) -> tuple[VerificationLayer, ...]:
        """没跑成的层。"""
        return self._layers_with(LayerOutcome.ERROR)

    @property
    def not_judged_layers(self) -> tuple[VerificationLayer, ...]:
        """判不了的层。"""
        return self._layers_with(LayerOutcome.NOT_JUDGED)

    @property
    def diagnoses(self) -> Mapping[VerificationLayer, Diagnosis]:
        """每一层自己的修复诊断。

        **只有没通过的层才有诊断** —— 通过的层没有要修的东西，硬给它编一
        条"建议"只会稀释这份报告。
        """
        found = {
            layer: result.diagnosis
            for layer, result in self.layers.items()
            if result.diagnosis is not None
        }
        return MappingProxyType(found)

    @property
    def verdict(self) -> SixLayerVerdict:
        """整体结论 —— **只算那些算数的层**。

        优先级是"失败 > 没跑成 > 判不了 > 通过"：

        - 有层**判不过去** —— 有一个确定的问题，最该处理；
        - 有层**没跑成** —— 那部分还不知道，不知道不等于没问题；
        - 有层**判不了** —— 依据不具备，同样不能当成通过；
        - 都没有 —— 通过。

        "失败"排在"没跑成"前面，是因为两者的处置不同：确定的失败该返工，
        没跑成的该重跑 —— 而它们都被记在各自的属性里，一件都没少。整体
        结论只是给"这次能不能放行"一个答案。
        """
        considered = self.required_layers
        outcomes = {self.layers[layer].outcome for layer in considered}
        if LayerOutcome.FAILED in outcomes:
            return SixLayerVerdict.FAILED
        if LayerOutcome.ERROR in outcomes:
            return SixLayerVerdict.ERROR
        if LayerOutcome.NOT_JUDGED in outcomes:
            return SixLayerVerdict.INCOMPLETE
        return SixLayerVerdict.PASSED

    @property
    def passed(self) -> bool:
        """能不能放行。

        **六层过了，还要契约也过了。** 两件事都要满足 —— 六层是分维度看，
        契约是"这次要的验收口径有没有满足"。
        """
        if self.verdict is not SixLayerVerdict.PASSED:
            return False
        if self.contract_evaluation is None:
            return True
        return self.contract_evaluation.passed

    def _layers_with(self, outcome: LayerOutcome) -> tuple[VerificationLayer, ...]:
        """结论是某个值的那些层（只算算数的）。"""
        return tuple(
            layer
            for layer in VerificationLayer
            if layer in self.layers
            and self.layers[layer].outcome is outcome
            and self.layers[layer].required
        )

    def to_document(self) -> dict[str, object]:
        """结构化多维报告 —— **可以 JSON 序列化**。

        每一层都带着自己的结论、发现与诊断，谁也不替谁说话。
        """
        return {
            "task_id": self.task_id,
            "verdict": self.verdict.value,
            "passed": self.passed,
            "layers": {
                layer.value: {
                    "outcome": result.outcome.value,
                    "required": result.required,
                    "detail": result.detail,
                    "findings": [
                        {
                            "code": item.code,
                            "message": item.message,
                            "location": item.location,
                            "severity": item.severity.value,
                        }
                        for item in result.findings
                    ],
                    "diagnosis": (
                        None
                        if result.diagnosis is None
                        else {
                            "summary": result.diagnosis.summary,
                            "location": result.diagnosis.location,
                            "repair": result.diagnosis.repair,
                        }
                    ),
                    "metrics": {name: str(value) for name, value in result.metrics.items()},
                    "artifacts": sorted(kind.value for kind in result.artifacts),
                }
                for layer, result in self.layers.items()
            },
            "contract": (
                None
                if self.contract_evaluation is None
                else {
                    "passed": self.contract_evaluation.passed,
                    "failures": list(self.contract_evaluation.failures),
                    "missing": list(self.contract_evaluation.missing),
                }
            ),
        }

    def to_report(self) -> VerificationReport:
        """转成领域层的单一结论 —— **给既有链路用的投影**。

        这一层报告才是多维的；这里是把它压成一个结论，好让
        :mod:`core.task.outcome` 那条链路用得上。压的过程**会丢掉维度**，
        所以只当投影看，别拿它当这份报告本身。

        ``NOT_JUDGED`` 在这里落成 ``False``（没过）而不是 ``True`` ——
        判不了不等于过了。
        """
        verdict = VerificationVerdict.PASSED
        if self.verdict is SixLayerVerdict.FAILED:
            verdict = VerificationVerdict.FAILED
        elif self.verdict in (SixLayerVerdict.ERROR, SixLayerVerdict.INCOMPLETE):
            verdict = VerificationVerdict.ERROR
        reasons = [
            f"{layer.value}：{self.layers[layer].detail or self.layers[layer].outcome.value}"
            for layer in self.required_layers
            if self.layers[layer].outcome is not LayerOutcome.PASSED
        ]
        if self.contract_evaluation is not None and not self.contract_evaluation.passed:
            reasons.extend(self.contract_evaluation.failures)
        return VerificationReport(
            verdict=verdict,
            reasons=tuple(reasons),
            checks=tuple(
                (layer.value, result.outcome is LayerOutcome.PASSED)
                for layer, result in self.layers.items()
            ),
        )


@dataclass(frozen=True, slots=True)
class SixLayerVerifier:
    """六层验证器。

    :param runners: 每一层的执行器。**没配执行器的层不跑**，也不会出现在
        报告里 —— 但如果这一层在本次验收范围内，它会以 ``ERROR`` 出现
        （"要求这一层却没配执行器"），而不是被当成通过。
    :param required_layers: 显式指定哪些层算数。给了就以它为准；没给就按
        契约 → 任务类型 → 全部有执行器的层 依次推导。
    """

    runners: Mapping[VerificationLayer, LayerRunner] = field(default_factory=dict)
    required_layers: frozenset[VerificationLayer] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "runners", MappingProxyType(dict(self.runners)))

    async def verify(self, request: SixLayerRequest) -> SixLayerReport:
        """跑一遍六层验证，给出多维报告。

        **六层并发跑，但判定各归各的。** 一层崩了不许带倒其余五层 ——
        崩了的那层记成 ``ERROR``，其余五层的结论照常给。
        """
        required = self._required_layers(request)
        context = LayerContext(
            task_id=request.task_id,
            workspace=request.workspace,
            env=request.env,
            timeout_seconds=request.timeout_seconds,
        )
        layers = list(self.runners)
        outcomes = await asyncio.gather(
            *(self._run_layer(layer, context) for layer in layers),
            return_exceptions=True,
        )
        results: dict[VerificationLayer, LayerResult] = {}
        for layer, outcome in zip(layers, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                # 取消得往上走 —— 记成"这一层出错"就等于把取消吞了。
                raise outcome
            if isinstance(outcome, BaseException):
                results[layer] = LayerResult(
                    layer,
                    LayerOutcome.ERROR,
                    required=layer in required,
                    detail=f"这一层抛了异常：{outcome!r}",
                    diagnosis=Diagnosis(
                        "这一层没跑完",
                        repair="先修这一层自己（它崩了，不是被测代码的问题）",
                    ),
                )
                continue
            results[layer] = _with_required(outcome, layer in required)
        for layer in required:
            results.setdefault(
                layer,
                LayerResult(
                    layer,
                    LayerOutcome.ERROR,
                    required=True,
                    detail="本次验收要求这一层，但没有配置执行器",
                    diagnosis=Diagnosis(
                        "这一层没有执行器",
                        repair="给这一层配一个执行器；没有执行器就不能说它过了",
                    ),
                ),
            )
        return SixLayerReport(
            task_id=request.task_id,
            layers=results,
            contract_evaluation=_contract_evaluation(request, results),
        )

    def _required_layers(self, request: SixLayerRequest) -> frozenset[VerificationLayer]:
        """哪些层算数 —— 三条依据，按顺序取第一个有的。

        1. 验证器上显式指定的；
        2. 契约里 ``required`` 的必检项所归属的层；
        3. 任务类型对应的默认层（R-077）。

        都没有就全部有执行器的层都算数。
        """
        if self.required_layers is not None:
            return self.required_layers
        if request.contract is not None:
            owned = {
                layer for check in request.contract.required if (layer := CHECK_LAYERS.get(check))
            }
            if owned:
                return frozenset(owned)
        if request.task_type is not None:
            return REQUIRED_LAYERS_BY_TASK_TYPE.get(request.task_type, frozenset())
        return frozenset(self.runners)

    async def _run_layer(
        self,
        layer: VerificationLayer,
        context: LayerContext,
    ) -> LayerResult:
        """跑一层并判定。**这一层崩了不影响别的层。**"""
        runner = self.runners[layer]
        try:
            evidence = await runner.run(context)
        except asyncio.CancelledError:
            # 取消不是"这一层出错" —— 取消得往上走，不然一次取消会变成六
            # 层各自报了一次错，然后整轮验证"正常"结束。
            raise
        except Exception as exc:  # 一层崩了不能带倒其余五层
            return LayerResult(
                layer,
                LayerOutcome.ERROR,
                detail=f"这一层抛了异常：{exc!r}",
                diagnosis=Diagnosis(
                    "这一层没跑完",
                    repair="先修这一层自己（它崩了，不是被测代码的问题）",
                ),
            )
        return runner.judge(evidence)


def _with_required(result: LayerResult, required: bool) -> LayerResult:
    """把"这一层算不算数"标上去。"""
    if result.required == required:
        return result
    return LayerResult(
        layer=result.layer,
        outcome=result.outcome,
        required=required,
        findings=result.findings,
        diagnosis=result.diagnosis,
        metrics=result.metrics,
        artifacts=result.artifacts,
        detail=result.detail,
    )


def _contract_evaluation(
    request: SixLayerRequest,
    results: Mapping[VerificationLayer, LayerResult],
) -> ContractEvaluation | None:
    """按契约判一次。

    **复用阶段一的判定函数**，不另写一套 —— 两套判定语义分叉的那一天，
    就会有一份契约给出两种答案。

    六层的结果在这里被翻译成契约看得懂的形状：每个必检项归一层，那层
    的结论就是这个必检项的结论；指标、产物、禁止项则是六层合起来的。
    """
    contract = request.contract
    if contract is None:
        return None
    checks: dict[RequiredCheck, CheckResult] = {}
    for check in contract.required:
        layer = CHECK_LAYERS.get(check)
        result = None if layer is None else results.get(layer)
        if result is None:
            continue
        checks[check] = CheckResult(check, _check_outcome(result.outcome), result.detail)
    metrics: dict[str, TargetValue] = {}
    artifacts: set[ArtifactKind] = set()
    violations: set[ForbiddenRule] = set()
    forbidden_values = {rule.value for rule in contract.forbidden}
    for result in results.values():
        metrics.update(result.metrics)
        artifacts.update(result.artifacts)
        for item in result.findings:
            if item.code in forbidden_values:
                violations.add(ForbiddenRule(item.code))
    evidence = ContractEvidence(
        task_id=request.task_id,
        checks=checks,
        metrics=metrics,
        violations=frozenset(violations),
        produced_artifacts=frozenset(artifacts),
    )
    return evaluate_contract(contract, evidence)


def _check_outcome(outcome: LayerOutcome) -> CheckOutcome:
    """层结论 → 契约看得懂的三态。

    ``NOT_JUDGED`` 落成 ``ERROR`` —— 契约那一侧没有"判不了"这一档，而
    "判不了"绝不能被当成"过了"，所以它归到"没得出结论"那一档。
    """
    if outcome is LayerOutcome.PASSED:
        return CheckOutcome.PASSED
    if outcome is LayerOutcome.FAILED:
        return CheckOutcome.FAILED
    return CheckOutcome.ERROR


__all__ = [
    "CHECK_LAYERS",
    "DEFAULT_LAYER_TIMEOUT_SECONDS",
    "MAX_DIAGNOSIS_ITEMS",
    "REQUIRED_LAYERS_BY_TASK_TYPE",
    "ArchitectureLayerRunner",
    "CodeLayerRunner",
    "CommandSpec",
    "Diagnosis",
    "EvidenceState",
    "Finding",
    "FindingSeverity",
    "FunctionalLayerRunner",
    "GoalTarget",
    "HumanReview",
    "LayerContext",
    "LayerEvidence",
    "LayerOutcome",
    "LayerResult",
    "LayerRunner",
    "PerformanceBaseline",
    "PerformanceLayerRunner",
    "ProductLayerRunner",
    "SecurityLayerRunner",
    "SixLayerReport",
    "SixLayerRequest",
    "SixLayerVerdict",
    "SixLayerVerifier",
    "VerificationLayer",
]
