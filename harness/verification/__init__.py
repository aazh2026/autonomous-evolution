"""工程验证 —— 验收契约与独立验证引擎。

设计依据：docs/design.md §3.5B（"不要让 Coding Agent 自己决定是否验收
通过"；Acceptance Contract）、§6（准入闸门）；docs/tasks.md TASK-P1-040、
TASK-P1-041、R-049~R-054。

- :mod:`harness.verification.contract` —— 验收契约与判定规则："什么算完成"；
- :mod:`harness.verification.engine` —— 独立验证引擎："这次够不够"；
- :mod:`harness.verification.six_layer` —— 六层独立验证："哪一层坏了、怎么修"。

**前两者分开的理由是方向。** 契约是执行**之前**定下的验收标准，引擎是执行
**之后**给出的结论。合成一个模块，就等于让执行者自己定义验收标准 ——
而那正是 design.md §3.5B 一开始就禁止的。

**第三个模块分开的理由是形状。** 阶段一的引擎给的是一个结论；六层验证给的是
**六份各自独立的结论**，各自带着自己的修复诊断。**一个布尔值回答不了"下一步
该干什么"** —— 改测试、改 lint 规则、倒转依赖方向、撤掉泄漏的密钥、补一次
人工评估，返工成本差着几个数量级，而压成一个布尔值时被压掉的就是这份信息。
"""

from __future__ import annotations

from harness.verification.contract import (
    AcceptanceContract,
    ArtifactKind,
    CheckOutcome,
    CheckResult,
    ContractEvaluation,
    ForbiddenRule,
    RequiredCheck,
    Threshold,
    evaluate_contract,
)
from harness.verification.engine import (
    CheckExecution,
    CheckRunner,
    CheckSpec,
    MetricSpec,
    ScanSpec,
    SubprocessCheckRunner,
    VerificationEngine,
    VerificationEnvError,
    VerificationPlan,
    VerificationRequest,
    VerificationRun,
    VerificationSetupError,
    build_verification_env,
)
from harness.verification.six_layer import (
    CHECK_LAYERS,
    REQUIRED_LAYERS_BY_TASK_TYPE,
    ArchitectureLayerRunner,
    CodeLayerRunner,
    CommandSpec,
    Diagnosis,
    EvidenceState,
    Finding,
    FindingSeverity,
    FunctionalLayerRunner,
    GoalTarget,
    HumanReview,
    LayerContext,
    LayerEvidence,
    LayerOutcome,
    LayerResult,
    LayerRunner,
    PerformanceBaseline,
    PerformanceLayerRunner,
    ProductLayerRunner,
    SecurityLayerRunner,
    SixLayerReport,
    SixLayerRequest,
    SixLayerVerdict,
    SixLayerVerifier,
    VerificationLayer,
)

__all__ = [
    "CHECK_LAYERS",
    "REQUIRED_LAYERS_BY_TASK_TYPE",
    "AcceptanceContract",
    "ArchitectureLayerRunner",
    "ArtifactKind",
    "CheckExecution",
    "CheckOutcome",
    "CheckResult",
    "CheckRunner",
    "CheckSpec",
    "CodeLayerRunner",
    "CommandSpec",
    "ContractEvaluation",
    "Diagnosis",
    "EvidenceState",
    "Finding",
    "FindingSeverity",
    "ForbiddenRule",
    "FunctionalLayerRunner",
    "GoalTarget",
    "HumanReview",
    "LayerContext",
    "LayerEvidence",
    "LayerOutcome",
    "LayerResult",
    "LayerRunner",
    "MetricSpec",
    "PerformanceBaseline",
    "PerformanceLayerRunner",
    "ProductLayerRunner",
    "RequiredCheck",
    "ScanSpec",
    "SecurityLayerRunner",
    "SixLayerReport",
    "SixLayerRequest",
    "SixLayerVerdict",
    "SixLayerVerifier",
    "SubprocessCheckRunner",
    "Threshold",
    "VerificationEngine",
    "VerificationEnvError",
    "VerificationLayer",
    "VerificationPlan",
    "VerificationRequest",
    "VerificationRun",
    "VerificationSetupError",
    "build_verification_env",
    "evaluate_contract",
]
