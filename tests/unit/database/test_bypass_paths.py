"""TASK-P1-022 ③ —— 静态检查：代码库中不存在不经 EvolutionDecision 构造 Task 的调用点。

这是"无旁路写入路径"的**可执行**证据。规则很朴素：

> 全代码库里，除了仓储层那一个受控入口，不允许出现 ``Task(...)`` 的构造调用。

用 AST 扫描而不是正则 —— 正则分不清 ``Task(`` 是调用还是字符串、
注释或类型注解。AST 只看真实的调用节点。
"""

from __future__ import annotations

import ast
import importlib
import inspect
from collections.abc import Iterator
from pathlib import Path

import pytest

from infrastructure.database.repositories import TaskRepository

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 允许构造 ``Task`` 的文件。只有仓储层 ——
#: 它是"决策 → 任务"这条路的唯一入口。
TASK_CONSTRUCTION_ALLOWLIST = {
    "infrastructure/database/repositories.py",
}

#: 允许构造 ``EvolutionDecision`` 的文件。
DECISION_CONSTRUCTION_ALLOWLIST = {
    "infrastructure/database/repositories.py",
}

#: 扫描时跳过的目录。
SKIP_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "htmlcov",
    "node_modules",
    "build",
    "dist",
}


def _python_files() -> Iterator[Path]:
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _constructed_names(path: Path) -> set[str]:
    """找出该文件里被**调用**的类构造器名。

    只认 ``Call`` 节点 —— ``class Task(...)`` 是 ClassDef，
    ``Mapped[list["Task"]]`` 是注解，``session.get(Task, id)`` 里 ``Task``
    是参数。三者都不是"构造 Task"。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


class TestNoBypassWritePath:
    """③ 静态检查。"""

    def test_task_is_only_constructed_in_the_repository(self) -> None:
        offenders = [
            _relative(path) for path in _python_files() if "Task" in _constructed_names(path)
        ]
        # 实体定义文件里是 class Task(...) —— 不是 Call，不会被算进来。
        assert set(offenders) <= TASK_CONSTRUCTION_ALLOWLIST, (
            f"以下文件直接构造了 Task，绕过了决策路径：{sorted(set(offenders))}"
        )

    def test_decision_is_only_constructed_in_the_repository(self) -> None:
        offenders = [
            _relative(path)
            for path in _python_files()
            if "EvolutionDecision" in _constructed_names(path)
        ]
        assert set(offenders) <= DECISION_CONSTRUCTION_ALLOWLIST, (
            f"以下文件直接构造了 EvolutionDecision：{sorted(set(offenders))}"
        )

    def test_repository_is_the_only_place_that_builds_tasks(self) -> None:
        """仓储层里确实有构造 —— 否则上一条测试会因为"没人建任务"而空过。"""
        repository_source = (REPO_ROOT / "infrastructure/database/repositories.py").read_text(
            encoding="utf-8"
        )
        assert "Task(" in repository_source

    def test_task_repository_has_no_generic_insert_method(self) -> None:
        """仓储层不提供 ``add`` / ``create`` 这类通用写入口。"""
        public_methods = {
            name
            for name in dir(TaskRepository)
            if not name.startswith("_") and callable(getattr(TaskRepository, name))
        }
        forbidden = {"add", "create", "save", "insert", "put", "new"}
        assert not (public_methods & forbidden), (
            f"TaskRepository 暴露了通用写入口：{sorted(public_methods & forbidden)}"
        )

    def test_task_repository_creation_path_is_decision_bound(self) -> None:
        """唯一的创建方法必须以 decision_id 为必需参数。"""
        signature = inspect.signature(TaskRepository.create_from_decision)
        assert "decision_id" in signature.parameters
        parameter = signature.parameters["decision_id"]
        assert parameter.default is inspect.Parameter.empty, (
            "decision_id 不能有默认值 —— 那等于允许无决策的任务"
        )

    def test_no_direct_session_add_of_task_outside_repository(self) -> None:
        """``session.add(Task(...))`` 这类写法同样被上面的扫描覆盖。

        这里补一条更严格的：任何 ``.add(`` 调用如果参数里含 ``Task``，
        只能出现在仓储层。
        """
        offenders: list[str] = []
        for path in _python_files():
            relative = _relative(path)
            if relative in TASK_CONSTRUCTION_ALLOWLIST:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or func.attr != "add":
                    continue
                for argument in node.args:
                    if (
                        isinstance(argument, ast.Call)
                        and isinstance(argument.func, ast.Name)
                        and argument.func.id == "Task"
                    ):
                        offenders.append(relative)
        assert not offenders, f"以下文件把 Task 直接塞进了会话：{sorted(set(offenders))}"


class TestScanCoverage:
    """扫描本身要真的扫到了东西。"""

    def test_scanner_finds_the_repository(self) -> None:
        """扫描器能发现仓储层里的 Task 构造 —— 否则"没有违例"是假的。"""
        repository = REPO_ROOT / "infrastructure/database/repositories.py"
        assert "Task" in _constructed_names(repository)

    def test_scanner_visits_the_whole_repository(self) -> None:
        files = list(_python_files())
        assert len(files) > 20, f"只扫描到 {len(files)} 个文件，扫描范围不对"
        relatives = {_relative(path) for path in files}
        assert "core/entities/task.py" in relatives
        assert "infrastructure/database/repositories.py" in relatives

    def test_entity_definition_file_is_not_an_offender(self) -> None:
        """``class Task(...)`` 不是构造调用 —— 别把它误判成违例。"""
        entity_file = REPO_ROOT / "core/entities/task.py"
        assert "Task" not in _constructed_names(entity_file)


@pytest.mark.parametrize(
    "module_name",
    ["core.entities", "infrastructure.database", "infrastructure.database.repositories"],
)
def test_modules_are_importable(module_name: str) -> None:
    """导入不报错 —— 静态检查扫的必须是能正常导入的代码。"""
    assert importlib.import_module(module_name) is not None
