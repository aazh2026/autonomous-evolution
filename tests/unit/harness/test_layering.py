"""``harness/`` 内部的包依赖 —— 工作空间与沙箱互不引用。

设计依据：docs/tasks.md TASK-P1-032、待裁决 13。

**为什么这条值得单独守。** 工作空间与沙箱是两件**失效模式完全不同**的事：

- 工作空间出问题 → **丢产出**（分支没了、目录没了）；
- 沙箱出问题 → **越过隔离**（凭据进了容器、生产库被连上）。

把两者放进同一个包，"这里出问题会怎样"就没有唯一答案。所以它们被拆成两个
包 —— 而拆开之后还得守住"它们不会重新长在一起"，否则拆分只是一次搬家。

**耦合必须是数据而不是代码。** 沙箱要挂载工作空间，但它拿到的是一个
**路径**（``build_workspace_sandbox_spec(workspace_path=...)``），不是
``Workspace`` 对象。两个包因此互不 import —— 各自能单独测、单独替换：工作
空间换成非 git 的实现，沙箱那侧一行都不用改。

反过来说，如果哪天有人让 ``sandbox/`` 直接 import ``workspace/``，那说明
"沙箱需要知道工作空间是什么"已经发生了 —— 那正是待裁决 13 里说的、拆分要
避免的那种依赖长了出来。

**用 AST 查，不用 grep。** grep 查 import 会漏掉动态 import，也会把注释与
文档字符串里的字样算进去；AST 查的是**真的被 import 了什么**。
"""

from __future__ import annotations

import ast
from pathlib import Path

from harness.sandbox.policy import build_workspace_sandbox_spec

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_DIR = REPO_ROOT / "harness"

WORKSPACE_PACKAGE = "harness.workspace"
SANDBOX_PACKAGE = "harness.sandbox"


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


def _package_files(package: str) -> list[Path]:
    directory = REPO_ROOT / package.replace(".", "/")
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def _depends_on(package: str, target: str) -> list[tuple[Path, str]]:
    """``package`` 里哪些文件 import 了 ``target``。"""
    hits: list[tuple[Path, str]] = []
    for path in _package_files(package):
        for imported in _imported_modules(path):
            if imported == target or imported.startswith(f"{target}."):
                hits.append((path, imported))
                break
    return hits


class TestTheTwoPackagesAreIndependent:
    """工作空间与沙箱之间没有代码依赖。"""

    def test_the_workspace_does_not_import_the_sandbox(self) -> None:
        hits = _depends_on(WORKSPACE_PACKAGE, SANDBOX_PACKAGE)
        assert hits == [], f"工作空间不该依赖沙箱：{hits}"

    def test_the_sandbox_does_not_import_the_workspace(self) -> None:
        hits = _depends_on(SANDBOX_PACKAGE, WORKSPACE_PACKAGE)
        assert hits == [], f"沙箱不该依赖工作空间：{hits}"

    def test_the_sandbox_takes_a_path_not_a_workspace(self) -> None:
        """沙箱拿的是**路径**，不是工作空间对象 —— 这就是不互相依赖的原因。"""
        parameters = build_workspace_sandbox_spec.__annotations__
        assert parameters["workspace_path"] == "Path"


class TestTheHarnessStaysAnAdapter:
    """``harness/`` 是适配层：它可以用 ``core/``，但不该用编排层的东西。"""

    def test_the_harness_does_not_import_the_orchestration(self) -> None:
        hits: list[tuple[Path, str]] = []
        for path in sorted(HARNESS_DIR.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for imported in _imported_modules(path):
                if imported.startswith(("orchestration.", "agents.")):
                    hits.append((path, imported))
        assert hits == [], f"harness/ 不该依赖编排层：{hits}"
