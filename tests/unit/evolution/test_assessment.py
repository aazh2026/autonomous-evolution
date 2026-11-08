"""TASK-P1-051 —— 项目理解与差距分析的结构化模型。

这一层是纯逻辑，不需要数据库。

三条 T1 对应三组用例：

1. **schema 校验拒绝缺 ``evidence`` 的 gap** —— 没有证据的差距是猜测，
   而猜测一旦流到决策层就会变成没人能复核的结论；
2. **``evidence`` 至少一条且指向可解析的引用** —— 能当证据的只有三种形态：
   ``artifact://``、``issue:``、文件路径；
3. **``deferred`` 条目必须带 ``reason``** —— 没有理由的暂缓是一个不用负责
   的垃圾桶：既说不出为什么，也无法在下一轮被重新评估。

另外还有两组用例守着**跨字段的一致性**：引用必须指得到（推荐的 id 要真的
是某个机会、机会声称回应的差距要真的存在），以及"暂缓"必须是真的暂缓
（不能既不做又要做）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.evolution.assessment import (
    DeferredOpportunity,
    GapSeverity,
    GoalGap,
    Opportunity,
    OverallStatus,
    ProjectAssessment,
)
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.taxonomy import OpportunityCategory, PriorityLevel

#: design.md §5 的原样示例。**必须能原样解析** —— 文档里写的形状如果解析不了，
#: 那文档就是在骗人。
DESIGN_EXAMPLE = """\
project_assessment:
  project_id: my-product
  snapshot: commit-abc123

  overall_status: evolving

  goal_gap:
    - id: GAP-001
      description: SVG 页面渲染质量未达到目标
      evidence:
        - test_report: artifact://report-001
      severity: high

    - id: GAP-002
      description: 模板提取缺少复杂布局支持
      evidence:
        - issue: ISSUE-102
      severity: medium

  opportunities:
    - id: EVO-001
      category: product
      priority: high
      action: 补充复杂布局解析能力

    - id: EVO-002
      category: quality
      priority: high
      action: 建立 SVG 渲染回归评测集

  recommendations:
    - EVO-002
    - EVO-001

  deferred:
    - reason: 当前证据不足
      candidate: EVO-003
"""


def _gap(**overrides: object) -> GoalGap:
    """一条合法的目标差距。"""
    data: dict[str, object] = {
        "id": "GAP-001",
        "description": "SVG 页面渲染质量未达到目标",
        "evidence": (Evidence(kind=EvidenceKind.TEST_REPORT, ref="artifact://report-001"),),
        "severity": GapSeverity.HIGH,
    }
    data.update(overrides)
    return GoalGap.model_validate(data)


def _opportunity(**overrides: object) -> Opportunity:
    """一个合法的演进机会。"""
    data: dict[str, object] = {
        "id": "EVO-001",
        "category": OpportunityCategory.PRODUCT,
        "action": "补充复杂布局解析能力",
        "priority": PriorityLevel.HIGH,
    }
    data.update(overrides)
    return Opportunity.model_validate(data)


def _assessment(**overrides: object) -> ProjectAssessment:
    """一份合法的项目评估。"""
    data: dict[str, object] = {
        "project_id": "my-product",
        "snapshot": "commit-abc123",
        "overall_status": OverallStatus.EVOLVING,
        "goal_gap": (_gap(),),
        "opportunities": (_opportunity(),),
        "recommendations": ("EVO-001",),
    }
    data.update(overrides)
    return ProjectAssessment.model_validate(data)


class TestEvidenceShape:
    """证据引用 —— 形态与引用必须对得上。"""

    def test_an_artifact_reference_is_accepted(self) -> None:
        evidence = Evidence(kind=EvidenceKind.ARTIFACT, ref="artifact://report-001")

        assert evidence.identifier == "report-001"

    def test_an_issue_reference_is_accepted(self) -> None:
        evidence = Evidence(kind=EvidenceKind.ISSUE, ref="issue:ISSUE-102")

        assert evidence.identifier == "ISSUE-102"

    def test_a_bare_issue_id_is_accepted(self) -> None:
        """design.md §5 写的就是裸 id —— 形态已经由 kind 声明过了。"""
        evidence = Evidence(kind=EvidenceKind.ISSUE, ref="ISSUE-102")

        assert evidence.identifier == "ISSUE-102"

    def test_an_issue_reference_cannot_point_at_another_scheme(self) -> None:
        with pytest.raises(ValidationError, match="不能指向其它 scheme"):
            Evidence(kind=EvidenceKind.ISSUE, ref="artifact://report-001")

    def test_a_file_path_is_accepted(self) -> None:
        evidence = Evidence(kind=EvidenceKind.FILE, ref="src/render/svg.py")

        assert evidence.identifier == "src/render/svg.py"

    def test_a_bare_filename_with_an_extension_is_accepted(self) -> None:
        """``README.md`` 这种没有目录的也是路径。"""
        evidence = Evidence(kind=EvidenceKind.FILE, ref="README.md")

        assert evidence.identifier == "README.md"

    def test_a_directory_path_without_an_extension_is_accepted(self) -> None:
        """``docs/架构`` 也是路径 —— 带分隔符就够了。"""
        evidence = Evidence(kind=EvidenceKind.FILE, ref="docs/架构")

        assert evidence.identifier == "docs/架构"

    def test_a_url_is_not_a_file_path(self) -> None:
        """网址是另一种引用形态 —— 它得用 artifact:// 或 issue:。"""
        with pytest.raises(ValidationError, match="不是可解析的文件路径"):
            Evidence(kind=EvidenceKind.FILE, ref="https://example.com/render.py")

    def test_a_prose_sentence_is_not_a_path(self) -> None:
        """一句自然语言不是证据 —— 它没法被独立核验。"""
        with pytest.raises(ValidationError, match="不是可解析的文件路径"):
            Evidence(kind=EvidenceKind.FILE, ref="文件耦合度过高")

    def test_a_reference_of_the_wrong_scheme_is_rejected(self) -> None:
        """形态是声明出来的，声明了就得兑现。"""
        with pytest.raises(ValidationError, match="必须以 'artifact://' 开头"):
            Evidence(kind=EvidenceKind.ARTIFACT, ref="issue:ISSUE-102")

    def test_an_artifact_reference_without_an_identifier_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="标识符为空"):
            Evidence(kind=EvidenceKind.ARTIFACT, ref="artifact://")

    def test_a_reference_containing_whitespace_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="标识符为空或含空白"):
            Evidence(kind=EvidenceKind.ISSUE, ref="issue:ISSUE 102")

    def test_the_single_key_form_of_the_design_document_is_accepted(self) -> None:
        """design.md §5 写的就是 ``- test_report: artifact://report-001``。"""
        evidence = Evidence.model_validate({"test_report": "artifact://report-001"})

        assert evidence.kind is EvidenceKind.TEST_REPORT
        assert evidence.ref == "artifact://report-001"

    def test_an_unknown_single_key_is_not_silently_rewritten(self) -> None:
        """``{"note": "..."}`` 不是形态 —— 该报缺字段，而不是被改写。"""
        with pytest.raises(ValidationError):
            Evidence.model_validate({"note": "随手写的一句"})


class TestGapRequiresEvidence:
    """T1 ① —— 缺证据的差距被拒。"""

    def test_a_gap_without_an_evidence_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GoalGap.model_validate(
                {
                    "id": "GAP-001",
                    "description": "渲染质量不达标",
                    "severity": "high",
                }
            )

    def test_a_gap_with_an_empty_evidence_list_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _gap(evidence=())

    def test_a_gap_with_one_valid_evidence_is_accepted(self) -> None:
        gap = _gap()

        assert len(gap.evidence) == 1


class TestEvidenceMustBeResolvable:
    """T1 ② —— 证据至少一条，且指向可解析的引用。"""

    def test_a_gap_with_an_unresolvable_reference_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _gap(evidence=(Evidence(kind=EvidenceKind.ISSUE, ref="artifact://x"),))

    def test_a_gap_whose_evidence_is_prose_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="不是可解析的文件路径"):
            _gap(evidence=(Evidence(kind=EvidenceKind.FILE, ref="这里渲染很慢"),))

    def test_a_gap_may_carry_several_kinds_of_evidence(self) -> None:
        gap = _gap(
            evidence=(
                Evidence(kind=EvidenceKind.TEST_REPORT, ref="artifact://report-001"),
                Evidence(kind=EvidenceKind.ISSUE, ref="issue:ISSUE-102"),
                Evidence(kind=EvidenceKind.FILE, ref="src/render/svg.py"),
            )
        )

        assert len(gap.evidence) == 3


class TestBlankFieldsAreRejected:
    """空白的字段等于没有内容 —— 一条没内容的结论不成立。"""

    def test_a_gap_with_a_blank_description_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="差距的 id 与描述都不能是空白"):
            _gap(description="   ")

    def test_an_opportunity_with_a_blank_action_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="机会的 id 与动作不能是空白"):
            _opportunity(action="   ")

    def test_an_assessment_with_a_blank_project_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="项目 id 与快照不能是空白"):
            _assessment(project_id="  ")

    def test_an_assessment_with_a_blank_snapshot_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="项目 id 与快照不能是空白"):
            _assessment(snapshot=" ")


class TestDeferredRequiresReason:
    """T1 ③ —— 暂缓必须说清为什么。"""

    def test_a_deferred_entry_without_a_reason_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DeferredOpportunity.model_validate({"candidate": "EVO-003"})

    def test_a_deferred_entry_with_a_blank_reason_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DeferredOpportunity.model_validate({"candidate": "EVO-003", "reason": "   "})

    def test_a_deferred_entry_with_a_reason_is_accepted(self) -> None:
        deferred = DeferredOpportunity(candidate="EVO-003", reason="当前证据不足")

        assert deferred.reason == "当前证据不足"


class TestCrossFieldConsistency:
    """引用必须指得到，暂缓必须是真的暂缓。"""

    def test_a_recommendation_must_point_at_an_existing_opportunity(self) -> None:
        with pytest.raises(ValidationError, match="推荐项指向不存在的机会"):
            _assessment(recommendations=("EVO-999",))

    def test_the_same_opportunity_cannot_be_recommended_twice(self) -> None:
        with pytest.raises(ValidationError, match="推荐项 不能重复"):
            _assessment(recommendations=("EVO-001", "EVO-001"))

    def test_two_gaps_cannot_share_an_id(self) -> None:
        with pytest.raises(ValidationError, match="目标差距的 id 不能重复"):
            _assessment(goal_gap=(_gap(), _gap()))

    def test_two_opportunities_cannot_share_an_id(self) -> None:
        with pytest.raises(ValidationError, match="机会的 id 不能重复"):
            _assessment(opportunities=(_opportunity(), _opportunity()))

    def test_an_opportunity_must_address_an_existing_gap(self) -> None:
        with pytest.raises(ValidationError, match="声称回应了不存在的差距"):
            _assessment(opportunities=(_opportunity(addresses=("GAP-999",)),))

    def test_a_deferred_candidate_cannot_also_be_recommended(self) -> None:
        """既不做又要做 —— 那就不是暂缓。"""
        with pytest.raises(ValidationError, match="既被暂缓又在推荐里"):
            _assessment(
                deferred=(DeferredOpportunity(candidate="EVO-001", reason="证据不足"),),
            )

    def test_at_target_means_there_is_no_goal_gap(self) -> None:
        """目标差距量的是离目标的距离 —— 达标了就没有距离。"""
        with pytest.raises(ValidationError, match="at_target 就不该还有目标差距"):
            _assessment(overall_status=OverallStatus.AT_TARGET)

    def test_at_target_without_any_gap_is_accepted(self) -> None:
        assessment = _assessment(overall_status=OverallStatus.AT_TARGET, goal_gap=())

        assert assessment.overall_status is OverallStatus.AT_TARGET


class TestTheDesignExample:
    """design.md §5 的示例必须原样能解析。"""

    def test_the_example_parses(self) -> None:
        assessment = ProjectAssessment.from_yaml(DESIGN_EXAMPLE)

        assert assessment.project_id == "my-product"
        assert assessment.snapshot == "commit-abc123"
        assert assessment.overall_status is OverallStatus.EVOLVING

    def test_the_example_gaps_carry_their_evidence(self) -> None:
        assessment = ProjectAssessment.from_yaml(DESIGN_EXAMPLE)

        first = assessment.gap("GAP-001")
        assert first is not None
        assert first.evidence[0].kind is EvidenceKind.TEST_REPORT
        assert first.evidence[0].ref == "artifact://report-001"

        second = assessment.gap("GAP-002")
        assert second is not None
        assert second.evidence[0].kind is EvidenceKind.ISSUE

    def test_the_example_keeps_the_recommendation_order(self) -> None:
        """推荐列表是**有次序**的 —— 先做评测集，再做功能。"""
        assessment = ProjectAssessment.from_yaml(DESIGN_EXAMPLE)

        assert assessment.recommendations == ("EVO-002", "EVO-001")

    def test_the_example_records_the_deferral(self) -> None:
        assessment = ProjectAssessment.from_yaml(DESIGN_EXAMPLE)

        assert assessment.deferred[0].candidate == "EVO-003"
        assert assessment.deferred[0].reason == "当前证据不足"

    def test_an_opportunity_can_be_looked_up_by_id(self) -> None:
        assessment = ProjectAssessment.from_yaml(DESIGN_EXAMPLE)

        opportunity = assessment.opportunity("EVO-002")

        assert opportunity is not None
        assert opportunity.category is OpportunityCategory.QUALITY
