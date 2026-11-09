"""TASK-P2-014 —— 六层独立验证：六层各判各的，不坍缩成一个布尔值。

**这里验的就是 docs/tasks.md TASK-P2-014 的验收条件**："六层验证独立执行并
产出结构化多维报告，任一层失败独立给出修复诊断，不坍缩为单个布尔值"。

拆成四组用例：

1. **独立**（``TestLayersAreIndependent``）—— 一层失败、一层崩了，其余五层
   照常给结论。这是"六层独立执行"。
2. **多维**（``TestTheReportIsNotOneBoolean``）—— 报告里能同时看到"功能层
   过了、性能层判不了、安全层命中一条密钥"。这是"产出结构化多维报告"。
3. **各自的诊断**（``TestEachLayerGivesItsOwnDiagnosis``）—— 功能层说用例名，
   代码层说 lint 位置，安全层说命中的规则。这是"任一层失败独立给出修复
   诊断"。
4. **判不了不等于通过**（``TestPerformanceWithoutABaseline`` 与
   ``TestProductLayer``）—— 降级 G 那条："性能基线未建立"要报出来，不能
   当成 0，也不能当成通过。

**为什么诊断要用"这一层自己说的话"来验，而不是验"报告里有一段修复建议"。**
一段泛泛的"检查未通过，请修复"对六层都成立 —— 那说明它没有提供任何信息。
所以每条用例都钉住**这一层特有的东西**：用例名、文件行号、规则名、依赖
边。哪一层的诊断被换成了通用话术，这条用例就会红。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from core.evidence.artifacts import ArtifactKind
from core.planning.roadmap import TaskType
from core.project.models import ComparisonOperator, TargetValue, ValueKind
from harness.verification.contract import (
    AcceptanceContract,
    ContractEvaluation,
    ForbiddenRule,
    RequiredCheck,
    Threshold,
)
from harness.verification.engine import CheckExecution, MetricSpec, build_verification_env
from harness.verification.six_layer import (
    ArchitectureLayerRunner,
    CodeLayerRunner,
    CommandSpec,
    Diagnosis,
    FunctionalLayerRunner,
    GoalTarget,
    HumanReview,
    LayerContext,
    LayerEvidence,
    LayerOutcome,
    LayerResult,
    PerformanceBaseline,
    PerformanceLayerRunner,
    ProductLayerRunner,
    SecurityLayerRunner,
    SixLayerReport,
    SixLayerRequest,
    SixLayerVerdict,
    SixLayerVerifier,
    VerificationLayer,
)


@dataclass
class ScriptedCheckRunner:
    """按命令给结果的运行器。

    **结果按命令给。** 六层各自跑各自的命令，而一条命令的结果只在这一层
    有意义 —— 用"按工作区给"的分法就分不出六层了。
    """

    results: Mapping[tuple[str, ...], CheckExecution] = field(default_factory=dict)
    default: CheckExecution = field(default_factory=lambda: CheckExecution(0, "", ""))
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def run(
        self,
        command: Sequence[str],
        *,
        workspace: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> CheckExecution:
        # 工作区、环境与超时是这个替身的**全部含义**：结果按命令给，
        # 而"在哪儿跑、带什么环境、跑多久"对这份剧本没有影响。
        del workspace, env, timeout_seconds
        self.calls.append(tuple(command))
        return self.results.get(tuple(command), self.default)


def _ok(stdout: str = "") -> CheckExecution:
    return CheckExecution(0, stdout, "")


def _failed(stdout: str = "", stderr: str = "") -> CheckExecution:
    return CheckExecution(1, stdout, stderr)


def _not_started() -> CheckExecution:
    return CheckExecution(127, "", "command not found", started=False)


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _layer(report: SixLayerReport, layer: VerificationLayer) -> LayerResult:
    """取某一层的结果。

    **层不在报告里就是 bug** —— 六层少了一层，后面的断言就全都不作数了。
    与其让每处断言自己判一遍"是不是 None"，不如在这里炸掉。
    """
    result = report.result(layer)
    assert result is not None, f"报告里没有 {layer.value} 层 —— 六层少了一层"
    return result


def _diagnosis(result: LayerResult) -> Diagnosis:
    """取某一层的诊断。

    **没有诊断就是 bug** —— 判不过去的层必须给出该层自己的修复建议。
    """
    diagnosis = result.diagnosis
    assert diagnosis is not None, f"{result.layer.value} 层没有诊断"
    return diagnosis


def _evaluation(report: SixLayerReport) -> ContractEvaluation:
    """取契约判定。

    **没判定就是 bug** —— 带了契约的报告一定有判定结果。
    """
    evaluation = report.contract_evaluation
    assert evaluation is not None, "报告里没有契约判定"
    return evaluation


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """一个小仓库 —— **干净**的那种：产品代码不依赖测试代码。"""
    root = tmp_path / "repo"
    _write(root, "src/pkg/__init__.py", "")
    _write(root, "src/pkg/thing.py", "def f():\n    return 1\n")
    _write(
        root,
        "tests/test_thing.py",
        "from pkg.thing import f\n\n\ndef test_f():\n    assert f() == 1\n",
    )
    return root


@pytest.fixture
def broken_workspace(tmp_path: Path) -> Path:
    """一个**违反分层**的仓库 —— 产品代码依赖了测试代码。"""
    root = tmp_path / "broken"
    _write(root, "src/pkg/__init__.py", "")
    _write(root, "src/pkg/thing.py", "import tests.test_thing\n\n\ndef f():\n    return 1\n")
    _write(root, "tests/test_thing.py", "def test_f():\n    assert True\n")
    return root


def _contract(task_id: str = "T-1") -> AcceptanceContract:
    return AcceptanceContract(
        task_id=task_id,
        required=(RequiredCheck.UNIT_TESTS_PASS, RequiredCheck.LINT_PASS),
    )


def _verifier(runner: ScriptedCheckRunner) -> SixLayerVerifier:
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
                commands=(
                    CommandSpec(
                        ForbiddenRule.SECRETS_IN_DIFF.value,
                        ("scan-secrets",),
                    ),
                ),
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


class TestLayersAreIndependent:
    async def test_one_failing_layer_does_not_stop_the_others(self, workspace: Path) -> None:
        """**一层失败，其余五层照常给结论。**"""
        runner = ScriptedCheckRunner(
            results={("pytest", "tests/unit"): _failed("2 failed")},
        )
        report = await _verifier(runner).verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        assert _layer(report, VerificationLayer.FUNCTIONAL).outcome is LayerOutcome.FAILED
        assert _layer(report, VerificationLayer.CODE).outcome is LayerOutcome.PASSED
        assert _layer(report, VerificationLayer.SECURITY).outcome is LayerOutcome.PASSED

    async def test_a_command_that_never_started_is_an_error_not_a_failure(
        self, workspace: Path
    ) -> None:
        """**"没验成"与"没过"必须分开。** 命令压根没起来，是环境问题。"""
        runner = ScriptedCheckRunner(
            results={("pytest", "tests/unit"): _not_started()},
        )
        report = await _verifier(runner).verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        result = _layer(report, VerificationLayer.FUNCTIONAL)
        assert result.outcome is LayerOutcome.ERROR
        assert report.errored_layers == (VerificationLayer.FUNCTIONAL,)
        assert report.failed_layers == ()

    async def test_a_crashing_layer_leaves_the_others_intact(self, workspace: Path) -> None:
        """一层自己崩了 —— 其余五层的结论照给，崩的那层记成"没跑成"。"""

        class CrashingRunner:
            layer = VerificationLayer.FUNCTIONAL

            async def run(self, context: LayerContext) -> LayerEvidence:
                del context
                raise RuntimeError("层自己炸了")

            def judge(self, evidence: LayerEvidence) -> LayerResult:
                del evidence
                raise AssertionError("不该走到判定")

        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.FUNCTIONAL: CrashingRunner(),
                VerificationLayer.CODE: CodeLayerRunner(
                    commands=(CommandSpec("lint", ("ruff",)),),
                    runner=ScriptedCheckRunner(),
                ),
            }
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        assert _layer(report, VerificationLayer.FUNCTIONAL).outcome is LayerOutcome.ERROR
        assert _layer(report, VerificationLayer.CODE).outcome is LayerOutcome.PASSED

    async def test_a_layer_without_a_runner_is_an_error_when_required(
        self, workspace: Path
    ) -> None:
        """**要求这一层却没配执行器 —— 不能当成通过。**"""
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.CODE: CodeLayerRunner(
                    commands=(CommandSpec("lint", ("ruff",)),),
                    runner=ScriptedCheckRunner(),
                ),
            },
            required_layers=frozenset({VerificationLayer.CODE, VerificationLayer.PERFORMANCE}),
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        result = _layer(report, VerificationLayer.PERFORMANCE)
        assert result.outcome is LayerOutcome.ERROR
        assert "没有配置执行器" in result.detail


class TestTheReportIsNotOneBoolean:
    async def test_the_report_keeps_six_separate_results(self, workspace: Path) -> None:
        """**这是"不坍缩为单个布尔值"的核心。**"""
        runner = ScriptedCheckRunner(
            results={("pytest", "tests/unit"): _failed("boom")},
        )
        report = await _verifier(runner).verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        assert set(report.layers) == set(VerificationLayer)
        outcomes = {layer: result.outcome for layer, result in report.layers.items()}
        assert outcomes[VerificationLayer.FUNCTIONAL] is LayerOutcome.FAILED
        assert outcomes[VerificationLayer.CODE] is LayerOutcome.PASSED
        assert outcomes[VerificationLayer.SECURITY] is LayerOutcome.PASSED

    async def test_three_different_outcomes_coexist_in_one_report(self, workspace: Path) -> None:
        """**"功能层过了、性能层判不了、安全层命中一条密钥"能同时出现。**

        这正是把六层压成一个布尔值会丢掉的那些信息。
        """
        runner = ScriptedCheckRunner(
            results={
                ("scan",): _failed("found: AKIA...."),
            },
        )
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.FUNCTIONAL: FunctionalLayerRunner(
                    commands=(CommandSpec("unit", ("pytest",)),),
                    runner=runner,
                ),
                VerificationLayer.SECURITY: SecurityLayerRunner(
                    commands=(CommandSpec(ForbiddenRule.SECRETS_IN_DIFF.value, ("scan",)),),
                    runner=runner,
                ),
                VerificationLayer.PERFORMANCE: PerformanceLayerRunner(
                    commands=(CommandSpec("bench", ("bench",)),),
                    runner=runner,
                ),
            }
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        assert _layer(report, VerificationLayer.FUNCTIONAL).outcome is LayerOutcome.PASSED
        assert _layer(report, VerificationLayer.PERFORMANCE).outcome is LayerOutcome.NOT_JUDGED
        assert _layer(report, VerificationLayer.SECURITY).outcome is LayerOutcome.FAILED
        assert report.verdict is SixLayerVerdict.FAILED

    async def test_the_document_keeps_every_layer(self, workspace: Path) -> None:
        runner = ScriptedCheckRunner()
        report = await _verifier(runner).verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        document = report.to_document()
        layers = document["layers"]
        assert isinstance(layers, dict)
        assert set(layers) == {layer.value for layer in VerificationLayer}
        assert document["task_id"] == "T-1"

    async def test_only_the_required_layers_decide_the_verdict(self, workspace: Path) -> None:
        """不算数的层照样记结果 —— 但它不参与整体结论。"""
        runner = ScriptedCheckRunner(
            results={("pytest", "tests/unit"): _failed("boom")},
        )
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.FUNCTIONAL: FunctionalLayerRunner(
                    commands=(CommandSpec("unit", ("pytest", "tests/unit")),),
                    runner=runner,
                ),
                VerificationLayer.CODE: CodeLayerRunner(
                    commands=(CommandSpec("lint", ("ruff",)),),
                    runner=runner,
                ),
            },
            required_layers=frozenset({VerificationLayer.CODE}),
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        assert _layer(report, VerificationLayer.FUNCTIONAL).outcome is LayerOutcome.FAILED
        assert _layer(report, VerificationLayer.FUNCTIONAL).required is False
        assert report.verdict is SixLayerVerdict.PASSED

    async def test_the_verdict_prefers_a_failure_over_an_error(self, workspace: Path) -> None:
        """**有确定的失败时，结论是失败** —— "没跑成"另记在各自的属性里。"""
        runner = ScriptedCheckRunner(
            results={
                ("pytest", "tests/unit"): _failed("boom"),
                ("ruff", "check", "."): _not_started(),
            },
        )
        report = await _verifier(runner).verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        assert report.verdict is SixLayerVerdict.FAILED
        assert report.failed_layers == (VerificationLayer.FUNCTIONAL,)
        assert report.errored_layers == (VerificationLayer.CODE,)


class TestEachLayerGivesItsOwnDiagnosis:
    async def test_the_functional_layer_names_the_failing_cases(self, workspace: Path) -> None:
        runner = ScriptedCheckRunner(
            results={
                (
                    "pytest",
                    "tests/unit",
                ): _failed(
                    "FAILED tests/unit/test_a.py::test_one\nFAILED tests/unit/test_b.py::test_two"
                ),
            },
        )
        report = await _verifier(runner).verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        diagnosis = report.diagnoses[VerificationLayer.FUNCTIONAL]
        assert "tests/unit/test_a.py::test_one" in diagnosis.location
        assert "tests/unit/test_b.py::test_two" in diagnosis.location

    async def test_the_code_layer_names_the_lint_locations(self, workspace: Path) -> None:
        runner = ScriptedCheckRunner(
            results={
                ("ruff", "check", "."): _failed("src/pkg/thing.py:12:5: F401 未使用的导入"),
            },
        )
        report = await _verifier(runner).verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        diagnosis = report.diagnoses[VerificationLayer.CODE]
        assert "src/pkg/thing.py:12:5" in diagnosis.location

    async def test_the_code_layer_reads_ruff_s_full_output_format(self, workspace: Path) -> None:
        """**ruff 默认格式也要认** —— 规则名一行、位置一行。

        只认紧凑格式的话，ruff 默认输出的报错就只剩"检查没通过"这句话
        —— 而"哪一行"恰恰是这一层诊断里最有用的一半。
        """
        runner = ScriptedCheckRunner(
            results={
                ("ruff", "check", "."): _failed(
                    "F401 [*] `tests.test_thing` imported but unused\n"
                    " --> src/pkg/thing.py:1:8\n"
                    "  |\n"
                    "1 | import tests.test_thing\n"
                    "  |        ^^^^^^^^^^^^^^^^\n"
                ),
            },
        )
        report = await _verifier(runner).verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        diagnosis = report.diagnoses[VerificationLayer.CODE]
        assert "src/pkg/thing.py:1:8" in diagnosis.location
        assert "F401" in diagnosis.location

    async def test_the_security_layer_names_the_rule_it_hit(self, workspace: Path) -> None:
        runner = ScriptedCheckRunner(
            results={("scan-secrets",): _failed("AKIAXXXX found in diff")},
        )
        report = await _verifier(runner).verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        diagnosis = report.diagnoses[VerificationLayer.SECURITY]
        assert ForbiddenRule.SECRETS_IN_DIFF.value in diagnosis.location

    async def test_the_architecture_layer_names_the_dependency(
        self, broken_workspace: Path
    ) -> None:
        """**架构层说的必须是那条依赖边** —— 不是"架构有问题"。"""
        report = await _verifier(ScriptedCheckRunner()).verify(
            SixLayerRequest(task_id="T-1", workspace=broken_workspace)
        )
        diagnosis = report.diagnoses[VerificationLayer.ARCHITECTURE]
        assert "pkg.thing" in diagnosis.location
        assert "tests.test_thing" in diagnosis.location

    async def test_a_passing_layer_has_no_diagnosis(self, workspace: Path) -> None:
        """通过的层没有要修的东西 —— 硬编一条只会稀释这份报告。"""
        report = await _verifier(ScriptedCheckRunner()).verify(
            SixLayerRequest(task_id="T-1", workspace=workspace)
        )
        assert VerificationLayer.CODE not in report.diagnoses

    async def test_each_failing_layer_gets_its_own_diagnosis(self, workspace: Path) -> None:
        """**六层同时坏，每层说的是自己那件事。**"""
        runner = ScriptedCheckRunner(
            results={
                ("pytest", "tests/unit"): _failed("FAILED tests/unit/test_a.py::test_one"),
                ("ruff", "check", "."): _failed("src/pkg/thing.py:12:5: F401"),
                ("scan-secrets",): _failed("found"),
            },
        )
        report = await _verifier(runner).verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        diagnoses = report.diagnoses
        assert "test_one" in diagnoses[VerificationLayer.FUNCTIONAL].location
        assert "F401" in diagnoses[VerificationLayer.CODE].location
        assert ForbiddenRule.SECRETS_IN_DIFF.value in diagnoses[VerificationLayer.SECURITY].location


class TestPerformanceWithoutABaseline:
    """**降级 G —— 性能基线未建立就不判定。**"""

    async def test_no_baseline_means_not_judged(self, workspace: Path) -> None:
        runner = ScriptedCheckRunner()
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.PERFORMANCE: PerformanceLayerRunner(
                    commands=(CommandSpec("bench", ("bench",)),),
                    runner=runner,
                ),
            }
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        result = _layer(report, VerificationLayer.PERFORMANCE)
        assert result.outcome is LayerOutcome.NOT_JUDGED
        assert "基线未建立" in result.detail
        assert report.verdict is SixLayerVerdict.INCOMPLETE

    async def test_a_missing_baseline_is_not_reported_as_zero(self, workspace: Path) -> None:
        """**"没量过"不能被写成 0。**"""
        runner = ScriptedCheckRunner()
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.PERFORMANCE: PerformanceLayerRunner(
                    commands=(
                        CommandSpec(
                            "bench",
                            ("bench",),
                            metric=MetricSpec(name="latency", pattern=r"latency: (\d+)"),
                        ),
                    ),
                    runner=runner,
                ),
            }
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        assert "latency" not in _layer(report, VerificationLayer.PERFORMANCE).metrics

    async def test_a_regression_fails_the_layer(self, workspace: Path) -> None:
        runner = ScriptedCheckRunner(
            results={("bench",): _ok("latency: 120")},
        )
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.PERFORMANCE: PerformanceLayerRunner(
                    commands=(
                        CommandSpec(
                            "bench",
                            ("bench",),
                            metric=MetricSpec(name="latency", pattern=r"latency: (\d+)"),
                        ),
                    ),
                    baseline=PerformanceBaseline(
                        metric="latency",
                        value=Decimal(100),
                        higher_is_better=False,
                    ),
                    runner=runner,
                ),
            }
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        result = _layer(report, VerificationLayer.PERFORMANCE)
        assert result.outcome is LayerOutcome.FAILED
        assert "latency" in result.detail

    async def test_a_metric_within_tolerance_passes(self, workspace: Path) -> None:
        runner = ScriptedCheckRunner(
            results={("bench",): _ok("latency: 102")},
        )
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.PERFORMANCE: PerformanceLayerRunner(
                    commands=(
                        CommandSpec(
                            "bench",
                            ("bench",),
                            metric=MetricSpec(name="latency", pattern=r"latency: (\d+)"),
                        ),
                    ),
                    baseline=PerformanceBaseline(
                        metric="latency",
                        value=Decimal(100),
                        tolerance=Decimal("0.05"),
                        higher_is_better=False,
                    ),
                    runner=runner,
                ),
            }
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        assert _layer(report, VerificationLayer.PERFORMANCE).outcome is LayerOutcome.PASSED


class TestProductLayer:
    async def test_without_human_review_the_layer_is_not_judged(self, workspace: Path) -> None:
        """**没人评 ≠ 达到预期。**"""
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.PRODUCT: ProductLayerRunner(
                    commands=(CommandSpec("scenario", ("scenario",)),),
                    runner=ScriptedCheckRunner(),
                ),
            }
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        result = _layer(report, VerificationLayer.PRODUCT)
        assert result.outcome is LayerOutcome.NOT_JUDGED
        assert "人工评估" in result.detail

    async def test_an_unmet_goal_fails_before_the_missing_review(self, workspace: Path) -> None:
        """**目标确定没达到 —— 先判失败，别等评估。**"""
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.PRODUCT: ProductLayerRunner(
                    commands=(CommandSpec("scenario", ("scenario",)),),
                    targets=(
                        GoalTarget(
                            name="响应时间",
                            threshold=Threshold(
                                operator=ComparisonOperator.LTE,
                                value=TargetValue(kind=ValueKind.NUMBER, number=Decimal(100)),
                            ),
                            measured=TargetValue(kind=ValueKind.NUMBER, number=Decimal(250)),
                        ),
                    ),
                    runner=ScriptedCheckRunner(),
                ),
            }
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        result = _layer(report, VerificationLayer.PRODUCT)
        assert result.outcome is LayerOutcome.FAILED
        assert "响应时间" in _diagnosis(result).location

    async def test_a_goal_without_a_measurement_is_not_a_pass(self, workspace: Path) -> None:
        """**缺实测值算没达标** —— 那是契约那一侧的老规矩，这里一样。"""
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.PRODUCT: ProductLayerRunner(
                    commands=(CommandSpec("scenario", ("scenario",)),),
                    targets=(
                        GoalTarget(
                            name="响应时间",
                            threshold=Threshold(
                                operator=ComparisonOperator.LTE,
                                value=TargetValue(kind=ValueKind.NUMBER, number=Decimal(100)),
                            ),
                        ),
                    ),
                    runner=ScriptedCheckRunner(),
                ),
            }
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        assert _layer(report, VerificationLayer.PRODUCT).outcome is LayerOutcome.FAILED

    async def test_a_rejected_review_fails_the_layer(self, workspace: Path) -> None:
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.PRODUCT: ProductLayerRunner(
                    commands=(CommandSpec("scenario", ("scenario",)),),
                    review=HumanReview(reviewer="张三", accepted=False, comment="体验不对"),
                    runner=ScriptedCheckRunner(),
                ),
            }
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        result = _layer(report, VerificationLayer.PRODUCT)
        assert result.outcome is LayerOutcome.FAILED
        assert "体验不对" in _diagnosis(result).repair


class TestScope:
    async def test_the_contract_decides_which_layers_count(self, workspace: Path) -> None:
        """契约里的必检项归哪层，那层就算数。"""
        runner = ScriptedCheckRunner(
            results={("pytest", "tests/unit"): _failed("boom")},
        )
        report = await _verifier(runner).verify(
            SixLayerRequest(
                task_id="T-1",
                workspace=workspace,
                contract=_contract(),
            )
        )
        assert _layer(report, VerificationLayer.FUNCTIONAL).required is True
        assert _layer(report, VerificationLayer.CODE).required is True
        assert _layer(report, VerificationLayer.SECURITY).required is False

    async def test_the_task_type_decides_when_there_is_no_contract(self, workspace: Path) -> None:
        """**R-077 —— 不同任务类型走不同的验收流程。**"""
        runner = ScriptedCheckRunner(
            results={("pytest", "tests/unit"): _failed("boom")},
        )
        report = await _verifier(runner).verify(
            SixLayerRequest(task_id="T-1", workspace=workspace, task_type=TaskType.DOCS)
        )
        assert _layer(report, VerificationLayer.FUNCTIONAL).required is False
        assert _layer(report, VerificationLayer.PRODUCT).required is True

    async def test_different_task_types_ask_for_different_layers(self) -> None:
        """功能类任务要功能层，文档类任务不要。"""
        runner = ScriptedCheckRunner(
            results={("pytest", "tests/unit"): _failed("boom")},
        )
        feature = await _verifier(runner).verify(
            SixLayerRequest(task_id="T-1", workspace=Path(), task_type=TaskType.FEATURE)
        )
        docs = await _verifier(runner).verify(
            SixLayerRequest(task_id="T-1", workspace=Path(), task_type=TaskType.DOCS)
        )
        assert VerificationLayer.FUNCTIONAL in feature.required_layers
        assert VerificationLayer.FUNCTIONAL not in docs.required_layers


class TestContractEvaluation:
    async def test_the_contract_is_judged_with_the_existing_rules(self, workspace: Path) -> None:
        """**复用阶段一的判定函数** —— 不另写一套。"""
        runner = ScriptedCheckRunner(
            results={("pytest", "tests/unit"): _failed("boom")},
        )
        report = await _verifier(runner).verify(
            SixLayerRequest(task_id="T-1", workspace=workspace, contract=_contract())
        )
        assert report.contract_evaluation is not None
        assert _evaluation(report).passed is False
        assert any("unit_tests_pass" in item for item in report.contract_evaluation.failures)

    async def test_a_forbidden_rule_hit_fails_the_contract(self, workspace: Path) -> None:
        """**禁止项一票否决** —— 别的层过了也不行。"""
        runner = ScriptedCheckRunner(
            results={("scan-secrets",): _failed("found a key")},
        )
        contract = AcceptanceContract(
            task_id="T-1",
            required=(RequiredCheck.UNIT_TESTS_PASS,),
            forbidden=(ForbiddenRule.SECRETS_IN_DIFF,),
        )
        report = await _verifier(runner).verify(
            SixLayerRequest(task_id="T-1", workspace=workspace, contract=contract)
        )
        assert _evaluation(report).passed is False
        assert any("secrets_in_diff" in item for item in _evaluation(report).failures)

    async def test_a_threshold_is_judged_against_the_collected_metrics(
        self, workspace: Path
    ) -> None:
        runner = ScriptedCheckRunner(
            results={("pytest", "tests/unit"): _ok("coverage: 91.0%")},
        )
        contract = AcceptanceContract(
            task_id="T-1",
            required=(RequiredCheck.UNIT_TESTS_PASS,),
            thresholds={
                "coverage": Threshold(
                    operator=ComparisonOperator.GTE,
                    value=TargetValue(kind=ValueKind.NUMBER, number=Decimal("0.85")),
                ),
            },
        )
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.FUNCTIONAL: FunctionalLayerRunner(
                    commands=(
                        CommandSpec(
                            "unit",
                            ("pytest", "tests/unit"),
                            metric=MetricSpec(
                                name="coverage",
                                pattern=r"coverage: ([\d.]+)",
                                scale=Decimal("0.01"),
                            ),
                        ),
                    ),
                    runner=runner,
                ),
            }
        )
        report = await verifier.verify(
            SixLayerRequest(task_id="T-1", workspace=workspace, contract=contract)
        )
        assert _evaluation(report).passed is True

    async def test_a_contract_bound_to_another_task_fails(self, workspace: Path) -> None:
        """**拿 A 任务的契约放行 B 任务，等于没有契约。**"""
        runner = ScriptedCheckRunner()
        report = await _verifier(runner).verify(
            SixLayerRequest(
                task_id="T-2",
                workspace=workspace,
                contract=_contract(task_id="T-1"),
            )
        )
        assert _evaluation(report).passed is False


class TestReportProjection:
    async def test_the_projection_collapses_but_keeps_the_reasons(self, workspace: Path) -> None:
        """**投影给既有链路用** —— 但理由不能丢。"""
        runner = ScriptedCheckRunner(
            results={("pytest", "tests/unit"): _failed("boom")},
        )
        report = await _verifier(runner).verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        projected = report.to_report()
        assert projected.verdict.value == "failed"
        assert any("functional" in reason for reason in projected.reasons)

    async def test_a_not_judged_layer_is_not_projected_as_passed(self, workspace: Path) -> None:
        """**判不了不许投影成"过了"。**"""
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.PERFORMANCE: PerformanceLayerRunner(
                    commands=(CommandSpec("bench", ("bench",)),),
                    runner=ScriptedCheckRunner(),
                ),
            }
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        projected = report.to_report()
        assert projected.verdict.value == "error"
        assert ("performance", False) in projected.checks


class TestArtifacts:
    async def test_a_completed_command_produces_its_artifact(self, workspace: Path) -> None:
        runner = ScriptedCheckRunner()
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.FUNCTIONAL: FunctionalLayerRunner(
                    commands=(
                        CommandSpec(
                            "unit",
                            ("pytest",),
                            produces=(ArtifactKind.TEST_REPORT,),
                        ),
                    ),
                    runner=runner,
                ),
            }
        )
        report = await verifier.verify(SixLayerRequest(task_id="T-1", workspace=workspace))
        assert ArtifactKind.TEST_REPORT in _layer(report, VerificationLayer.FUNCTIONAL).artifacts


class TestRealCommands:
    """**真的起进程。** 前面那些用例用替身 —— 这一组起真的进程。

    替身验的是"判定逻辑"，这一组验的是"真的跑起来也说得清"。两件事都可能
    出错，而只有后者能发现"命令压根没起来却被当成了通过"。
    """

    async def test_a_real_command_that_succeeds(self, tmp_path: Path) -> None:
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.CODE: CodeLayerRunner(
                    commands=(CommandSpec("lint", (sys.executable, "-c", "print('ok')")),),
                ),
            }
        )
        report = await verifier.verify(
            SixLayerRequest(task_id="T-1", workspace=tmp_path, env=build_verification_env())
        )
        assert _layer(report, VerificationLayer.CODE).outcome is LayerOutcome.PASSED

    async def test_a_real_command_that_fails_carries_its_own_output(self, tmp_path: Path) -> None:
        """**失败详情是这一层看到的原文** —— 不是"检查失败"四个字。"""
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.CODE: CodeLayerRunner(
                    commands=(
                        CommandSpec(
                            "lint",
                            (
                                sys.executable,
                                "-c",
                                "import sys; sys.stderr.write('E501 行太长'); sys.exit(1)",
                            ),
                        ),
                    ),
                ),
            }
        )
        report = await verifier.verify(
            SixLayerRequest(task_id="T-1", workspace=tmp_path, env=build_verification_env())
        )
        result = _layer(report, VerificationLayer.CODE)
        assert result.outcome is LayerOutcome.FAILED
        assert any("E501" in item.message for item in result.findings)

    async def test_a_real_command_that_cannot_start_is_an_error(self, tmp_path: Path) -> None:
        """**命令不存在是"没验成"** —— 不是"没过"。"""
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.CODE: CodeLayerRunner(
                    commands=(CommandSpec("lint", ("ases-根本不存在的命令-xyz",)),),
                ),
            }
        )
        report = await verifier.verify(
            SixLayerRequest(task_id="T-1", workspace=tmp_path, env=build_verification_env())
        )
        result = _layer(report, VerificationLayer.CODE)
        assert result.outcome is LayerOutcome.ERROR
        assert "没能启动" in result.findings[0].message

    async def test_a_real_metric_is_read_out_of_the_output(self, tmp_path: Path) -> None:
        verifier = SixLayerVerifier(
            runners={
                VerificationLayer.PERFORMANCE: PerformanceLayerRunner(
                    commands=(
                        CommandSpec(
                            "bench",
                            (sys.executable, "-c", "print('latency: 120')"),
                            metric=MetricSpec(name="latency", pattern=r"latency: (\d+)"),
                        ),
                    ),
                ),
            }
        )
        report = await verifier.verify(
            SixLayerRequest(task_id="T-1", workspace=tmp_path, env=build_verification_env())
        )
        metric = _layer(report, VerificationLayer.PERFORMANCE).metrics["latency"]
        assert metric.number == Decimal(120)
