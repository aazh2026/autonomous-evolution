"""TASK-P4-070 —— 自我改进的规矩：只能往严里加。

验证三件事：

1. **下限是钉死的** —— 每类提案天生至少什么风险、至少几个人批。
2. **规矩只能往上抬** —— 写一个比下限更松的值，**策略压根建不出来**。
3. **下限与项目加严取更严的那个** —— 两边都在时，取更严的。
"""

from __future__ import annotations

import pytest

from core.evolution.proposal import MINIMUM_APPROVALS_BY_KIND, MINIMUM_RISK_BY_KIND, ProposalKind
from core.evolution.taxonomy import RiskLevel
from core.policy.self_evolution import SelfEvolutionPolicy


class TestFloors:
    """下限 —— **每类提案天生多严**。"""

    def test_the_governance_kinds_are_high_risk(self) -> None:
        """**改验收标准、改策略、改治理 —— 在任何项目里都是高风险。**"""
        for kind in (
            ProposalKind.ACCEPTANCE_CRITERIA_CHANGE,
            ProposalKind.POLICY_CHANGE,
            ProposalKind.GOVERNANCE_CHANGE,
        ):
            assert MINIMUM_RISK_BY_KIND[kind] is RiskLevel.HIGH

    def test_the_default_policy_uses_the_floors(self) -> None:
        policy = SelfEvolutionPolicy()
        for kind in ProposalKind:
            assert policy.risk_for(kind) is MINIMUM_RISK_BY_KIND[kind]
            assert policy.approvals_for(kind) == MINIMUM_APPROVALS_BY_KIND[kind]


class TestOnlyStricter:
    """规矩只能往严里加 —— **往松了写就是建不出来**。"""

    def test_a_looser_approval_count_is_refused(self) -> None:
        with pytest.raises(ValueError, match="往严里加"):
            SelfEvolutionPolicy(required_approvals={ProposalKind.GOVERNANCE_CHANGE: 1})

    def test_a_looser_risk_is_refused(self) -> None:
        with pytest.raises(ValueError, match="往严里加"):
            SelfEvolutionPolicy(risk_overrides={ProposalKind.POLICY_CHANGE: RiskLevel.LOW})

    def test_a_stricter_approval_count_is_accepted(self) -> None:
        policy = SelfEvolutionPolicy(required_approvals={ProposalKind.CODE_CHANGE: 3})
        assert policy.approvals_for(ProposalKind.CODE_CHANGE) == 3

    def test_a_stricter_risk_is_accepted(self) -> None:
        policy = SelfEvolutionPolicy(risk_overrides={ProposalKind.CODE_CHANGE: RiskLevel.HIGH})
        assert policy.risk_for(ProposalKind.CODE_CHANGE) is RiskLevel.HIGH


class TestEffective:
    """取更严的那个 —— **两处规矩都在时**。"""

    def test_the_floor_wins_when_the_override_is_absent(self) -> None:
        policy = SelfEvolutionPolicy()
        assert policy.approvals_for(ProposalKind.POLICY_CHANGE) == 2

    def test_the_override_wins_when_it_is_stricter(self) -> None:
        policy = SelfEvolutionPolicy(required_approvals={ProposalKind.POLICY_CHANGE: 5})
        assert policy.approvals_for(ProposalKind.POLICY_CHANGE) == 5

    def test_an_unrelated_kind_is_unaffected(self) -> None:
        """**给一类加严不该影响另一类。**"""
        policy = SelfEvolutionPolicy(required_approvals={ProposalKind.CODE_CHANGE: 4})
        assert (
            policy.approvals_for(ProposalKind.DEPENDENCY_CHANGE)
            == MINIMUM_APPROVALS_BY_KIND[ProposalKind.DEPENDENCY_CHANGE]
        )
