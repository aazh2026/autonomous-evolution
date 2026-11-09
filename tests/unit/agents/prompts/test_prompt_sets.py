"""角色化 Prompt 集的契约测试。

设计依据：docs/tasks.md TASK-P1-057（"每套 Prompt 的输出绑定对应 Pydantic
schema"、"每条结论必须带证据引用"、"模型与温度可外部配置"）；T1 ①「每类角色的
真实输出都能被解析成对应 schema」、T1 ②「没有证据的结论会被标记并在下游被
排除」。

**这里测的是契约，不是文采。** 六个角色的正文不逐字断言 —— 那样的测试在
改一个字的提示词时就红了，而它守不住任何东西。这里守的是四件会真的出事的事：

1. **模型回一份 JSON，能落进这个角色自己的 schema**（T1 ①）；
2. **模型回一份没带证据的结论，它会被标记成 ``unsupported``，并在下游被排除**
   （T1 ②）—— 这条是整套系统里"看起来有依据"的解毒剂；
3. **解析失败报的是"这份输出不合格"，不是"调用失败"**；
4. **六套 Prompt 各自钉死自己的输出形状与职责边界** —— 六份输出互相套不上。

**为什么"真实输出"要用字面量 JSON 而不是拼字典。** 模型回的就是一段文本；
测试里拼出来的字典绕过了"文本 → JSON"这一步，而那一步恰好是出问题最多的
地方（围栏、前后缀说明）。
"""

from __future__ import annotations

import json
from typing import cast

import pytest
from pydantic import ValidationError

from agents.prompts import (
    ANALYST_PROMPT_SET,
    ARCHITECT_PROMPT_SET,
    CRITIC_PROMPT_SET,
    DIRECTOR_PROMPT_SET,
    PROMPT_SETS,
    QUALITY_PROMPT_SET,
    STRATEGIST_PROMPT_SET,
    AnalystOutput,
    ArchitectOutput,
    CandidateRoleOutput,
    ConclusionStatus,
    CriticOutput,
    DirectorOutput,
    ModelSettings,
    PromptParseError,
    PromptSet,
    QualityOutput,
    Role,
    RoleOutput,
    StrategistOutput,
    parse_output,
    prompt_set_for,
    supported,
    unsupported,
)
from core.evolution.decision import DecisionState
from core.evolution.taxonomy import OpportunityCategory
from infrastructure.config.settings import Settings
from orchestration.prompt_runtime import model_settings_from, with_configured_model

# --------------------------------------------------------------------------
# 六个角色的"真实输出" —— 模型回的就是这么一段文本。
# --------------------------------------------------------------------------

ANALYST_RAW = """\
{
  "project_id": "proj-001",
  "snapshot": "2026-11-08T09:00:00Z",
  "overall_status": "evolving",
  "gaps": [
    {
      "id": "gap-render",
      "statement": "SVG 复合路径没有回退实现，渲染会直接失败",
      "evidence": [
        {
          "kind": "file",
          "ref": "src/render/svg.py",
          "note": "复合路径分支直接抛异常"
        }
      ],
      "severity": "high"
    }
  ],
  "opportunities": [
    {
      "id": "opp-render",
      "statement": "给 SVG 渲染补一条复合路径回退",
      "evidence": [{"kind": "issue", "ref": "ISSUE-102"}],
      "category": "engineering",
      "priority": "high",
      "addresses": ["gap-render"]
    }
  ],
  "recommendations": ["opp-render"]
}
"""

STRATEGIST_RAW = """\
{
  "candidates": [
    {
      "id": "cand-feedback",
      "statement": "把用户反馈接进发布节奏",
      "evidence": [
        {
          "kind": "artifact",
          "ref": "artifact://feedback-001",
          "note": "近三个月 42 条反馈"
        }
      ],
      "hypothesis": "按反馈频次排期能缩短用户等待",
      "expected_outcome": ["反馈到发布的间隔缩短一半"],
      "risk": "medium",
      "confidence": 0.7,
      "estimated_effort": "3 人天",
      "benefit": {
        "summary": "高频反馈的用户不必等下一个版本",
        "applies_to": ["gap-render"]
      }
    }
  ]
}
"""

ARCHITECT_RAW = """\
{
  "candidates": [
    {
      "id": "cand-boundary",
      "statement": "把渲染层与存储层之间的耦合拆开",
      "evidence": [{"kind": "file", "ref": "src/render/svg.py"}],
      "hypothesis": "拆开之后存储层可以单独替换",
      "expected_outcome": ["换存储实现不用改渲染代码"],
      "risk": "high",
      "confidence": 0.6,
      "estimated_effort": "5 人天"
    }
  ]
}
"""

QUALITY_RAW = """\
{
  "candidates": [
    {
      "id": "cand-coverage",
      "statement": "给渲染层补回归测试",
      "evidence": [{"kind": "test_report", "ref": "artifact://report-001"}],
      "hypothesis": "补上回归测试能挡住复合路径这类回归",
      "expected_outcome": ["渲染层覆盖率从 0.4 提到 0.7"],
      "risk": "low",
      "confidence": 0.8,
      "estimated_effort": "2 人天"
    }
  ]
}
"""

DIRECTOR_RAW = """\
{
  "cycle_id": "cycle-2026-11-08",
  "decisions": [
    {
      "id": "dec-1",
      "statement": "值得做：证据齐、代价可控",
      "evidence": [{"kind": "artifact", "ref": "artifact://report-001"}],
      "candidate_id": "cand-feedback",
      "decision": "record",
      "requested_analysis": [],
      "factors": {
        "value": 0.8,
        "impact": 0.6,
        "confidence": 0.7,
        "urgency": 0.5,
        "cost": 0.3,
        "risk": 0.2
      }
    },
    {
      "id": "dec-2",
      "statement": "还缺基线数据，先补测量",
      "evidence": [{"kind": "file", "ref": "src/render/svg.py"}],
      "candidate_id": "cand-coverage",
      "decision": "defer",
      "requested_analysis": [
        {
          "kind": "measurement",
          "description": "跑一遍渲染基准，量出当前吞吐",
          "rationale": "没有基线就说不清补测试值不值"
        }
      ],
      "factors": {
        "value": 0.4,
        "impact": 0.5,
        "confidence": 0.3,
        "urgency": 0.2,
        "cost": 0.2,
        "risk": 0.2
      }
    }
  ]
}
"""

CRITIC_RAW = """\
{
  "candidate_id": "cand-feedback",
  "outcomes": [
    {
      "check": "problem_exists",
      "verdict": "passed",
      "summary": "问题在仓库里能查到",
      "evidence": [{"kind": "repository", "reference": "src/render/svg.py"}],
      "vetoes": false
    },
    {
      "check": "evidence_sufficient",
      "verdict": "passed",
      "summary": "候选带了三份可核验的引用",
      "evidence": [{"kind": "candidate", "reference": "confidence=0.7"}],
      "vetoes": false
    },
    {
      "check": "not_already_solved",
      "verdict": "passed",
      "summary": "任务表里没有同类任务",
      "evidence": [{"kind": "task", "reference": "T-42"}],
      "vetoes": false
    },
    {
      "check": "respects_goals_and_constraints",
      "verdict": "passed",
      "summary": "不违反任何一条约束",
      "evidence": [{"kind": "constraint", "reference": "不允许未经授权修改生产环境"}],
      "vetoes": false
    },
    {
      "check": "has_lower_cost_alternative",
      "verdict": "passed",
      "summary": "没有更省的做法",
      "evidence": [{"kind": "repository", "reference": "src/render/svg.py"}],
      "vetoes": false
    },
    {
      "check": "no_unnecessary_complexity",
      "verdict": "passed",
      "summary": "改动面限于渲染层",
      "evidence": [{"kind": "git_history", "reference": "a1b2c3d"}],
      "vetoes": false
    },
    {
      "check": "worth_doing_now",
      "verdict": "passed",
      "summary": "收益高于代价",
      "evidence": [{"kind": "candidate", "reference": "expected_outcome=间隔缩短一半"}],
      "vetoes": false
    },
    {
      "check": "respects_non_goals",
      "verdict": "passed",
      "summary": "不碰非目标范围",
      "evidence": [{"kind": "non_goal", "reference": "不支持多人实时协作"}],
      "vetoes": false
    },
    {
      "check": "does_not_trigger_stop_conditions",
      "verdict": "passed",
      "summary": "不触发任何停止条件",
      "evidence": [{"kind": "stop_condition", "reference": "KR-001 连续两轮达标"}],
      "vetoes": false
    }
  ]
}
"""

#: 三个"提候选"的角色 —— 形状一样，类别不同。
CANDIDATE_ROLES: tuple[tuple[PromptSet[RoleOutput], str, OpportunityCategory], ...] = (
    (STRATEGIST_PROMPT_SET, STRATEGIST_RAW, OpportunityCategory.PRODUCT),
    (ARCHITECT_PROMPT_SET, ARCHITECT_RAW, OpportunityCategory.ARCHITECTURE),
    (QUALITY_PROMPT_SET, QUALITY_RAW, OpportunityCategory.QUALITY),
)


class TestRealisticOutputParses:
    """T1 ① —— 每类角色的真实输出都能被解析成对应 schema。"""

    @pytest.mark.parametrize(
        ("prompt_set", "raw", "expected_type"),
        [
            (ANALYST_PROMPT_SET, ANALYST_RAW, AnalystOutput),
            (STRATEGIST_PROMPT_SET, STRATEGIST_RAW, StrategistOutput),
            (ARCHITECT_PROMPT_SET, ARCHITECT_RAW, ArchitectOutput),
            (QUALITY_PROMPT_SET, QUALITY_RAW, QualityOutput),
            (DIRECTOR_PROMPT_SET, DIRECTOR_RAW, DirectorOutput),
            (CRITIC_PROMPT_SET, CRITIC_RAW, CriticOutput),
        ],
        ids=["analyst", "strategist", "architect", "quality", "director", "critic"],
    )
    def test_parses_into_its_own_schema(
        self,
        prompt_set: PromptSet[RoleOutput],
        raw: str,
        expected_type: type[RoleOutput],
    ) -> None:
        parsed = parse_output(prompt_set, raw)
        assert isinstance(parsed, expected_type)

    def test_analyst_output_becomes_a_project_assessment(self) -> None:
        """分析师的输出落进领域层的项目评估。"""
        parsed = parse_output(ANALYST_PROMPT_SET, ANALYST_RAW)
        assert isinstance(parsed, AnalystOutput)

        assessment = parsed.to_assessment()

        assert assessment.project_id == "proj-001"
        assert [gap.id for gap in assessment.goal_gap] == ["gap-render"]
        assert [item.id for item in assessment.opportunities] == ["opp-render"]
        assert assessment.recommendations == ("opp-render",)

    @pytest.mark.parametrize(
        ("prompt_set", "raw", "category"),
        CANDIDATE_ROLES,
        ids=["strategist", "architect", "quality"],
    )
    def test_candidate_roles_produce_domain_candidates(
        self,
        prompt_set: PromptSet[RoleOutput],
        raw: str,
        category: OpportunityCategory,
    ) -> None:
        """三个提候选的角色落进领域层的演进候选。

        **类别由角色钉死** —— 三份输出用的是同一份形状，差的是看的角度。
        """
        parsed = cast(CandidateRoleOutput, parse_output(prompt_set, raw))

        candidates = parsed.to_candidates()

        assert len(candidates) == 1
        assert candidates[0].category is category
        assert candidates[0].evidence, "有依据的候选必须带着它的证据"

    def test_director_output_becomes_a_decision_round(self) -> None:
        """总监的输出落进领域层的决策轮。"""
        parsed = parse_output(DIRECTOR_PROMPT_SET, DIRECTOR_RAW)
        assert isinstance(parsed, DirectorOutput)

        decision_round = parsed.to_decision_round()

        assert decision_round.cycle_id == "cycle-2026-11-08"
        assert decision_round.total == 2
        assert [item.decision for item in decision_round.decisions] == [
            DecisionState.RECORD,
            DecisionState.DEFER,
        ]
        # 只有 record 派生任务。
        assert [item.candidate_id for item in decision_round.recorded] == ["cand-feedback"]
        assert len(decision_round.pending_analysis) == 1

    def test_critic_output_becomes_a_report(self) -> None:
        """Critic 的输出落进领域层的审查报告 —— 九项一项不缺。"""
        parsed = parse_output(CRITIC_PROMPT_SET, CRITIC_RAW)
        assert isinstance(parsed, CriticOutput)

        report = parsed.to_report()

        assert report.candidate_id == "cand-feedback"
        assert len(report.outcomes) == 9
        assert not report.vetoed

    def test_a_failed_check_with_evidence_vetoes(self) -> None:
        """**否决是有的** —— 不然"能否决"这条能力压根没被验过。

        反过来对照上一条：一份九项全过的报告不否决。一正一反，两条都验了，
        才敢说"否决与否"这件事不是恒定的。

        用的是**可否决的那五项之一**（``evidence_sufficient``）—— "问题是否
        真实存在"不在其中，它检出了、列出来了，判不判由人看。
        """
        payload = json.loads(CRITIC_RAW)
        payload["outcomes"][1]["verdict"] = "failed"
        payload["outcomes"][1]["vetoes"] = True

        parsed = parse_output(CRITIC_PROMPT_SET, json.dumps(payload))
        assert isinstance(parsed, CriticOutput)

        report = parsed.to_report()

        assert report.vetoed
        assert [item.check.value for item in report.vetoing_outcomes] == ["evidence_sufficient"]


class TestTheConclusionContract:
    """结论本身的几条硬约束 —— 模型填错的地方。"""

    def test_supported_and_unsupported_partition_the_conclusions(self) -> None:
        """两条结论，一条有依据一条没有 —— 它们**恰好**分进两堆。

        盯的是**个数**不是"在某堆里"：一个"全都算有依据"的实现，同样能让
        "这条没依据的结论在 unsupported 那堆里"这条断言通过。
        """
        payload = json.loads(ANALYST_RAW)
        payload["gaps"][0].pop("evidence")

        parsed = parse_output(ANALYST_PROMPT_SET, json.dumps(payload))
        assert isinstance(parsed, AnalystOutput)

        assert len(parsed.conclusions) == 2
        assert len(parsed.supported_conclusions) == 1
        assert len(parsed.unsupported_conclusions) == 1
        assert supported(parsed.conclusions) == parsed.supported_conclusions
        assert unsupported(parsed.conclusions) == parsed.unsupported_conclusions

    @pytest.mark.parametrize("field", ["id", "statement"], ids=["id", "statement"])
    def test_a_blank_conclusion_field_is_refused(self, field: str) -> None:
        """空白的 id 与空白的主张都不收 —— 它们看着像有内容，其实没有。"""
        payload = json.loads(ANALYST_RAW)
        payload["gaps"][0][field] = "   "

        with pytest.raises(ValidationError):
            parse_output(ANALYST_PROMPT_SET, json.dumps(payload))

    def test_a_candidate_without_an_expected_outcome_is_refused(self) -> None:
        """预期结果至少一条 —— 没有它，事后无从核验当初的判断准不准。"""
        payload = json.loads(STRATEGIST_RAW)
        payload["candidates"][0]["expected_outcome"] = []

        with pytest.raises(ValidationError):
            parse_output(STRATEGIST_PROMPT_SET, json.dumps(payload))

    @pytest.mark.parametrize("confidence", [-0.1, 1.1], ids=["below", "above"])
    def test_a_confidence_outside_the_range_is_refused(self, confidence: float) -> None:
        """置信度落在 0~1 之外就报错，不夹紧 —— 夹紧了就与填 1 变成同一件事。"""
        payload = json.loads(STRATEGIST_RAW)
        payload["candidates"][0]["confidence"] = confidence

        with pytest.raises(ValidationError):
            parse_output(STRATEGIST_PROMPT_SET, json.dumps(payload))

    def test_an_evidence_reference_must_match_its_kind(self) -> None:
        """``kind="artifact"`` 配 ``ref="ISSUE-102"`` —— 形态与引用对不上就报错。

        写错了地方不会立刻出事：取证据的那一端会按产物去取，取不到 ——
        而那时候这条结论已经流到决策层了。
        """
        payload = json.loads(ANALYST_RAW)
        payload["gaps"][0]["evidence"][0]["kind"] = "artifact"

        with pytest.raises(ValidationError):
            parse_output(ANALYST_PROMPT_SET, json.dumps(payload))


class TestUnsupportedConclusionsAreExcluded:
    """T1 ② —— 没有证据的结论被标记，并在下游被排除。"""

    def test_analyst_gap_without_evidence_is_excluded(self) -> None:
        payload = json.loads(ANALYST_RAW)
        payload["gaps"][0].pop("evidence")
        payload["opportunities"][0]["evidence"] = []

        parsed = parse_output(ANALYST_PROMPT_SET, json.dumps(payload))
        assert isinstance(parsed, AnalystOutput)

        gap = parsed.gaps[0]
        assert gap.status is ConclusionStatus.UNSUPPORTED
        assert gap in parsed.unsupported_conclusions

        assessment = parsed.to_assessment()
        assert assessment.goal_gap == ()
        assert assessment.opportunities == ()
        assert assessment.recommendations == ()

    def test_opportunity_pointing_at_a_dropped_gap_loses_that_pointer(self) -> None:
        """差距掉队了，指着它的机会不该再指 —— 那是一条断掉的引用。"""
        payload = json.loads(ANALYST_RAW)
        payload["gaps"][0].pop("evidence")

        parsed = parse_output(ANALYST_PROMPT_SET, json.dumps(payload))
        assert isinstance(parsed, AnalystOutput)

        assessment = parsed.to_assessment()
        assert assessment.goal_gap == ()
        assert assessment.opportunities[0].addresses == ()

    @pytest.mark.parametrize(
        ("prompt_set", "raw"),
        [(item[0], item[1]) for item in CANDIDATE_ROLES],
        ids=["strategist", "architect", "quality"],
    )
    def test_candidate_without_evidence_never_becomes_a_candidate(
        self,
        prompt_set: PromptSet[RoleOutput],
        raw: str,
    ) -> None:
        payload = json.loads(raw)
        payload["candidates"][0].pop("evidence")

        parsed = cast(CandidateRoleOutput, parse_output(prompt_set, json.dumps(payload)))
        conclusion = parsed.candidates[0]
        assert conclusion.status is ConclusionStatus.UNSUPPORTED

        assert parsed.to_candidates() == ()

    def test_director_decision_without_evidence_never_becomes_a_decision(self) -> None:
        payload = json.loads(DIRECTOR_RAW)
        payload["decisions"][0].pop("evidence")

        parsed = parse_output(DIRECTOR_PROMPT_SET, json.dumps(payload))
        assert isinstance(parsed, DirectorOutput)

        decision = parsed.decisions[0]
        assert decision.status is ConclusionStatus.UNSUPPORTED
        assert decision in parsed.unsupported_conclusions

        decision_round = parsed.to_decision_round()
        assert decision_round.total == 1
        assert decision_round.recorded == ()

    def test_critic_check_without_evidence_becomes_unverified_and_does_not_veto(self) -> None:
        """**没查成不等于有问题** —— 缺证据的检查降级成"未核实"，且不否决。"""
        payload = json.loads(CRITIC_RAW)
        payload["outcomes"][0].pop("evidence")
        payload["outcomes"][0]["verdict"] = "failed"
        payload["outcomes"][0]["vetoes"] = True

        parsed = parse_output(CRITIC_PROMPT_SET, json.dumps(payload))
        assert isinstance(parsed, CriticOutput)

        outcome = parsed.outcomes[0]
        assert outcome.status is ConclusionStatus.UNSUPPORTED

        report = parsed.to_report()
        checked = report.outcome_for(outcome.check)
        assert checked.verdict.value == "unverified"
        assert checked.vetoes is False
        assert not report.vetoed

    def test_model_cannot_claim_status_it_does_not_have(self) -> None:
        """模型多说一句 ``status`` —— 收下但不采用。"""
        payload = json.loads(STRATEGIST_RAW)
        payload["candidates"][0]["status"] = "supported"

        parsed = cast(CandidateRoleOutput, parse_output(STRATEGIST_PROMPT_SET, json.dumps(payload)))
        conclusion = parsed.candidates[0]

        # 模型说了 supported，但证据还在，所以状态由证据说了算。
        assert conclusion.status is ConclusionStatus.SUPPORTED

    def test_model_cannot_promote_an_unsupported_conclusion(self) -> None:
        """模型既不能给自己加证据，也不能自己声明有依据。"""
        payload = json.loads(STRATEGIST_RAW)
        payload["candidates"][0].pop("evidence")
        payload["candidates"][0]["status"] = "supported"

        parsed = cast(CandidateRoleOutput, parse_output(STRATEGIST_PROMPT_SET, json.dumps(payload)))
        conclusion = parsed.candidates[0]

        assert conclusion.status is ConclusionStatus.UNSUPPORTED
        assert parsed.to_candidates() == ()

    def test_a_stray_category_does_not_change_the_role(self) -> None:
        """模型多说的 ``category`` 不作数 —— 类别由角色钉死。"""
        payload = json.loads(STRATEGIST_RAW)
        payload["candidates"][0]["category"] = "technology"

        parsed = cast(CandidateRoleOutput, parse_output(STRATEGIST_PROMPT_SET, json.dumps(payload)))

        assert parsed.to_candidates()[0].category is OpportunityCategory.PRODUCT


class TestParsingContract:
    """解析失败报的是"这份输出不合格"，不是"调用失败"。"""

    def test_fenced_json_is_accepted(self) -> None:
        """模型爱加围栏 —— 加围栏不是错误。"""
        raw = f"```json\n{ANALYST_RAW}\n```"
        parsed = parse_output(ANALYST_PROMPT_SET, raw)
        assert isinstance(parsed, AnalystOutput)

    def test_prose_is_not_accepted(self) -> None:
        """围栏里不是 JSON 就该报错 —— 从散文里抠 JSON 只会造出假合格。"""
        with pytest.raises(PromptParseError):
            parse_output(ANALYST_PROMPT_SET, "这是我的分析：项目还行。")

    def test_unknown_field_is_rejected(self) -> None:
        """schema 之外的字段是"模型没读契约"的硬证据 —— 该当场停下。"""
        payload = json.loads(ANALYST_RAW)
        payload["extra"] = "不该出现的字段"

        with pytest.raises(ValidationError):
            parse_output(ANALYST_PROMPT_SET, json.dumps(payload))

    def test_one_role_output_cannot_be_parsed_as_another(self) -> None:
        """六套输出互相套不上。"""
        with pytest.raises(ValidationError):
            parse_output(DIRECTOR_PROMPT_SET, ANALYST_RAW)


class TestPromptSetRegistry:
    """六套 Prompt 各自钉死自己的输出形状与职责边界。"""

    def test_there_are_six_prompt_sets(self) -> None:
        assert len(PROMPT_SETS) == 6

    def test_every_role_has_exactly_one_prompt_set(self) -> None:
        roles = [item.role for item in PROMPT_SETS]
        assert len(set(roles)) == len(roles) == len(Role)

    @pytest.mark.parametrize("role", list(Role), ids=lambda role: role.value)
    def test_prompt_set_for_role(self, role: Role) -> None:
        prompt_set = prompt_set_for(role)
        assert prompt_set.role is role
        assert prompt_set is prompt_set_for(role)

    @pytest.mark.parametrize("prompt_set", PROMPT_SETS, ids=lambda item: item.role.value)
    def test_purpose_and_boundary_are_written_down(self, prompt_set: PromptSet[RoleOutput]) -> None:
        """职责边界不是只写在文档里 —— 它得在 Prompt 集上看得见。"""
        assert prompt_set.purpose.strip()
        assert prompt_set.boundary.strip()

    @pytest.mark.parametrize("prompt_set", PROMPT_SETS, ids=lambda item: item.role.value)
    def test_output_schema_matches_the_prompt_set(self, prompt_set: PromptSet[RoleOutput]) -> None:
        assert issubclass(prompt_set.output_schema, RoleOutput)
        assert prompt_set.output_schema.model_json_schema()["title"] == (
            prompt_set.output_schema.__name__
        )

    @pytest.mark.parametrize("prompt_set", PROMPT_SETS, ids=lambda item: item.role.value)
    def test_request_carries_the_schema_and_the_model(
        self, prompt_set: PromptSet[RoleOutput]
    ) -> None:
        request = prompt_set.request("这里是你要读的材料。")

        assert request.role is prompt_set.role
        assert request.system_prompt == prompt_set.system_prompt
        assert request.output_schema == prompt_set.output_schema.model_json_schema()
        assert request.model == prompt_set.model.model
        assert request.temperature == prompt_set.model.temperature
        assert request.max_tokens == prompt_set.model.max_tokens

    def test_swapping_the_model_does_not_touch_the_prompt(self) -> None:
        """换模型不改 Prompt —— 换了模型跑出的差异才是可比的。"""
        swapped = ANALYST_PROMPT_SET.with_model(
            ModelSettings(model="claude-opus-4-8", temperature=0.0, max_tokens=8192)
        )

        assert swapped is not ANALYST_PROMPT_SET
        assert swapped.system_prompt == ANALYST_PROMPT_SET.system_prompt
        assert swapped.output_schema is ANALYST_PROMPT_SET.output_schema
        assert swapped.model.model == "claude-opus-4-8"
        assert ANALYST_PROMPT_SET.model.model != "claude-opus-4-8"


class TestConfiguredModel:
    """模型与温度可以外部配置 —— 配置在运行配置里，桥在编排层。"""

    def test_settings_are_the_source_of_the_model_settings(self) -> None:
        settings = Settings(
            model_name="claude-opus-4-8", model_temperature=0.0, model_max_tokens=8192
        )

        model_settings = model_settings_from(settings)

        assert model_settings.model == "claude-opus-4-8"
        assert model_settings.temperature == 0.0
        assert model_settings.max_tokens == 8192

    def test_with_configured_model_returns_a_new_prompt_set(self) -> None:
        settings = Settings(model_name="claude-opus-4-8")

        configured = with_configured_model(ANALYST_PROMPT_SET, settings)

        assert configured is not ANALYST_PROMPT_SET
        assert configured.system_prompt == ANALYST_PROMPT_SET.system_prompt
        assert configured.model.model == "claude-opus-4-8"

    def test_default_settings_match_the_prompt_defaults(self) -> None:
        """默认配置与 Prompt 的默认值对得上 —— 两处不该各写一套。"""
        settings = Settings()
        model_settings = model_settings_from(settings)

        assert model_settings.model == ANALYST_PROMPT_SET.model.model
        assert model_settings.temperature == ANALYST_PROMPT_SET.model.temperature
        assert model_settings.max_tokens == ANALYST_PROMPT_SET.model.max_tokens
