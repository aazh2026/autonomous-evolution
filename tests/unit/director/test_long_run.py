"""TASK-P3-018 —— 长跑驱动器的契约。

设计依据：docs/tasks.md TASK-P3-018、R-130（首个里程碑：连续 10 个有效演进
周期）。

**这一层不跑长跑。** 长跑要连着跑十轮、每轮都实测一遍 —— 那是
``scripts/run_long_run.py`` 的事，跑一次要几分钟。这里的用例只钉住**长跑
的选路与执行**这两件决定性的事：

- **选路**：每一轮挑的是"台账里第一条还开着的有效发现" —— 顺序、跳过无效
  发现、复核型发现的判据，都得对。
- **执行**：补丁按**候选标题**分发；标题对不上就报错，而不是"改点什么算
  什么"。

**达成度是实测的，不是写死的** —— 这件事由
:mod:`apps.director.pilot_measure` 与 ``scripts/run_long_run.py`` 保证；这里
不重复断言。
"""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from apps.director.long_run import (
    EXECUTOR_NAME,
    LongRunPatchExecutor,
    UnknownFixError,
    build_candidate,
    next_finding,
)
from apps.director.pilot_fixes import FIX_BY_ISSUE, FIX_BY_TITLE
from apps.director.pilot_measure import PilotMeasurement, parse_ledger
from executors.base import ExecutionTask, SessionConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
PILOT_DIR = REPO_ROOT / "samples" / "pilot_repo"

#: 台账里九条复现型发现的用例名 —— 顺序就是推进顺序。
REPRO_TESTS: tuple[str, ...] = (
    "test_the_cli_reports_an_empty_stats_input_instead_of_crashing",
    "test_the_cli_reports_an_out_of_range_percentile_instead_of_crashing",
    "test_the_cli_does_not_silently_drop_the_limit_option",
    "test_main_returns_an_exit_code_instead_of_raising",
    "test_money_amount_cannot_be_reassigned",
    "test_load_config_rejects_an_empty_key",
    "test_load_config_reports_a_duplicated_key",
    "test_word_wrap_breaks_a_word_longer_than_the_width",
    "test_is_due_rejects_a_non_positive_interval_the_same_way_next_run_does",
)


def _measurement(
    *,
    failing: tuple[str, ...] = REPRO_TESTS,
    cli_coverage: Decimal = Decimal("0"),
    money_covered: bool = False,
    boundary_covered: bool = False,
) -> PilotMeasurement:
    """造一次实测结果 —— 默认是"什么都没修"的样子。"""
    return PilotMeasurement(
        line_coverage=Decimal("0.740385"),
        cli_coverage=cli_coverage,
        covered_lines=154,
        total_lines=208,
        test_count=43,
        defects_fixed=0,
        failing_repro=failing,
        money_paths_covered=money_covered,
        boundary_covered=boundary_covered,
    )


class TestLedger:
    """台账的解析。"""

    def test_the_ledger_has_twelve_valid_findings(self) -> None:
        entries = parse_ledger(PILOT_DIR)
        valid = [item for item in entries if item.valid]
        assert len(valid) == 12

    def test_the_ledger_has_eight_invalid_findings(self) -> None:
        """那八条是**误报率的分母**，不是待办事项。"""
        entries = parse_ledger(PILOT_DIR)
        invalid = [item for item in entries if not item.valid]
        assert len(invalid) == 8

    def test_nine_valid_findings_are_repro_type(self) -> None:
        entries = parse_ledger(PILOT_DIR)
        repro = [item for item in entries if item.valid and item.evidence_type == "repro"]
        assert len(repro) == 9

    def test_repro_entries_carry_their_test_name(self) -> None:
        entries = parse_ledger(PILOT_DIR)
        names = tuple(
            item.repro_test for item in entries if item.valid and item.evidence_type == "repro"
        )
        assert names == REPRO_TESTS


class TestDiscovery:
    """选路 —— 每一轮推进哪一条。"""

    def test_the_first_round_picks_the_first_finding(self) -> None:
        entry = next_finding(PILOT_DIR, _measurement())
        assert entry is not None
        assert entry.issue == "PILOT-001"

    def test_the_second_round_picks_the_second_finding(self) -> None:
        """第一条修好了，就该轮到第二条 —— 顺序由台账定，不由实现定。"""
        entry = next_finding(PILOT_DIR, _measurement(failing=REPRO_TESTS[1:]))
        assert entry is not None
        assert entry.issue == "PILOT-002"

    def test_the_ninth_round_picks_the_ninth_finding(self) -> None:
        entry = next_finding(PILOT_DIR, _measurement(failing=REPRO_TESTS[8:]))
        assert entry is not None
        assert entry.issue == "PILOT-009"

    def test_the_tenth_round_picks_the_review_finding(self) -> None:
        """九条复现型都修完了 → 轮到复核型。"""
        entry = next_finding(PILOT_DIR, _measurement(failing=()))
        assert entry is not None
        assert entry.issue == "PILOT-010"

    def test_the_cli_finding_closes_once_cli_is_covered(self) -> None:
        """``cli.py`` 有覆盖了 → PILOT-010 不再是"还开着"。"""
        entry = next_finding(
            PILOT_DIR,
            _measurement(failing=(), cli_coverage=Decimal("0.5")),
        )
        assert entry is not None
        assert entry.issue == "PILOT-011"

    def test_the_money_finding_closes_once_the_three_paths_are_covered(self) -> None:
        entry = next_finding(
            PILOT_DIR,
            _measurement(failing=(), cli_coverage=Decimal("0.5"), money_covered=True),
        )
        assert entry is not None
        assert entry.issue == "PILOT-012"

    def test_nothing_is_left_when_everything_is_closed(self) -> None:
        entry = next_finding(
            PILOT_DIR,
            _measurement(
                failing=(),
                cli_coverage=Decimal("0.5"),
                money_covered=True,
                boundary_covered=True,
            ),
        )
        assert entry is None


class TestCandidate:
    """候选 —— 标题、证据、假设。"""

    def test_the_candidate_title_is_the_fix_title(self) -> None:
        """**标题就是复现用例的函数名** —— Critic 靠它去测试报告里找失败用例。"""
        fix = FIX_BY_ISSUE["PILOT-001"]
        candidate = build_candidate("PILOT-001", fix, "cycle-1")
        assert candidate.title == fix.title

    def test_the_candidate_carries_evidence(self) -> None:
        fix = FIX_BY_ISSUE["PILOT-001"]
        candidate = build_candidate("PILOT-001", fix, "cycle-1")
        assert candidate.evidence
        assert all(item.ref for item in candidate.evidence)

    def test_every_fix_has_a_fix_for_its_title(self) -> None:
        assert len(FIX_BY_TITLE) == len(FIX_BY_ISSUE)


class TestExecutor:
    """执行器 —— 按候选标题分发补丁。"""

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        """一份 git 仓库 —— 执行器要往里提交。"""
        root = tmp_path / "workspace"
        root.mkdir()
        subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "lr@ases.local"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "长跑"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        return root

    async def test_an_unknown_title_is_rejected(self, workspace: Path) -> None:
        """标题对不上就报错 —— **不"改点什么算什么都行"**。"""
        executor = LongRunPatchExecutor()
        session = await executor.create_session(
            SessionConfig(workspace=str(workspace), branch="main")
        )
        task = ExecutionTask(
            session_id=session.session_id,
            task_id="task-1",
            title="某个不存在的候选标题",
            instruction="改点什么",
            timeout_seconds=60,
        )
        with pytest.raises(UnknownFixError):
            await executor.execute(task)

    async def test_the_executor_name_is_stable(self) -> None:
        """执行器名字落进会话行与事件载荷 —— 事后查得出"是谁干的"。"""
        executor = LongRunPatchExecutor()
        assert executor.name == EXECUTOR_NAME
