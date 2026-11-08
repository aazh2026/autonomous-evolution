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

#: 持久化实体所在的包。
#:
#: **为什么扫描要按来源分辨，而不是按名字。** 领域模型
#: ``core.evolution.decision.EvolutionDecision`` 与持久化实体
#: ``core.entities.EvolutionDecision`` **同名** —— 一个是"决策长什么样"的领域
#: 模型（纯 Python，随便造），一个是"决策那一行"的数据库实体（只能由仓储层
#: 写）。只看名字的扫描分不出这两者：要么把领域模型的构造误判成"绕过决策
#: 路径写库"，要么图省事加个白名单 —— 而白名单会把**真正的旁路**一起放过
#: 去。**按来源分辨，两个问题一起没有。**
ENTITY_MODULE = "core.entities"


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


def _entity_local_names(tree: ast.Module, entity_name: str) -> set[str]:
    """这个文件里指向该持久化实体的**本地名字**。

    空集意味着这个文件压根没引用那个实体 —— 它里面同名的构造调用是别的东西
    （多半是领域模型）。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.startswith(ENTITY_MODULE):
            continue
        for alias in node.names:
            if alias.name == entity_name:
                names.add(alias.asname or alias.name)
    return names


def _entity_module_aliases(tree: ast.Module) -> set[str]:
    """``import core.entities.xxx`` 引进来的模块别名。"""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            if alias.name.startswith(ENTITY_MODULE):
                aliases.add(alias.asname or alias.name)
    return aliases


def _attribute_root(node: ast.expr) -> str | None:
    """属性链的根名字：``a.b.c`` → ``"a"``。"""
    current = node
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


def _entity_constructed_names(path: Path, entity_name: str) -> set[str]:
    """这个文件里构造了**持久化实体**的调用名。

    **按来源分辨，不按名字分辨** —— 见 :data:`ENTITY_MODULE` 的说明。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    local_names = _entity_local_names(tree, entity_name)
    module_aliases = _entity_module_aliases(tree)
    if not local_names and not module_aliases:
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in local_names:
                names.add(func.id)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == entity_name
            # ``core.entities.EvolutionDecision(...)`` 这种带包前缀的写法。
            and _attribute_root(func.value) in module_aliases
        ):
            names.add(entity_name)
    return names


def _constructing_methods(tree: ast.Module, entity_name: str) -> list[str]:
    """哪些**方法**里构造了这个持久化实体。

    **文件级的白名单管不住方法级。** "构造 Task 的文件是仓储层"这条规则，
    放行了 ``land_roadmap`` 里直接 ``Task(...)`` 的写法 —— 文件合法，但三道
    准入全被绕过了。所以这里按方法再问一遍。
    """
    local_names = _entity_local_names(tree, entity_name)
    module_aliases = _entity_module_aliases(tree)
    if not local_names and not module_aliases:
        return []

    methods: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if isinstance(func, ast.Name) and func.id in local_names:
                methods.append(node.name)
                break
            if (
                isinstance(func, ast.Attribute)
                and func.attr == entity_name
                and _attribute_root(func.value) in module_aliases
            ):
                methods.append(node.name)
                break
    return methods


class TestNoBypassWritePath:
    """③ 静态检查。"""

    def test_task_is_only_constructed_in_the_repository(self) -> None:
        offenders = [
            _relative(path)
            for path in _python_files()
            if "Task" in _entity_constructed_names(path, "Task")
        ]
        # 实体定义文件里是 class Task(...) —— 不是 Call，不会被算进来。
        assert set(offenders) <= TASK_CONSTRUCTION_ALLOWLIST, (
            f"以下文件直接构造了 Task，绕过了决策路径：{sorted(set(offenders))}"
        )

    def test_decision_is_only_constructed_in_the_repository(self) -> None:
        offenders = [
            _relative(path)
            for path in _python_files()
            if "EvolutionDecision" in _entity_constructed_names(path, "EvolutionDecision")
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

    def test_the_repository_constructs_tasks_in_exactly_one_place(self) -> None:
        """仓储层内部也只有一个构造点 —— :meth:`create_from_decision`。

        **为什么"在仓储层里"还不够。** 上面那条扫描只保证"构造 Task 的文件是
        仓储层"—— 但仓储层自己有好几个方法。在 ``land_roadmap`` 里直接
        ``Task(...)``，文件层面完全合法，却绕开了那三道准入（决策存在、是
        ``record``、没有被推翻）。

        这就是**文件级白名单的天花板**：它管得住"在哪个文件"，管不住"在这个
        文件的哪个方法"。所以这一条把粒度收到方法级。
        """
        repository = REPO_ROOT / "infrastructure/database/repositories.py"
        tree = ast.parse(repository.read_text(encoding="utf-8"), filename=str(repository))

        constructing_methods = _constructing_methods(tree, "Task")

        assert constructing_methods == ["create_from_decision"], (
            "构造 Task 的方法必须只有 create_from_decision 一个 —— "
            f"实际是：{sorted(constructing_methods)}"
        )

    def test_the_one_construction_site_is_actually_guarded(self) -> None:
        """那个唯一的构造点必须真的在做准入 —— 否则"只有一处"是空的。

        守卫的三条：决策存在、是 ``record``、没有被推翻。前两条在没有决策行
        时会直接报错，第三条只在 ``overridden_by`` 非空时才触发。
        """
        source = (REPO_ROOT / "infrastructure/database/repositories.py").read_text(encoding="utf-8")
        body = source.split("async def create_from_decision", 1)[1]
        body = body.split("\n    async def ", 1)[0]
        assert "NonExecutableDecisionError" in body, "准入检查之一被删掉了"
        assert "OverriddenDecisionError" in body, "准入检查之一被删掉了"

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

        这里补一条更严格的：任何 ``.add(`` 调用如果参数里含**持久化实体**
        ``Task``，只能出现在仓储层。
        """
        offenders: list[str] = []
        for path in _python_files():
            relative = _relative(path)
            if relative in TASK_CONSTRUCTION_ALLOWLIST:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            local_names = _entity_local_names(tree, "Task")
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
                        and argument.func.id in local_names
                    ):
                        offenders.append(relative)
        assert not offenders, f"以下文件把 Task 直接塞进了会话：{sorted(set(offenders))}"


class TestScanCoverage:
    """扫描本身要真的扫到了东西。"""

    def test_scanner_finds_the_repository(self) -> None:
        """扫描器能发现仓储层里的 Task 构造 —— 否则"没有违例"是假的。"""
        repository = REPO_ROOT / "infrastructure/database/repositories.py"
        assert "Task" in _constructed_names(repository)

    def test_the_scanner_catches_a_real_bypass(self, tmp_path: Path) -> None:
        """扫描器自己能逮住一个真的旁路 —— 否则"没有违例"是假的。"""
        offender = tmp_path / "rogue.py"
        offender.write_text(
            "from core.entities import EvolutionDecision\n\n\n"
            "def make():\n"
            "    return EvolutionDecision(candidate_id='x')\n",
            encoding="utf-8",
        )

        assert "EvolutionDecision" in _entity_constructed_names(offender, "EvolutionDecision")

    def test_the_scanner_ignores_a_same_named_domain_model(self, tmp_path: Path) -> None:
        """同名但**不同源**的领域模型不算旁路。

        领域模型随便造 —— 那是它的本职。把它算成"绕过决策路径写库"，逼着人
        加白名单，而白名单会把真正的旁路一起放过。
        """
        innocent = tmp_path / "innocent.py"
        innocent.write_text(
            "from core.evolution.decision import EvolutionDecision\n\n\n"
            "def make():\n"
            "    return EvolutionDecision(id='EVD-1')\n",
            encoding="utf-8",
        )

        assert not _entity_constructed_names(innocent, "EvolutionDecision")

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
