"""契约测试的参数化装置。

**为什么用 conftest 参数化，而不是写两份测试。** 契约测试的价值在于"两个
实现跑同一份断言"。手写两份等于两份断言会各自漂移 —— 改了一条忘了改另一条，
两个实现就"都通过"了，而契约已经变了。参数化把同一段断言跑两遍，漂移无从
发生。

**参数化到类而不是实例。** 每个用例拿到一个**全新**的适配器：共用实例会让
用例之间通过内部状态互相影响，那样"通过"靠的是运气而不是契约。
"""

from __future__ import annotations

import pytest

from executors.base import CodingAgentAdapter
from tests.unit.executors.doubles import InlineAdapter, SessionAdapter


@pytest.fixture(params=[InlineAdapter, SessionAdapter])
def adapter(request: pytest.FixtureRequest) -> CodingAgentAdapter:
    """待测适配器 —— 两个结构不同的实现跑同一套契约测试。"""
    factory: type[CodingAgentAdapter] = request.param
    return factory()
