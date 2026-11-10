"""TASK-P3-014 —— 自动优先级决策：校准能把次序救回来，救不回来就退回并记录。

**这是验收测试。** 验收条件是一条：

    **校准后模型的排序与"事后实际收益"有正相关（Spearman ρ > 0.3），否则退回
    启发式模型并记录。**

**这里的地基是造出来的，不是测出来的。** 下面那批"事后实际收益"是**按构造
排出来的** —— 每个候选实际兑现了多少，是我们自己定的。**真实项目里还没有
这样一份账**，所以这个 ρ 证明的是"这套校准的机制是对的"（被骗的时候救得回
来、没有关系的时候退得回去），**不是"这个模型在真实项目里有多准"**。两者差
的那一截，就是还没做的事 —— 架构文档里写着。

**最后一段是真的。** 那一段跑的是真仓库、真 pytest、真台账，走的是 P3-013 的
整条发现流水线 —— **今天它必然走退回那条路**（还没有任何实际收益），而这正是
它该有的样子。
"""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from apps.observe.pipeline import PipelineConfig, build_pipeline
from apps.priority.assessor import CategoryPriorAssessor
from core.evolution.candidate import EvolutionCandidate
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.judgement import FactorJudgement
from core.evolution.priority import ActualOutcome
from core.evolution.taxonomy import OpportunityCategory, RiskLevel
from orchestration.prioritize import PriorityDecider

#: 校准后必须超过的相关性门槛（验收条件里的那个 0.3）。
THRESHOLD = Decimal("0.3")


def _candidate(candidate_id: str) -> EvolutionCandidate:
    """造一个候选 —— 因子全一样，只留"价值"与"紧迫程度"两个可变量。"""
    return EvolutionCandidate(
        id=candidate_id,
        category=OpportunityCategory.ENGINEERING,
        title=f"候选 {candidate_id}",
        evidence=(Evidence(kind=EvidenceKind.FILE, ref="src/pkg/a.py"),),
        hypothesis="做了这件事会变好",
        expected_outcome=("变好了",),
        risk=RiskLevel.MEDIUM,
        confidence=Decimal("0.80"),
    )


def _ground_truth() -> tuple[
    tuple[EvolutionCandidate, ...],
    dict[str, FactorJudgement],
    dict[str, ActualOutcome],
]:
    """一批**按构造**排出来的候选 —— 实际收益跟着价值走。

    **紧迫程度与实际收益反着走。** 这是这份地基的全部用意：启发式模型把六
    个因子相乘，于是"越急越该先做"这条直觉会把次序带偏 —— **校准该学到的正
    是"紧迫程度不参与排序"**。

    **这批数据不是测出来的。** 它证明的是机制，不是真实准确度。
    """
    values = (
        "0.20",
        "0.30",
        "0.40",
        "0.50",
        "0.60",
        "0.70",
        "0.80",
        "0.90",
        "0.25",
        "0.35",
        "0.45",
        "0.55",
    )
    candidates: list[EvolutionCandidate] = []
    judgements: dict[str, FactorJudgement] = {}
    outcomes: dict[str, ActualOutcome] = {}
    for index, text in enumerate(values):
        candidate_id = f"c{index:02d}"
        value = Decimal(text)
        candidates.append(_candidate(candidate_id))
        judgements[candidate_id] = FactorJudgement(
            value=value,
            impact=Decimal("0.60"),
            urgency=Decimal(1) - value,
            cost=Decimal("0.30"),
        )
        outcomes[candidate_id] = ActualOutcome(
            candidate_id=candidate_id,
            realized_value=value,
        )
    return tuple(candidates), judgements, outcomes


def _noise(candidate_ids: tuple[str, ...]) -> dict[str, ActualOutcome]:
    """一批**没有关系**的实际收益 —— 排序救不回来的那种。"""
    realized = (
        "0.40",
        "0.70",
        "0.10",
        "0.80",
        "0.20",
        "0.50",
        "0.30",
        "0.60",
        "0.85",
        "0.25",
        "0.55",
        "0.15",
    )
    return {
        candidate_id: ActualOutcome(
            candidate_id=candidate_id,
            realized_value=Decimal(realized[index % len(realized)]),
        )
        for index, candidate_id in enumerate(candidate_ids)
    }


class _Assessor:
    """按 id 给判断的判断来源。"""

    def __init__(self, judgements: dict[str, FactorJudgement]) -> None:
        self._judgements = judgements

    def assess(self, candidate: EvolutionCandidate) -> FactorJudgement | None:
        """按 id 给判断。"""
        return self._judgements.get(candidate.id)


class TestTheCalibrationRecoversTheOrder:
    """验收条件的第一半：**校准后与实际收益正相关。"""

    def test_the_calibration_is_accepted(self) -> None:
        """**校准被采纳** —— 相关性过了门槛。"""
        candidates, judgements, outcomes = _ground_truth()
        decision = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=outcomes
        )
        assert decision.report.accepted is True

    def test_the_correlation_is_above_the_threshold(self) -> None:
        """**ρ > 0.3** —— 验收条件里的那个数。"""
        candidates, judgements, outcomes = _ground_truth()
        decision = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=outcomes
        )
        assert decision.report.rho_after is not None
        assert decision.report.rho_after > THRESHOLD

    def test_the_measured_correlation_is_the_leave_one_out_one(self) -> None:
        """**这个 ρ 是留出法量出来的** —— 不是拿拟合过的数据自证。

        自我验证的数一定好看；**"好看"不是证据，"没见过的数据上也对"才是**。
        """
        candidates, judgements, outcomes = _ground_truth()
        decision = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=outcomes
        )
        assert decision.report.rho_before is not None
        assert decision.report.rho_after is not None
        assert decision.report.rho_after > decision.report.rho_before

    def test_the_calibrated_order_follows_the_realised_value(self) -> None:
        """**校准后的次序该跟着实际收益走** —— 那正是校准的目的。

        这批地基里实际收益跟着价值走，而价值最大的那个候选**紧迫程度最小**
        —— 启发式模型正是在这儿翻的车。
        """
        candidates, judgements, outcomes = _ground_truth()
        decision = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=outcomes
        )
        ordered = [item.candidate_id for item in decision.ranked]
        by_value = sorted(
            ordered,
            key=lambda candidate_id: -judgements[candidate_id].value,
        )
        assert ordered == by_value


class TestTheFallback:
    """验收条件的第二半：**没到门槛就退回启发式模型，并记录。**"""

    def test_without_a_relation_it_falls_back(self) -> None:
        """**没有关系的实际收益 → 校准不被采纳。**"""
        candidates, judgements, _ = _ground_truth()
        noise = _noise(tuple(judgements))
        decision = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=noise
        )
        assert decision.report.accepted is False
        assert decision.report.rho_after is not None
        assert decision.report.rho_after <= THRESHOLD

    def test_the_fallback_is_recorded(self) -> None:
        """**"我们试过校准，没成"要留下来。**

        不记的话，下一次会有人以为这个模型是校准过的。
        """
        candidates, judgements, _ = _ground_truth()
        noise = _noise(tuple(judgements))
        decision = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=noise
        )
        assert "退回启发式模型" in decision.report.reason

    def test_the_order_is_the_heuristic_one(self) -> None:
        """**退回就一个字都不改** —— 次序与"压根没有实际收益"时一模一样。"""
        candidates, judgements, _ = _ground_truth()
        plain = PriorityDecider().decide(candidates, assessor=_Assessor(judgements))
        noise = _noise(tuple(judgements))
        fallen = PriorityDecider().decide(
            candidates, assessor=_Assessor(judgements), outcomes=noise
        )
        assert [item.candidate_id for item in fallen.ranked] == [
            item.candidate_id for item in plain.ranked
        ]


class TestTheRealPipeline:
    """真仓库 → 真候选 → 真次序。"""

    @pytest.fixture
    def candidates(self, tmp_path: Path) -> tuple[EvolutionCandidate, ...]:
        """真仓库里跑一遍 P3-013 的流水线，拿真的候选。"""
        root = tmp_path / "project"
        files = {
            "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
            "src/pkg/__init__.py": '"""一个看起来很正常的包。"""\n',
            "src/pkg/ok.py": '"""一个没毛病的模块。"""\n\nVALUE = 1\n',
            "known_issues/__init__.py": "",
            "known_issues/test_known.py": (
                '"""一个已知缺陷的复现用例。"""\n\nimport pytest\n\n\n'
                "@pytest.mark.xfail(strict=True)\n"
                "def test_the_known_bug_is_still_there() -> None:\n    assert 1 == 2\n"
            ),
            "docs/issues.md": (
                "# 问题清单\n\n### PILOT-001 —— `stats` 子命令收到空输入时抛未捕获的异常\n"
            ),
        }
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        for key, value in (("user.email", "tester@example.invalid"), ("user.name", "Tester")):
            subprocess.run(
                ["git", "-C", str(root), "config", key, value], check=True, capture_output=True
            )
        subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-q", "-m", "第一版"],
            check=True,
            capture_output=True,
        )
        built = build_pipeline(root, PipelineConfig(project_id="P1", targets=("known_issues/",)))
        _snapshot, discovery = built.run()
        return discovery.candidates

    def test_the_real_candidates_get_ranked(
        self, candidates: tuple[EvolutionCandidate, ...]
    ) -> None:
        """**真候选排得出次序** —— 一条都不落下。"""
        decision = PriorityDecider().decide(candidates, assessor=CategoryPriorAssessor())
        assert candidates
        assert len(decision.ranked) == len(candidates)
        assert decision.unranked == ()

    def test_the_order_is_by_score(self, candidates: tuple[EvolutionCandidate, ...]) -> None:
        """**次序是按分数降下来的** —— 不是一个随机的排列。"""
        decision = PriorityDecider().decide(candidates, assessor=CategoryPriorAssessor())
        scores = [item.score for item in decision.ranked]
        assert scores == sorted(scores, reverse=True)

    def test_today_it_falls_back(self, candidates: tuple[EvolutionCandidate, ...]) -> None:
        """**今天没有实际收益，所以走退回那条路** —— 而且这件事被记下来了。

        **这不是缺陷，是现状。** 真实项目里还没有一份"事后实际收益"的账，
        校准没有数据可用。**假装校准过了才是缺陷。**
        """
        decision = PriorityDecider().decide(candidates, assessor=CategoryPriorAssessor())
        assert decision.report.accepted is False
        assert "样本不足" in decision.report.reason
