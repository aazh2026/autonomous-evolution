"""定时项目扫描的编排 —— 从索引推导跨模块的长期积累问题。

设计依据：docs/tasks.md TASK-P3-012（交付物：跨模块、长期积累问题的定期
扫描；验收条件：定期扫描能发现事件触发发现不了的问题（跨模块耦合类））；
R-080（事件驱动与定期扫描结合）；docs/design.md §4。

**这一层只读索引，不读仓库。** 输入是一份 :class:`ArchitectureModel`
（由 :class:`~knowledge.repository_index.index.RepositoryIndex` 推导而来），
输出是一份 :class:`~core.evolution.scan.ScanReport`。这正是 TASK-P2-016
的验收条件要求的形状（R-064：不每轮重读仓库重建认知）：扫描是**定期**跑
的，一次全量重读仓库的成本会被放大成"每天一次全仓库遍历"，而读索引只要
几毫秒。

**五种信号都是从索引里推出来的事实，不是判断。** 耦合环是强连通分量算
出来的；耦合枢纽是扇入数数出来的；没人用的公开接口是"测试文件的 import
列表里有没有它"查出来的。唯一的判断成分是**阈值**（扇入到多少算枢纽），
而阈值写在策略里，与结论分开（见 :class:`~core.evolution.scan.ScanPolicy`）。

**信号不是候选，候选也不是任务。** 这一层只产出信号；把信号变成候选是
:meth:`ProjectScanner.candidates` 的事，而候选进入决策（做 / 不做 / 推迟）
是决策层的事（P1-054）。三层各有各的形状，抄本迟早对不上。

**证据必须是能打开的文件路径。** 候选的 evidence 要求"可独立核验"
（R-029）。所以这里给的证据不是"耦合度 7"，而是**模块在仓库里的那个文件**
—— 拿着它就能去看、去改、去查它到底被谁依赖。
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Final

from core.evolution.assessment import GapSeverity
from core.evolution.candidate import EvolutionCandidate
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.scan import (
    ScanFact,
    ScanKind,
    ScanPolicy,
    ScanReport,
    ScanScope,
    ScanSignal,
)
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from knowledge.architecture.model import ArchitectureModel, ViolationKind
from knowledge.repository_index.index import ModuleLayer

#: **结构性事实的置信度**。环、扇入、依赖方向 —— 这些是从索引里算出来的，
#: 索引说它是什么它就是什么（前提是索引本身是准的）。
STRUCTURAL_CONFIDENCE: Final[Decimal] = Decimal("0.9")

#: **静态覆盖事实的置信度**。没人引用过某个公开符号 —— 这是"没有任何一条
#: 用例 import 过它"，而不是"它没被执行过"：动态调用（``getattr``）、
#: 插件式注册这类用法静态上看不见。所以它比结构性事实低一档。
STATIC_COVERAGE_CONFIDENCE: Final[Decimal] = Decimal("0.8")

#: 耦合枢纽证据里最多列几个依赖者。**名单全列会让证据条目变得没法读**，
#: 而"还有更多"这件事由 ``fan_in`` 这个数字说清楚了。
MAX_LISTED_DEPENDENTS: Final[int] = 10


class ProjectScanner:
    """定期扫一遍项目，找出跨模块的长期积累问题。

    :param architecture: 架构模型 —— 扫描的全部依据。
    :param policy: 扫描策略。默认扫全部五种。

    **它是纯函数式的**：扫一遍不改变任何东西，也不写任何地方。定时扫描
    天天跑，同一份索引扫两次得到同样的报告 —— 这一点很重要，因为"今天
    比昨天多了一个耦合环"这句话只有在两次扫描可比时才成立。
    """

    __slots__ = ("_architecture", "_policy")

    def __init__(
        self,
        architecture: ArchitectureModel,
        *,
        policy: ScanPolicy | None = None,
    ) -> None:
        self._architecture = architecture
        self._policy = policy or ScanPolicy()

    @property
    def policy(self) -> ScanPolicy:
        """这次的扫描策略。"""
        return self._policy

    def scan(self, scope: ScanScope, *, scan_id: str) -> ScanReport:
        """扫一遍，返回报告。

        :param scope: 扫描范围。
        :param scan_id: 这次扫描的标识 —— 报告里要能看出是哪一次。
        """
        signals: list[ScanSignal] = []
        for kind, collect in self._collectors():
            if not self._policy.scans(kind):
                continue
            signals.extend(collect(scope))
        signals.sort(key=lambda item: (item.kind, item.module))
        return ScanReport(
            scan_id=scan_id,
            scope=scope,
            policy=self._policy,
            signals=tuple(signals),
            examined_modules=self._examined(scope),
        )

    # ------------------------------------------------------------ 采集器

    def _collectors(self) -> tuple[tuple[ScanKind, Callable[[ScanScope], list[ScanSignal]]], ...]:
        """(种类, 采集函数) 的对照表。

        **顺序固定** —— 报告里的信号按种类排序给出，读的人能一眼看出
        "先是环、再是枢纽"。
        """
        return (
            (ScanKind.DEPENDENCY_CYCLE, self._cycle_signals),
            (ScanKind.COUPLING_HUB, self._coupling_hub_signals),
            (ScanKind.LAYER_VIOLATION, self._layer_violation_signals),
            (ScanKind.UNTESTED_MODULE, self._untested_module_signals),
            (ScanKind.ORPHANED_PUBLIC_API, self._orphaned_api_signals),
        )

    def _examined(self, scope: ScanScope) -> int:
        """这次扫了多少个模块。"""
        return sum(1 for item in self._architecture.index.modules if scope.covers(item.module))

    def _in_scope(self, scope: ScanScope, *modules: str) -> bool:
        """这组模块在范围内吗。

        **有一个在范围内就算。** 耦合环是几个模块的事，只要其中一个落在
        范围内，这条环就与这次扫描有关 —— 用"全部在范围内"会把"半个环"
        藏起来，而半个环照样是个问题。
        """
        return any(scope.covers(module) for module in modules)

    def _cycle_signals(self, scope: ScanScope) -> list[ScanSignal]:
        """耦合环。"""
        found: list[ScanSignal] = []
        for cycle in self._architecture.cycles():
            if not self._in_scope(scope, *cycle.modules):
                continue
            found.append(
                ScanSignal(
                    kind=ScanKind.DEPENDENCY_CYCLE,
                    module=cycle.modules[0],
                    modules=cycle.modules,
                    severity=GapSeverity.HIGH,
                    summary=f"模块之间形成循环依赖：{cycle.describe()}",
                    facts=(
                        ScanFact(name="cycle_size", value=str(len(cycle.modules))),
                        ScanFact(name="modules", value=", ".join(cycle.modules)),
                    ),
                )
            )
        return found

    def _coupling_hub_signals(self, scope: ScanScope) -> list[ScanSignal]:
        """耦合枢纽 —— 被太多模块依赖的源模块。"""
        threshold = self._policy.coupling_hub_threshold
        found: list[ScanSignal] = []
        for item in self._architecture.index.modules:
            if item.layer is not ModuleLayer.SOURCE:
                continue
            if not scope.covers(item.module):
                continue
            dependents = self._architecture.dependents(item.module)
            if len(dependents) < threshold:
                continue
            impact = self._architecture.impact_of(item.module)
            listed = dependents[:MAX_LISTED_DEPENDENTS]
            found.append(
                ScanSignal(
                    kind=ScanKind.COUPLING_HUB,
                    module=item.module,
                    modules=(item.module,),
                    severity=(
                        GapSeverity.HIGH if len(dependents) >= 2 * threshold else GapSeverity.MEDIUM
                    ),
                    summary=(
                        f"模块 {item.module} 被 {len(dependents)} 个模块依赖，"
                        f"改一处会波及 {len(impact)} 个模块"
                    ),
                    facts=(
                        ScanFact(name="fan_in", value=str(len(dependents))),
                        ScanFact(
                            name="fan_out",
                            value=str(len(self._architecture.dependencies(item.module))),
                        ),
                        ScanFact(name="impact", value=str(len(impact))),
                        ScanFact(name="dependents", value=", ".join(listed)),
                    ),
                )
            )
        return found

    def _layer_violation_signals(self, scope: ScanScope) -> list[ScanSignal]:
        """分层违规 —— 依赖方向错了。

        **循环依赖不在这里。** 它在 ``violations()`` 里也有一条，但那是同一
        个问题的另一种说法；两处都报，同一个问题会出两次。
        """
        found: list[ScanSignal] = []
        for violation in self._architecture.violations():
            if violation.kind is ViolationKind.CYCLIC_DEPENDENCY:
                continue
            if not scope.covers(violation.importer):
                continue
            found.append(
                ScanSignal(
                    kind=ScanKind.LAYER_VIOLATION,
                    module=violation.importer,
                    modules=(violation.importer, violation.imported),
                    severity=self._violation_severity(violation.kind),
                    summary=violation.message,
                    facts=(
                        ScanFact(name="violation", value=str(violation.kind)),
                        ScanFact(name="importer", value=violation.importer),
                        ScanFact(name="imported", value=violation.imported),
                    ),
                )
            )
        return found

    def _violation_severity(self, kind: ViolationKind) -> GapSeverity:
        """违规的严重度。

        **产品代码依赖测试代码最重** —— 那意味着测试代码被编进了产品，
        或者产品在运行时去找测试。依赖脚本轻一些：脚本本来就是要被调用的。
        """
        if kind is ViolationKind.SOURCE_IMPORTS_TEST:
            return GapSeverity.HIGH
        if kind is ViolationKind.SOURCE_IMPORTS_KNOWN_ISSUE:
            return GapSeverity.MEDIUM
        return GapSeverity.LOW

    def _untested_module_signals(self, scope: ScanScope) -> list[ScanSignal]:
        """有对外接口却没有任何自有用例碰过的源模块。"""
        found: list[ScanSignal] = []
        for module in self._architecture.untested_modules():
            if not scope.covers(module):
                continue
            surface = self._architecture.public_surface(module)
            found.append(
                ScanSignal(
                    kind=ScanKind.UNTESTED_MODULE,
                    module=module,
                    modules=(module,),
                    severity=GapSeverity.MEDIUM,
                    summary=(
                        f"模块 {module} 暴露 {len(surface)} 个公开接口，却没有任何自有用例引用过它"
                    ),
                    facts=(
                        ScanFact(name="public_symbols", value=str(len(surface))),
                        ScanFact(name="symbols", value=", ".join(surface)),
                    ),
                )
            )
        return found

    def _orphaned_api_signals(self, scope: ScanScope) -> list[ScanSignal]:
        """没有任何用例引用过的公开符号（按模块归并成一条信号）。"""
        found: list[ScanSignal] = []
        for module, symbols in self._architecture.unreferenced_public_symbols().items():
            if not scope.covers(module):
                continue
            found.append(
                ScanSignal(
                    kind=ScanKind.ORPHANED_PUBLIC_API,
                    module=module,
                    modules=(module,),
                    severity=GapSeverity.LOW,
                    summary=(f"模块 {module} 有 {len(symbols)} 个公开符号没有任何用例引用过"),
                    facts=(
                        ScanFact(name="symbol_count", value=str(len(symbols))),
                        ScanFact(name="symbols", value=", ".join(symbols)),
                    ),
                )
            )
        return found

    # ------------------------------------------------------------ 候选

    def candidates(self, report: ScanReport) -> tuple[EvolutionCandidate, ...]:
        """把信号变成候选。

        **证据是文件路径。** 候选的 evidence 必须能被独立核验（R-029），
        而这里唯一能打开、能查证的东西就是模块在仓库里的那个文件。
        """
        result: list[EvolutionCandidate] = []
        for signal in report.signals:
            evidence = self._evidence_of(signal)
            if not evidence:
                continue
            result.append(self._candidate_of(signal, evidence))
        return tuple(result)

    def _evidence_of(self, signal: ScanSignal) -> tuple[Evidence, ...]:
        """信号的证据 —— 涉及模块在仓库里的文件。

        **取不到文件就不给证据。** 候选要求至少一条证据（R-029），而
        "模块不在索引里"这件事本身说明信号有问题 —— 与其编一条路径出来，
        不如不产出这个候选。
        """
        evidence: list[Evidence] = []
        for module in signal.modules or (signal.module,):
            record = self._architecture.index.record(module)
            if record is None:
                continue
            evidence.append(
                Evidence(
                    kind=EvidenceKind.FILE,
                    ref=record.path,
                    note=f"{module} —— {signal.kind.value}",
                )
            )
        return tuple(evidence)

    def _candidate_of(
        self,
        signal: ScanSignal,
        evidence: tuple[Evidence, ...],
    ) -> EvolutionCandidate:
        """一条信号 → 一个候选。"""
        structural = signal.kind in (
            ScanKind.DEPENDENCY_CYCLE,
            ScanKind.COUPLING_HUB,
            ScanKind.LAYER_VIOLATION,
        )
        confidence = STRUCTURAL_CONFIDENCE if structural else STATIC_COVERAGE_CONFIDENCE
        return EvolutionCandidate(
            id=f"scan:{signal.kind.value}:{signal.module}",
            category=_category_of(signal.kind),
            title=signal.summary,
            evidence=evidence,
            hypothesis=_hypothesis_of(signal),
            expected_outcome=_expected_outcomes_of(signal),
            risk=_risk_of(signal),
            confidence=confidence,
        )


def _category_of(kind: ScanKind) -> OpportunityCategory:
    """信号的类别。

    **结构问题归架构，覆盖问题归质量。** 五类演进机会的划分在这里被用
    上了 —— 分类不是为了好看，是为了让决策层能按类别分派处理方式。
    """
    if kind in (ScanKind.DEPENDENCY_CYCLE, ScanKind.COUPLING_HUB, ScanKind.LAYER_VIOLATION):
        return OpportunityCategory.ARCHITECTURE
    return OpportunityCategory.QUALITY


def _risk_of(signal: ScanSignal) -> RiskLevel:
    """信号的风险档。

    **严重度不等于风险。** 严重度说的是"这个问题多要紧"，风险说的是
    "动了它可能出什么事"。一个耦合很深的模块未必难改（改一行就好），
    一个覆盖缺口很小的模块也未必安全。所以这里按严重度给一个**保守的
    起点**，真正的风险要由知道上下文的人来判。
    """
    if signal.severity.value in ("critical", "high"):
        return RiskLevel.HIGH
    if signal.severity.value == "medium":
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _hypothesis_of(signal: ScanSignal) -> str:
    """信号的假设 —— 做了会怎样。

    **写的是"如果处理了会怎样"，不是"为什么有问题"。** 后面那半句
    :attr:`ScanSignal.summary` 已经说了；假设说的是**收益的方向**，
    它是事后拿实际收益来比的对象（R-033）。
    """
    if signal.kind is ScanKind.DEPENDENCY_CYCLE:
        return "拆掉这个环之后，依赖方向能被排成一条线，改动的影响面才说得清"
    if signal.kind is ScanKind.COUPLING_HUB:
        return "把这个模块的职责收窄之后，改它不再波及整个仓库"
    if signal.kind is ScanKind.LAYER_VIOLATION:
        return "把这处依赖方向纠正之后，产品代码与测试代码重新分开"
    if signal.kind is ScanKind.UNTESTED_MODULE:
        return "给这个模块补上用例之后，它的行为被固定住，改它才敢改"
    return "给这些公开接口补上用例之后，对外契约被固定住"


def _expected_outcomes_of(signal: ScanSignal) -> tuple[str, ...]:
    """预期结果 —— 事后拿来比的东西。"""
    if signal.kind is ScanKind.DEPENDENCY_CYCLE:
        return ("依赖图里不再有环", "改一个模块的影响面可以被完整列举")
    if signal.kind is ScanKind.COUPLING_HUB:
        return ("该模块的直接依赖者数量下降", "一次改动波及的模块数量下降")
    if signal.kind is ScanKind.LAYER_VIOLATION:
        return ("产品代码不再依赖测试或脚本", "分层违规数归零")
    if signal.kind is ScanKind.UNTESTED_MODULE:
        return ("该模块至少有一条自有用例引用它",)
    return ("这些公开符号至少有一条用例引用",)


__all__ = [
    "MAX_LISTED_DEPENDENTS",
    "STATIC_COVERAGE_CONFIDENCE",
    "STRUCTURAL_CONFIDENCE",
    "ProjectScanner",
]
