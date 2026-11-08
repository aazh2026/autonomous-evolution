"""``core/`` 下不允许出现类型逃逸。

设计依据：docs/tasks.md TASK-P1-001
（"断言 ``mypy`` 以 ``--strict`` 运行且 ``core/`` 下无 ``Any`` 逃逸"）。

**为什么还要写这个测试，而不是只靠 mypy**：mypy 的 ``disallow_any_explicit``
有一个盲区 —— Pydantic v2 的 ``BaseModel.__init__(**data: Any)`` 会被子类
继承，mypy 把这个**继承来的** ``Any`` 记在子类定义行上。于是任何定义 Pydantic
模型的模块都会报 ``explicit-any``，与该模块自己是否写了 ``Any`` 无关。

工程上的处理是给那几个模块加 per-module override（见 ``pyproject.toml``）。
但 override 是**按模块**关掉的，不区分"这个模块自己写的 Any"和"从
BaseModel 继承来的 Any"。本测试用 AST 精确区分两者：

    **我们自己写出来的注解里，一个 ``Any`` 都不许有。**

继承来的不算 —— 那是 Pydantic 的签名，不是我们的选择。

同时本测试还守着两件事，防止规则被悄悄架空：

1. ``pyproject.toml`` 里 ``disallow_any_explicit`` 必须仍是 ``true``；
2. per-module override 的清单必须**正好**是已知的那些动态边界 ——
   多一个就说明有人用"加豁免"代替了"改代码"。
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_DIR = REPO_ROOT / "core"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: 允许保留 ``Any`` 的模块 —— 以及**为什么**。
#:
#: 每一项都必须能回答："这里为什么不得不动态？"答不上来的就该改代码。
#:
#: - ``core.policy.autonomy`` / ``core.policy.budget`` / ``core.project.models`` /
#:   ``core.metrics.cycle`` / ``executors.base`` / ``harness.verification.contract``：
#:   定义 Pydantic 模型 —— mypy 把继承自
#:   ``BaseModel.__init__`` 的 ``Any`` 记在子类定义行上。
#: - ``infrastructure.config.settings``：``BaseSettings`` 同上。
#: - ``infrastructure.storage.object_store``：aioboto3 / botocore 不发布
#:   类型标记，S3 客户端的方法面由 botocore 在运行时按服务模型校验。
#: - ``tests.*``：测试里用 ``Any`` 造动态结构的假数据是正当的 ——
#:   被断言的对象本身就是动态的，强行标成具体类型只会让假数据失真。
ALLOWED_ANY_MODULES: dict[str, str] = {
    "core.policy.autonomy": "Pydantic 模型 —— 继承自 BaseModel.__init__ 的 Any",
    "core.policy.budget": "Pydantic 模型 —— 继承自 BaseModel.__init__ 的 Any",
    "core.project.models": "Pydantic 模型 —— 继承自 BaseModel.__init__ 的 Any",
    "core.metrics.cycle": "Pydantic 模型 —— 继承自 BaseModel.__init__ 的 Any",
    "executors.base": "Pydantic 模型 —— 继承自 BaseModel.__init__ 的 Any",
    "harness.verification.contract": "Pydantic 模型 —— 继承自 BaseModel.__init__ 的 Any",
    "infrastructure.config.settings": "Pydantic BaseSettings —— 继承自 BaseModel.__init__ 的 Any",
    "infrastructure.storage.object_store": "aioboto3 / botocore 无类型标记，客户端方法面是动态的",
    "tests.*": "测试用 Any 造动态结构的假数据 —— 被断言的对象本身就是动态的",
}


def _module_name(path: Path) -> str:
    """文件路径 → 模块名（点分）。"""
    relative = path.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(relative.parts)


def _core_modules() -> list[Path]:
    return sorted(p for p in CORE_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _annotation_of(node: ast.AST) -> ast.expr | None:
    """取一个节点上挂着的注解。

    注解出现在三处：参数注解（``arg.annotation``）、变量注解
    （``AnnAssign.annotation``）、返回注解（``FunctionDef.returns``）。
    """
    if isinstance(node, ast.arg):
        return node.annotation
    if isinstance(node, ast.AnnAssign):
        return node.annotation
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.returns
    return None


def _annotation_nodes(tree: ast.Module) -> list[ast.expr]:
    """收集所有**注解**表达式节点。"""
    nodes: list[ast.expr] = []
    for node in ast.walk(tree):
        annotation = _annotation_of(node)
        if annotation is not None:
            nodes.append(annotation)
    return nodes


def _mentions_any(node: ast.expr) -> bool:
    """注解里是否出现了 ``Any``。

    两种写法都算：``Any``（裸名）与 ``typing.Any``（属性访问）。
    字符串注解（``"dict[str, Any]"``）在 AST 里仍是表达式节点，
    所以同样能检出。
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == "Any":
            return True
        if isinstance(child, ast.Attribute) and child.attr == "Any":
            return True
    return False


def _pyproject() -> dict[str, object]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


class TestCoreHasNoAnyEscape:
    """``core/`` 下我们自己写的注解里不许有 ``Any``。"""

    @pytest.mark.parametrize(
        "path",
        _core_modules(),
        ids=_module_name,
    )
    def test_no_explicit_any_in_annotations(self, path: Path) -> None:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders = [node for node in _annotation_nodes(tree) if _mentions_any(node)]
        assert not offenders, (
            f"{_module_name(path)} 的注解里出现了 Any："
            f"{[ast.unparse(node) for node in offenders]}。"
            " 用具体的类型替代；确实只能动态的地方写进 ALLOWED_ANY_MODULES 并说明理由。"
        )

    def test_core_is_not_empty(self) -> None:
        """守卫：万一路径写错，上面那条会"零文件通过"，看起来像没问题。"""
        assert len(_core_modules()) >= 10, f"core/ 下只找到 {len(_core_modules())} 个模块"


class TestExemptionListIsNotAWideDoor:
    """豁免清单本身也要被检查。"""

    def test_disallow_any_explicit_is_enabled(self) -> None:
        config = _pyproject()
        mypy = config.get("tool", {})
        assert isinstance(mypy, dict)
        tool_mypy = mypy.get("mypy", {})
        assert isinstance(tool_mypy, dict)
        assert tool_mypy.get("disallow_any_explicit") is True, (
            "disallow_any_explicit 必须保持开启 —— 它是「Any 不许逃逸」这条规则的执行者"
        )

    def test_exemption_list_matches_the_documented_set(self) -> None:
        """per-module override 的模块必须与 ``ALLOWED_ANY_MODULES`` 完全一致。

        多一个豁免，就等于多一个"这里可以不守规矩"的地方 —— 而豁免本身
        没有任何强制说明理由的机制。逼着人改清单，就是逼着人想清楚。
        """
        config = _pyproject()
        tool = config.get("tool", {})
        assert isinstance(tool, dict)
        overrides = tool.get("mypy", {})
        assert isinstance(overrides, dict)
        raw_overrides = overrides.get("overrides", [])
        assert isinstance(raw_overrides, list)

        exempted: set[str] = set()
        for entry in raw_overrides:
            assert isinstance(entry, dict)
            if entry.get("disallow_any_explicit") is not False:
                continue
            modules = entry.get("module", [])
            assert isinstance(modules, list)
            exempted.update(str(m) for m in modules)

        assert exempted == set(ALLOWED_ANY_MODULES), (
            f"豁免清单与文档不一致。豁免：{sorted(exempted)}；文档：{sorted(ALLOWED_ANY_MODULES)}"
        )

    def test_core_modules_in_exemption_list_are_still_any_free(self) -> None:
        """被豁免的 ``core/`` / ``executors/`` 模块里，注解也不许有 ``Any``。

        豁免只针对 mypy 记在**继承成员**上的那笔账 —— 我们自己写的
        注解一律受这条约束。所以豁免模块也要单独查一遍。

        ``infrastructure.storage.object_store`` 不在这次检查范围内：它的豁免
        是另一回事（第三方库没有类型标记，``Any`` 是**如实**的），它用不用
        ``Any`` 由它自己的模块负责。
        """
        for module_name in ALLOWED_ANY_MODULES:
            if not module_name.startswith(("core.", "executors.")):
                continue
            path = REPO_ROOT / Path(*module_name.split(".")).with_suffix(".py")
            assert path.exists(), f"豁免清单里的模块不存在：{module_name}"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders = [node for node in _annotation_nodes(tree) if _mentions_any(node)]
            assert not offenders, (
                f"{module_name} 在豁免清单里，但它自己的注解里还有 Any："
                f"{[ast.unparse(node) for node in offenders]}"
            )
