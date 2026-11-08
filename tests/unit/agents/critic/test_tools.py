"""TASK-P1-055 —— 事实核验工具与调用账本。

这一组用例盯的是**工具本身**：每个工具真的去查它该查的地方，查到的东西
真的能被取回来；以及最要紧的一条 —— :class:`FactChecker` 的账本**只记真的
调过的工具**。

**账本为什么是这么重要的一件事。** 一份说"我查过了"的报告，如果拿不出查过
什么，就跟没查一样 —— 而且分不清"查过没找到"与"压根没查"。所以账本记的是
"真的调用过"，不是"打算调用"：没注册的工具不产生记录，否则报告里会出现
"查过仓库"而实际根本没查的行，那比不记还坏。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from agents.critic.sources import (
    ArtifactStoreTool,
    GitHistoryTool,
    ProjectStatusTool,
    RepositorySearchTool,
    TaskTableTool,
)
from agents.critic.sources import TestReportTool as ReportTool
from agents.critic.tools import (
    FactChecker,
    ProjectStatusFact,
    TaskLookup,
    TaskSummary,
    ToolKind,
    ToolResult,
    ToolUnavailableError,
)
from core.evidence.artifacts import ArtifactKind, ArtifactRecord, VerificationRunRecord
from core.project.models import ProjectGoal, TargetValue, ValueKind

_AUTONOMY: dict[str, object] = {
    "rules": {
        "analyze_project": {"level": "L4", "permission": "autonomous"},
        "plan_tasks": {"level": "L4", "permission": "autonomous"},
        "code_changes": {"level": "L4", "permission": "autonomous"},
        "test_execution": {"level": "L4", "permission": "autonomous"},
        "branch_creation": {"level": "L4", "permission": "autonomous"},
        "merge_to_main": {"level": "L2", "permission": "approval_required"},
        "production_deployment": {"level": "L1", "permission": "advisory_only"},
        "modify_governance": {"level": "L1", "permission": "advisory_only"},
    }
}

_BASE_GOAL: dict[str, object] = {
    "objective": [{"id": "OBJ-001", "description": "把系统跑通"}],
    "key_results": [{"id": "KR-001", "metric": "e2e_success_rate", "target": ">= 0.95"}],
    "constraints": [
        {
            "id": "C-001",
            "description": "变更必须通过测试",
            "kind": "all_changes_must_pass_tests",
            "machine_checkable": True,
            "expression": "tests_passed == true",
        }
    ],
    "non_goals": [{"id": "NG-001", "description": "不做移动端适配"}],
    "autonomy": _AUTONOMY,
    "stop_conditions": [
        {"id": "SC-BASE", "description": "人工请求停止", "kind": "human_stop_requested"}
    ],
}


def _goal(**overrides: object) -> ProjectGoal:
    return ProjectGoal.model_validate({**_BASE_GOAL, **overrides})


# ---------------------------------------------------------------------------
# 调用账本
# ---------------------------------------------------------------------------


class _EchoTool:
    """回声工具 —— 记下自己被问了什么。"""

    def __init__(self, kind: ToolKind) -> None:
        self.kind = kind
        self.asked: list[str] = []

    async def query(self, argument: str) -> ToolResult:
        self.asked.append(argument)
        return ToolResult(found=True, summary=f"问了 {argument}", references=(argument,))


async def test_the_ledger_records_only_tools_that_were_really_called() -> None:
    checker = FactChecker()
    tool = _EchoTool(ToolKind.REPOSITORY_SEARCH)
    checker.register(tool)

    await checker.try_call(ToolKind.REPOSITORY_SEARCH, "登录超时")

    assert tool.asked == ["登录超时"]
    assert len(checker.calls) == 1
    assert checker.calls[0].kind is ToolKind.REPOSITORY_SEARCH
    assert checker.calls[0].argument == "登录超时"
    assert checker.calls[0].found is True


async def test_an_unregistered_tool_leaves_no_record() -> None:
    """没注册的工具不产生调用记录 —— 否则报告里会有没查过的行。"""
    checker = FactChecker()

    result = await checker.try_call(ToolKind.GIT_HISTORY, "登录超时")

    assert result is None
    assert checker.calls == ()


async def test_call_raises_when_the_tool_is_missing() -> None:
    checker = FactChecker()

    with pytest.raises(ToolUnavailableError):
        await checker.call(ToolKind.GIT_HISTORY, "登录超时")


async def test_the_ledger_keeps_the_call_order() -> None:
    checker = FactChecker()
    checker.register(_EchoTool(ToolKind.REPOSITORY_SEARCH))
    checker.register(_EchoTool(ToolKind.GIT_HISTORY))

    await checker.try_call(ToolKind.REPOSITORY_SEARCH, "一")
    await checker.try_call(ToolKind.GIT_HISTORY, "二")
    await checker.try_call(ToolKind.REPOSITORY_SEARCH, "三")

    assert [call.argument for call in checker.calls] == ["一", "二", "三"]
    assert checker.called_kinds == {ToolKind.REPOSITORY_SEARCH, ToolKind.GIT_HISTORY}


async def test_a_later_tool_of_the_same_kind_replaces_the_earlier_one() -> None:
    checker = FactChecker()
    first = _EchoTool(ToolKind.REPOSITORY_SEARCH)
    second = _EchoTool(ToolKind.REPOSITORY_SEARCH)
    checker.register(first)
    checker.register(second)

    await checker.try_call(ToolKind.REPOSITORY_SEARCH, "x")

    assert first.asked == []
    assert second.asked == ["x"]


# ---------------------------------------------------------------------------
# 仓库搜索
# ---------------------------------------------------------------------------


async def test_the_repository_search_finds_files_by_name(tmp_path: Path) -> None:
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "session.py").write_text("x = 1\n", encoding="utf-8")

    result = await RepositorySearchTool(tmp_path).query("session")

    assert result.found is True
    assert "core/session.py" in result.references


async def test_the_repository_search_finds_files_by_content(tmp_path: Path) -> None:
    (tmp_path / "login.py").write_text("# 登录超时的处理\n", encoding="utf-8")

    result = await RepositorySearchTool(tmp_path).query("登录超时")

    assert result.found is True
    assert "login.py" in result.references


async def test_the_repository_search_skips_noise_directories(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "session.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "session.py").write_text("x = 1\n", encoding="utf-8")

    result = await RepositorySearchTool(tmp_path).query("session")

    assert result.references == ("session.py",)


async def test_the_repository_search_reports_an_empty_query(tmp_path: Path) -> None:
    result = await RepositorySearchTool(tmp_path).query("   ")

    assert result.found is False


async def test_the_repository_search_reports_a_miss(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    result = await RepositorySearchTool(tmp_path).query("根本不存在的词")

    assert result.found is False
    assert result.references == ()


# ---------------------------------------------------------------------------
# Git 历史
# ---------------------------------------------------------------------------


def _git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=critic@example.com",
            "-c",
            "user.name=Critic",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "修复登录超时",
        ],
        cwd=path,
        check=True,
    )
    return path


async def test_the_git_history_finds_a_matching_commit(tmp_path: Path) -> None:
    _git_repo(tmp_path)

    result = await GitHistoryTool(tmp_path).query("登录超时")

    assert result.found is True
    assert any("登录超时" in line for line in result.references)


async def test_the_git_history_reports_a_miss(tmp_path: Path) -> None:
    _git_repo(tmp_path)

    result = await GitHistoryTool(tmp_path).query("根本不存在的词")

    assert result.found is False


async def test_the_git_history_reports_an_empty_query(tmp_path: Path) -> None:
    _git_repo(tmp_path)

    result = await GitHistoryTool(tmp_path).query("  ")

    assert result.found is False


# ---------------------------------------------------------------------------
# 测试报告
# ---------------------------------------------------------------------------


def _report(path: Path, tests: list[dict[str, str]]) -> Path:
    path.write_text(json.dumps({"tests": tests}), encoding="utf-8")
    return path


async def test_the_test_report_only_reports_failures(tmp_path: Path) -> None:
    report = _report(
        tmp_path / "report.json",
        [
            {"nodeid": "tests/test_a.py::test_ok", "outcome": "passed"},
            {"nodeid": "tests/test_a.py::test_bad", "outcome": "failed"},
        ],
    )

    result = await ReportTool(report).query("test_a")

    assert result.found is True
    assert result.references == ("tests/test_a.py::test_bad",)


async def test_the_test_report_reports_a_missing_file(tmp_path: Path) -> None:
    result = await ReportTool(tmp_path / "nope.json").query("x")

    assert result.found is False


async def test_the_test_report_reports_broken_json(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text("{不是 JSON", encoding="utf-8")

    result = await ReportTool(report).query("x")

    assert result.found is False


async def test_the_test_report_reports_a_report_without_tests(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"other": []}), encoding="utf-8")

    result = await ReportTool(report).query("x")

    assert result.found is False


async def test_the_test_report_says_the_failures_are_unrelated(tmp_path: Path) -> None:
    """有失败用例，但都不是这一件事 —— 这不是证据。"""
    report = _report(
        tmp_path / "report.json",
        [{"nodeid": "tests/test_other.py::test_bad", "outcome": "failed"}],
    )

    result = await ReportTool(report).query("登录")

    assert result.found is False


# ---------------------------------------------------------------------------
# 任务表
# ---------------------------------------------------------------------------


class _FakeLookup:
    """任务表端口的一个实现。"""

    def __init__(self, tasks: tuple[TaskSummary, ...]) -> None:
        self._tasks = tasks
        self.asked: list[str] = []

    async def find_similar(self, text: str, *, limit: int = 20) -> tuple[TaskSummary, ...]:
        self.asked.append(text)
        return self._tasks[:limit]


async def test_the_task_table_tool_reports_what_it_found() -> None:
    lookup = _FakeLookup((TaskSummary(id="TASK-001", title="修复登录超时"),))
    tool = TaskTableTool(lookup)

    result = await tool.query("登录超时")

    assert result.found is True
    assert result.references == ("TASK-001",)
    assert "修复登录超时" in result.summary
    assert lookup.asked == ["登录超时"]


async def test_the_task_table_tool_reports_a_miss() -> None:
    tool = TaskTableTool(_FakeLookup(()))

    result = await tool.query("登录超时")

    assert result.found is False


async def test_the_task_table_tool_reports_an_empty_query() -> None:
    tool = TaskTableTool(_FakeLookup(()))

    result = await tool.query("   ")

    assert result.found is False


async def test_the_task_table_tool_only_echoes_a_few_titles() -> None:
    """标题回显有上限，但引用里是全量 id —— id 才是可核验的。"""
    lookup = _FakeLookup(
        tuple(TaskSummary(id=f"TASK-{index:03d}", title=f"任务 {index}") for index in range(10))
    )
    tool = TaskTableTool(lookup)

    result = await tool.query("任务")

    assert len(result.references) == 10
    assert result.summary.count("（TASK-") == 3


# ---------------------------------------------------------------------------
# 项目状态
# ---------------------------------------------------------------------------


async def test_the_project_status_reports_triggered_stop_conditions() -> None:
    goal = _goal(
        stop_conditions=[{"id": "SC-BUDGET", "description": "预算耗尽", "kind": "budget_exhausted"}]
    )
    tool = ProjectStatusTool(goal, facts={"budget_exhausted": True})

    result = await tool.query("stop_conditions")

    assert result.found is True
    assert result.references == ("SC-BUDGET",)
    assert result.status is not None
    assert result.status.stop_conditions_triggered == ("SC-BUDGET",)


async def test_the_project_status_reports_an_untriggered_stop_condition() -> None:
    tool = ProjectStatusTool(_goal())

    result = await tool.query("stop_conditions")

    assert result.found is False
    assert result.status is not None
    assert result.status.stop_conditions_triggered == ()


async def test_the_project_status_reports_unmet_key_results() -> None:
    tool = ProjectStatusTool(
        _goal(), {"KR-001": TargetValue(kind=ValueKind.NUMBER, number=Decimal("0.5"))}
    )

    result = await tool.query("key_results")

    assert result.found is True
    assert result.references == ("KR-001",)
    assert result.status is not None
    assert result.status.all_key_results_met is False


async def test_the_project_status_reports_met_key_results() -> None:
    tool = ProjectStatusTool(
        _goal(), {"KR-001": TargetValue(kind=ValueKind.NUMBER, number=Decimal("0.98"))}
    )

    result = await tool.query("key_results")

    assert result.status is not None
    assert result.status.all_key_results_met is True


async def test_the_project_status_says_unknown_when_key_results_are_unmeasured() -> None:
    """没测 ≠ 没达成 —— 三态里的第三态。"""
    tool = ProjectStatusTool(_goal())

    result = await tool.query("key_results")

    assert result.status is not None
    assert result.status.all_key_results_met is None


async def test_the_project_status_does_not_claim_the_key_results_are_met_when_unmeasured() -> None:
    """一句"所有关键结果都已达成"，读的人就不会再去测了。

    "没有未达成的"和"都达成了"是两句话 —— 还没测的那些不算未达成，它们是
    "不知道"。这一句写错，报告里"值得现在做"那一问就跟着说错。
    """
    tool = ProjectStatusTool(_goal())

    result = await tool.query("key_results")

    assert result.summary == "关键结果尚未测全 —— 说不出还差多少"
    assert "达成" not in result.summary


async def test_the_project_status_says_the_key_results_are_met_only_when_they_are() -> None:
    tool = ProjectStatusTool(
        _goal(), {"KR-001": TargetValue(kind=ValueKind.NUMBER, number=Decimal("0.98"))}
    )

    result = await tool.query("key_results")

    assert result.summary == "所有关键结果都已达成"


async def test_the_project_status_hands_out_the_non_goals() -> None:
    tool = ProjectStatusTool(_goal())

    result = await tool.query("non_goals")

    assert result.found is True
    assert result.references == ("NG-001",)
    assert result.status is not None
    assert [item.id for item in result.status.non_goals] == ["NG-001"]


async def test_the_project_status_hands_out_the_constraints() -> None:
    tool = ProjectStatusTool(_goal())

    result = await tool.query("constraints")

    assert result.status is not None
    assert [item.id for item in result.status.constraints] == ["C-001"]
    assert result.status.violated_constraints == ()


async def test_the_project_status_reports_injected_violations() -> None:
    tool = ProjectStatusTool(_goal(), violated_constraints=("C-001",))

    result = await tool.query("constraints")

    assert result.found is True
    assert result.references == ("C-001",)
    assert result.status is not None
    assert result.status.violated_constraints == ("C-001",)


# ---------------------------------------------------------------------------
# 产物登记处
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """产物登记处的一个替身 —— 只实现读的那一面。"""

    def __init__(self, records: dict[str, ArtifactRecord]) -> None:
        self._records = records

    async def get(self, artifact_id: str) -> ArtifactRecord | None:
        return self._records.get(artifact_id)

    async def get_by_uri(self, uri: str) -> ArtifactRecord | None:
        return None

    async def list_for(self, verification_run_id: str) -> tuple[ArtifactRecord, ...]:
        return tuple(
            record
            for record in self._records.values()
            if record.verification_run_id == verification_run_id
        )

    async def record_archive(
        self,
        run: VerificationRunRecord,
        artifacts: Sequence[ArtifactRecord],
    ) -> tuple[ArtifactRecord, ...]:
        raise NotImplementedError


def _artifact(artifact_id: str) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        task_id="TASK-001",
        verification_run_id="RUN-001",
        uri=f"s3://bucket/{artifact_id}",
        hash="abc123",
        type=ArtifactKind.TEST_REPORT,
    )


async def test_the_artifact_store_finds_a_registered_artifact() -> None:
    tool = ArtifactStoreTool(_FakeRegistry({"report-001": _artifact("report-001")}))

    result = await tool.query("report-001")

    assert result.found is True
    assert result.references == ("s3://bucket/report-001",)


async def test_the_artifact_store_reports_an_unregistered_artifact() -> None:
    tool = ArtifactStoreTool(_FakeRegistry({}))

    result = await tool.query("report-001")

    assert result.found is False


async def test_the_artifact_store_reports_an_empty_id() -> None:
    tool = ArtifactStoreTool(_FakeRegistry({}))

    result = await tool.query("   ")

    assert result.found is False


# ---------------------------------------------------------------------------
# 类型本身的约束
# ---------------------------------------------------------------------------


def test_the_task_lookup_is_a_port_not_an_implementation() -> None:
    """端口只承诺"像的都给" —— 判"是不是同一件事"是检查的事。"""
    assert hasattr(TaskLookup, "find_similar")


def test_a_status_fact_can_say_it_does_not_know() -> None:
    fact = ProjectStatusFact(all_key_results_met=None)

    assert fact.all_key_results_met is None
    assert fact.stop_conditions_triggered == ()
