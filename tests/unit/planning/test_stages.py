"""五阶段契约的单元测试（TASK-P1-050）。

设计依据：docs/tasks.md TASK-P1-050、docs/design.md §5。

**这里验的是"契约本身"，不是"图跑起来"。** 图跑起来要 LangGraph，契约不
要 —— 而契约才是真正兜底的那一层：编排器**不校验状态**（实测把一个整数字段
写成字符串，它照跑不误），所以"这一步的产出对不对"只有领域层能判。

五条契约各有用例：

1. 次序 —— 早跑一步，后面那份产物就建在空地基上；
2. 类型 —— 类型不对的话，最后得到的是一份"结构完整但内容错位"的状态；
3. 只跑一次 —— 重跑会把定下来的结论换成另一个样子，而状态里只留最后一份；
4. 不越级 —— 跑完这一步之后，那一步的产物必须真的在状态里；
5. 不缺输入 —— 这一阶段的输入不在状态里（只可能来自一份坏掉的 checkpoint），
   就地停下，而不是接着跑出一份看起来完整的错计划。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.planning.roadmap import EvolutionRoadmap
from core.planning.stages import (
    STAGE_ORDER,
    GapAnalysis,
    PipelineState,
    ProjectContext,
    Stage,
    StageContractError,
    advance,
    draft_keys,
    next_stage,
    stage_field,
    stage_input,
)
from tests.support.decision_stages import (
    assessment,
    decision_outcome,
    discovery_result,
    gap_analysis,
    project_context,
    project_goal,
    roadmap,
)


def _state_after(*stages: Stage) -> PipelineState:
    """跑完前若干个阶段的状态。"""
    artifacts = {
        Stage.UNDERSTAND: assessment,
        Stage.DIAGNOSE: gap_analysis,
        Stage.EXPLORE: discovery_result,
        Stage.DECIDE: decision_outcome,
        Stage.PLAN: roadmap,
    }
    state = PipelineState(cycle_id="cycle-0007", context=project_context())
    for stage in stages:
        state = advance(state, stage, artifacts[stage]())
    return state


class TestTheStageOrder:
    """五阶段的次序是固定的。"""

    def test_the_order_is_understand_diagnose_explore_decide_plan(self) -> None:
        assert STAGE_ORDER == (
            Stage.UNDERSTAND,
            Stage.DIAGNOSE,
            Stage.EXPLORE,
            Stage.DECIDE,
            Stage.PLAN,
        )

    def test_the_next_stage_is_the_first_one_not_done(self) -> None:
        state = _state_after(Stage.UNDERSTAND, Stage.DIAGNOSE)
        assert next_stage(state) is Stage.EXPLORE

    def test_a_finished_chain_has_no_next_stage(self) -> None:
        state = _state_after(*STAGE_ORDER)
        assert next_stage(state) is None

    def test_each_stage_lands_on_its_own_field(self) -> None:
        assert stage_field(Stage.UNDERSTAND) == "assessment"
        assert stage_field(Stage.DIAGNOSE) == "gaps"
        assert stage_field(Stage.EXPLORE) == "discovery"
        assert stage_field(Stage.DECIDE) == "outcome"
        assert stage_field(Stage.PLAN) == "roadmap"


class TestTheOrderIsEnforced:
    """次序不对就报错 —— 早跑一步，后面那份产物就建在空地基上。"""

    def test_a_stage_cannot_run_before_its_predecessor(self) -> None:
        state = _state_after(Stage.UNDERSTAND)
        with pytest.raises(StageContractError) as caught:
            advance(state, Stage.EXPLORE, discovery_result())

        assert caught.value.stage is Stage.EXPLORE
        assert "diagnose" in caught.value.message

    def test_the_first_stage_cannot_be_skipped(self) -> None:
        state = PipelineState(cycle_id="cycle-0007", context=project_context())
        with pytest.raises(StageContractError) as caught:
            advance(state, Stage.DECIDE, decision_outcome())

        assert caught.value.stage is Stage.DECIDE
        assert "understand" in caught.value.message

    def test_nothing_can_run_after_the_last_stage(self) -> None:
        state = _state_after(*STAGE_ORDER)
        with pytest.raises(StageContractError) as caught:
            advance(state, Stage.PLAN, roadmap())

        assert "都已经跑完" in caught.value.message


class TestTheArtifactTypeIsEnforced:
    """产物类型不对就报错。"""

    def test_a_stage_will_not_accept_another_stage_s_artifact(self) -> None:
        state = _state_after(Stage.UNDERSTAND)
        with pytest.raises(StageContractError) as caught:
            advance(state, Stage.DIAGNOSE, roadmap())

        assert caught.value.stage is Stage.DIAGNOSE
        assert "类型不对" in caught.value.message

    def test_a_stage_will_not_accept_a_plain_string(self) -> None:
        state = _state_after(Stage.UNDERSTAND)
        with pytest.raises(StageContractError):
            advance(state, Stage.DIAGNOSE, "项目离目标还差一步")

    def test_the_error_names_both_types(self) -> None:
        """报错要说清"给的是什么、该是什么" —— 只说"错了"的错误没人能修。"""
        state = _state_after(Stage.UNDERSTAND)
        with pytest.raises(StageContractError) as caught:
            advance(state, Stage.DIAGNOSE, roadmap())

        assert "EvolutionRoadmap" in caught.value.message
        assert "GapAnalysis" in caught.value.message


class TestAStageRunsOnlyOnce:
    """同一个阶段不跑第二遍。"""

    def test_a_completed_stage_cannot_run_again(self) -> None:
        state = _state_after(Stage.UNDERSTAND, Stage.DIAGNOSE)
        with pytest.raises(StageContractError) as caught:
            advance(state, Stage.DIAGNOSE, gap_analysis())

        assert caught.value.stage is Stage.DIAGNOSE

    def test_advancing_returns_a_new_state_and_leaves_the_old_one_alone(self) -> None:
        """**状态是冻结的。** 返回新状态而不是就地改 —— 就地改的话，"上一次
        的状态"就没了，而从 checkpoint 恢复时正需要那一份。
        """
        state = _state_after(Stage.UNDERSTAND)
        updated = advance(state, Stage.DIAGNOSE, gap_analysis())

        assert state.completed_stages == (Stage.UNDERSTAND,)
        assert updated.completed_stages == (Stage.UNDERSTAND, Stage.DIAGNOSE)
        assert state.gaps is None
        assert updated.gaps is not None

    def test_the_artifact_lands_on_the_state(self) -> None:
        state = _state_after(Stage.UNDERSTAND)
        updated = advance(state, Stage.DIAGNOSE, gap_analysis())

        assert updated.gaps is not None
        assert updated.gaps.gaps[0].id == "GAP-1"


class TestTheDiagnosisNarrowsTheAssessment:
    """诊断阶段把评估**收窄**，不是复制。"""

    def test_the_opportunities_are_left_for_the_explore_stage(self) -> None:
        source = assessment()
        narrowed = GapAnalysis.from_assessment(source)

        assert narrowed.gaps == source.goal_gap
        assert narrowed.deferred == source.deferred
        assert narrowed.overall_status == source.overall_status
        # 机会不在诊断产物里 —— 它是探索阶段的原料。
        assert not hasattr(narrowed, "opportunities")
        assert not hasattr(narrowed, "recommendations")

    def test_the_gap_analysis_keeps_the_project_identity(self) -> None:
        source = assessment()
        narrowed = GapAnalysis.from_assessment(source)

        assert narrowed.project_id == source.project_id
        assert narrowed.snapshot == source.snapshot


class TestTheApprovalTravelsWithItsDecision:
    """放行凭据跟着决策走。"""

    def test_the_approval_for_a_decision_is_found(self) -> None:
        outcome = decision_outcome()
        decided = outcome.decisions.decisions[0]

        found = outcome.approval_for(decided.id)
        assert found is not None
        assert found.approved is True

    def test_an_unknown_decision_has_no_approval(self) -> None:
        """没有就是没有 —— 不猜。"""
        outcome = decision_outcome()

        assert outcome.approval_for("EVD-不存在") is None


class TestTheContextRequiresAGoal:
    """目标是必填的。"""

    def test_a_context_without_a_goal_is_refused(self) -> None:
        """**没有目标就没有"差距"。** 一份没有目标的项目评估看起来和真的评估
        一模一样 —— 状态恒为某个默认值、差距恒为空。
        """
        with pytest.raises(ValidationError) as caught:
            ProjectContext(project_id="p-demo", snapshot="snapshot-1")  # type: ignore[call-arg]

        assert "goal" in str(caught.value)

    def test_a_goal_that_is_missing_elements_is_refused(self) -> None:
        """六要素不齐的目标进不来 —— 那正是"目标不完整"最容易溜过去的地方。"""
        with pytest.raises(ValidationError):
            ProjectContext(project_id="p-demo", snapshot="snapshot-1", goal=object())  # type: ignore[arg-type]

    def test_a_complete_goal_is_accepted(self) -> None:
        context = ProjectContext(project_id="p-demo", snapshot="snapshot-1", goal=project_goal())
        assert context.goal.objective[0].id == "OBJ-1"


class TestTheRoadmapKeys:
    """路线图里所有任务的标识。"""

    def test_the_keys_come_in_schedule_order(self) -> None:
        assert draft_keys(roadmap()) == ("T-1",)

    def test_an_empty_roadmap_has_no_keys(self) -> None:
        assert draft_keys(EvolutionRoadmap(cycle_id="cycle-0007")) == ()


class TestEachStageReadsItsOwnInput:
    """**每个阶段读哪个字段，是契约的一部分。**

    取错字段是最难发现的一类错：类型往往还对得上（``GapAnalysis`` 与
    ``ProjectAssessment`` 不会混），但内容不对题 —— 于是下游拿着一份
    "看起来没问题"的输入继续算。
    """

    def test_the_diagnose_stage_reads_the_assessment(self) -> None:
        state = _state_after(Stage.UNDERSTAND)
        assert stage_input(state, Stage.DIAGNOSE) is state.assessment

    def test_the_explore_stage_reads_the_gaps(self) -> None:
        state = _state_after(Stage.UNDERSTAND, Stage.DIAGNOSE)
        assert stage_input(state, Stage.EXPLORE) is state.gaps

    def test_the_decide_stage_reads_the_discovery(self) -> None:
        state = _state_after(Stage.UNDERSTAND, Stage.DIAGNOSE, Stage.EXPLORE)
        assert stage_input(state, Stage.DECIDE) is state.discovery

    def test_the_plan_stage_reads_the_outcome(self) -> None:
        state = _state_after(Stage.UNDERSTAND, Stage.DIAGNOSE, Stage.EXPLORE, Stage.DECIDE)
        assert stage_input(state, Stage.PLAN) is state.outcome

    def test_the_understand_stage_reads_the_context(self) -> None:
        state = _state_after()
        assert stage_input(state, Stage.UNDERSTAND) is state.context


class TestAStageWithoutItsInputIsRefused:
    """**输入不在状态里，就地停下。**

    那种状态只能来自一份坏掉的 checkpoint（"认知阶段标记为已完成，评估却
    不在状态里"）。接着跑它不会得到一份错计划 —— 它会得到一份**看起来完整**
    的错计划。
    """

    def test_the_diagnose_stage_refuses_to_run_without_an_assessment(self) -> None:
        """认知阶段标记为已完成，评估却不在状态里。"""
        state = PipelineState(
            cycle_id="cycle-0007",
            context=project_context(),
            completed_stages=(Stage.UNDERSTAND,),
        )

        with pytest.raises(StageContractError) as caught:
            stage_input(state, Stage.DIAGNOSE)

        assert caught.value.stage is Stage.DIAGNOSE
        assert "assessment" in caught.value.message

    def test_the_explore_stage_refuses_to_run_without_the_gaps(self) -> None:
        state = PipelineState(
            cycle_id="cycle-0007",
            context=project_context(),
            completed_stages=(Stage.UNDERSTAND, Stage.DIAGNOSE),
        )

        with pytest.raises(StageContractError) as caught:
            stage_input(state, Stage.EXPLORE)

        assert caught.value.stage is Stage.EXPLORE
        assert "gaps" in caught.value.message

    def test_the_error_says_which_field_is_missing(self) -> None:
        """只说"缺输入"的话，读的人得自己猜缺的是哪一个。"""
        state = PipelineState(cycle_id="cycle-0007", context=project_context())

        with pytest.raises(StageContractError) as caught:
            stage_input(state, Stage.DECIDE)

        assert "discovery" in caught.value.message
