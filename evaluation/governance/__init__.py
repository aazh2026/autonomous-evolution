"""评测层里的"治理"这一支 —— 拿真语料量一遍治理规矩。

设计依据：docs/tasks.md TASK-P4-060（高风险变更治理；验收条件"标记为高风险的
变更 100% 经过独立评审后才可执行"；验证 T1 + T2）、R-061（高风险操作独立授
权）。

这一支做的事只有一件：**把真跑出来的审批记录送进新的放行闸里跑一遍，看它会
怎么判**。

**为什么真语料这么要紧。** 规矩写在纸上，谁都能写一串"应该会拦住"的自测用
例 —— 而那些用例里的数据是自己造的，**造出来的数据证明不了真数据上的效果**。
真语料里有什么就有什么：每条合并审批只有一个人签了字，那正是要量出来的东西。

这一支也不碰数据库 —— 判定是纯计算，数据从哪来是别的事
（``scripts/run_release_audit.py`` 读库）。
"""

from __future__ import annotations

from evaluation.governance.release_audit import (
    CONCLUDED_STATUSES,
    REASON_RULES,
    RULE_LABELS,
    AuditScenario,
    ChangeVerdict,
    LegacyGate,
    LiveApproval,
    ReleaseAuditReport,
    audit,
    classify,
    legacy_gate,
    roster_from_corpus,
    scenario_architecture,
    scenario_as_is,
    scenario_paired,
)

__all__ = [
    "CONCLUDED_STATUSES",
    "REASON_RULES",
    "RULE_LABELS",
    "AuditScenario",
    "ChangeVerdict",
    "LegacyGate",
    "LiveApproval",
    "ReleaseAuditReport",
    "audit",
    "classify",
    "legacy_gate",
    "roster_from_corpus",
    "scenario_architecture",
    "scenario_as_is",
    "scenario_paired",
]
