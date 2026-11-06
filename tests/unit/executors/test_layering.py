"""``executors/`` 的包依赖 —— 执行器不管理工作空间。

设计依据：docs/tasks.md TASK-P1-031、待裁决 14。

**为什么这条值得单独守。** 执行器与工作空间管理面是两件**职责完全不同**的
事：

- 工作空间管理面（``harness/workspace/manager.py``）负责**建**工作空间 ——
  建 worktree、切分支、挂租约、回收；
- 执行器（``executors/``）负责在**已经给定的**工作空间里跑一个 CLI 进程，
  然后**如实报告**这个工作空间现在是什么样。

让执行器也能建工作空间，"一任务一目录一分支"这条不变量就有**两个**执行点：
harness 建一次，执行器可能再建一次 —— 而两次建出来的分支可能不是同一条。
那种不一致在单任务演示里完全看不出来，等到并行两个任务、产出串到一条分支
上时才发现，那时已经有一轮演进被提交到了错误的分支上。

**方向也是反的。** 工作空间是执行器的**输入**，不是它的产物。执行器去 import
工作空间管理面，等于让下游去决定上游的环境。

**唯一允许的例外是那一张约定表。** ``harness.workspace.naming`` 里没有 IO、
没有依赖，只有一张"分支怎么命名"的表 —— 产出分支的**约定**必须只有一份，
复述一遍就会漂移，而漂移的分支名会让两个执行器报出不同的产出位置。所以这里
引用它，而不是抄一遍。**引用约定 ≠ 管理工作空间**：一个是读一张表，一个是
建目录、切分支、管租约。

**用 AST 查，不用 grep。** grep 查 import 会漏掉动态 import，也会把注释与
文档字符串里的字样算进去；AST 查的是**真的被 import 了什么**。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTORS_DIR = REPO_ROOT / "executors"

#: 执行器**只准**引用这一个工作空间模块 —— 纯约定表，无 IO、无依赖。
ALLOWED_HARNESS_MODULES = ("harness.workspace.naming",)

#: 工作空间的管理面：建 worktree、切分支、挂租约、回收。
FORBIDDEN_WORKSPACE_MODULES = (
    "harness.workspace.manager",
    "harness.workspace.leases",
    "harness.workspace.gc",
    "harness.workspace.git",
    "harness.workspace.models",
)


def _imported_modules(path: Path) -> set[str]:
    """一个文件里 import 到的模块名。

    只取 ``import x`` 与 ``from x import y`` 的**根包名** —— 判断的是
    "依赖了哪个包"，而不是"依赖了包里的哪个模块"。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def _source_files() -> list[Path]:
    return sorted(p for p in EXECUTORS_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _hits(prefix: str) -> list[tuple[Path, str]]:
    hits: list[tuple[Path, str]] = []
    for path in _source_files():
        for imported in _imported_modules(path):
            if imported == prefix or imported.startswith(f"{prefix}."):
                hits.append((path, imported))
                break
    return hits


class TestTheExecutorDoesNotManageWorkspaces:
    """执行器只**读**工作空间，不**管**工作空间。"""

    def test_it_does_not_import_the_workspace_manager(self) -> None:
        hits = _hits("harness.workspace.manager")
        assert hits == [], f"执行器不该管理工作空间：{hits}"

    def test_it_does_not_import_the_lease_or_gc_machinery(self) -> None:
        hits = _hits("harness.workspace.leases") + _hits("harness.workspace.gc")
        assert hits == [], f"执行器不该碰租约与回收：{hits}"

    def test_it_does_not_import_the_git_client(self) -> None:
        """git 操作面是**管理面**的一部分。

        执行器要读工作空间的 git 状态，用的是 ``executors/cli/artifacts.py``
        里自己的只读探针 —— 它 ``rev-parse`` / ``symbolic-ref`` / ``diff``，
        不建分支、不切分支、不提交。
        """
        hits = _hits("harness.workspace.git")
        assert hits == [], f"执行器不该调用工作空间的 git 操作面：{hits}"

    def test_the_only_harness_module_it_may_import_is_the_naming_table(self) -> None:
        """只准引用那一张纯约定表。

        这是唯一被允许的跨适配器依赖 —— 它没有 IO、没有依赖，只有一张命名
        约定表。约定必须只有一份：复述一遍就会漂移，而漂移的分支名会让两个
        执行器报出不同的产出位置。
        """
        allowed = set(ALLOWED_HARNESS_MODULES)
        offenders: list[tuple[Path, str]] = []
        for path in _source_files():
            for imported in _imported_modules(path):
                if not imported.startswith("harness."):
                    continue
                if imported in allowed:
                    continue
                offenders.append((path, imported))
        assert offenders == [], f"执行器只准引用约定表 {allowed}，不该引用：{offenders}"


class TestTheExecutorStaysAnAdapter:
    """``executors/`` 是适配层：它可以用 ``core/``，但不该用编排层的东西。"""

    def test_it_does_not_import_the_orchestration(self) -> None:
        hits: list[tuple[Path, str]] = []
        for path in _source_files():
            for imported in _imported_modules(path):
                if imported.startswith(("orchestration.", "agents.")):
                    hits.append((path, imported))
        assert hits == [], f"executors/ 不该依赖编排层：{hits}"

    def test_it_does_not_import_the_sandbox(self) -> None:
        """沙箱隔离在执行器**之上**。

        执行器默认继承宿主环境 —— 这是"沙箱隔离必须先于执行器接入"那条顺序
        约束的由来（TASK-P1-032 → TASK-P1-031）。沙箱那一侧通过
        ``env_provider`` 把环境换成按白名单重造的那一份，而不是让执行器去
        认识沙箱。
        """
        hits = _hits("harness.sandbox")
        assert hits == [], f"executors/ 不该依赖沙箱：{hits}"
