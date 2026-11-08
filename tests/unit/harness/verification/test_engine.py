"""独立验证引擎 —— "不让 Coding Agent 自己决定是否验收通过"这句话的结构。

验证（T1，对应 docs/tasks.md TASK-P1-041）：

1. 验证进程以独立容器/进程启动，断言其环境中不存在 Agent 会话句柄；
2. 故意让 Agent 报告 ``self_assessment.completed = true`` 而实际测试失败
   → 验证结果为 **failed**，任务状态为 failed；
3. 验证进程挂掉时结果为 **error** 而非 **passed**（fail-closed）。

验证（T2）：验证结论与 ``self_assessment`` 的一致率为 0。

**这个文件最有价值的几条断言，是那些"结构上做不到"的断言。**

比如"引擎看不见 Agent 的自我报告"——如果只是写一句"引擎不读
``self_assessment``"，那它随时会被一句"就参考一下"破坏。所以这里断言的是
:class:`VerificationRequest` 的**字段集合**：那个类里压根没有能装下
执行结果的地方。同理，"验证进程里没有会话句柄"断言的是**真的起一个子进程
把它的环境变量打印出来**，而不是断言某个函数"没有传"。
"""

from __future__ import annotations

import ast
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from core.task.outcome import (
    ExecutionStatus,
    TaskOutcome,
    VerificationVerdict,
    resolve_outcome,
)
from executors.base import (
    ExecutionArtifacts,
    ExecutionMetrics,
    ExecutionResult,
    SelfAssessment,
)
from harness.verification.contract import (
    AcceptanceContract,
    ArtifactKind,
    CheckOutcome,
    ForbiddenRule,
    RequiredCheck,
    Threshold,
)
from harness.verification.engine import (
    CheckExecution,
    CheckSpec,
    MetricSpec,
    ScanSpec,
    SubprocessCheckRunner,
    VerificationEngine,
    VerificationEnvError,
    VerificationPlan,
    VerificationRequest,
    assert_verification_env_is_safe,
    build_verification_env,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
ENGINE_PATH = REPO_ROOT / "harness" / "verification" / "engine.py"

TASK_ID = "TASK-1024"

_UNIT = ("fake", "unit")
_INTEGRATION = ("fake", "integration")
_LINT = ("fake", "lint")
_ARCHITECTURE = ("fake", "architecture")
_SECRET_SCAN = ("fake", "secret-scan")


def _threshold(operator: str, value: object) -> Threshold:
    return Threshold.model_validate({"operator": operator, "value": value})


def _contract(**overrides: object) -> AcceptanceContract:
    """一份标准契约：四项必检 + 覆盖率阈值 + 三条禁止项 + 三个产物。"""
    payload: dict[str, object] = {
        "task_id": TASK_ID,
        "required": (
            RequiredCheck.UNIT_TESTS_PASS,
            RequiredCheck.INTEGRATION_TESTS_PASS,
            RequiredCheck.LINT_PASS,
            RequiredCheck.ARCHITECTURE_RULES_PASS,
        ),
        "thresholds": {"test_coverage": _threshold(">=", 0.85)},
        "forbidden": (
            ForbiddenRule.SECRETS_IN_DIFF,
            ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE,
            ForbiddenRule.PUBLIC_API_BREAKING_CHANGE,
        ),
        "artifacts": (ArtifactKind.GIT_DIFF, ArtifactKind.TEST_REPORT),
    }
    payload.update(overrides)
    return AcceptanceContract.model_validate(payload)


def _plan(**overrides: object) -> VerificationPlan:
    """一份标准计划：四项检查 + 一条密钥扫描。"""
    payload: dict[str, object] = {
        "checks": (
            CheckSpec(
                check=RequiredCheck.UNIT_TESTS_PASS,
                command=_UNIT,
                # 覆盖率工具吐的是百分数（95.00），契约里写的是比例（0.85）——
                # 差的就是这个 scale。少乘 0.01 的话 95% 会被当成 95 倍达标。
                metric=MetricSpec(
                    name="test_coverage",
                    pattern=r"TOTAL\s+(\d+\.\d+)%",
                    scale=Decimal("0.01"),
                ),
                produces=(ArtifactKind.TEST_REPORT,),
            ),
            CheckSpec(check=RequiredCheck.INTEGRATION_TESTS_PASS, command=_INTEGRATION),
            CheckSpec(check=RequiredCheck.LINT_PASS, command=_LINT),
            CheckSpec(check=RequiredCheck.ARCHITECTURE_RULES_PASS, command=_ARCHITECTURE),
        ),
        "scans": (
            ScanSpec(
                rule=ForbiddenRule.SECRETS_IN_DIFF,
                command=_SECRET_SCAN,
                produces=(ArtifactKind.GIT_DIFF,),
            ),
        ),
    }
    payload.update(overrides)
    return VerificationPlan(**payload)  # type: ignore[arg-type]


@dataclass
class FakeRunner:
    """按命令返回预设结果的运行器。

    **不是 mock 框架里的那种"万能替身"** —— 它只做一件事：命令 → 结果。
    引擎怎么把结果折成结论，由引擎自己的代码负责，替身不参与。
    """

    results: dict[tuple[str, ...], CheckExecution] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    seen_envs: list[Mapping[str, str]] = field(default_factory=list)

    async def run(
        self,
        command: Sequence[str],
        *,
        workspace: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> CheckExecution:
        key = tuple(command)
        self.calls.append(key)
        self.seen_envs.append(dict(env))
        return self.results.get(key, CheckExecution(exit_code=0, stdout="", stderr=""))


def _passing_runner() -> FakeRunner:
    """四项检查全过、扫描干净、覆盖率 0.95。"""
    return FakeRunner(
        results={
            _UNIT: CheckExecution(exit_code=0, stdout="TOTAL   95.00% covered", stderr=""),
            _INTEGRATION: CheckExecution(exit_code=0, stdout="", stderr=""),
            _LINT: CheckExecution(exit_code=0, stdout="", stderr=""),
            _ARCHITECTURE: CheckExecution(exit_code=0, stdout="", stderr=""),
            _SECRET_SCAN: CheckExecution(exit_code=0, stdout="", stderr=""),
        }
    )


def _request(tmp_path: Path, **overrides: object) -> VerificationRequest:
    payload: dict[str, object] = {
        "task_id": TASK_ID,
        "workspace": tmp_path,
        "contract": _contract(),
        "plan": _plan(),
    }
    payload.update(overrides)
    return VerificationRequest(**payload)  # type: ignore[arg-type]


def _agent_report(*, completed: bool = True) -> ExecutionResult:
    """Agent 的自我报告 —— ``completed=True`` 是这里最该被无视的东西。"""
    return ExecutionResult(
        task_id=TASK_ID,
        executor="claude-code",
        status=ExecutionStatus.COMPLETED,
        artifacts=ExecutionArtifacts(branch="task/x", commit="abc123", changed_files=3),
        execution=ExecutionMetrics(
            duration=Decimal("12.5"), tool_calls=7, cost=Decimal("0.42"), currency="USD"
        ),
        self_assessment=SelfAssessment(completed=completed, limitations=("我觉得没问题",)),
        idempotency_key=f"{TASK_ID}:1",
    )


class TestTheEngineCannotSeeTheAgent:
    """R-049：验证引擎独立于执行器。这条要守成**结构**，不是约定。"""

    def test_the_request_has_nowhere_to_put_an_execution_result(self) -> None:
        """引擎的输入里没有能装下执行结果的地方。

        不是"引擎不读 self_assessment" —— 是**没地方传进来**。想"参考一下
        Agent 怎么说"的调用方会发现这个参数不存在。
        """
        fields = set(VerificationRequest.__dataclass_fields__)

        assert fields == {"task_id", "workspace", "contract", "plan"}

    def test_the_engine_does_not_import_the_executors(self) -> None:
        """引擎不 import 执行器层 —— 依赖方向只能是"引擎 → 领域"。

        （整条规则由 ``tests/unit/harness/test_layering.py`` 对整个
        ``harness/`` 守着；这里再钉一遍，是为了让"为什么"留在引擎旁边。）
        """
        tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"), filename=str(ENGINE_PATH))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)

        assert not any(name == "executors" or name.startswith("executors.") for name in imported), (
            f"验证引擎不该依赖执行器层：{sorted(imported)}"
        )

    async def test_the_verification_process_sees_no_agent_session_handle(
        self, tmp_path: Path
    ) -> None:
        """真的起一个进程，把它的环境变量打印出来看。

        **这是"环境中不存在 Agent 会话句柄"这条断言唯一有意义的写法。**
        断言"某个函数没传某个参数"只是在断言函数的实现，而这里断言的是
        子进程**实际看到的东西**。
        """
        env = build_verification_env(
            {
                "PATH": os.environ.get("PATH", ""),
                "ANTHROPIC_API_KEY": "EXAMPLE-KEY-VALUE-42",
                "ASES_SESSION_ID": "session-abcdef",
                "CLAUDE_SESSION_ID": "session-abcdef",
            },
            model="verifier-model",
            home=tmp_path,
        )
        runner = SubprocessCheckRunner()

        execution = await runner.run(
            (sys.executable, "-c", "import json,os;print(json.dumps(dict(os.environ)))"),
            workspace=tmp_path,
            env=env,
            timeout_seconds=60,
        )

        assert execution.exit_code == 0, execution.stderr
        seen = json.loads(execution.stdout)

        assert "session-abcdef" not in json.dumps(seen)
        assert "EXAMPLE-KEY-VALUE-42" not in json.dumps(seen)
        assert "ASES_SESSION_ID" not in seen
        assert "ANTHROPIC_API_KEY" not in seen

    async def test_the_verification_process_runs_in_its_own_process(self, tmp_path: Path) -> None:
        """验证跑在**独立进程**里 —— 不是同进程里调个函数。"""
        runner = SubprocessCheckRunner()

        execution = await runner.run(
            (sys.executable, "-c", "import os;print(os.getpid())"),
            workspace=tmp_path,
            env=build_verification_env({}, home=tmp_path),
            timeout_seconds=60,
        )

        assert execution.exit_code == 0, execution.stderr
        assert int(execution.stdout.strip()) != os.getpid()

    def test_the_home_of_the_verification_process_is_not_the_host_home(
        self, tmp_path: Path
    ) -> None:
        """``HOME`` 指向一次性目录，不是宿主机的家目录。

        宿主机家目录里有 ``~/.claude/projects/<会话>.jsonl``（Agent 的完整
        会话记录）、``~/.ssh``、``~/.aws``。验证进程跑的是**被验收项目自己
        的代码** —— 那份代码是 Agent 写的，所以它可能顺手就读了。
        """
        env = build_verification_env({"HOME": str(Path.home())}, home=tmp_path)

        assert env["HOME"] == str(tmp_path)
        assert env["HOME"] != str(Path.home())

    def test_the_verifier_model_is_separate_from_the_executor_model(self, tmp_path: Path) -> None:
        """I-22：验证端模型与生成端模型是两个旋钮。

        引擎压根不知道"生成端的模型"是什么 —— 它没地方知道。所以解耦不靠
        约定，靠**输入里没有那个字段**。
        """
        env = build_verification_env({}, model="verifier-model", home=tmp_path)

        assert env["ASES_VERIFIER_MODEL"] == "verifier-model"
        assert "executor-model" not in json.dumps(env)

    def test_a_credential_shaped_variable_is_refused(self) -> None:
        """第二道防线：名字像凭据的一律拒绝。

        第一道是白名单（不在名单里的根本不进来）。第二道防的是"有人往白名单
        里加了一个名字"——比如有人为了方便把 ``ASES_SESSION_ID`` 加进去。
        """
        with pytest.raises(VerificationEnvError, match="凭据"):
            assert_verification_env_is_safe({"ASES_SESSION_TOKEN": "abc"})

    def test_a_variable_outside_the_whitelist_never_reaches_the_process(self) -> None:
        env = build_verification_env(
            {"ASES_SESSION_TOKEN": "abc", "PATH": "/usr/bin"}, home=Path("/tmp")
        )

        assert "ASES_SESSION_TOKEN" not in env
        assert "abc" not in json.dumps(dict(env))


class TestTheVerdictDoesNotFollowTheAgent:
    """T1 ② 与 T2：验证结论不受 self_assessment 影响。"""

    async def test_the_agent_claiming_success_does_not_rescue_a_failing_test(
        self, tmp_path: Path
    ) -> None:
        """Agent 说"我做完了"，测试其实没过 → 验证判 **failed**。"""
        runner = FakeRunner(
            results={
                _UNIT: CheckExecution(exit_code=1, stdout="1 failed, 9 passed", stderr=""),
            }
        )
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert run.evaluation.passed is False
        assert run.to_report().verdict is VerificationVerdict.FAILED

    async def test_the_task_fails_even_though_the_agent_claims_success(
        self, tmp_path: Path
    ) -> None:
        """任务状态为 failed —— 领域层拿的是验证结论，不是 Agent 的话。"""
        runner = FakeRunner(
            results={
                _UNIT: CheckExecution(exit_code=1, stdout="1 failed", stderr=""),
            }
        )
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))
        resolution = resolve_outcome(_agent_report(completed=True), run.to_report())

        assert resolution.outcome is TaskOutcome.FAILED

    async def test_the_verdict_never_agrees_with_the_self_assessment(self, tmp_path: Path) -> None:
        """T2：验证结论与 ``self_assessment`` 的一致率为 0。

        用一组**构造样本**验证二者完全解耦：每个样本里 Agent 都说"做完了"，
        而验证给出的结论都不是"通过"。一致率必须是 0。

        一致率不是 0 意味着什么？意味着"Agent 说自己做完了"这件事在某个
        样本里**影响了结论** —— 那就是被测者给自己判卷。
        """
        samples = {
            "单元测试失败": {_UNIT: CheckExecution(exit_code=1, stdout="1 failed", stderr="")},
            "lint 失败": {_LINT: CheckExecution(exit_code=1, stdout="E501 too long", stderr="")},
            "进程起不来": {_UNIT: CheckExecution(exit_code=-1, stdout="", stderr="no such file")},
            "密钥命中": {_SECRET_SCAN: CheckExecution(exit_code=1, stdout="found", stderr="")},
        }

        agreements = 0
        for name, results in samples.items():
            runner = FakeRunner(results=results)
            engine = VerificationEngine(runner=runner)

            run = await engine.verify(_request(tmp_path))
            resolution = resolve_outcome(_agent_report(completed=True), run.to_report())

            agrees = resolution.verdict is VerificationVerdict.PASSED
            agreements += int(agrees)
            assert resolution.outcome is not TaskOutcome.COMPLETED, f"样本 {name} 不该被判为完成"

        assert agreements == 0, "验证结论与 self_assessment 的一致率必须为 0"

    async def test_the_environment_handed_to_the_check_has_no_session_handle(
        self, tmp_path: Path
    ) -> None:
        """检查进程拿到的环境里没有会话句柄。

        断言的是**运行器实际收到的环境** —— 不是"构造函数的某个参数没传"。
        """
        runner = _passing_runner()
        engine = VerificationEngine(
            runner=runner,
            host_env={
                "PATH": os.environ.get("PATH", ""),
                "ASES_SESSION_ID": "session-abcdef",
                "ANTHROPIC_API_KEY": "EXAMPLE-KEY-VALUE-42",
            },
        )

        await engine.verify(_request(tmp_path))

        assert runner.seen_envs
        for env in runner.seen_envs:
            assert "session-abcdef" not in json.dumps(dict(env))
            assert "EXAMPLE-KEY-VALUE-42" not in json.dumps(dict(env))


class TestTheFailClosedDirections:
    """T1 ③：验证进程挂掉时结果是 error，而不是 passed。"""

    async def test_a_check_that_cannot_start_is_an_error(self, tmp_path: Path) -> None:
        runner = FakeRunner(
            results={_UNIT: CheckExecution(exit_code=-1, stdout="", stderr="", started=False)}
        )
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert run.checks[RequiredCheck.UNIT_TESTS_PASS].outcome is CheckOutcome.ERROR
        assert run.to_report().verdict is VerificationVerdict.ERROR
        assert run.evaluation.passed is False

    async def test_a_check_that_was_killed_is_an_error(self, tmp_path: Path) -> None:
        """被信号打死 = 没跑完 —— 不是"没通过"。"""
        runner = FakeRunner(
            results={_UNIT: CheckExecution(exit_code=-9, stdout="", stderr="", signalled=True)}
        )
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert run.checks[RequiredCheck.UNIT_TESTS_PASS].outcome is CheckOutcome.ERROR
        assert run.to_report().verdict is VerificationVerdict.ERROR

    async def test_a_check_that_times_out_is_an_error(self, tmp_path: Path) -> None:
        runner = FakeRunner(
            results={_UNIT: CheckExecution(exit_code=-1, stdout="", stderr="超时", timed_out=True)}
        )
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert run.checks[RequiredCheck.UNIT_TESTS_PASS].outcome is CheckOutcome.ERROR
        assert run.to_report().verdict is VerificationVerdict.ERROR

    async def test_a_missing_check_is_not_a_pass(self, tmp_path: Path) -> None:
        """计划里缺一项必检 → 那一项没有结果 → 整体不通过。"""
        plan = VerificationPlan(
            checks=(CheckSpec(check=RequiredCheck.UNIT_TESTS_PASS, command=_UNIT),)
        )
        runner = _passing_runner()
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path, plan=plan))

        assert run.evaluation.passed is False
        assert RequiredCheck.LINT_PASS.value in run.evaluation.missing
        assert run.to_report().verdict is VerificationVerdict.ERROR

    async def test_a_missing_metric_is_not_a_pass(self, tmp_path: Path) -> None:
        """检查过了，但没输出覆盖率 → "没测到"不能被当成"达标"。"""
        runner = FakeRunner(
            results={_UNIT: CheckExecution(exit_code=0, stdout="no coverage here", stderr="")}
        )
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert "test_coverage" not in run.metrics
        assert run.evaluation.passed is False
        assert "test_coverage" in run.evaluation.missing

    async def test_a_failing_check_never_yields_a_passed_verdict(self, tmp_path: Path) -> None:
        """把所有检查都搞挂，结论也得是 failed/error，不是 passed。"""
        runner = FakeRunner(
            results={
                _UNIT: CheckExecution(exit_code=1, stdout="boom", stderr=""),
                _INTEGRATION: CheckExecution(exit_code=1, stdout="boom", stderr=""),
                _LINT: CheckExecution(exit_code=1, stdout="boom", stderr=""),
                _ARCHITECTURE: CheckExecution(exit_code=1, stdout="boom", stderr=""),
            }
        )
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert run.to_report().verdict is not VerificationVerdict.PASSED


class TestTheHappyPath:
    async def test_everything_passing_passes(self, tmp_path: Path) -> None:
        runner = _passing_runner()
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert run.evaluation.passed is True
        assert run.to_report().verdict is VerificationVerdict.PASSED

    async def test_the_coverage_threshold_is_read_from_the_output(self, tmp_path: Path) -> None:
        runner = _passing_runner()
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        coverage = run.metrics["test_coverage"]
        assert coverage.as_number() == Decimal("0.95")

    async def test_a_coverage_below_the_threshold_fails(self, tmp_path: Path) -> None:
        runner = FakeRunner(
            results={
                _UNIT: CheckExecution(exit_code=0, stdout="TOTAL   42.00% covered", stderr=""),
            }
        )
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert run.evaluation.passed is False
        assert any("未达标" in reason for reason in run.evaluation.failures)

    async def test_only_the_checks_the_contract_requires_are_run(self, tmp_path: Path) -> None:
        """契约没要求的检查不该跑 —— 不该产生副作用，也不该花时间。"""
        plan = VerificationPlan(
            checks=(
                CheckSpec(check=RequiredCheck.UNIT_TESTS_PASS, command=_UNIT),
                CheckSpec(check=RequiredCheck.LINT_PASS, command=_LINT),
            )
        )
        runner = _passing_runner()
        engine = VerificationEngine(runner=runner)
        contract = _contract(required=(RequiredCheck.UNIT_TESTS_PASS,))

        await engine.verify(_request(tmp_path, contract=contract, plan=plan))

        assert runner.calls == [_UNIT]

    async def test_only_the_scanners_the_contract_requires_are_run(self, tmp_path: Path) -> None:
        plan = VerificationPlan(
            scans=(
                ScanSpec(rule=ForbiddenRule.SECRETS_IN_DIFF, command=_SECRET_SCAN),
                ScanSpec(rule=ForbiddenRule.PUBLIC_API_BREAKING_CHANGE, command=("fake", "api")),
            )
        )
        runner = _passing_runner()
        engine = VerificationEngine(runner=runner)
        contract = _contract(forbidden=(ForbiddenRule.SECRETS_IN_DIFF,))

        await engine.verify(_request(tmp_path, contract=contract, plan=plan))

        assert runner.calls == [_SECRET_SCAN]

    async def test_a_scanner_that_hits_vetoes_the_contract(self, tmp_path: Path) -> None:
        runner = FakeRunner(
            results={_SECRET_SCAN: CheckExecution(exit_code=1, stdout="found", stderr="")}
        )
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert ForbiddenRule.SECRETS_IN_DIFF in run.violations
        assert run.evaluation.passed is False
        assert run.to_report().verdict is VerificationVerdict.FAILED

    async def test_a_scanner_that_could_not_run_is_not_treated_as_clean(
        self, tmp_path: Path
    ) -> None:
        """扫描器起不来 → **不是**"扫干净了"。"""
        runner = FakeRunner(
            results={
                _SECRET_SCAN: CheckExecution(exit_code=-1, stdout="", stderr="", started=False)
            }
        )
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert ForbiddenRule.SECRETS_IN_DIFF not in run.violations
        assert run.evaluation.passed is False


class TestTheArtifacts:
    async def test_the_execution_trace_is_always_produced(self, tmp_path: Path) -> None:
        runner = _passing_runner()
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert ArtifactKind.EXECUTION_TRACE in run.artifacts

    async def test_a_failed_check_still_produced_its_report(self, tmp_path: Path) -> None:
        """测试没过也会产出测试报告 —— "没产出"只对应"压根没跑成"。"""
        runner = FakeRunner(
            results={_UNIT: CheckExecution(exit_code=1, stdout="1 failed", stderr="")}
        )
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert ArtifactKind.TEST_REPORT in run.artifacts

    async def test_a_check_that_never_ran_produced_nothing(self, tmp_path: Path) -> None:
        runner = FakeRunner(
            results={
                _UNIT: CheckExecution(exit_code=-1, stdout="", stderr="", started=False),
            }
        )
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        assert ArtifactKind.TEST_REPORT not in run.artifacts


class TestTheRunIsReadOnly:
    async def test_the_run_cannot_be_edited_after_the_fact(self, tmp_path: Path) -> None:
        """结论一旦产出就不能就地改 —— 改结论等于改验收。"""
        runner = _passing_runner()
        engine = VerificationEngine(runner=runner)

        run = await engine.verify(_request(tmp_path))

        with pytest.raises(TypeError):
            run.checks[RequiredCheck.UNIT_TESTS_PASS] = run.checks[RequiredCheck.UNIT_TESTS_PASS]
        with pytest.raises(TypeError):
            run.metrics["test_coverage"] = run.metrics["test_coverage"]


class TestTheMetricsAreReadHonestly:
    def test_an_unreadable_metric_is_none_not_zero(self) -> None:
        """读不到就返回 ``None`` —— 返回 0 会把"没测到"说成"测到个位数"。"""
        spec = MetricSpec(name="test_coverage", pattern=r"TOTAL\s+(\d+\.\d+)%")

        assert spec.read("nothing here") is None

    def test_a_malformed_number_is_none(self) -> None:
        spec = MetricSpec(name="test_coverage", pattern=r"TOTAL\s+(\S+)%")

        assert spec.read("TOTAL   abc%") is None

    def test_the_scale_turns_a_percentage_into_a_ratio(self) -> None:
        spec = MetricSpec(
            name="test_coverage", pattern=r"TOTAL\s+(\d+\.\d+)%", scale=Decimal("0.01")
        )

        value = spec.read("TOTAL   85.00%")

        assert value is not None
        assert value.as_number() == Decimal("0.85")
