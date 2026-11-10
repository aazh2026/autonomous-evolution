"""发现编排 —— 把观察翻译成演进候选。

设计依据：docs/design.md §3 第二层（演进机会发现 → 统一 Evolution Candidate；
"Candidate 不等于任务"）、§3.3（Candidate 的证据必须能被独立核验）；
docs/tasks.md TASK-P3-013（验收条件"六类数据源各接入 ≥1 个真实来源；候选的
evidence 指向真实可解析的证据"；R-024、R-028、R-029、R-030）。

**这一层是流水线的中段。** 上游是观察（:mod:`orchestration.observe`），下游
是决策（P1-054 的 record / defer / reject）。**它只做翻译与核验，不做决定**
—— 候选在这儿产生，采不采纳是下游的事（R-030）。

**候选能不能活下来，看它的证据核不核得上。** 这一层拿每一条证据引用去问
核验器：指向的文件在不在仓库里、指向的问题单在不在台账里。

- **找到了** → 候选成立；
- **没找到** → **候选丢掉**。证据指错了地方还说"有证据"，那是在骗人；
- **查不了** → **候选丢掉**。这一条是有意的：验收条件写的是"候选的 evidence
  指向真实可解析的证据"，而"本机没有产物存储所以查不了"**证明不了它真实**。
  **宁可少一条候选，不要一条查不出来的候选混在里面。**

**丢掉的理由要能分辨。** "证据指错了"要改证据；"这一类观察本来就不产出候
选"什么都不用改；"本机查不了"要接产物存储 —— 三种情况的补救动作完全不
同，混成一句话就等于没记。

**同一主体只出一个候选。** 观察的指纹是"种类 + 主体"，重复的观察只留第一
条，后面的丢掉 —— **候选 id 是主键，重复的 id 落库会炸。**
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from core.evolution.candidate import EvolutionCandidate
from core.evolution.observe import Observation, ObservationKind, ObservationSnapshot
from core.evolution.opportunity import (
    OPPORTUNITY_RULES,
    DroppedOpportunity,
    EvidenceResolver,
    OpportunityDiscovery,
    OpportunityRule,
    ResolutionStatus,
)


@dataclass
class OpportunityDiscoverer:
    """机会发现器 —— **观察进，候选出**。

    :param resolver: 证据核验器。
    :param rules: 规则表。**默认是 :data:`core.evolution.opportunity.OPPORTUNITY_RULES`**。
    :param id_prefix: 候选 id 的前缀。

    **它不读世界。** 上游给的是观察，它不回头去扫仓库、翻台账 —— **那正是
    :mod:`orchestration.observe` 的活**。两边的边界在这里划清楚。
    """

    resolver: EvidenceResolver
    rules: Mapping[ObservationKind, OpportunityRule] = field(
        default_factory=lambda: OPPORTUNITY_RULES
    )
    id_prefix: str = "opp"

    def discover(self, snapshot: ObservationSnapshot) -> OpportunityDiscovery:
        """把一份快照翻译成候选。"""
        candidates: list[EvolutionCandidate] = []
        dropped: list[DroppedOpportunity] = []
        seen: set[str] = set()
        for observation in snapshot.observations:
            candidate = self._one(observation)
            if isinstance(candidate, EvolutionCandidate):
                if candidate.id in seen:
                    dropped.append(
                        DroppedOpportunity(
                            subject=observation.subject,
                            reason="同一个主体的观察重复了 —— 候选 id 会撞车，只留第一条",
                        )
                    )
                    continue
                seen.add(candidate.id)
                candidates.append(candidate)
            else:
                dropped.append(candidate)
        return OpportunityDiscovery(candidates=tuple(candidates), dropped=tuple(dropped))

    def _one(self, observation: Observation) -> EvolutionCandidate | DroppedOpportunity:
        """一条观察 → 一个候选，或者一个丢弃记录。"""
        rule = self.rules.get(observation.kind)
        if rule is None:
            return DroppedOpportunity(
                subject=observation.subject,
                reason=_NO_RULE_REASON.format(kind=observation.kind.value),
            )
        broken = self._broken_evidence(observation)
        if broken is not None:
            return DroppedOpportunity(subject=observation.subject, reason=broken)
        return EvolutionCandidate(
            id=self._candidate_id(observation),
            category=rule.category,
            title=f"{rule.title_prefix}：{observation.subject}",
            evidence=observation.evidence,
            hypothesis=rule.hypothesis_template.format(subject=observation.subject),
            expected_outcome=rule.expected_outcome,
            risk=rule.risk,
            confidence=rule.confidence,
            estimated_effort=rule.estimated_effort,
        )

    def _broken_evidence(self, observation: Observation) -> str | None:
        """核验这条观察的证据。**全都能核上就返回 ``None``**，否则返回丢弃
        的理由。

        **一条证据核不上，整条候选就作废。** 不搞"大部分证据核得上就算"
        —— 那等于让一条候选带着一条假证据往前走，而假证据最难查。
        """
        for evidence in observation.evidence:
            resolution = self.resolver.resolve(evidence)
            if resolution.status is ResolutionStatus.RESOLVED:
                continue
            if resolution.status is ResolutionStatus.MISSING:
                return (
                    f"证据 {evidence.ref} 核不上 —— {resolution.detail}"
                    "（证据指错了地方，候选不成立）"
                )
            return (
                f"证据 {evidence.ref} 查不了 —— {resolution.detail}"
                "（这一条流水线只产出当场核验过的候选）"
            )
        return None

    def _candidate_id(self, observation: Observation) -> str:
        """候选的 id —— **种类 + 主体**。

        **主体要能进 id。** 主体是文件路径或者用例 node id，里面有斜杠、有
        冒号 —— 直接拼进去会得到一个没法当主键的字符串，所以要洗一遍。
        """
        return f"{self.id_prefix}-{observation.kind.value}-{_slug(observation.subject)}"


#: 没有规则的观察为什么被丢掉 —— **写清楚，免得读的人以为是漏了**。
_NO_RULE_REASON: str = "{kind} 这一类观察不产出候选 —— 决策记录写的是当初已经做过的决定，不是待办"


def _slug(text: str) -> str:
    """把一段文字洗成能当 id 的一段。

    **保留字母与数字（含中文），其余换成短横。** 换成短横而不是删掉，是
    为了不把两个不同的主体洗成同一个 —— ``a/b`` 与 ``ab`` 删掉斜杠就撞了。
    """
    cleaned = "".join(char if (char.isalnum() or char in "-_") else "-" for char in text)
    return cleaned.strip("-") or "unknown"


__all__ = ["OpportunityDiscoverer"]
