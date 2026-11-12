"""负金标矩阵 —— 失败关闭的每一条，逐条钉住。

**这个矩阵是什么。** 一张表，每一行是**一个真实故障 + 一个期望行为**：

======================  ==========================================  ==========
注入的故障               期望行为（fail-closed）                    守住的规则
======================  ==========================================  ==========
验证命令压根没启动      结论是 ``error``，不是 ``passed``            不知道 ≠ 没问题
证据不齐                结论是 ``error``，不是 ``passed``            同上
阈值没有实测值          判失败，不是"没这项"                      不靠降标准换通过
命中禁止项              一票否决，别的都过了也不行                  密钥不入代码不入库
必检项执行出错          判失败，且与"没通过"分开记                  崩了 ≠ 没过
六层性能层没有基线      ``NOT_JUDGED``，不是 ``passed``              没量过 ≠ 量过且达标
六层判不了投到契约上    落成 ``ERROR``，不是 ``PASSED``              同上
置信度不够              ``defer``，**不建任务**                      不行动也是合法决策
没有验收契约            进不了 ``RUNNING``                          契约是准入门不是事后报告
审计写不进去            不发布，零副作用                            审计写失败即阻断
授权身份有一个空字段    拒绝构造                                    身份不可混用
======================  ==========================================  ==========

**为什么必须有这张表，而不是"写几个测试"。** 因为本项目的主张是
fail-closed，而**fail-open 的错在顺利路径上根本看不见**。顺利的时候没人
会注意到"检查压根没启动"被当成了"通过"。所以这张表一半的行都是**故意
注入故障**的 —— 它们的价值恰恰在于顺利路径上不会发生。

**每一行都带着一个专属变异体。** 变异体是"把这条 fail-closed 规则改坏"
的具体改动（精确到源码字符串替换）。这条绑定是**结构性的**，不是注释里
的一句约定：矩阵的一行没有变异体就构造不出来，而变异体没有被任何行引用
会在自检里报错。

**它防的是什么。** 一个从来没有被证明会失败的断言，不叫断言，叫装饰。
``scripts/verify_golden_mutants.py`` 负责证明：把每个变异体施加到生产代码
上，这一行**必须变红**。九个变异体今天已经被现有测试杀死 —— 矩阵把它们
钉在同一个地方；剩下一个（六层投影那个）**现有测试杀不掉**，它是一个
活着的缺口，由矩阵补上。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

from core.evolution.decision import DecisionPolicy
from core.evolution.priority import PriorityFactors, ScoredCandidate
from core.project.models import TargetValue
from core.task.state_machine import (
    AcceptanceContractRequiredError,
    TaskAction,
    TaskState,
    TransitionContext,
    apply_action,
)
from harness.security.authorization import TaskAuthorization
from harness.verification.contract import (
    AcceptanceContract,
    CheckOutcome,
    CheckResult,
    ContractEvidence,
    ForbiddenRule,
    RequiredCheck,
    Threshold,
    evaluate_contract,
)
from harness.verification.engine import (
    CheckSpec,
    MetricSpec,
    VerificationEngine,
    VerificationPlan,
    VerificationRequest,
)
from harness.verification.six_layer import (
    ArchitectureLayerRunner,
    CodeLayerRunner,
    CommandSpec,
    FunctionalLayerRunner,
    PerformanceLayerRunner,
    ProductLayerRunner,
    SecurityLayerRunner,
    SixLayerRequest,
    SixLayerVerifier,
    VerificationLayer,
)
from orchestration.self_evolution import ProposalAuditError
from tests.golden.doubles import (
    ScriptedRunner,
    not_started,
    ok,
    self_evolution_rig,
    submit_and_approve,
)

#: 仓库根 —— 变异体的路径是相对它给的。
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: 矩阵里用的任务号。
TASK_ID: Final[str] = "TASK-GOLDEN-0001"

#: 矩阵里用的工作区目录名（临时目录里真的建出来 —— 契约要求工作区存在）。
_WORKSPACE: Final[str] = "repo"


# --------------------------------------------------------------------------
# 变异体
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mutant:
    """一个把 fail-closed 改坏的具体改动。

    :attr:`before` 与 :attr:`after` 是**精确的源码片段** —— 不是正则，不是
    行号。片段必须唯一命中，否则"变异没生效"这件事会被"测试还是绿的"掩盖
    过去，而那正是最危险的失败模式：变异脚本报告"抓住 3/11"，其实有一个
    压根没写进去。

    :attr:`why` 说清它把哪条规则改成了什么。这句话会进变异验证脚本的输出
    —— 有人问"这个变异体是干什么的"时，答案不该是"看 diff"。
    """

    id: str
    path: str
    before: str
    after: str
    why: str


# --------------------------------------------------------------------------
# 矩阵的一行
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailClosedCase:
    """一行 —— 一个故障，一份期望。

    :attr:`observe` 注入故障并**观测发生了什么**，返回一组事实。
    :attr:`expect` 是这些事实该长什么样。

    **事实是"看得见的结论"，不是"内部状态"。** 比如"结论是 error"而不是
    "outcome 字段等于 CheckOutcome.ERROR" —— 前者在实现换了表示方式之后
    仍然成立，后者会在重构时变成一片假红。事实要选**从外面能读到、而且
    读出来就说明问题的那些**。

    :attr:`red_targets` 是**变异之后必须变红的 pytest 目标**。每个目标都
    要是独立证人：矩阵自己是一条，既有测试里那条也是一条。两条都得红 ——
    少一条就说明这个变异体只被一个证人看着，那个证人一旦被改坏，规则就
    没人守了。
    """

    id: str
    fault: str
    invariant: str
    covers: tuple[str, ...]
    observe: Callable[[Path], Awaitable[Mapping[str, object]]]
    expect: Mapping[str, object]
    mutants: tuple[Mutant, ...]
    red_targets: tuple[str, ...]


# --------------------------------------------------------------------------
# 共用的构造件
# --------------------------------------------------------------------------


def _contract(**overrides: object) -> AcceptanceContract:
    """一份契约 —— 真模型，模型自己的校验照样生效。"""
    payload: dict[str, object] = {
        "task_id": TASK_ID,
        "required": (
            RequiredCheck.UNIT_TESTS_PASS,
            RequiredCheck.INTEGRATION_TESTS_PASS,
            RequiredCheck.LINT_PASS,
        ),
        "thresholds": {
            "test_coverage": Threshold.model_validate({"operator": ">=", "value": 0.85}),
        },
        "forbidden": (ForbiddenRule.SECRETS_IN_DIFF,),
    }
    payload.update(overrides)
    return AcceptanceContract.model_validate(payload)


def _plan(**overrides: object) -> VerificationPlan:
    """一份计划 —— 两项必检 + 一条密钥扫描。"""
    payload: dict[str, object] = {
        "checks": (
            CheckSpec(
                check=RequiredCheck.UNIT_TESTS_PASS,
                command=("fake", "unit"),
                metric=MetricSpec(
                    name="test_coverage",
                    pattern=r"TOTAL\s+(\d+\.\d+)%",
                    scale=Decimal("0.01"),
                ),
            ),
            CheckSpec(check=RequiredCheck.LINT_PASS, command=("fake", "lint")),
        ),
        "scans": (),
    }
    payload.update(overrides)
    return VerificationPlan(**payload)  # type: ignore[arg-type]


def _request(workspace: Path, **overrides: object) -> VerificationRequest:
    payload: dict[str, object] = {
        "task_id": TASK_ID,
        "workspace": workspace,
        "contract": _contract(),
        "plan": _plan(),
    }
    payload.update(overrides)
    return VerificationRequest(**payload)  # type: ignore[arg-type]


def _six_layer_verifier(runner: ScriptedRunner) -> SixLayerVerifier:
    """一套六层执行器 —— 每层一份，命令各不相同。"""
    return SixLayerVerifier(
        runners={
            VerificationLayer.FUNCTIONAL: FunctionalLayerRunner(
                commands=(CommandSpec("unit", ("pytest", "tests/unit")),),
                runner=runner,
            ),
            VerificationLayer.CODE: CodeLayerRunner(
                commands=(CommandSpec("lint", ("ruff", "check", ".")),),
                runner=runner,
            ),
            VerificationLayer.ARCHITECTURE: ArchitectureLayerRunner(),
            VerificationLayer.SECURITY: SecurityLayerRunner(
                commands=(CommandSpec("secrets", ("scan-secrets",)),),
                runner=runner,
            ),
            VerificationLayer.PERFORMANCE: PerformanceLayerRunner(
                commands=(CommandSpec("bench", ("bench",)),),
                runner=runner,
            ),
            VerificationLayer.PRODUCT: ProductLayerRunner(
                commands=(CommandSpec("scenario", ("scenario",)),),
                runner=runner,
            ),
        },
    )


# --------------------------------------------------------------------------
# 各行的观测
# --------------------------------------------------------------------------


async def _observe_check_never_started(workspace: Path) -> Mapping[str, object]:
    """检查压根没启动 —— **结论必须是 error**。"""
    runner = ScriptedRunner(results={("fake", "unit"): not_started()})
    engine = VerificationEngine(runner=runner)
    run = await engine.verify(_request(workspace))
    return {
        "verdict": run.to_report().verdict.value,
        "unit_outcome": run.checks[RequiredCheck.UNIT_TESTS_PASS].outcome.value,
        "evaluation_passed": run.evaluation.passed,
    }


async def _observe_evidence_incomplete(workspace: Path) -> Mapping[str, object]:
    """计划里少一项必检 —— **那一项没有结果，结论必须是 error**。

    这里用一份**只要求两项、没有阈值**的契约 —— 少了一项，缺的就只有那
    一项。契约里带阈值的话，缺的那一项和缺的那个阈值会混在一张缺失清单
    里，看不出到底是哪一样没到。
    """
    contract = AcceptanceContract(
        task_id=TASK_ID,
        required=(RequiredCheck.UNIT_TESTS_PASS, RequiredCheck.LINT_PASS),
    )
    plan = VerificationPlan(
        checks=(CheckSpec(check=RequiredCheck.UNIT_TESTS_PASS, command=("fake", "unit")),)
    )
    engine = VerificationEngine(runner=ScriptedRunner())
    run = await engine.verify(_request(workspace, contract=contract, plan=plan))
    return {
        "verdict": run.to_report().verdict.value,
        "evaluation_passed": run.evaluation.passed,
        "missing": sorted(run.evaluation.missing),
    }


async def _observe_threshold_without_measurement(workspace: Path) -> Mapping[str, object]:
    """阈值没有实测值 —— **判失败，不是"没这项"**。"""
    del workspace
    contract = _contract()
    evidence = ContractEvidence(
        task_id=TASK_ID,
        checks={
            check: CheckResult(check, CheckOutcome.PASSED, "通过") for check in contract.required
        },
        metrics={},
        violations=frozenset(),
        produced_artifacts=frozenset(),
    )
    result = evaluate_contract(contract, evidence)
    return {
        "passed": result.passed,
        "missing": sorted(result.missing),
        "failures": list(result.failures),
    }


async def _observe_forbidden_rule_hit(workspace: Path) -> Mapping[str, object]:
    """命中禁止项 —— **一票否决，别的都过了也不行**。"""
    del workspace
    contract = _contract()
    evidence = ContractEvidence(
        task_id=TASK_ID,
        checks={
            check: CheckResult(check, CheckOutcome.PASSED, "通过") for check in contract.required
        },
        metrics={"test_coverage": TargetValue.parse("0.95")},
        violations=frozenset({ForbiddenRule.SECRETS_IN_DIFF}),
        produced_artifacts=frozenset(),
    )
    result = evaluate_contract(contract, evidence)
    return {
        "passed": result.passed,
        "failures": list(result.failures),
    }


async def _observe_errored_required_check(workspace: Path) -> Mapping[str, object]:
    """必检项执行出错 —— **判失败，且与"没通过"分开记**。"""
    del workspace
    contract = _contract()
    evidence = ContractEvidence(
        task_id=TASK_ID,
        checks={
            RequiredCheck.UNIT_TESTS_PASS: CheckResult(
                RequiredCheck.UNIT_TESTS_PASS,
                CheckOutcome.ERROR,
                "命令没找到",
            ),
            RequiredCheck.INTEGRATION_TESTS_PASS: CheckResult(
                RequiredCheck.INTEGRATION_TESTS_PASS,
                CheckOutcome.PASSED,
                "通过",
            ),
            RequiredCheck.LINT_PASS: CheckResult(
                RequiredCheck.LINT_PASS,
                CheckOutcome.PASSED,
                "通过",
            ),
        },
        metrics={"test_coverage": TargetValue.parse("0.95")},
        violations=frozenset(),
        produced_artifacts=frozenset(),
    )
    result = evaluate_contract(contract, evidence)
    return {
        "passed": result.passed,
        "failures": list(result.failures),
        "mentions_error": any("执行出错" in item for item in result.failures),
    }


async def _observe_performance_without_baseline(workspace: Path) -> Mapping[str, object]:
    """性能层没有基线 —— **层结论是 NOT_JUDGED，整体结论是 INCOMPLETE**。"""
    runner = ScriptedRunner(results={("bench",): ok("latency 120ms")})
    report = await _six_layer_verifier(runner).verify(
        SixLayerRequest(task_id=TASK_ID, workspace=workspace)
    )
    layer = report.result(VerificationLayer.PERFORMANCE)
    assert layer is not None
    return {
        "layer_outcome": layer.outcome.value,
        "verdict": report.verdict.value,
        "mentions_baseline": "基线" in layer.detail,
    }


async def _observe_not_judged_projection(workspace: Path) -> Mapping[str, object]:
    """六层"判不了"投到契约上 —— **必须落成 ERROR，绝不能是 PASSED**。

    **这一行是矩阵里唯一一条现有测试杀不掉的变异体的证人。**
    ``harness/verification/six_layer.py`` 的 ``_check_outcome`` 把
    ``NOT_JUDGED`` 落成 ``ERROR``，而那个分支今天没有任何测试走到过 ——
    契约要么不要求任何层，要么要求的层都是过了或没过。把那个分支改成
    ``PASSED``，现有测试全绿。
    """
    runner = ScriptedRunner(results={("bench",): ok("latency 120ms")})
    contract = AcceptanceContract(
        task_id=TASK_ID,
        required=(RequiredCheck.PERFORMANCE_BASELINE_PASS,),
    )
    report = await _six_layer_verifier(runner).verify(
        SixLayerRequest(task_id=TASK_ID, workspace=workspace, contract=contract)
    )
    evaluation = report.contract_evaluation
    assert evaluation is not None
    layer = report.result(VerificationLayer.PERFORMANCE)
    assert layer is not None
    return {
        "layer_outcome": layer.outcome.value,
        "contract_passed": evaluation.passed,
        "contract_failures": list(evaluation.failures),
        "verdict": report.verdict.value,
    }


async def _observe_low_confidence(workspace: Path) -> Mapping[str, object]:
    """置信度不够 —— **defer，且不建任务**。"""
    del workspace
    scored = ScoredCandidate(
        candidate_id="cand-1",
        factors=PriorityFactors(
            value=Decimal("0.90"),
            impact=Decimal("0.80"),
            confidence=Decimal("0.10"),
            urgency=Decimal("0.50"),
            cost=Decimal("0.30"),
            risk=Decimal("0.20"),
        ),
    )
    decision = DecisionPolicy().decide(scored, decision_id="EVD-GOLDEN-0001")
    return {
        "decision": decision.decision.value,
        "produces_task": decision.produces_task,
        "analysis_count": len(decision.requested_analysis),
    }


async def _observe_contract_missing(workspace: Path) -> Mapping[str, object]:
    """没有验收契约 —— **进不了 RUNNING**。"""
    del workspace
    try:
        apply_action(
            TaskState.READY,
            TaskAction.START,
            TransitionContext(task_id=TASK_ID, acceptance_contract_bound=False),
        )
    except AcceptanceContractRequiredError as error:
        return {"raised": type(error).__name__, "message": str(error)}
    return {"raised": "", "message": ""}


async def _observe_audit_broken(workspace: Path) -> Mapping[str, object]:
    """审计写不进去 —— **不发布，零副作用**。

    **"零副作用"要验两件事**：改动没落地（``applied_count`` 为 0），**而且
    这次发布连一条审计痕迹都没留下**。只查前一件是不够的 —— 一次被拦下
    的发布如果还留下了痕迹，说明流程在失败之后仍然往下走了一步。

    审计条数**不比绝对值** —— 流程加一步，绝对值就变了，而那不是这条规则
    的事。这里比的是"发布前后一样"。
    """
    del workspace
    rig = self_evolution_rig()
    await submit_and_approve(rig)
    events_before = len(rig.audit.events)
    rig.audit.broken = True
    raised = ""
    try:
        await rig.flow.publish("p-1")
    except ProposalAuditError as error:
        raised = type(error).__name__
    return {
        "raised": raised,
        "applied_count": len(rig.applier.applied),
        "audit_unchanged": len(rig.audit.events) == events_before,
    }


async def _observe_empty_identity_field(workspace: Path) -> Mapping[str, object]:
    """授权身份有一个空字段 —— **拒绝构造**。"""
    del workspace
    try:
        TaskAuthorization(
            agent_id="agent-1",
            task_id=TASK_ID,
            project_id="proj-1",
            workspace_id="  ",
        )
    except ValueError as error:
        return {"raised": type(error).__name__, "message": str(error)}
    return {"raised": "", "message": ""}


# --------------------------------------------------------------------------
# 变异体
# --------------------------------------------------------------------------

MUT_ENG_01 = Mutant(
    id="MUT-ENG-01",
    path="harness/verification/engine.py",
    before="""    if not execution.started:
        return CheckResult(
            spec.check,
            CheckOutcome.ERROR,
            f"检查没能启动：{execution.stderr.strip() or '未知原因'}",
        )
""",
    after="""    if not execution.started:
        return CheckResult(
            spec.check,
            CheckOutcome.PASSED,
            f"检查没能启动：{execution.stderr.strip() or '未知原因'}",
        )
""",
    why="把“压根没启动”记成通过 —— 命令不存在时，验收会在什么都没验的情况下放行。",
)

MUT_ENG_02 = Mutant(
    id="MUT-ENG-02",
    path="harness/verification/engine.py",
    before="""        if not self.evidence_is_complete:
            verdict = VerificationVerdict.ERROR
        elif self.evaluation.passed:
""",
    after="""        if not self.evidence_is_complete:
            verdict = VerificationVerdict.PASSED
        elif self.evaluation.passed:
""",
    why="证据不齐却给通过 —— 缺项、缺结果都成了放行。",
)

MUT_CON_01 = Mutant(
    id="MUT-CON-01",
    path="harness/verification/contract.py",
    before="""        if actual is None:
            missing.append(name)
            failures.append(f"阈值 {name} 没有实测值")
            continue
""",
    after="""        if actual is None:
            missing.append(name)
            continue
""",
    why="阈值缺实测值时不再算失败 —— 用“没有证据”换“达标”。",
)

MUT_CON_02 = Mutant(
    id="MUT-CON-02",
    path="harness/verification/contract.py",
    # 片段用**拼接**写，不用三引号 —— 源码那一行本身就有 89 个字符，
    # 写成一行就超了行宽。片段的内容一个字都不能改，所以只能拆写法。
    before=(
        "    hit = sorted(rule.value for rule in contract.forbidden "
        "if rule in evidence.violations)\n"
        "    if not hit:\n"
        "        return []\n"
        "    return [f\"命中禁止项：{'、'.join(hit)}\"]\n"
    ),
    after=(
        "    hit = sorted(rule.value for rule in contract.forbidden "
        "if rule in evidence.violations)\n"
        "    if not hit:\n"
        "        return []\n"
        "    return []\n"
    ),
    why="禁止项不再一票否决 —— diff 里有密钥也能过。",
)

MUT_CON_03 = Mutant(
    id="MUT-CON-03",
    path="harness/verification/contract.py",
    before="""        elif result.outcome is CheckOutcome.ERROR:
            failures.append(f"必检项 {check.value} 执行出错：{result.detail or '无详情'}")
        elif result.outcome is CheckOutcome.FAILED:
""",
    after="""        elif result.outcome is CheckOutcome.FAILED:
""",
    why="执行出错的必检项既不算失败也不算缺失 —— 崩了的检查被当成过了。",
)

MUT_SIX_01 = Mutant(
    id="MUT-SIX-01",
    path="harness/verification/six_layer.py",
    before="""        if self.baseline is None:
            return LayerResult(
                self.layer,
                LayerOutcome.NOT_JUDGED,
""",
    after="""        if self.baseline is None:
            return LayerResult(
                self.layer,
                LayerOutcome.PASSED,
""",
    why="性能层没有基线时直接判通过 —— “没量过”被当成“量过且达标”。",
)

MUT_SIX_02 = Mutant(
    id="MUT-SIX-02",
    path="harness/verification/six_layer.py",
    before="""    if outcome is LayerOutcome.PASSED:
        return CheckOutcome.PASSED
    if outcome is LayerOutcome.FAILED:
        return CheckOutcome.FAILED
    return CheckOutcome.ERROR
""",
    after="""    if outcome is LayerOutcome.PASSED:
        return CheckOutcome.PASSED
    if outcome is LayerOutcome.FAILED:
        return CheckOutcome.FAILED
    return CheckOutcome.PASSED
""",
    why="六层“判不了”投到契约上时落成通过 —— 这条分支今天没有任何测试走到过。",
)

MUT_DEC_01 = Mutant(
    id="MUT-DEC-01",
    path="core/evolution/decision.py",
    before="""                decision=DecisionState.DEFER,
                rationale=(
                    f"证据置信度 {factors.confidence} 低于阈值 "
                    f"{self.minimum_confidence}，先补充分析再定"
                ),
                requested_analysis=(self._evidence_analysis(scored),),
""",
    after="""                decision=DecisionState.RECORD,
                rationale=(
                    f"证据置信度 {factors.confidence} 低于阈值 "
                    f"{self.minimum_confidence}，先补充分析再定"
                ),
""",
    why="置信度不够也照样 record —— 没坐实的问题被当成任务派了出去。",
)

MUT_GATE_01 = Mutant(
    id="MUT-GATE-01",
    path="core/task/state_machine.py",
    before="""    if target is TaskState.RUNNING and not context.acceptance_contract_bound:
        raise AcceptanceContractRequiredError(context.task_id)
""",
    after="""    if target is TaskState.RUNNING and False:
        raise AcceptanceContractRequiredError(context.task_id)
""",
    why="没有验收契约也能开工 —— 契约从“准入门”退化成“事后报告”。",
)

MUT_SELF_01 = Mutant(
    id="MUT-SELF-01",
    path="orchestration/self_evolution.py",
    before="""        try:
            await self.ports.audit.record(event)
        except Exception as error:
            raise ProposalAuditError(
                f"提案 {proposal.proposal_id} 的审计写不进去 —— 不往下走：{error}"
            ) from error
""",
    after="""        try:
            await self.ports.audit.record(event)
        except Exception:
            return
""",
    why="审计写失败被吞掉 —— 改动落地了，却答不出“这次凭什么发的”。",
)

MUT_AUTH_01 = Mutant(
    id="MUT_AUTH-01",
    path="harness/security/authorization.py",
    # 片段里带上了那句报错文案 —— ``AuthorizationRequest.__post_init__`` 里
    # 有一模一样的四元组校验，只有文案不同（“授权记录”对“授权请求”）。
    # 不带文案的话这段会命中两处，变异就会改错地方。
    before="""    def __post_init__(self) -> None:
        for name, value in (
            ("agent_id", self.agent_id),
            ("task_id", self.task_id),
            ("project_id", self.project_id),
            ("workspace_id", self.workspace_id),
        ):
            if not value.strip():
                raise ValueError(f"授权记录的 {name} 不能为空")
""",
    after="""    def __post_init__(self) -> None:
        for name, value in (
            ("agent_id", self.agent_id),
            ("task_id", self.task_id),
            ("project_id", self.project_id),
        ):
            if not value.strip():
                raise ValueError(f"授权记录的 {name} 不能为空")
""",
    why="授权身份的工作空间那一项不再必填 —— 空身份意味着“谁都能用”。",
)


# --------------------------------------------------------------------------
# 矩阵
# --------------------------------------------------------------------------

CASES: Final[tuple[FailClosedCase, ...]] = (
    FailClosedCase(
        id="FC-01",
        fault="验证命令压根没启动（命令不存在）",
        invariant="不知道 ≠ 没问题",
        covers=("R-049", "R-053"),
        observe=_observe_check_never_started,
        expect={
            "verdict": "error",
            "unit_outcome": "error",
            "evaluation_passed": False,
        },
        mutants=(MUT_ENG_01,),
        red_targets=(
            "tests/golden/test_fail_closed_matrix.py::TestTheFailClosedMatrix::test_row[FC-01]",
            "tests/unit/harness/verification/test_engine.py::TestTheFailClosedDirections"
            "::test_a_check_that_cannot_start_is_an_error",
        ),
    ),
    FailClosedCase(
        id="FC-02",
        fault="计划里少一项必检，那一项没有结果",
        invariant="不知道 ≠ 没问题",
        covers=("R-053",),
        observe=_observe_evidence_incomplete,
        expect={
            "verdict": "error",
            "evaluation_passed": False,
            "missing": ["lint_pass"],
        },
        mutants=(MUT_ENG_02,),
        red_targets=(
            "tests/golden/test_fail_closed_matrix.py::TestTheFailClosedMatrix::test_row[FC-02]",
            "tests/unit/harness/verification/test_engine.py::TestTheFailClosedDirections"
            "::test_a_missing_check_is_not_a_pass",
        ),
    ),
    FailClosedCase(
        id="FC-03",
        fault="阈值没有实测值（检查过了，但没输出覆盖率）",
        invariant="不靠降标准换通过",
        covers=("R-055",),
        observe=_observe_threshold_without_measurement,
        expect={
            "passed": False,
            "missing": ["test_coverage"],
            "failures": ["阈值 test_coverage 没有实测值"],
        },
        mutants=(MUT_CON_01,),
        red_targets=(
            "tests/golden/test_fail_closed_matrix.py::TestTheFailClosedMatrix::test_row[FC-03]",
            "tests/unit/harness/verification/test_contract.py::TestTheEvaluation"
            "::test_a_missing_metric_fails_the_contract",
        ),
    ),
    FailClosedCase(
        id="FC-04",
        fault="diff 里命中密钥（其它检查全过）",
        invariant="密钥不入代码、不入库",
        covers=("R-062",),
        observe=_observe_forbidden_rule_hit,
        expect={
            "passed": False,
            "failures": ["命中禁止项：secrets_in_diff"],
        },
        mutants=(MUT_CON_02,),
        red_targets=(
            "tests/golden/test_fail_closed_matrix.py::TestTheFailClosedMatrix::test_row[FC-04]",
            "tests/unit/harness/verification/test_contract.py::TestTheForbiddenRulesAreOneShot"
            "::test_a_secrets_violation_fails_even_when_everything_else_passes",
        ),
    ),
    FailClosedCase(
        id="FC-05",
        fault="必检项执行出错（不是“没通过”，是“没跑成”）",
        invariant="崩了 ≠ 没过",
        covers=("R-053",),
        observe=_observe_errored_required_check,
        expect={
            "passed": False,
            "failures": ["必检项 unit_tests_pass 执行出错：命令没找到"],
            "mentions_error": True,
        },
        mutants=(MUT_CON_03,),
        red_targets=(
            "tests/golden/test_fail_closed_matrix.py::TestTheFailClosedMatrix::test_row[FC-05]",
            "tests/unit/harness/verification/test_contract.py::TestTheEvaluation"
            "::test_an_errored_check_is_distinguished_from_a_failed_one",
        ),
    ),
    FailClosedCase(
        id="FC-06",
        fault="性能层没有基线",
        invariant="没量过 ≠ 量过且达标",
        covers=("R-050", "R-077"),
        observe=_observe_performance_without_baseline,
        expect={
            "layer_outcome": "not_judged",
            "verdict": "incomplete",
            "mentions_baseline": True,
        },
        mutants=(MUT_SIX_01,),
        red_targets=(
            "tests/golden/test_fail_closed_matrix.py::TestTheFailClosedMatrix::test_row[FC-06]",
            "tests/unit/harness/verification/test_six_layer.py::TestPerformanceWithoutABaseline"
            "::test_no_baseline_means_not_judged",
        ),
    ),
    FailClosedCase(
        id="FC-07",
        fault="六层“判不了”投到契约上（**现有测试杀不掉这个变异体**）",
        invariant="判不了绝不等于过了",
        covers=("R-077",),
        observe=_observe_not_judged_projection,
        expect={
            "layer_outcome": "not_judged",
            "contract_passed": False,
            "contract_failures": [
                "必检项 performance_baseline_pass 执行出错：性能基线未建立 "
                "—— 按降级 G，性能层在基线建立前不判定"
            ],
            "verdict": "incomplete",
        },
        mutants=(MUT_SIX_02,),
        red_targets=(
            "tests/golden/test_fail_closed_matrix.py::TestTheFailClosedMatrix::test_row[FC-07]",
        ),
    ),
    FailClosedCase(
        id="FC-08",
        fault="候选证据置信度不够",
        invariant="不行动也是合法决策",
        covers=("R-034", "R-035"),
        observe=_observe_low_confidence,
        expect={
            "decision": "defer",
            "produces_task": False,
            "analysis_count": 1,
        },
        mutants=(MUT_DEC_01,),
        red_targets=(
            "tests/golden/test_fail_closed_matrix.py::TestTheFailClosedMatrix::test_row[FC-08]",
            "tests/unit/evolution/test_decision.py::TestLowConfidenceProducesADefer"
            "::test_a_low_confidence_candidate_is_deferred",
        ),
    ),
    FailClosedCase(
        id="FC-09",
        fault="任务没有绑定验收契约就要开工",
        invariant="契约是准入门，不是事后报告",
        covers=("R-051",),
        observe=_observe_contract_missing,
        expect={
            "raised": "AcceptanceContractRequiredError",
            "message": f"任务 {TASK_ID} 在进入 RUNNING 前必须已有绑定 task_id 的验收契约",
        },
        mutants=(MUT_GATE_01,),
        red_targets=(
            "tests/golden/test_fail_closed_matrix.py::TestTheFailClosedMatrix::test_row[FC-09]",
            "tests/unit/task/test_state_machine.py::TestAcceptanceContractGate"
            "::test_cannot_start_without_acceptance_contract",
        ),
    ),
    FailClosedCase(
        id="FC-10",
        fault="自我改进提案落地时审计存储写不进去",
        invariant="审计写失败即阻断",
        covers=("R-104",),
        observe=_observe_audit_broken,
        expect={
            "raised": "ProposalAuditError",
            "applied_count": 0,
            "audit_unchanged": True,
        },
        mutants=(MUT_SELF_01,),
        red_targets=(
            "tests/golden/test_fail_closed_matrix.py::TestTheFailClosedMatrix::test_row[FC-10]",
            "tests/unit/orchestration/test_self_evolution.py::TestFailClosed"
            "::test_a_broken_audit_blocks_the_publish",
        ),
    ),
    FailClosedCase(
        id="FC-11",
        fault="授权记录的工作空间身份是空字符串",
        invariant="身份不可混用",
        covers=("R-060",),
        observe=_observe_empty_identity_field,
        expect={
            "raised": "ValueError",
            "message": "授权记录的 workspace_id 不能为空",
        },
        mutants=(MUT_AUTH_01,),
        red_targets=(
            "tests/golden/test_fail_closed_matrix.py::TestTheFailClosedMatrix::test_row[FC-11]",
            # **点名到具体那一条参数。** 这条用例是参数化的（四个身份字段各
            # 一条），而这次变异只动 ``workspace_id`` 那一项 —— 写函数名的话
            # 另外三条仍然绿着，"整个函数红了"这句话就不成立。点名到参数，
            # 红的到底是不是该红的那一条，才是能核对的。
            "tests/unit/harness/security/test_authorization.py::TestTheModelGuardsItself"
            "::test_an_empty_identity_field_is_refused[workspace_id]",
        ),
    ),
)


def case_by_id(case_id: str) -> FailClosedCase:
    """按 id 取一行。"""
    for case in CASES:
        if case.id == case_id:
            return case
    raise KeyError(f"矩阵里没有 {case_id!r} 这一行")


def all_mutants() -> tuple[Mutant, ...]:
    """矩阵引用的全部变异体 —— **按出现顺序，不重复**。"""
    seen: dict[str, Mutant] = {}
    for case in CASES:
        for mutant in case.mutants:
            seen.setdefault(mutant.id, mutant)
    return tuple(seen.values())


__all__ = [
    "CASES",
    "REPO_ROOT",
    "FailClosedCase",
    "Mutant",
    "all_mutants",
    "case_by_id",
]
