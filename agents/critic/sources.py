"""事实核验工具的具体实现 —— Critic 真去查的那些地方。

**为什么工具放在 ``agents/`` 而不是 ``core/``。** ``core/`` 是领域层，一行 IO
都不做（这是本仓库的分层铁律）；而这些工具**唯一的职责就是做 IO** —— 读文件、
跑 git、读测试报告、查数据库。放进 ``core/`` 会把 IO 引进领域层；放进
``infrastructure/`` 又会让 Critic 反过来依赖基础设施的具体实现。``agents/``
是第三者的位置：**它按接口取事实，不拥有事实**。

五类工具各自回答一个问题：

- **仓库搜索** —— 这件事在代码里落在哪儿？改动会碰到哪些文件？
- **Git 历史** —— 这件事以前有没有人做过？提交信息里有没有它？
- **测试报告** —— 有没有失败的用例能证明问题真实存在？
- **项目状态** —— 停止条件触发了没？目标达成了没？约束 violated 了没？
- **任务表** —— 是不是已经有别的任务在做了？
- **产物登记处** —— 证据指向的产物真的登记过吗？

**每个工具都只做查询，不做判定。** "查到了什么"由工具负责，"这意味着什么"
由检查负责。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final

from agents.critic.tools import ProjectStatusFact, TaskLookup, ToolKind, ToolResult
from core.evidence.artifacts import ArtifactRegistry
from core.project.models import ProjectGoal, TargetValue
from core.project.stop_conditions import Facts, GoalAttainmentCalculator, StopConditionEvaluator

#: 搜索时跳过的目录 —— 与静态检查那份清单一致，别让噪声淹没信号。
SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "htmlcov",
        "dist",
        "build",
    }
)

#: 只在这些扩展名的文件里找内容命中。
_TEXT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".py",
        ".md",
        ".txt",
        ".yaml",
        ".yml",
        ".json",
        ".toml",
        ".ini",
        ".cfg",
        ".sql",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".rb",
    }
)

#: 单个文件超过这个大小就不搜内容（只看文件名）。
MAX_CONTENT_BYTES: Final[int] = 1_000_000

#: 目标达成度等比率量的呈现精度 —— 与评分模型那边一致（4 位小数）。
_PRECISION: Final[Decimal] = Decimal("0.0001")

#: 任务表查询时回显多少条标题（引用里仍然是全量 id）。
_MAX_TITLE_ECHO: Final[int] = 3


class RepositorySearchTool:
    """仓库搜索 —— 在代码树里按文本找线索。

    **同时找文件名与文件内容。** 只找文件名会漏掉"这个概念散落在哪些地方"，
    只找内容会漏掉"这个模块叫什么名字"。
    """

    kind = ToolKind.REPOSITORY_SEARCH

    def __init__(
        self,
        root: Path,
        *,
        max_results: int = 20,
        search_contents: bool = True,
    ) -> None:
        self._root = root
        self._max_results = max_results
        self._search_contents = search_contents

    async def query(self, argument: str) -> ToolResult:
        return await asyncio.to_thread(self._search, argument)

    def _search(self, argument: str) -> ToolResult:
        needle = argument.strip().lower()
        if not needle:
            return ToolResult(found=False, summary="查询词是空白，没有可查的内容")

        matches: list[str] = []
        for path in self._walk():
            relative = path.relative_to(self._root).as_posix()
            if needle in relative.lower():
                matches.append(relative)
                if len(matches) >= self._max_results:
                    break
                continue
            if self._search_contents and self._content_matches(path, needle):
                matches.append(relative)
                if len(matches) >= self._max_results:
                    break

        if not matches:
            return ToolResult(
                found=False,
                summary=f"仓库里没有找到与「{argument}」相关的文件",
            )
        return ToolResult(
            found=True,
            summary=f"仓库里有 {len(matches)} 处与「{argument}」相关",
            references=tuple(matches),
        )

    def _walk(self) -> list[Path]:
        found: list[Path] = []
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(self._root).parts):
                continue
            found.append(path)
        return found

    def _content_matches(self, path: Path, needle: str) -> bool:
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            return False
        try:
            if path.stat().st_size > MAX_CONTENT_BYTES:
                return False
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        return needle in content.lower()


class GitHistoryTool:
    """Git 历史查询 —— 提交信息里有没有提到过这件事。

    **只查提交信息，不查 diff。** Critic 要的是"这件事以前有没有人碰过"，
    而不是"当时改了哪几行" —— 后者是代码评审的事。
    """

    kind = ToolKind.GIT_HISTORY

    def __init__(self, root: Path, *, max_results: int = 20) -> None:
        self._root = root
        self._max_results = max_results

    async def query(self, argument: str) -> ToolResult:
        return await asyncio.to_thread(self._log, argument)

    def _log(self, argument: str) -> ToolResult:
        needle = argument.strip()
        if not needle:
            return ToolResult(found=False, summary="查询词是空白，没有可查的提交")

        completed = subprocess.run(
            [
                "git",
                "log",
                f"--max-count={self._max_results}",
                "--pretty=%h %s",
                f"--grep={needle}",
            ],
            cwd=self._root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return ToolResult(
                found=False,
                summary=f"读取 Git 历史失败：{completed.stderr.strip() or '未知错误'}",
            )

        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            return ToolResult(
                found=False,
                summary=f"Git 历史里没有提到「{needle}」的提交",
            )
        return ToolResult(
            found=True,
            summary=f"Git 历史里有 {len(lines)} 条提到「{needle}」的提交",
            references=tuple(lines),
        )


class TestReportTool:
    """测试报告读取 —— 有没有失败的用例能证明问题真实存在。

    **报告格式**：一份 JSON，形如
    ``{"tests": [{"nodeid": "...", "outcome": "passed|failed|skipped"}]}``
    —— 与 ``pytest --json-report`` 的 ``tests`` 数组同形。

    **只看失败的用例。** 通过的用例证明不了"这个问题存在"，它们只证明"别的
    事情没问题"。报告里若有失败的用例且其中提到查询词，才算"查到了证据"。
    """

    kind = ToolKind.TEST_REPORT

    def __init__(self, path: Path) -> None:
        self._path = path

    async def query(self, argument: str) -> ToolResult:
        return await asyncio.to_thread(self._read, argument)

    def _read(self, argument: str) -> ToolResult:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError:
            return ToolResult(found=False, summary=f"测试报告读不到：{self._path}")
        except json.JSONDecodeError as exc:
            return ToolResult(found=False, summary=f"测试报告不是合法 JSON：{exc}")

        tests = raw.get("tests") if isinstance(raw, dict) else None
        if not isinstance(tests, list):
            return ToolResult(found=False, summary="测试报告里没有 tests 数组")

        needle = argument.strip().lower()
        failed = [
            str(item.get("nodeid", ""))
            for item in tests
            if isinstance(item, dict) and item.get("outcome") == "failed"
        ]
        if not failed:
            return ToolResult(found=False, summary="测试报告里没有失败的用例")

        matched = [item for item in failed if needle in item.lower()] if needle else list(failed)
        if not matched:
            return ToolResult(
                found=False,
                summary=f"测试报告里有 {len(failed)} 个失败的用例，但都与「{argument}」无关",
            )
        summary = (
            f"测试报告里有 {len(matched)} 个与「{argument}」相关的失败用例"
            if needle
            else f"测试报告里有 {len(matched)} 个失败的用例"
        )
        return ToolResult(found=True, summary=summary, references=tuple(matched))


class ProjectStatusTool:
    """项目状态查询 —— 停止条件、目标达成度、约束当前状态。

    **按查询词返回不同侧面。** ``argument`` 取 ``stop_conditions`` /
    ``key_results`` / ``constraints`` / ``non_goals``，分别只回对应的那一面；
    其它取值回全量。检查只关心自己那一问要的东西，多给反而容易看错。

    **"已违反的约束"是外面告诉它的。** 约束的表达式要拿运行期的指标才能求值，
    而指标不在本工具手里 —— 所以它只报**已确认违反**的那些（由调用方注入）。
    没注入就报"没有已确认违反的约束"，**不是**"没有违反任何约束"：前者是
    "没听说过出问题"，后者是"查过了，都好着呢"。
    """

    kind = ToolKind.PROJECT_STATUS

    def __init__(
        self,
        goal: ProjectGoal,
        actuals: Mapping[str, TargetValue] | None = None,
        *,
        facts: Facts | None = None,
        known_metrics: Sequence[str] = (),
        violated_constraints: Sequence[str] = (),
    ) -> None:
        self._goal = goal
        self._actuals = dict(actuals or {})
        self._facts: Facts = dict(facts or {})
        self._known_metrics = tuple(known_metrics)
        self._violated_constraints = tuple(violated_constraints)

    async def query(self, argument: str) -> ToolResult:
        return await asyncio.to_thread(self._status, argument)

    def _status(self, argument: str) -> ToolResult:
        facet = argument.strip().lower()
        fact = self._fact()
        if facet == "stop_conditions":
            references = fact.stop_conditions_triggered
            summary = (
                f"已触发 {len(references)} 条停止条件" if references else "没有触发任何停止条件"
            )
        elif facet == "key_results":
            references = fact.unmet_key_results
            # "没有未达成的"不等于"都达成了" —— 还没测的那些不算未达成，
            # 它们是"不知道"。三态要在这里就说清楚，别让读的人以为测过了。
            if fact.all_key_results_met is True:
                summary = "所有关键结果都已达成"
            elif fact.all_key_results_met is False:
                summary = f"{len(references)} 个关键结果尚未达成"
            else:
                summary = "关键结果尚未测全 —— 说不出还差多少"
        elif facet == "constraints":
            references = fact.violated_constraints
            summary = (
                f"当前已违反 {len(references)} 条约束" if references else "当前没有已确认违反的约束"
            )
        elif facet == "non_goals":
            references = tuple(non_goal.id for non_goal in fact.non_goals)
            summary = (
                f"项目声明了 {len(references)} 条非目标" if references else "项目没有声明任何非目标"
            )
        else:
            references = (
                *fact.stop_conditions_triggered,
                *fact.violated_constraints,
                *fact.unmet_key_results,
            )
            summary = fact.notes or "项目状态查询完成"

        return ToolResult(
            found=bool(references),
            summary=summary,
            references=tuple(references),
            status=fact,
        )

    def _fact(self) -> ProjectStatusFact:
        evaluator = StopConditionEvaluator(self._goal, known_metrics=self._known_metrics)
        stop = evaluator.evaluate(self._facts, actuals=self._actuals)

        calculator = GoalAttainmentCalculator(self._goal)
        attainment = calculator.evaluate(self._actuals)
        # "未达成"只算**测了没达标**的那些 —— 没测的不算未达成，它算"不知道"。
        unmet = tuple(
            key_result.id
            for key_result in self._goal.key_results
            if key_result.id not in attainment.unmeasured
            and key_result.id not in attainment.satisfied
        )
        if not self._goal.key_results or attainment.unmeasured:
            all_met: bool | None = None
        else:
            all_met = len(attainment.satisfied) == len(self._goal.key_results)

        return ProjectStatusFact(
            stop_conditions_triggered=stop.triggered_ids,
            violated_constraints=self._violated_constraints,
            unmet_key_results=unmet,
            all_key_results_met=all_met,
            key_results=self._goal.key_results,
            constraints=self._goal.constraints,
            non_goals=self._goal.non_goals,
            notes=(
                f"目标达成度 {attainment.overall.quantize(_PRECISION)}，"
                f"关键结果覆盖率 {attainment.coverage.quantize(_PRECISION)}"
            ),
        )


class ArtifactStoreTool:
    """产物登记处查询 —— 证据指向的产物真的登记过吗。

    **只查登记，不读内容。** Critic 要确认的是"这份证据存在"，不是"这份证据
    说了什么" —— 后者是读证据那一端的事。
    """

    kind = ToolKind.ARTIFACT_STORE

    def __init__(self, registry: ArtifactRegistry) -> None:
        self._registry = registry

    async def query(self, argument: str) -> ToolResult:
        artifact_id = argument.strip()
        if not artifact_id:
            return ToolResult(found=False, summary="产物 id 是空白")

        record = await self._registry.get(artifact_id)
        if record is None:
            return ToolResult(found=False, summary=f"产物登记处里没有 {artifact_id}")
        return ToolResult(
            found=True,
            summary=f"产物 {artifact_id} 已登记",
            references=(record.uri,),
        )


class TaskTableTool:
    """任务表查询 —— 是不是已经有别的任务在做了。

    **查到就交出去，判不判是检查的事。** 工具只负责把"像的"找出来 ——
    一旦它自作主张地认定"这就是同一件事"，检查就再也看不见那条任务了，
    而"像不像同一件事"恰恰是最需要人复核的一步。

    **标题回显是有上限的。** 一张任务表里可能躺着几十条像的任务，全列出来
    报告就没法读了 —— 引用里给全量 id（那是可核验的），标题只回显前几条。
    """

    kind = ToolKind.TASK_TABLE

    def __init__(self, lookup: TaskLookup) -> None:
        self._lookup = lookup

    async def query(self, argument: str) -> ToolResult:
        needle = argument.strip()
        if not needle:
            return ToolResult(found=False, summary="查询词是空白，没有可查的任务")

        tasks = await self._lookup.find_similar(needle)
        if not tasks:
            return ToolResult(found=False, summary=f"任务表里没有与「{needle}」相关的任务")

        titles = "、".join(f"{task.title}（{task.id}）" for task in tasks[:_MAX_TITLE_ECHO])
        summary = f"任务表里有 {len(tasks)} 条与「{needle}」相关的任务：{titles}"
        if len(tasks) > _MAX_TITLE_ECHO:
            summary += f" 等共 {len(tasks)} 条"

        return ToolResult(
            found=True,
            summary=summary,
            references=tuple(task.id for task in tasks),
        )


__all__ = [
    "ArtifactStoreTool",
    "GitHistoryTool",
    "ProjectStatusTool",
    "RepositorySearchTool",
    "TaskTableTool",
    "TestReportTool",
]
