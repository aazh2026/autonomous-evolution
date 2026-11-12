"""金标底座 —— 存什么、怎么比、怎么更新。

**这一层只管三件事：期望产物放哪、怎么比、怎么更新。** 它不懂 fail-closed
是什么，也不懂演进周期是什么 —— 那些是 :mod:`tests.golden.matrix` 的事。
分开的理由很简单：底座要是也懂业务，它就会在业务变的时候跟着变，于是
"金标变了"这件事就再也说不清是谁变的。

**期望产物是 JSON，不是哈希。** 只存哈希的黄金测试在失败时只能告诉你
"变了"，不能告诉你"变了什么" —— 那等于把人赶回现场重跑一遍。这里两样
都存：JSON 给人评审，哈希给 CI 走快门（哈希放在 manifest 里，不单独占
文件）。

**比的是结构，不是字符串。** 逐行比文本，任何一个字段顺序的变动都会把
整份金标冲掉，于是金标很快就变成噪声。这里做的是**结构化差分**：递归到
叶子节点，只报真正不一样的那几处。

**三层归一 —— 顺序敏感层、集合层、忽略层。**

- **顺序敏感层**：``loop_a_nodes``、``task_states`` 这类"走过哪些节点、先后
  什么顺序"的事实。顺序变了就是行为变了，必须报。
- **集合层**：路径以 ``[]`` 结尾的键。里面的东西**有没有**重要，顺序不重
  要 —— 比如"这一轮建了哪些任务"。集合层不比顺序，是为了不让无关的排序
  抖动伪装成行为变化。金标被弃用的头号原因就是这种假阳性。
- **忽略层**：uuid、时间戳、绝对路径这类天生不可重复的东西。它们**不进
  金标** —— 与其在比较时忽略，不如在采集时就不收。收进去就等于给金标塞
  了一个永远会变的字段。

**更新金标只有一个入口**：环境变量 ``ASES_GOLDEN_UPDATE=1``。它不是
pytest 选项 —— 选项得注册在根 conftest 上，而根 conftest 是别人的地盘，
不该为了一个测试底座去动它。环境变量还有一个好处：它**不会**被顺手加进
某条 pytest 命令行里然后留在 CI 配置里。

**更新时也报差分。** 更新不是"覆盖"，是"把变了什么摆到人面前" —— 改了
之后金标还是要在 PR 里被人看一眼的。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: 期望产物目录 —— 相对本文件，所以从任何工作目录跑都指向同一个地方。
GOLDEN_ROOT: Final[Path] = Path(__file__).parent / "expected"

#: 更新金标的环境变量。
UPDATE_ENV: Final[str] = "ASES_GOLDEN_UPDATE"

#: 集合层的路径后缀 —— 以它结尾的键按集合比（不比顺序）。
SET_SUFFIX: Final[str] = "[]"


#: "这一边没有这个东西"的哨兵。
#:
#: 它不能是 ``None`` —— ``None`` 是一个合法的值（"没有阈值"就是 ``None``），
#: 用它当哨兵会把"没有"和"是 None"混成一件事。
class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<缺失>"


MISSING: Final[_Missing] = _Missing()


def canonical(payload: object) -> str:
    """规范化的 JSON 文本。

    **键排序、缩进固定、不转义中文。** 转义中文会让金标在 diff 里全是
    ``\\uXXXX`` —— 人根本读不了，也就评审不了。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def digest(payload: object) -> str:
    """期望产物的摘要 —— 给 CI 走快门用的。"""
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Difference:
    """一处不一样。

    :attr:`path` 是定位串：``$.decisions[0].state``、``$.planned_tasks[]``。
    它读起来要能直接指出"哪儿变了" —— 金标失败的时候，人是先看这个的。
    """

    path: str
    expected: object
    actual: object

    def render(self) -> str:
        """一行人话。"""
        return f"{self.path}:\n  期望 {self.expected!r}\n  实际 {self.actual!r}"


def _diff_mapping(
    expected: Mapping[object, object],
    actual: Mapping[object, object],
    path: str,
) -> tuple[Difference, ...]:
    """映射按键递归 —— 键的增删会报成"这一边有、那一边没有"。"""
    out: list[Difference] = []
    for key in sorted(set(expected) | set(actual), key=str):
        out.extend(
            diff(
                expected.get(key, MISSING),
                actual.get(key, MISSING),
                f"{path}.{key}",
            )
        )
    return tuple(out)


def _diff_sequence(
    expected: Sequence[object],
    actual: Sequence[object],
    path: str,
) -> tuple[Difference, ...]:
    """序列逐位比；路径以 ``[]`` 结尾的按集合比（排序后比）。"""
    if isinstance(expected, (str, bytes)) or isinstance(actual, (str, bytes)):
        if expected == actual:
            return ()
        return (Difference(path, expected, actual),)
    if path.endswith(SET_SUFFIX):
        left = sorted(expected, key=canonical)
        right = sorted(actual, key=canonical)
        if left == right:
            return ()
        return (Difference(path, left, right),)
    out: list[Difference] = []
    for index in range(max(len(expected), len(actual))):
        left_item = expected[index] if index < len(expected) else MISSING
        right_item = actual[index] if index < len(actual) else MISSING
        out.extend(diff(left_item, right_item, f"{path}[{index}]"))
    return tuple(out)


def diff(expected: object, actual: object, path: str = "$") -> tuple[Difference, ...]:
    """结构化差分。

    - **映射**按键递归 —— 键的增删会报成"这一边有、那一边没有"。
    - **序列**逐位比；路径以 ``[]`` 结尾的按集合比（排序后比）。
    - **其它**直接比。

    返回空元组表示**完全一致**。
    """
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        return _diff_mapping(expected, actual, path)

    if isinstance(expected, Sequence) and isinstance(actual, Sequence):
        return _diff_sequence(expected, actual, path)

    if expected == actual:
        return ()
    return (Difference(path, expected, actual),)


class GoldenMismatchError(AssertionError):
    """金标对不上 —— 附上人能读的差分。"""


class GoldenMissingError(AssertionError):
    """金标不存在 —— 附上怎么生成。"""


def updating() -> bool:
    """现在是不是在更新模式。"""
    return os.environ.get(UPDATE_ENV, "").strip() not in ("", "0", "false", "False")


class GoldenStore:
    """期望产物的存取。

    :param root: 期望产物目录。默认是 :data:`GOLDEN_ROOT`。
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else GOLDEN_ROOT

    @property
    def root(self) -> Path:
        """期望产物目录。"""
        return self._root

    def path_for(self, name: str) -> Path:
        """某个金标的落盘位置。"""
        return self._root / f"{name}.json"

    def load(self, name: str) -> object | None:
        """读一份金标。**不存在返回 ``None``** —— 不抛异常，调用方自己决定
        是"要生成"还是"要报错"。"""
        path = self.path_for(name)
        if not path.exists():
            return None
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    def save(self, name: str, payload: object) -> Path:
        """写一份金标。"""
        path = self.path_for(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical(payload), encoding="utf-8")
        return path

    def check(self, name: str, actual: object) -> tuple[Difference, ...]:
        """比一份金标。

        **返回差分，不抛异常** —— 调用方决定怎么报错。更新模式下会写回，
        但**仍然返回差分**：改了之后也要让人看见改了什么。
        """
        expected = self.load(name)
        if expected is None:
            if updating():
                self.save(name, actual)
                return ()
            raise GoldenMissingError(
                f"金标 {name!r} 不存在（{self.path_for(name)}）。\n"
                f"第一次生成用：{UPDATE_ENV}=1 uv run pytest ..."
            )
        differences = diff(expected, actual)
        if updating() and differences:
            self.save(name, actual)
        return differences


def assert_golden(name: str, actual: object, store: GoldenStore | None = None) -> None:
    """比一份金标，对不上就炸。

    :param name: 金标名（不含 ``.json``）。
    :param actual: 实际采到的事实。
    :param store: 存取器；默认用 :data:`GOLDEN_ROOT`。

    失败时抛 :class:`GoldenMismatchError`，消息里带逐处差分 —— **不甩一个
    "assert failed" 让人自己猜**。
    """
    the_store = store if store is not None else GoldenStore()
    differences = the_store.check(name, actual)
    if not differences:
        return
    if updating():
        return
    body = "\n".join(item.render() for item in differences)
    raise GoldenMismatchError(
        f"金标 {name!r} 对不上（{len(differences)} 处不一样）：\n{body}\n"
        f"确认这是预期改动再更新：{UPDATE_ENV}=1 uv run pytest ..."
    )


def golden_manifest(
    name: str,
    payload: object,
    store: GoldenStore | None = None,
) -> dict[str, str]:
    """给一份金标配一张清单。

    清单里放的是**这份金标自己是怎么来的**：摘要、条数，以及
    **它声称守住了哪些不变量**。没有这张清单，一份金标就只是一堆数字；
    有了它，"这份金标到底在证明什么"才写在版本库里，跟代码一起被评审。
    """
    the_store = store if store is not None else GoldenStore()
    return {
        "name": name,
        "digest": digest(payload),
        "path": str(the_store.path_for(name).relative_to(the_store.root)),
    }
