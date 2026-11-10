"""耦合仓库的测试构造器 —— 一个"局部都正常、整体有问题"的仓库。

设计依据：docs/tasks.md TASK-P3-012（验收条件：定期扫描能发现事件触发
发现不了的问题（跨模块耦合类））。

**这个仓库是照着验收条件造的。** 里面刻意安排了三类"没有事件"的问题：

1. **耦合枢纽** —— ``pkg.hub`` 被五个模块依赖，自己却没有任何用例；
2. **循环依赖** —— ``pkg.ring_a`` 与 ``pkg.ring_b`` 互相依赖；
3. **没人用的公开接口** —— ``pkg.orphan.never_called``。

**它们共同的特点是：没有一件事"发生"了。** 流水线是绿的、用例是全过的、
没有用户反馈、没有依赖漏洞 —— 六个触发源里没有任何一个会为它们开一轮
演进。它们只在**把整个仓库摊开看**的时候才存在。

**刻意安排成"全绿"。** 只有当仓库看起来完全健康时，"扫描发现了事件发现
不了的问题"这句话才站得住 —— 否则读的人分不清扫出来的是**问题**还是
本来就坏掉的东西。

**它是一个真能跑的仓库。** 用例是真的、跑得起来、断言是真的 —— 不是为了
让测试通过而写出来的空壳。
"""

from __future__ import annotations

from pathlib import Path

#: 仓库里的文件。``{path: 内容}``。
#:
#: **``pkg.hub`` 是被依赖最多的那个模块** —— 五个用户模块都 import 它，
#: 而它自己**一条用例都没有**。这就是"耦合枢纽"：改它要动五个地方，
#: 而它改坏了没人会发现。
#:
#: **``pkg.ring_a`` 与 ``pkg.ring_b`` 互相 import** —— 环。改哪一个都像
#: 是被另一个卡住。
#:
#: **``pkg.orphan`` 有一个没人调用的公开函数** —— 静态事实（没有用例
#: import 过它），不是运行出来的事实。
#:
#: **``pkg.importer`` 反过来依赖测试代码** —— 依赖方向错了。
FILES: dict[str, str] = {
    "pyproject.toml": """\
[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
""",
    "src/pkg/__init__.py": '"""一个看起来很正常的包。"""\n',
    "src/pkg/hub.py": '''\
"""被五个模块依赖，却没有任何用例碰过它。"""


def hub_value() -> int:
    """一个被广泛复用的函数。"""
    return 1
''',
    "src/pkg/user1.py": "from pkg import hub\n\n\ndef use() -> int:\n    return hub.hub_value()\n",
    "src/pkg/user2.py": "from pkg import hub\n\n\ndef use() -> int:\n    return hub.hub_value()\n",
    "src/pkg/user3.py": "from pkg import hub\n\n\ndef use() -> int:\n    return hub.hub_value()\n",
    "src/pkg/user4.py": "from pkg import hub\n\n\ndef use() -> int:\n    return hub.hub_value()\n",
    "src/pkg/user5.py": "from pkg import hub\n\n\ndef use() -> int:\n    return hub.hub_value()\n",
    "src/pkg/ring_a.py": '''\
"""环上的一个模块 —— 依赖方向绕回来了。"""

from pkg import ring_b


def one() -> int:
    """一个普通的函数。"""
    return 1


def uses_the_other() -> int:
    """运行时用一下对方 —— **但不递归**。

    环是**导入层面**的，不是调用层面的：一个绕回来的依赖环在运行时会
    无限递归，那是另一个问题。这里要的是"改哪一个都像被另一个卡住"，
    而仓库本身是好的、跑得起来的。
    """
    return ring_b.two()
''',
    "src/pkg/ring_b.py": '''\
"""环上的另一个模块。"""

from pkg import ring_a


def two() -> int:
    """一个普通的函数。"""
    return 2


def uses_the_other() -> int:
    """运行时用一下对方 —— 但不递归。"""
    return ring_a.one()
''',
    "src/pkg/orphan.py": '''\
"""有一个没人用的公开接口。"""


def never_called() -> int:
    """没有任何用例引用过它。"""
    return 2
''',
    "src/pkg/importer.py": '''\
"""反过来依赖测试代码的模块。"""

from tests import helpers


def use() -> int:
    """产品代码去找测试代码 —— 依赖方向错了。"""
    return helpers.helper()
''',
    "tests/__init__.py": "",
    "tests/helpers.py": '''\
"""测试侧的帮手 —— 产品代码反过来依赖了它。"""


def helper() -> int:
    return 0
''',
    "tests/test_ring.py": '''\
"""环上的两个模块都能被用例引用 —— 环本身不是"坏了"，只是绕不过去。"""

from pkg import ring_a, ring_b


def test_the_ring_still_works() -> None:
    assert ring_a.one() == 1
    assert ring_b.two() == 2
''',
    "tests/test_users.py": '''\
"""用户模块有用例 —— 被测过的东西不该被报成"没人测"。"""

from pkg import user1


def test_user_works() -> None:
    assert user1.use() == 1
''',
}

#: 耦合枢纽的模块名。
HUB_MODULE: str = "pkg.hub"

#: 耦合枢纽的依赖者个数。**五个** —— 正好卡在默认阈值（5）上。
HUB_FAN_IN: int = 5

#: 环上的两个模块。
RING_MODULES: tuple[str, str] = ("pkg.ring_a", "pkg.ring_b")

#: 那个没人用的公开接口所在的模块与符号名。
ORPHAN_MODULE: str = "pkg.orphan"
ORPHAN_SYMBOL: str = "never_called"

#: 反过来依赖测试代码的模块。
IMPORTER_MODULE: str = "pkg.importer"


def build_coupled_repo(root: Path) -> Path:
    """把耦合仓库写出来。

    :param root: 写到哪里（会被创建）。
    :return: 仓库根目录。
    """
    for relative, content in FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


__all__ = [
    "FILES",
    "HUB_FAN_IN",
    "HUB_MODULE",
    "IMPORTER_MODULE",
    "ORPHAN_MODULE",
    "ORPHAN_SYMBOL",
    "RING_MODULES",
    "build_coupled_repo",
]
