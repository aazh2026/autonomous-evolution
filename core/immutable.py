"""只读容器。

领域模型大量使用 Pydantic 的 ``frozen=True``。它禁止**属性赋值**，但不禁止
**就地修改容器字段** —— 而治理相关的数据恰好都以映射的形式存在
（自主权规则、项目环境配置）。这两者放在一起就是一个洞：

.. code-block:: python

    policy.rules[Operation.MERGE_TO_MAIN] = OperationRule(L4, AUTONOMOUS)

这行代码改掉了正在生效的权限，却不碰外部配置、不留审批痕迹。
design.md §7 / R-102~R-104 要求"策略与预算是外部配置 + 变更需审批"，
这条边界不能靠约定维持。

所以治理数据一律用这里的 :class:`FrozenDict` 承载 —— 不是靠代码评审保证
"大家记得别改"，而是靠类型保证"改不了"。

**为什么继承 ``dict`` 而不是用 ``MappingProxyType``**：
``MappingProxyType`` 无法被 Pydantic 序列化（``model_dump`` 抛
``PydanticSerializationError``），而治理数据要随项目落库
（``project.policy``、``project.environment`` 列）。
"""

from __future__ import annotations

from typing import NoReturn, TypeVar

#: :class:`FrozenDict` 的类型参数。
K = TypeVar("K")
V = TypeVar("V")


class FrozenDict(dict[K, V]):
    """不可就地修改的字典。

    覆盖**全部** mutator，而不是只挡 ``__setitem__`` —— ``dict.update``、
    ``dict.pop`` 这些是 C 实现的，不会走 ``__setitem__``，只挡一个等于没挡。
    """

    def _immutable(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise TypeError(
            "该配置是只读的：修改必须走外部配置 + 审批，不能在进程内就地改写"
            "（见 docs/tasks.md TASK-P1-011 / TASK-P1-013）"
        )

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


__all__ = ["FrozenDict"]
