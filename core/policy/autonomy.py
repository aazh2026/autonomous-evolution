"""自主权限策略。

设计依据：docs/design.md §3.1（Autonomy Policy 按操作维度设定）、§7（四级自主权与
初始基线）、§7 末段（允许系统自主改进被管项目 ≠ 允许它自主修改控制系统自身）。

关键约束：**策略来自外部配置，不硬编码在代码中**。
``default_policy()`` 只是兜底默认值；``PolicyManager.from_file()`` 读取的外部 YAML
才是生效来源。这条边界是阶段四"系统自身改进提案机制"（design.md §7 / R-103）
能够落地的前提 —— 若权限规则写在代码里，系统根本没有可挂靠的审批基线。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class AutonomyLevel(StrEnum):
    """四级自主权。design.md §7。

    L1 分析自主  —— 分析、诊断、生成建议；人工决定是否执行
    L2 开发自主  —— 创建分支、修改代码、测试；人工审核合并
    L3 集成自主  —— 自动修复、集成、合并指定范围；关键变更审批
    L4 运营自主  —— 自动发布、监控、回滚；重大操作授权
    """

    L1_ANALYSIS = "L1"
    L2_DEVELOPMENT = "L2"
    L3_INTEGRATION = "L3"
    L4_OPERATIONAL = "L4"


class Operation(StrEnum):
    """可被授权的操作。粒度对齐 design.md §3.1 的 autonomy 配置项。"""

    ANALYZE_PROJECT = "analyze_project"
    PLAN_TASKS = "plan_tasks"
    CODE_CHANGES = "code_changes"
    TEST_EXECUTION = "test_execution"
    BRANCH_CREATION = "branch_creation"
    MERGE_TO_MAIN = "merge_to_main"
    PRODUCTION_DEPLOYMENT = "production_deployment"
    MODIFY_GOVERNANCE = "modify_governance"


class Permission(StrEnum):
    """单个操作被授予的权限形态。"""

    AUTONOMOUS = "autonomous"
    APPROVAL_REQUIRED = "approval_required"
    ADVISORY_ONLY = "advisory_only"
    DENIED = "denied"


class DecisionKind(StrEnum):
    """判定结果。"""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Decision:
    """一次授权判定的结果。"""

    kind: DecisionKind
    operation: Operation
    reason: str
    granted_level: AutonomyLevel | None = None

    @property
    def is_allowed(self) -> bool:
        return self.kind is DecisionKind.ALLOW

    @property
    def needs_approval(self) -> bool:
        return self.kind is DecisionKind.REQUIRE_APPROVAL


class OperationRule(BaseModel):
    """单个操作的规则：授予到哪一级、给什么权限。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: AutonomyLevel
    permission: Permission

    @field_validator("permission")
    @classmethod
    def _permission_matches_level(cls, value: Permission, info: ValidationInfo) -> Permission:
        """校验权限与等级的组合是否自洽。

        组合语义：
        - ``L4 + autonomous``       自主执行
        - ``L3/L2 + approval_required`` 需要人工审批
        - ``L1 + advisory_only``    只产出建议
        - 任意等级 + ``denied``     完全禁止
        """
        level = info.data.get("level")
        if level is None:
            return value
        if value is Permission.DENIED:
            return value
        if value is Permission.AUTONOMOUS and level is not AutonomyLevel.L4_OPERATIONAL:
            raise ValueError(f"只有 L4 运营自主才允许 autonomous 自主执行，收到 level={level}")
        if value is Permission.ADVISORY_ONLY and level is not AutonomyLevel.L1_ANALYSIS:
            raise ValueError(f"只有 L1 分析自主才对应 advisory_only 仅建议，收到 level={level}")
        return value


class AutonomyPolicy(BaseModel):
    """项目级自主策略：操作 → 规则。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rules: Mapping[Operation, OperationRule] = Field(default_factory=dict)

    @field_validator("rules")
    @classmethod
    def _check_complete(
        cls, value: Mapping[Operation, OperationRule]
    ) -> Mapping[Operation, OperationRule]:
        """要求每个操作都有明确规则。

        缺一条就等于把决定权交给了隐式默认值 —— 那是权限漏洞，不是便利。
        """
        missing = set(Operation) - set(value)
        if missing:
            names = ", ".join(sorted(op.value for op in missing))
            raise ValueError(f"策略缺少以下操作的规则：{names}")
        return value

    def rule_for(self, operation: Operation) -> OperationRule:
        return self.rules[operation]


def default_policy() -> AutonomyPolicy:
    """design.md §7「建议的初始自主权限」基线。

    - 项目分析与机会发现      → L4 自主
    - 任务拆解与计划生成      → L4 自主
    - 代码开发和自动测试      → L4 自主（隔离环境内）
    - 代码合并                → L2 需人工审批
    - 生产部署                → L1 仅提供建议
    - 修改自身治理策略        → L1 仅允许提交变更提案

    注意 ``production_deployment`` 在 §3.1 的 YAML 示例中写作 ``approval_required``，
    而 §7 的初始权限表写的是「仅提供建议 / L1」。本文以 §7 为准 —— §3.1 的 YAML
    自称"结构示意"，§7 才是明确的初始基线。参见 docs/tasks.md 第 6 节待裁决项。
    """
    return AutonomyPolicy(
        rules={
            Operation.ANALYZE_PROJECT: OperationRule(
                level=AutonomyLevel.L4_OPERATIONAL, permission=Permission.AUTONOMOUS
            ),
            Operation.PLAN_TASKS: OperationRule(
                level=AutonomyLevel.L4_OPERATIONAL, permission=Permission.AUTONOMOUS
            ),
            Operation.CODE_CHANGES: OperationRule(
                level=AutonomyLevel.L4_OPERATIONAL, permission=Permission.AUTONOMOUS
            ),
            Operation.TEST_EXECUTION: OperationRule(
                level=AutonomyLevel.L4_OPERATIONAL, permission=Permission.AUTONOMOUS
            ),
            Operation.BRANCH_CREATION: OperationRule(
                level=AutonomyLevel.L4_OPERATIONAL, permission=Permission.AUTONOMOUS
            ),
            Operation.MERGE_TO_MAIN: OperationRule(
                level=AutonomyLevel.L2_DEVELOPMENT, permission=Permission.APPROVAL_REQUIRED
            ),
            Operation.PRODUCTION_DEPLOYMENT: OperationRule(
                level=AutonomyLevel.L1_ANALYSIS, permission=Permission.ADVISORY_ONLY
            ),
            Operation.MODIFY_GOVERNANCE: OperationRule(
                level=AutonomyLevel.L1_ANALYSIS, permission=Permission.ADVISORY_ONLY
            ),
        }
    )


class PolicyManager:
    """判定某操作在当前策略下是否被允许。"""

    def __init__(self, policy: AutonomyPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> AutonomyPolicy:
        return self._policy

    @classmethod
    def default(cls) -> PolicyManager:
        """使用内置基线。仅作为兜底，生产路径应调用 :meth:`from_file`。"""
        return cls(default_policy())

    @classmethod
    def from_file(cls, path: Path) -> PolicyManager:
        """从外部 YAML 文件加载策略。

        文件格式::

            rules:
              merge_to_main:
                level: L2
                permission: approval_required
        """
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> PolicyManager:
        """从字典加载策略。用于测试与 API 传入。"""
        rules_raw = raw.get("rules", raw)
        if not isinstance(rules_raw, Mapping):
            raise ValueError("策略文件的 rules 必须是映射")
        rules: dict[Operation, OperationRule] = {}
        for op_name, rule_raw in rules_raw.items():
            op = Operation(op_name)
            if not isinstance(rule_raw, Mapping):
                raise ValueError(f"操作 {op_name} 的规则必须是映射")
            rules[op] = OperationRule.model_validate(rule_raw)
        return cls(AutonomyPolicy(rules=rules))

    def check(self, operation: Operation) -> Decision:
        """判定操作权限。"""
        rule = self._policy.rule_for(operation)
        match rule.permission:
            case Permission.AUTONOMOUS:
                return Decision(
                    kind=DecisionKind.ALLOW,
                    operation=operation,
                    reason=f"操作 {operation.value} 已授权自主执行（{rule.level.value}）",
                    granted_level=rule.level,
                )
            case Permission.APPROVAL_REQUIRED:
                return Decision(
                    kind=DecisionKind.REQUIRE_APPROVAL,
                    operation=operation,
                    reason=f"操作 {operation.value} 需人工审批（{rule.level.value}）",
                    granted_level=rule.level,
                )
            case Permission.ADVISORY_ONLY:
                return Decision(
                    kind=DecisionKind.DENY,
                    operation=operation,
                    reason=f"操作 {operation.value} 仅允许产出建议，不得执行（{rule.level.value}）",
                    granted_level=rule.level,
                )
            case Permission.DENIED:
                return Decision(
                    kind=DecisionKind.DENY,
                    operation=operation,
                    reason=f"操作 {operation.value} 被策略显式禁止",
                    granted_level=rule.level,
                )

    def allows(self, operation: Operation) -> bool:
        return self.check(operation).is_allowed

    def __repr__(self) -> str:
        return f"PolicyManager(operations={len(self._policy.rules)})"


def load_policy(path: Path | None, *, fallback: PolicyManager | None = None) -> PolicyManager:
    """加载策略文件；文件不存在时回退到给定或内置基线。

    回退是刻意的：策略文件缺失不应让整个系统起不来，但**回退到内置基线**
    仍是保守的（合并需审批、部署仅建议、自改治理禁止），不会意外放开权限。
    """
    if path is not None and path.exists():
        return PolicyManager.from_file(path)
    return fallback if fallback is not None else PolicyManager.default()


__all__ = [
    "AutonomyLevel",
    "AutonomyPolicy",
    "Decision",
    "DecisionKind",
    "Operation",
    "OperationRule",
    "Permission",
    "PolicyManager",
    "default_policy",
    "load_policy",
]
