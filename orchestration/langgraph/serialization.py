"""决策图状态的序列化（msgpack 白名单）。

设计依据：docs/tasks.md TASK-P1-050（"图状态可持久化（checkpoint）"）。

**为什么需要一份白名单。** LangGraph 的 checkpoint 序列化器（msgpack）在存
Pydantic 模型时，会把"这个值是哪个模块的哪个类"一起写进去；读回来时它要**按
那个信息把对象重建出来**。这一步就是**反序列化任意类型** —— 所以序列化器默认
只放行一小撮标准库类型，其余的要么报警告、要么直接拒绝。

而本项目的状态里装的全是我们自己的 Pydantic 模型（``ProjectAssessment``、
``GapAnalysis``、``EvolutionRoadmap``……）。不把它们列进白名单，状态就读不回来
—— 而"读不回来的 checkpoint"等于没有 checkpoint。

**为什么白名单是"算出来"的，不是"手写"的。** 手写一份清单，早晚会漏 —— 新加
一个领域模型，状态里多了它，checkpoint 却读不回来了，而症状是"跑到一半说
某个类型不让反序列化"，跟新加的那个模型八竿子打不着。所以这里从**状态模式本身**
出发，把所有够得着的自定义类型走一遍，自动得出清单。

**清单只多不少。** 走的是类型注解图，注解里出现过的都算上 —— 宁可多放行一个
自己项目里的类型，也不要少一个。少一个是"跑不起来"，多一个只是"多认得几个
自己的类"。
"""

from __future__ import annotations

import enum
from collections.abc import Iterator
from typing import get_args, get_origin

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

from core.planning.stages import PipelineState


def _annotation_types(annotation: object) -> Iterator[type[object]]:
    """把一个类型注解拆成它可能装着的那些**类**。

    ``Optional[X]`` / ``X | None`` / ``list[X]`` / ``tuple[X, ...]`` /
    ``dict[str, X]`` 都得拆开看 —— 只看最外层的话，``list[ProjectAssessment]``
    里的那个模型就漏了。
    """
    origin = get_origin(annotation)
    if origin is None:
        if isinstance(annotation, type):
            yield annotation
        return
    for argument in get_args(annotation):
        yield from _annotation_types(argument)


def collect_state_types(root: type[BaseModel]) -> frozenset[type[object]]:
    """状态里够得着的所有自定义类型。

    从 ``root`` 出发，沿着字段注解一层层往下走。**模型要递归，枚举只要收。**
    """
    found: set[type[object]] = set()
    seen: set[type[object]] = set()
    pending: list[type[BaseModel]] = [root]

    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        found.add(model)
        for field in model.model_fields.values():
            for candidate in _annotation_types(field.annotation):
                if issubclass(candidate, BaseModel):
                    pending.append(candidate)
                elif issubclass(candidate, enum.Enum):
                    found.add(candidate)

    return frozenset(found)


def state_allowlist(root: type[BaseModel] = PipelineState) -> tuple[tuple[str, str], ...]:
    """白名单：``(模块名, 类名)`` 二元组。

    **按模块+类名，而不是只按模块。** 只按模块放行的话，加进来一个模块就等于
    把整个模块都放了 —— 而模块里可能有不该被反序列化的东西。精确到类，放行的
    范围正好是状态里真正出现过的那些类型。
    """
    return tuple(sorted((item.__module__, item.__name__) for item in collect_state_types(root)))


def state_serializer(root: type[BaseModel] = PipelineState) -> JsonPlusSerializer:
    """给状态用的序列化器。

    **不用 pickle。** pickle 能存任何东西，但读回来会执行任意代码 —— 而
    checkpoint 是"从库里读出来"的东西，库里那份字节如果被人改过，读它就等于
    执行了别人的代码。msgpack 只重建白名单里的类型。
    """
    return JsonPlusSerializer(allowed_msgpack_modules=state_allowlist(root))


__all__ = [
    "collect_state_types",
    "state_allowlist",
    "state_serializer",
]
