"""审查报告 —— Critic 审完一个候选之后留下的东西。

**Critic 的产出是一份报告，不是一个分数。** 分数会把"查过仓库、Git 历史、
测试报告、项目状态、任务表、产物登记处"这六件事压成一个数，而读的人无法
复核那个数背后到底查了什么。报告把九项检查的结论逐条摊开，每条结论都带
**证据引用** —— 凭什么这么说，指到具体的东西。

**三态不是两态。** 每项检查的结论是"通过 / 不通过 / 未核实"。第三态是这
个模块存在的理由：工具没配、事实不足，就写"未核实"；把它写成"没问题"，
Critic 就成了一台会点头的机器 —— 而它点头的依据是"没查"。

**否决必须带证据，而且必须是可否决的检查。** 能不能否决是**政策**
（:data:`~agents.critic.checks.VETOING_CHECKS`），不是临场判断：同一个检查在
这一轮否决、在下一轮不否决，读的人就说不清这一轮的标准是什么。
"""

from __future__ import annotations

from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.critic.checks import (
    CHECK_QUESTIONS,
    CheckOutcome,
    CriticCheck,
    CriticEvidence,
    EvidenceKind,
    Verdict,
)
from agents.critic.matching import match_forbidden
from agents.critic.tools import FactChecker, ToolKind, ToolResult
from core.evolution.candidate import EvolutionCandidate
from core.evolution.evidence import EvidenceKind as CandidateEvidenceKind

#: 一项检查最多带回多少条证据引用。
#:
#: **上限不是"够用就行"。** 一次仓库搜索能命中几十个文件，全列进报告，
#: 报告就没法读了 —— 读不下去的报告等于没有报告。多出来的命中被丢掉，
#: 但**丢掉多少是知道的**：这一条上限写在代码里，不是悄悄发生的。
_MAX_EVIDENCE: Final[int] = 5


class CriticReport(BaseModel):
    """一份审查报告 —— 逐项结论 + 证据引用 + 是否否决。

    **九项一项都不能少。** 模型会校验这件事：少一项的报告不是"少查了一项"，
    是"读的人会以为查过了" —— 那比明说"没查"更坏。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 被审的候选 id。
    candidate_id: str = Field(min_length=1)
    #: 九项检查的结论，一项一条。
    outcomes: tuple[CheckOutcome, ...]

    @model_validator(mode="after")
    def _every_check_answered(self) -> Self:
        """九项检查必须各有一条结论。"""
        answered = [outcome.check for outcome in self.outcomes]
        if len(answered) != len(set(answered)):
            raise ValueError("同一项检查只能有一条结论")
        missing = sorted(check.value for check in set(CriticCheck) - set(answered))
        if missing:
            raise ValueError(f"九项检查没有全部给出结论，缺：{'、'.join(missing)}")
        return self

    @property
    def vetoed(self) -> bool:
        """这份报告是否否决了候选。"""
        return any(outcome.vetoes for outcome in self.outcomes)

    @property
    def vetoing_outcomes(self) -> tuple[CheckOutcome, ...]:
        """构成否决的那几项。"""
        return tuple(outcome for outcome in self.outcomes if outcome.vetoes)

    @property
    def unverified(self) -> tuple[CheckOutcome, ...]:
        """没查成的那几项 —— "未核实"不等于"没问题"。"""
        return tuple(outcome for outcome in self.outcomes if outcome.verdict is Verdict.UNVERIFIED)

    def outcome_for(self, check: CriticCheck) -> CheckOutcome:
        """取某一项的结论。"""
        for outcome in self.outcomes:
            if outcome.check is check:
                return outcome
        raise KeyError(check.value)


class CriticVetoError(RuntimeError):
    """候选被 Critic 否决。"""


def require_no_veto(report: CriticReport) -> None:
    """候选被否决时抛 :class:`CriticVetoError`。

    **这是"不得进入 ``APPROVED``"的那道闸。** 任务由一条 ``record`` 决策派生，
    任务一落库就是 ``APPROVED`` 态 —— 所以否决必须在派生**之前**挡住。

    **为什么抛错而不是返回一个布尔。** 返回布尔的话，调用方忘了看返回值就
    等于没挡 —— 而"忘了看"正是最可能发生的那种错。抛错会把它变成一次崩溃，
    崩溃比漏看容易发现。

    **这道闸现在由谁拉。** 决策层派生任务时调用它（TASK-P1-056 拆解与派生的
    那一步）；在那之前，报告里的 :attr:`CriticReport.vetoed` 是唯一的否决
    事实来源。
    """
    if not report.vetoed:
        return
    reasons = "；".join(
        f"{CHECK_QUESTIONS.get(outcome.check, outcome.check.value)}：{outcome.summary}"
        for outcome in report.vetoing_outcomes
    )
    raise CriticVetoError(f"候选 {report.candidate_id} 被 Critic 否决 —— {reasons}")


class Critic:
    """独立 Critic —— 按九项检查审一个候选。

    **每项检查都真的去查。** 没有工具 → "未核实"，不猜；查了没有 → "查过，
    确实没有"。这两件事在报告里分得开，靠的是 :class:`CheckOutcome` 的三态。

    **判定与取证分开。** 工具只负责"查到了什么"（:class:`ToolResult`），
    检查负责"这意味着什么"（:class:`CheckOutcome`）。所以同一个工具可以被
    几项检查用来看不同的侧面 —— 项目状态工具就被用了四次，每次问一面。
    """

    def __init__(self, checker: FactChecker) -> None:
        self._checker = checker

    @property
    def checker(self) -> FactChecker:
        """事实核验器 —— 它的调用记录就是"查过什么"的账本。"""
        return self._checker

    async def review(self, candidate: EvolutionCandidate) -> CriticReport:
        """审一个候选，产出一份报告。

        **九项检查一项都不省。** 顺序就是 :class:`CriticCheck` 的顺序 ——
        读报告的人按 design.md §5 的七问加上补进去的两问往下对，对得上。
        """
        outcomes = (
            await self._problem_exists(candidate),
            await self._evidence_sufficient(candidate),
            await self._not_already_solved(candidate),
            await self._respects_goals_and_constraints(candidate),
            await self._has_lower_cost_alternative(candidate),
            await self._no_unnecessary_complexity(candidate),
            await self._worth_doing_now(candidate),
            await self._respects_non_goals(candidate),
            await self._does_not_trigger_stop_conditions(candidate),
        )
        return CriticReport(candidate_id=candidate.id, outcomes=outcomes)

    # ------------------------------------------------------------------
    # 九项检查
    # ------------------------------------------------------------------

    async def _problem_exists(self, candidate: EvolutionCandidate) -> CheckOutcome:
        """第一问：这个问题是否真实存在？

        **这一问的两头不对称。** 有一条提到这件事的**失败用例**，说明问题
        确实存在 —— 有人已经把它写下来了；但仓库里搜不到相关文件，
        **证明不了问题不存在**（可能还没人写下来，可能叫的是别的名字）。
        所以搜不到时的结论是"未核实"，不是"没问题"。

        **这一问不能否决。** 一个失败用例可能是因为别的原因失败（ flaky ），
        拿它当拦路虎，误杀的代价比漏放的代价大。
        """
        report = await self._checker.try_call(ToolKind.TEST_REPORT, candidate.title)
        if report is None:
            return _unverified(CriticCheck.PROBLEM_EXISTS, "没配测试报告工具 —— 没有工具就没有结论")

        search = await self._checker.try_call(ToolKind.REPOSITORY_SEARCH, candidate.title)
        located = _evidence(EvidenceKind.REPOSITORY, search)

        if not report.found:
            return CheckOutcome(
                check=CriticCheck.PROBLEM_EXISTS,
                verdict=Verdict.UNVERIFIED,
                summary=(
                    f"查过测试报告：{report.summary}；"
                    "没有失败用例能证明这个问题存在，而搜不到证明不了它不存在"
                ),
                evidence=tuple(located),
            )
        return CheckOutcome(
            check=CriticCheck.PROBLEM_EXISTS,
            verdict=Verdict.PASSED,
            summary=f"测试报告里有与「{candidate.title}」相关的失败用例 —— 问题真实存在",
            evidence=tuple(_evidence(EvidenceKind.TEST_REPORT, report) + located),
        )

    async def _evidence_sufficient(self, candidate: EvolutionCandidate) -> CheckOutcome:
        """第二问：是否有充分证据？

        **证据必须指得到一个真的东西。** 指向产物的引用要在产物登记处登记过；
        指向文件的引用要在仓库里真的存在。指不到 —— 这条证据就是空的。

        **全是外部引用时给"未核实"而不是"充分"。** 问题单号在外部系统里，
        本仓库核不了它 —— 核不了不等于它是对的。
        """
        missing: list[CriticEvidence] = []
        checked = 0

        for item in candidate.evidence:
            match item.kind:
                case CandidateEvidenceKind.ARTIFACT | CandidateEvidenceKind.TEST_REPORT:
                    result = await self._checker.try_call(ToolKind.ARTIFACT_STORE, item.identifier)
                    if result is None:
                        return _unverified(
                            CriticCheck.EVIDENCE_SUFFICIENT,
                            "没配产物登记处工具 —— 没有工具就没有结论",
                        )
                    checked += 1
                    if not result.found:
                        missing.append(
                            CriticEvidence(
                                kind=EvidenceKind.ARTIFACT,
                                reference=item.ref,
                                detail=result.summary,
                            )
                        )
                case CandidateEvidenceKind.FILE:
                    result = await self._checker.try_call(ToolKind.REPOSITORY_SEARCH, item.ref)
                    if result is None:
                        return _unverified(
                            CriticCheck.EVIDENCE_SUFFICIENT,
                            "没配仓库搜索工具 —— 没有工具就没有结论",
                        )
                    checked += 1
                    if not result.found:
                        missing.append(
                            CriticEvidence(
                                kind=EvidenceKind.REPOSITORY,
                                reference=item.ref,
                                detail=result.summary,
                            )
                        )
                case CandidateEvidenceKind.ISSUE:
                    # 外部系统里的单号 —— 本仓库核不了，跳过。
                    continue

        if checked == 0:
            # 全是外部引用：仓库与登记处都核不了它，但"核不了"也得留下
            # 一条查证记录 —— 免得报告读起来像"压根没看"。
            await self._checker.try_call(ToolKind.REPOSITORY_SEARCH, candidate.title)
            return _unverified(
                CriticCheck.EVIDENCE_SUFFICIENT,
                "候选的证据全是外部引用，本仓库核不了 —— 核不了不等于充分",
            )

        if missing:
            return CheckOutcome(
                check=CriticCheck.EVIDENCE_SUFFICIENT,
                verdict=Verdict.FAILED,
                summary=f"{len(missing)} 条证据指向的东西不存在或没登记",
                evidence=tuple(missing),
                vetoes=True,
            )
        return CheckOutcome(
            check=CriticCheck.EVIDENCE_SUFFICIENT,
            verdict=Verdict.PASSED,
            summary=f"{checked} 条证据都指得到真的东西",
        )

    async def _not_already_solved(self, candidate: EvolutionCandidate) -> CheckOutcome:
        """第三问：是否已经有其他任务解决？

        **靠任务表核验，不靠记忆。** "我记得已经有人做过了"是不可核验的；
        任务表里躺着一条相关的任务，是可核验的。

        **查到相关的**不否决** —— 这一条是评审改出来的。** 任务表能给出的
        只是"标题像是"，而像不等于同一件事：T3 评审里五条历史候选**全部**
        被这一问否决，其中至少三条的匹配是误报。**相似度当成"已解决"，误报
        率高到这条规则没法用** —— 候选成批被挡下，而挡下的理由读的人一眼就
        知道站不住。

        所以这一问**只检出，不拦路**：任务 id 原样列进证据里，"是不是同一件
        事"由人判断 —— 而那几条 id 正是给人判断用的材料。查不到才是"通过"
        （真的没有相关任务），查到而判不了就是"未核实"。
        """
        result = await self._checker.try_call(ToolKind.TASK_TABLE, candidate.title)
        if result is None:
            return _unverified(
                CriticCheck.NOT_ALREADY_SOLVED, "没配任务表工具 —— 没有工具就没有结论"
            )
        if not result.found:
            return CheckOutcome(
                check=CriticCheck.NOT_ALREADY_SOLVED,
                verdict=Verdict.PASSED,
                summary=f"查过任务表：{result.summary}",
            )
        return CheckOutcome(
            check=CriticCheck.NOT_ALREADY_SOLVED,
            verdict=Verdict.UNVERIFIED,
            summary=f"任务表里有相关任务：{result.summary} —— 是不是同一件事要人判断",
            evidence=tuple(_evidence(EvidenceKind.TASK, result)),
        )

    async def _respects_goals_and_constraints(self, _candidate: EvolutionCandidate) -> CheckOutcome:
        """第四问：是否违反项目目标或架构约束？

        **只看已确认违反的约束。** 约束的表达式要运行期的指标才能求值，而
        指标不在 Critic 手里 —— 所以"没有已确认违反的约束"意思是"没听说过
        出问题"，不是"查过了，都好着呢"。这句话写进 summary，读的人不会
        把它误当成"已验证无违反"。

        **不拿词面去撞约束。** 非目标是"明确不做什么"，撞上了就是触碰；
        约束是"必须做到什么"，撞上两个词说明不了任何事 —— 用它否决会误杀
        （候选里提到"覆盖率"，约束里写着"覆盖率不低于 80%"，这就否决了）。
        """
        result = await self._checker.try_call(ToolKind.PROJECT_STATUS, "constraints")
        if result is None:
            return _unverified(
                CriticCheck.RESPECTS_GOALS_AND_CONSTRAINTS, "没配项目状态工具 —— 没有工具就没有结论"
            )
        fact = result.status
        if fact is None:
            return _unverified(
                CriticCheck.RESPECTS_GOALS_AND_CONSTRAINTS,
                "项目状态工具没有给出状态 —— 没有事实就没有结论",
            )
        if fact.violated_constraints:
            return CheckOutcome(
                check=CriticCheck.RESPECTS_GOALS_AND_CONSTRAINTS,
                verdict=Verdict.FAILED,
                summary=f"项目当前有 {len(fact.violated_constraints)} 条已确认违反的约束",
                evidence=tuple(
                    CriticEvidence(
                        kind=EvidenceKind.CONSTRAINT,
                        reference=constraint_id,
                        detail=result.summary,
                    )
                    for constraint_id in fact.violated_constraints
                ),
                vetoes=True,
            )
        return CheckOutcome(
            check=CriticCheck.RESPECTS_GOALS_AND_CONSTRAINTS,
            verdict=Verdict.PASSED,
            summary=(
                f"按项目状态查询，没有已确认违反的约束（项目声明了 {len(fact.constraints)} 条）"
            ),
            evidence=tuple(
                CriticEvidence(
                    kind=EvidenceKind.CONSTRAINT,
                    reference=constraint.id,
                    detail=constraint.description,
                )
                for constraint in fact.constraints
            ),
        )

    async def _has_lower_cost_alternative(self, candidate: EvolutionCandidate) -> CheckOutcome:
        """第五问：有没有更低成本的解决方案？

        **这一问给不出结论，只能给线索。** "更省"是一个要跟别的方案比出来
        的判断 —— Git 历史只能告诉你"以前有没有做过类似的事"，比不了成本。
        所以结论永远是"未核实"，但线索要带回来：以前做过，就值得看一眼。

        **它不能否决。** 把线索当结论用，否决理由就经不起复核。
        """
        result = await self._checker.try_call(ToolKind.GIT_HISTORY, candidate.title)
        if result is None:
            return _unverified(
                CriticCheck.HAS_LOWER_COST_ALTERNATIVE, "没配 Git 历史工具 —— 没有工具就没有结论"
            )
        return CheckOutcome(
            check=CriticCheck.HAS_LOWER_COST_ALTERNATIVE,
            verdict=Verdict.UNVERIFIED,
            summary=f"{result.summary} —— 有没有更省的做法只能人判断",
            evidence=tuple(_evidence(EvidenceKind.GIT_HISTORY, result)),
        )

    async def _no_unnecessary_complexity(self, candidate: EvolutionCandidate) -> CheckOutcome:
        """第六问：是否引入不必要的复杂性？

        **同样给不出结论，只给线索。** 仓库里有几处相关 —— 那只是改动面的
        一个粗略影子；复杂不复杂取决于改动本身，不是取决于命中了几个文件。

        **它不能否决。** 同上：估出来的复杂度撑不起一个否决。
        """
        result = await self._checker.try_call(ToolKind.REPOSITORY_SEARCH, candidate.title)
        if result is None:
            return _unverified(
                CriticCheck.NO_UNNECESSARY_COMPLEXITY, "没配仓库搜索工具 —— 没有工具就没有结论"
            )
        return CheckOutcome(
            check=CriticCheck.NO_UNNECESSARY_COMPLEXITY,
            verdict=Verdict.UNVERIFIED,
            summary=f"{result.summary} —— 命中多处不等于复杂，复杂不复杂只能人判断",
            evidence=tuple(_evidence(EvidenceKind.REPOSITORY, result)),
        )

    async def _worth_doing_now(self, _candidate: EvolutionCandidate) -> CheckOutcome:
        """第七问：是否值得现在执行？

        **目标都达成了，就没有值得现在做的事。** 演进循环在项目该停的时候
        继续提候选，那正是最该克制的地方。

        **关键结果没测全 → "未核实"。** 少测了一项就不知道还剩多少事，
        说"值得做"和说"不值得做"都没依据。
        """
        result = await self._checker.try_call(ToolKind.PROJECT_STATUS, "key_results")
        if result is None:
            return _unverified(
                CriticCheck.WORTH_DOING_NOW, "没配项目状态工具 —— 没有工具就没有结论"
            )
        fact = result.status
        if fact is None:
            return _unverified(
                CriticCheck.WORTH_DOING_NOW, "项目状态工具没有给出状态 —— 没有事实就没有结论"
            )

        goals = tuple(
            CriticEvidence(
                kind=EvidenceKind.GOAL,
                reference=key_result.id,
                detail=f"{key_result.metric} 目标 {key_result.target}",
            )
            for key_result in fact.key_results
        )

        if fact.all_key_results_met is True:
            return CheckOutcome(
                check=CriticCheck.WORTH_DOING_NOW,
                verdict=Verdict.FAILED,
                summary="项目的关键结果都已达成 —— 没有值得现在做的事",
                evidence=goals,
                vetoes=True,
            )
        if fact.all_key_results_met is False:
            return CheckOutcome(
                check=CriticCheck.WORTH_DOING_NOW,
                verdict=Verdict.PASSED,
                summary=f"还有关键结果没达成（{result.summary}）",
                evidence=goals,
            )
        return _unverified(
            CriticCheck.WORTH_DOING_NOW,
            f"{result.summary} —— 关键结果没测全，说不清现在做还值不值得",
        )

    async def _respects_non_goals(self, candidate: EvolutionCandidate) -> CheckOutcome:
        """第八问：是否触碰项目 Non-goals？

        **非目标是"明确不做什么"，撞上了就是触碰。** 这也是九问里唯一一处
        用词面判定的检查 —— 它换来的是**可复核**：报告里写清"对上了哪一段"，
        读的人去看一眼就知道这个否决站不站得住。

        **为什么非目标能词面判、约束不能。** 非目标的描述里被禁的是**一件事**，
        候选提到同一件事就是碰到了；约束的描述里是**一条规则**（"覆盖率不低于
        80%"），候选提到同一个词只是碰巧 —— 用它否决会误杀。

        **比对的是标题与假设。** 证据里的引用是"这件事有依据"，不是"这件事
        是要做的" —— 拿证据去撞非目标，撞上的多半是别的任务留下的痕迹。
        """
        result = await self._checker.try_call(ToolKind.PROJECT_STATUS, "non_goals")
        if result is None:
            return _unverified(
                CriticCheck.RESPECTS_NON_GOALS, "没配项目状态工具 —— 没有工具就没有结论"
            )
        fact = result.status
        if fact is None:
            return _unverified(
                CriticCheck.RESPECTS_NON_GOALS, "项目状态工具没有给出状态 —— 没有事实就没有结论"
            )
        if not fact.non_goals:
            return CheckOutcome(
                check=CriticCheck.RESPECTS_NON_GOALS,
                verdict=Verdict.PASSED,
                summary="项目没有声明任何非目标",
            )

        text = f"{candidate.title} {candidate.hypothesis}"
        hits: list[CriticEvidence] = []
        for non_goal in fact.non_goals:
            hit = match_forbidden(text, non_goal.description)
            if hit is None:
                continue
            hits.append(
                CriticEvidence(
                    kind=EvidenceKind.NON_GOAL,
                    reference=non_goal.id,
                    detail=(
                        f"候选「{candidate.title}」与本条 Non-goal"
                        f"「{non_goal.description}」对上了「{hit}」"
                    ),
                )
            )

        if hits:
            return CheckOutcome(
                check=CriticCheck.RESPECTS_NON_GOALS,
                verdict=Verdict.FAILED,
                summary=(
                    f"候选触碰了 {len(hits)} 条项目 Non-goals："
                    f"{'、'.join(hit.reference for hit in hits)}"
                ),
                evidence=tuple(hits),
                vetoes=True,
            )
        return CheckOutcome(
            check=CriticCheck.RESPECTS_NON_GOALS,
            verdict=Verdict.PASSED,
            summary=f"逐条比对了 {len(fact.non_goals)} 条 Non-goals，没有命中",
        )

    async def _does_not_trigger_stop_conditions(
        self, _candidate: EvolutionCandidate
    ) -> CheckOutcome:
        """第九问：是否触发 Stop Conditions？

        **停止条件是"满足了就该停"。** 演进循环不查它，就会在项目该停的时候
        继续提候选 —— 那正是最该克制的地方。

        **只认已触发的。** "判不了"的停止条件（缺事实）不算触发 ——
        拿"判不了"当"触发了"，一次都停不下来。
        """
        result = await self._checker.try_call(ToolKind.PROJECT_STATUS, "stop_conditions")
        if result is None:
            return _unverified(
                CriticCheck.DOES_NOT_TRIGGER_STOP_CONDITIONS,
                "没配项目状态工具 —— 没有工具就没有结论",
            )
        fact = result.status
        if fact is None:
            return _unverified(
                CriticCheck.DOES_NOT_TRIGGER_STOP_CONDITIONS,
                "项目状态工具没有给出状态 —— 没有事实就没有结论",
            )
        if fact.stop_conditions_triggered:
            return CheckOutcome(
                check=CriticCheck.DOES_NOT_TRIGGER_STOP_CONDITIONS,
                verdict=Verdict.FAILED,
                summary=f"已触发 {len(fact.stop_conditions_triggered)} 条停止条件",
                evidence=tuple(
                    CriticEvidence(
                        kind=EvidenceKind.STOP_CONDITION,
                        reference=condition_id,
                        detail=result.summary,
                    )
                    for condition_id in fact.stop_conditions_triggered
                ),
                vetoes=True,
            )
        return CheckOutcome(
            check=CriticCheck.DOES_NOT_TRIGGER_STOP_CONDITIONS,
            verdict=Verdict.PASSED,
            summary="没有触发任何停止条件",
        )


def _unverified(check: CriticCheck, summary: str) -> CheckOutcome:
    """一条"未核实"的结论。"""
    return CheckOutcome(check=check, verdict=Verdict.UNVERIFIED, summary=summary)


def _evidence(kind: EvidenceKind, result: ToolResult | None) -> list[CriticEvidence]:
    """把工具结果里的引用转成证据引用。

    **引用上限在这里生效。** 一次仓库搜索能命中几十个文件，全列进报告，
    报告就没法读了 —— 读不下去的报告等于没有报告。
    """
    if result is None:
        return []
    return [
        CriticEvidence(kind=kind, reference=reference, detail=result.summary)
        for reference in result.references[:_MAX_EVIDENCE]
    ]


__all__ = [
    "Critic",
    "CriticReport",
    "CriticVetoError",
    "require_no_veto",
]
