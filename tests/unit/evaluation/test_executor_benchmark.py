"""TASK-P2-018 —— 执行器横向评测的统一口径。

验证（T1）：口径本身是对的 —— 同一批任务、同一段指令、同一套验收；三态的边
界站得住；看板上的数与检验的结论都对得上。

**这一组测的是口径，不是执行器。** 真执行器要花钱、要联网、结果还会飘 —— 那
是 ``scripts/run_executor_benchmark.py`` 的事。这里用一个假执行器把口径钉死：

- 假执行器**改什么**由测试决定，所以"修好了""没修好""改坏了""作弊"四种结局
  都能复现；
- 验收跑的是**真的 pytest**（在一个小仓库上），所以"复现用例过没过""回归套件
  挂了几个"是真的跑出来的，不是编的。

**三态是这一组的重点。** "没跑成"与"跑过但没过"必须分开 —— 否则一个没装的
执行器会在看板上显示成"成功率 0"，那是在说它能力差。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from evaluation.common import MetricState
from evaluation.executors.benchmark import (
    METRIC_SUCCESS_RATE,
    METRIC_TOKENS,
    BenchmarkHarness,
    BenchmarkTask,
    CliRunner,
    CliSpec,
    ExecutionObservation,
    ExecutorReport,
    PriceTable,
    RunOutcome,
    RunRecord,
    TokenUsage,
    build_board,
    evaluate_executor,
    pilot_benchmark_tasks,
)
from evaluation.executors.statistics import Alternative, StatisticsOptions

#: 仓库根 —— 试点仓库在它下面。
REPO_ROOT = Path(__file__).resolve().parents[3]
#: 真的试点仓库。
PILOT_REPO = REPO_ROOT / "samples" / "pilot_repo"

#: 修好的源文件。
FIXED_SOURCE = """\
def divide(a, b):
    if b == 0:
        raise ValueError("除数不能为 0")
    return a / b
"""

#: 没修的源文件 —— 除零直接抛 ZeroDivisionError。
BUGGY_SOURCE = """\
def divide(a, b):
    return a / b
"""

#: 修了目标用例，却把别的用例弄挂了 —— 恒返回 0。
REGRESSING_SOURCE = """\
def divide(a, b):
    if b == 0:
        raise ValueError("除数不能为 0")
    return 0
"""

#: 小仓库里的复现用例文件。
KNOWN_ISSUES_SOURCE = '''\
"""小仓库的已知缺陷复现用例。"""

import pytest

from mylib.core import divide


@pytest.mark.xfail(strict=True, reason="PILOT-901：除零没有转成用法错误")
def test_divide_reports_a_division_by_zero():
    """除零应当是一句用法错误，不是 traceback。"""
    with pytest.raises(ValueError):
        divide(1, 0)
'''

#: **作弊版的复现用例** —— 把用例改成永远通过，源码一个字没动。
CHEATED_KNOWN_ISSUES = '''\
"""小仓库的已知缺陷复现用例。"""

import pytest


def test_divide_reports_a_division_by_zero():
    """被改成永远通过了 —— 源码还是没修。"""
    assert True
'''


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    """一个小而真的仓库 —— 跟试点仓库一个结构。

    **有 ``known_issues/``、有 ``tests/``、有 ``src/``** —— 缺一个，验收口径就
    验不到那一半。
    """
    repo = tmp_path / "repo"
    (repo / "src" / "mylib").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "known_issues").mkdir()
    (repo / "src" / "mylib" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "mylib" / "core.py").write_text(BUGGY_SOURCE, encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(
        "from mylib.core import divide\n\n\ndef test_divide():\n    assert divide(6, 2) == 3\n",
        encoding="utf-8",
    )
    (repo / "known_issues" / "test_known_issues.py").write_text(
        KNOWN_ISSUES_SOURCE, encoding="utf-8"
    )
    (repo / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\n\n"
        'SRC = Path(__file__).resolve().parent / "src"\n'
        "if str(SRC) not in sys.path:\n    sys.path.insert(0, str(SRC))\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    return repo


class FakeRunner:
    """假执行器 —— 改什么由测试决定。

    **它只做一件事：按剧本改工作区。** 验收不归它管 —— 那是评测装置的事。
    """

    def __init__(self, action: str = "fix") -> None:
        self._action = action
        self.workspaces: list[Path] = []

    @property
    def executor(self) -> str:
        return "fake-executor"

    @property
    def model(self) -> str:
        """这次跑的是哪个模型。

        **空串表示"没记"** —— 二维能力矩阵（TASK-P4-030）按模型切片，没记模型
        的运行切不进任何格子。
        """
        return ""

    async def run(self, _task: BenchmarkTask, workspace: Path) -> ExecutionObservation:
        self.workspaces.append(workspace)
        if self._action == "unavailable":
            return ExecutionObservation(executed=False, detail="执行器没装")
        if self._action == "raise":
            raise RuntimeError("进程起不来")
        source = workspace / "src" / "mylib" / "core.py"
        if self._action == "fix":
            source.write_text(FIXED_SOURCE, encoding="utf-8")
        elif self._action == "break_suite":
            source.write_text(REGRESSING_SOURCE, encoding="utf-8")
        elif self._action == "cheat":
            # 不改源码，改验收用例 —— 让它永远通过。
            path = workspace / "known_issues" / "test_known_issues.py"
            path.write_text(CHEATED_KNOWN_ISSUES, encoding="utf-8")
        return ExecutionObservation(
            executed=True,
            duration_seconds=Decimal("1.5"),
            cost=Decimal("0.25"),
            currency="USD",
            detail="干完了",
        )


def _cost_from_json(text: str) -> Decimal | None:
    """从 CLI 输出里读成本 —— **读不到就返回 ``None``**。

    四个 CLI 的输出格式不一样，但成本字段的叫法是同族的（``total_cost_usd``
    / ``cost_usd``）。这里只认一种，认不出就说认不出。
    """
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        value = payload.get("total_cost_usd")
        if value is not None:
            return Decimal(str(value))
    return None


def _has_error_event(text: str) -> bool:
    """输出里有没有"环境级失败"的事件。

    **只认环境级失败**（认证、连通性、配额）—— 模型没做对不算，那属于能力
    问题，该记 FAILED。
    """
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "error" or payload.get("is_error") is True:
            return True
    return False


def _write_provider_file(workspace: Path) -> None:
    """往工作区里放一份 provider 配置 —— 只放配置，不碰任务。"""
    (workspace / "provider.json").write_text('{"ok":true}', encoding="utf-8")


def _task(repo: Path) -> BenchmarkTask:
    """小仓库里的那个基准任务。"""
    tasks = pilot_benchmark_tasks(repo)
    assert len(tasks) == 1
    return tasks[0]


def _harness(runner: FakeRunner, repo: Path, tmp_path: Path) -> BenchmarkHarness:
    return BenchmarkHarness(runner, repo=repo, root=tmp_path / "workspaces")


class TestTheTaskSetIsReadFromTheRepository:
    """任务集从仓库里读出来 —— 不是写死一份清单。"""

    def test_the_pilot_repository_yields_nine_tasks(self) -> None:
        tasks = pilot_benchmark_tasks(PILOT_REPO)

        assert len(tasks) == 9
        assert [task.issue_id for task in tasks] == [f"PILOT-{index:03d}" for index in range(1, 10)]

    def test_every_task_points_at_a_real_test(self) -> None:
        tasks = pilot_benchmark_tasks(PILOT_REPO)

        for task in tasks:
            assert task.target_test.startswith("known_issues/test_known_issues.py::test_")

    def test_every_task_carries_the_same_shape_of_instruction(self) -> None:
        """**指令是自变量。** 每个任务都拿到同样结构的一段指令 —— 换了执行器
        才不会连指令一起换掉。
        """
        tasks = pilot_benchmark_tasks(PILOT_REPO)

        for task in tasks:
            assert task.issue_id in task.instruction
            assert task.target_test.rsplit("::", 1)[-1] in task.instruction
            assert "不要改 known_issues/" in task.instruction

    def test_an_empty_repository_is_refused(self, tmp_path: Path) -> None:
        """任务集空了要报错 —— 空任务集跑出来的看板是"零差异"，那是假的。"""
        empty = tmp_path / "empty"
        (empty / "known_issues").mkdir(parents=True)
        (empty / "known_issues" / "test_known_issues.py").write_text("", encoding="utf-8")

        with pytest.raises(ValueError, match="没找到任何已知缺陷用例"):
            pilot_benchmark_tasks(empty)


class TestTheAcceptanceIsOurs:
    """验收由我们跑 —— 而且跑在干净副本上。"""

    async def test_a_fixed_defect_is_a_success(self, mini_repo: Path, tmp_path: Path) -> None:
        harness = _harness(FakeRunner("fix"), mini_repo, tmp_path)

        record = await harness.run(_task(mini_repo))

        assert record.outcome is RunOutcome.SUCCEEDED
        assert record.target_passed is True
        assert record.regressions == 0

    async def test_an_unfixed_defect_is_a_failure(self, mini_repo: Path, tmp_path: Path) -> None:
        """执行器跑了，但没做到 —— 这是"失败"，不是"没跑成"。"""
        runner = FakeRunner("nothing")
        harness = _harness(runner, mini_repo, tmp_path)

        record = await harness.run(_task(mini_repo))

        assert record.outcome is RunOutcome.FAILED
        assert record.target_passed is False

    async def test_a_broken_suite_is_a_failure(self, mini_repo: Path, tmp_path: Path) -> None:
        """目标用例过了，但回归套件挂了 —— 同样是失败。"""
        harness = _harness(FakeRunner("break_suite"), mini_repo, tmp_path)

        record = await harness.run(_task(mini_repo))

        assert record.outcome is RunOutcome.FAILED
        assert record.target_passed is True
        assert record.regressions == 1

    async def test_cheating_on_the_test_file_does_not_help(
        self, mini_repo: Path, tmp_path: Path
    ) -> None:
        """**改验收文件改不动结论。**

        执行器把复现用例改成永远通过 —— 在它那份工作区里确实会过。但验收跑在
        从原仓库重新铺出来的副本上，那里的用例还是原来那条，源码还是没修 —— 所
        以结论是"没过"。
        """
        harness = _harness(FakeRunner("cheat"), mini_repo, tmp_path)

        record = await harness.run(_task(mini_repo))

        assert record.outcome is RunOutcome.FAILED
        assert record.target_passed is False
        assert record.touched_reserved is True

    async def test_the_workspace_has_the_marker_stripped(
        self, mini_repo: Path, tmp_path: Path
    ) -> None:
        """**执行器看到的是一条会失败的普通用例。** ``strict=True`` 的 xfail 用例
        在缺陷修好之后会变成 XPASS，而 strict 把 XPASS 算作失败 —— 那不是我们要
        的验收语义。
        """
        runner = FakeRunner("nothing")
        harness = _harness(runner, mini_repo, tmp_path)

        record = await harness.run(_task(mini_repo))

        source = (runner.workspaces[0] / "known_issues" / "test_known_issues.py").read_text(
            encoding="utf-8"
        )
        assert "xfail" not in source
        assert record.target_passed is False

    async def test_the_verification_copy_keeps_the_original_tests(
        self, mini_repo: Path, tmp_path: Path
    ) -> None:
        """验收副本里的测试文件取自原仓库 —— 执行器碰不到。"""
        runner = FakeRunner("cheat")
        harness = _harness(runner, mini_repo, tmp_path)

        await harness.run(_task(mini_repo))

        verify = tmp_path / "workspaces" / "pilot-901-r1-verify" / "known_issues"
        source = (verify / "test_known_issues.py").read_text(encoding="utf-8")
        assert "pytest.raises(ValueError)" in source


class TestTheThreeStates:
    """三态 —— "没跑成"与"跑过但没过"必须分开。"""

    async def test_an_unavailable_executor_is_not_run(
        self, mini_repo: Path, tmp_path: Path
    ) -> None:
        harness = _harness(FakeRunner("unavailable"), mini_repo, tmp_path)

        record = await harness.run(_task(mini_repo))

        assert record.outcome is RunOutcome.NOT_RUN
        assert record.executed is False

    async def test_a_crashing_executor_is_not_run(self, mini_repo: Path, tmp_path: Path) -> None:
        """执行器抛错 —— 那是"没跑成"，不是"它没做到"。"""
        harness = _harness(FakeRunner("raise"), mini_repo, tmp_path)

        record = await harness.run(_task(mini_repo))

        assert record.outcome is RunOutcome.NOT_RUN
        assert "执行器抛错" in record.detail


class TestTheReport:
    """成绩单 —— 指标怎么算出来的。"""

    def _runs(self, outcomes: list[RunOutcome]) -> list[RunRecord]:
        return [
            RunRecord(
                task_id=f"task-{index}",
                executor="fake",
                attempt=1,
                outcome=outcome,
                target_passed=outcome is RunOutcome.SUCCEEDED,
                regressions=0,
                suite_ran=True,
                duration_seconds=Decimal(index + 1),
                cost=Decimal("0.10") * (index + 1),
                currency="USD",
                tokens=TokenUsage(input=1000 * (index + 1), output=100 * (index + 1)),
            )
            for index, outcome in enumerate(outcomes)
        ]

    def test_the_not_run_ones_leave_the_success_denominator(self) -> None:
        """**"没跑成"不算失败。** 它不进成功率的分母，但进可用率的分母。"""
        report = evaluate_executor(
            self._runs([RunOutcome.SUCCEEDED, RunOutcome.FAILED, RunOutcome.NOT_RUN])
        )

        assert report.success_rate.numerator == 1
        assert report.success_rate.denominator == 2
        assert report.availability.numerator == 2
        assert report.availability.denominator == 3

    def test_the_availability_is_reported_on_its_own(self) -> None:
        """**没有可用率的成功率是会骗人的** —— 全都跑不起来时，成功率是"无样本"
        而不是 0。
        """
        report = evaluate_executor(self._runs([RunOutcome.NOT_RUN, RunOutcome.NOT_RUN]))

        assert report.availability.value == Decimal(0)
        assert report.success_rate.state is MetricState.NO_SAMPLES
        assert report.success_rate.value is None

    def test_the_regression_rate_is_over_the_measured_ones(self) -> None:
        """回归率的分母是"验过套件的那些运行"。"""
        runs = self._runs([RunOutcome.SUCCEEDED, RunOutcome.SUCCEEDED])
        regressed = RunRecord(
            task_id="task-2",
            executor="fake",
            attempt=1,
            outcome=RunOutcome.FAILED,
            target_passed=True,
            regressions=3,
            suite_ran=True,
        )
        report = evaluate_executor([*runs, regressed])

        assert report.regression_rate.numerator == 1
        assert report.regression_rate.denominator == 3

    def test_the_reported_cost_is_the_median(self) -> None:
        """**自报成本只记录。** 它是各家按自己的计价表算的数。"""
        report = evaluate_executor(self._runs([RunOutcome.SUCCEEDED] * 3))

        assert report.reported_cost.value == Decimal("0.20")
        assert report.reported_cost.detail["samples"] == 3

    def test_without_a_price_table_there_is_no_cost(self) -> None:
        """**没有价目表就没有成本这一列** —— 宁可空着，也不拿自报成本顶上。"""
        report = evaluate_executor(self._runs([RunOutcome.SUCCEEDED] * 3))

        assert report.cost.state is MetricState.NO_SAMPLES
        assert report.cost.value is None

    def test_the_cost_is_folded_from_the_tokens(self) -> None:
        """**成本 = 统一价目表 × token 用量。** 不是各家自报的那个数。"""
        prices = PriceTable(
            input=Decimal("0.30"),
            output=Decimal("1.20"),
            cache_read=Decimal("0.06"),
            cache_write=Decimal("0.30"),
            currency="USD",
        )
        report = evaluate_executor(self._runs([RunOutcome.SUCCEEDED] * 3), prices=prices)

        # 中间那次：输入 2000、输出 200 —— 2000×0.30/1M + 200×1.20/1M = 0.00084
        assert report.cost.value == Decimal("0.00084")
        assert report.cost.detail["currency"] == "USD"
        assert report.currency == "USD"

    def test_the_tokens_are_reported_on_their_own(self) -> None:
        """token 用量是独立的一列 —— 它比钱更耐久：价目表会变，用量不会。"""
        report = evaluate_executor(self._runs([RunOutcome.SUCCEEDED] * 3))

        assert report.tokens.value == Decimal(2200)

    def test_the_duration_is_the_median(self) -> None:
        report = evaluate_executor(self._runs([RunOutcome.SUCCEEDED] * 3))

        assert report.duration.value == Decimal(2)

    def test_mixed_currencies_make_the_reported_cost_incomparable(self) -> None:
        """**R-039：币种不一致就不换算。** 报"不可比"，不报一个折过的数。"""
        runs = self._runs([RunOutcome.SUCCEEDED, RunOutcome.SUCCEEDED])
        other = RunRecord(
            task_id="task-3",
            executor="fake",
            attempt=1,
            outcome=RunOutcome.SUCCEEDED,
            target_passed=True,
            regressions=0,
            suite_ran=True,
            duration_seconds=Decimal(1),
            cost=Decimal("0.2"),
            currency="EUR",
        )
        report = evaluate_executor([*runs, other])

        assert report.reported_cost.state is MetricState.BASELINE_MISSING
        assert report.reported_cost.value is None
        assert report.reported_currency is None


class TestTheBoard:
    """看板 —— 两两比较与多重比较校正。"""

    def _report(self, executor: str, successes: int, total: int) -> ExecutorReport:
        runs = [
            RunRecord(
                task_id=f"{executor}-{index}",
                executor=executor,
                attempt=1,
                outcome=(RunOutcome.SUCCEEDED if index < successes else RunOutcome.FAILED),
                target_passed=index < successes,
                regressions=0,
                suite_ran=True,
                duration_seconds=Decimal(index + 1),
                cost=Decimal("0.10") * (index + 1),
                currency="USD",
                tokens=TokenUsage(input=1000 * (index + 1), output=100),
            )
            for index in range(total)
        ]
        return evaluate_executor(runs)

    def test_a_big_gap_is_found(self) -> None:
        """一边全成，一边全败 —— 差异必须被找出来。"""
        board = build_board([self._report("a", 9, 9), self._report("b", 0, 9)])

        assert board.distinguishable is True
        assert METRIC_SUCCESS_RATE in board.significant_metrics

    def test_no_gap_means_no_difference(self) -> None:
        """两边一模一样 —— 不该报出"有差异"。"""
        board = build_board([self._report("a", 5, 9), self._report("b", 5, 9)])

        assert board.distinguishable is False

    def test_a_single_executor_has_nothing_to_compare(self) -> None:
        board = build_board([self._report("a", 5, 9)])

        assert board.comparisons == ()
        assert board.distinguishable is False

    def test_the_comparison_carries_the_correction(self) -> None:
        """**校正后的 p 值不能比原始的更小。**"""
        board = build_board([self._report("a", 9, 9), self._report("b", 0, 9)])

        for comparison in board.comparisons:
            assert comparison.adjusted_p_value >= comparison.test.p_value

    def test_the_board_renders_the_numbers(self) -> None:
        board = build_board([self._report("a", 9, 9), self._report("b", 3, 9)])

        rendered = board.render()

        assert "9/9 (100.0%)" in rendered
        assert "3/9 (33.3%)" in rendered
        assert METRIC_SUCCESS_RATE in rendered

    def test_the_board_says_when_there_is_no_sample(self) -> None:
        """样本不足时写"无样本" —— 不写 0。"""
        board = build_board([self._report("a", 0, 0), self._report("b", 0, 0)])

        assert "无样本" in board.render()

    def test_the_tokens_are_compared_too(self) -> None:
        """**token 用量也是一项指标** —— 它比钱更耐久，也更好核对。"""
        board = build_board([self._report("a", 9, 9), self._report("b", 0, 9)])

        metrics = {comparison.metric for comparison in board.comparisons}
        assert METRIC_TOKENS in metrics

    def test_the_reported_cost_is_not_compared(self) -> None:
        """**自报成本不参与比较** —— 各家计价表不一样，比的是记账口径。"""
        board = build_board([self._report("a", 9, 9), self._report("b", 0, 9)])

        metrics = {comparison.metric for comparison in board.comparisons}
        assert "executor_cost_reported_median" not in metrics


class TestTheCliRunner:
    """CLI 跑者 —— 直接起进程那条路。"""

    async def test_a_missing_binary_is_not_run(self, tmp_path: Path) -> None:
        """**没装就是没跑成** —— 不是"跑失败了"。"""
        spec = CliSpec(
            executor="missing",
            binary="definitely-not-installed-xyz",
            argv=lambda _workspace, _prompt: ("definitely-not-installed-xyz",),
            cost_of=lambda _text: None,
        )
        runner = CliRunner(spec)
        task = BenchmarkTask(
            task_id="t",
            issue_id="I-1",
            summary="缺个可执行文件",
            target_test="known_issues/test_known_issues.py::test_x",
            title="t",
            instruction="p",
            repo=tmp_path,
        )

        observation = await runner.run(task, tmp_path)

        assert observation.executed is False
        assert "起不来" in observation.detail

    async def test_the_cost_is_read_from_the_output(self, tmp_path: Path) -> None:
        """成本从输出里读 —— 读到就是读到。"""
        spec = CliSpec(
            executor="echo",
            binary="echo",
            argv=lambda _workspace, _prompt: ("echo", '{"total_cost_usd": 0.42}'),
            cost_of=_cost_from_json,
        )
        runner = CliRunner(spec)
        task = BenchmarkTask(
            task_id="t",
            issue_id="I-1",
            summary="读成本",
            target_test="known_issues/test_known_issues.py::test_x",
            title="t",
            instruction="p",
            repo=tmp_path,
        )

        observation = await runner.run(task, tmp_path)

        assert observation.executed is True
        assert observation.cost == Decimal("0.42")
        assert observation.currency == "USD"

    async def test_a_cost_that_cannot_be_read_is_not_zero(self, tmp_path: Path) -> None:
        """**读不到成本不等于成本是 0。** 报"没读到"，别报 0 —— 0 会被当成
        "这个执行器不要钱"，那是最贵的一种错。
        """
        spec = CliSpec(
            executor="echo",
            binary="echo",
            argv=lambda _workspace, _prompt: ("echo", "干完了，什么也没说"),
            cost_of=_cost_from_json,
        )
        runner = CliRunner(spec)
        task = BenchmarkTask(
            task_id="t",
            issue_id="I-1",
            summary="读不到成本",
            target_test="known_issues/test_known_issues.py::test_x",
            title="t",
            instruction="p",
            repo=tmp_path,
        )

        observation = await runner.run(task, tmp_path)

        assert observation.executed is True
        assert observation.cost is None
        assert observation.currency is None

    async def test_a_cli_that_never_spoke_and_hung_is_not_run(self, tmp_path: Path) -> None:
        """**一个字都没说就卡死 —— 它压根没起来。** 那不是能力差，是环境坏
        了；算成 FAILED 就是在给环境问题记账。
        """
        spec = CliSpec(
            executor="hang",
            binary="sleep",
            argv=lambda _workspace, _prompt: ("sleep", "30"),
            cost_of=lambda _text: None,
        )
        runner = CliRunner(spec)
        task = BenchmarkTask(
            task_id="t",
            issue_id="I-1",
            summary="卡死",
            target_test="known_issues/test_known_issues.py::test_x",
            title="t",
            instruction="p",
            repo=tmp_path,
            timeout_seconds=1,
        )

        observation = await runner.run(task, tmp_path)

        assert observation.executed is False
        assert "压根没起来" in observation.detail

    async def test_a_cli_that_was_talking_and_hung_ran(self, tmp_path: Path) -> None:
        """说了一堆才卡住 —— 它跑过，是它自己的问题。"""
        spec = CliSpec(
            executor="hang",
            binary="sh",
            argv=lambda _workspace, _prompt: ("sh", "-c", "echo '我开始干了'; sleep 30"),
            cost_of=lambda _text: None,
        )
        runner = CliRunner(spec)
        task = BenchmarkTask(
            task_id="t",
            issue_id="I-1",
            summary="卡死",
            target_test="known_issues/test_known_issues.py::test_x",
            title="t",
            instruction="p",
            repo=tmp_path,
            timeout_seconds=1,
        )

        observation = await runner.run(task, tmp_path)

        assert observation.executed is True
        assert "超过" in observation.detail

    async def test_an_environment_failure_is_not_run(self, tmp_path: Path) -> None:
        """**认证不通、后端连不上 —— 这是环境级失败。** 不该记成"它没做到"。"""
        spec = CliSpec(
            executor="broken",
            binary="echo",
            argv=lambda _workspace, _prompt: (
                "echo",
                '{"type":"error","error":{"name":"APIError","data":{"message":"Not Found"}}}',
            ),
            cost_of=lambda _text: None,
            is_error=_has_error_event,
        )
        runner = CliRunner(spec)
        task = BenchmarkTask(
            task_id="t",
            issue_id="I-1",
            summary="环境坏了",
            target_test="known_issues/test_known_issues.py::test_x",
            title="t",
            instruction="p",
            repo=tmp_path,
        )

        observation = await runner.run(task, tmp_path)

        assert observation.executed is False
        assert "执行器报错" in observation.detail
        assert "Not Found" in observation.detail

    async def test_the_duration_is_measured_by_us(self, tmp_path: Path) -> None:
        """**耗时是我们量的墙钟，不是工具自报的** —— 四个工具各报各的，自报
        的口径没法比。
        """
        spec = CliSpec(
            executor="slow",
            binary="sh",
            argv=lambda _workspace, _prompt: ("sh", "-c", "sleep 0.4"),
            cost_of=lambda _text: None,
        )
        runner = CliRunner(spec)
        task = BenchmarkTask(
            task_id="t",
            issue_id="I-1",
            summary="量耗时",
            target_test="known_issues/test_known_issues.py::test_x",
            title="t",
            instruction="p",
            repo=tmp_path,
        )

        observation = await runner.run(task, tmp_path)

        assert observation.duration_seconds is not None
        assert observation.duration_seconds >= Decimal("0.4")

    async def test_the_tokens_are_read_from_the_output(self, tmp_path: Path) -> None:
        """token 用量从输出里读 —— 成本折算的原料。"""
        spec = CliSpec(
            executor="echo",
            binary="echo",
            argv=lambda _workspace, _prompt: ("echo", '{"usage":{"input_tokens":2000}}'),
            cost_of=lambda _text: None,
            tokens_of=lambda text: TokenUsage(input=2000) if "2000" in text else None,
        )
        runner = CliRunner(spec)
        task = BenchmarkTask(
            task_id="t",
            issue_id="I-1",
            summary="读 token",
            target_test="known_issues/test_known_issues.py::test_x",
            title="t",
            instruction="p",
            repo=tmp_path,
        )

        observation = await runner.run(task, tmp_path)

        assert observation.tokens is not None
        assert observation.tokens.input == 2000

    async def test_the_extra_environment_reaches_the_child(self, tmp_path: Path) -> None:
        """**"这个执行器怎么找到那个模型"要能传下去。** Codex 与 OpenCode 各要
        一份自己的配置。
        """
        spec = CliSpec(
            executor="echo",
            binary="sh",
            argv=lambda _workspace, _prompt: ("sh", "-c", 'test "$BENCH_MARK" = "ok-mark"'),
            cost_of=lambda _text: None,
            env={"BENCH_MARK": "ok-mark"},
        )
        runner = CliRunner(spec)
        task = BenchmarkTask(
            task_id="t",
            issue_id="I-1",
            summary="传环境变量",
            target_test="known_issues/test_known_issues.py::test_x",
            title="t",
            instruction="p",
            repo=tmp_path,
        )

        observation = await runner.run(task, tmp_path)

        assert observation.executed is True
        assert "退出码" not in observation.detail

    async def test_the_workspace_is_prepared_before_the_run(self, tmp_path: Path) -> None:
        """**准备工作区这一步要有** —— provider 配置得在跑之前放进去。"""
        spec = CliSpec(
            executor="echo",
            binary="sh",
            argv=lambda _workspace, _prompt: ("sh", "-c", "cat provider.json"),
            cost_of=lambda _text: None,
            prepare=_write_provider_file,
        )
        runner = CliRunner(spec)
        task = BenchmarkTask(
            task_id="t",
            issue_id="I-1",
            summary="准备工作区",
            target_test="known_issues/test_known_issues.py::test_x",
            title="t",
            instruction="p",
            repo=tmp_path,
        )

        observation = await runner.run(task, tmp_path)

        assert observation.executed is True
        assert (tmp_path / "provider.json").exists()


class TestTheOptionsArePassedThrough:
    """设置要真的传到检验里去 —— 否则"单侧"只是嘴上说说。"""

    def test_the_alternative_reaches_the_test(self) -> None:
        """方向要能传下去 —— 看板上"谁更大"不能是猜的。"""
        runs = [
            RunRecord(
                task_id=f"t{index}",
                executor="fake",
                attempt=1,
                outcome=RunOutcome.SUCCEEDED if index < 3 else RunOutcome.FAILED,
                target_passed=index < 3,
                regressions=0,
                suite_ran=True,
                duration_seconds=Decimal(1),
                cost=Decimal("0.1"),
                currency="USD",
            )
            for index in range(4)
        ]
        other = [
            RunRecord(
                task_id=f"u{index}",
                executor="other",
                attempt=1,
                outcome=RunOutcome.SUCCEEDED,
                target_passed=True,
                regressions=0,
                suite_ran=True,
                duration_seconds=Decimal(1),
                cost=Decimal("0.1"),
                currency="USD",
            )
            for index in range(4)
        ]
        board = build_board(
            [evaluate_executor(runs), evaluate_executor(other)],
            options=StatisticsOptions(alternative=Alternative.GREATER),
        )

        success = [
            comparison
            for comparison in board.comparisons
            if comparison.metric == METRIC_SUCCESS_RATE
        ]
        assert success
        assert success[0].test.alternative is Alternative.GREATER
