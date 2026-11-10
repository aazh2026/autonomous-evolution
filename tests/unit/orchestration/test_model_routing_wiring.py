"""TASK-P4-020 ④ —— 路由要真的落到执行器上。

设计依据：docs/tasks.md TASK-P4-020（多模型路由：按任务 / 成本 / 能力路由模型）、
R-018。

**为什么单独一节。** 路由本身是纯函数，测它只能证明"挑模型挑得对"。但**挑出
来的模型有没有被用上**是另一件事 —— 挑对了却没接线，那整套路由就是一段没人
调用的代码，而"没人调用"是那种不会报错的失败。

**这一层只盯三件事。**

1. **没配路由时行为不变** —— 编排层不给模型，执行器用它自己惯用的那个；
2. **配了路由时模型名进了** :class:`~executors.base.SessionConfig`；
3. **路由失败时不静默兜底** —— 挑不出来就报错，不悄悄换一个模型跑完。

**为什么第 3 条要单独钉。** 静默兜底是这类系统最常见的失败方式：跑完了、记
了账、看着一切正常，而"这次根本没过路由"这件事没有任何地方会提。**一次没人
路由过的执行，比一次明确报错的执行危险得多** —— 因为前者不会被看见。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.planning.roadmap import TaskType
from orchestration.loop_a import ModelResolverPort, build_loop_a_graph, run_loop_a
from orchestration.model_dispatch import ModelResolutionError
from tests.support.execution_loop import StubHandlers, ready_state


class _RecordingResolver:
    """打桩的模型路由器 —— 记下它被问了什么。"""

    def __init__(self, model_id: str = "strong-model") -> None:
        self._model_id = model_id
        self.calls: list[tuple[str, TaskType]] = []

    async def resolve(self, *, project_id: str, task_type: TaskType) -> str:
        self.calls.append((project_id, task_type))
        return self._model_id


class _RefusingResolver:
    """挑不出模型的路由器 —— 抛错，不兜底。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, TaskType]] = []

    async def resolve(self, *, project_id: str, task_type: TaskType) -> str:
        self.calls.append((project_id, task_type))
        raise ModelResolutionError(project_id)


async def _run(handlers: StubHandlers, resolver: ModelResolverPort | None) -> None:
    """带着（可能没有）路由跑一遍整图。

    **参数类型要写准。** 写成 `object` 的话，`replace(...)` 那里就传不出
    `ModelResolverPort | None`，mypy --strict 直接报错 —— 类型写宽了反而
    把合法的调用挡在门外。
    """
    graph = build_loop_a_graph(replace(handlers.handlers, model_resolver=resolver))
    await run_loop_a(graph, ready_state())


class TestTheRoutedModelReachesTheExecutor:
    """路由的结果要落到执行器上。"""

    async def test_without_a_resolver_no_model_is_named(self) -> None:
        """**没配路由就是没配** —— 编排层不替执行器挑模型。"""
        handlers = StubHandlers()
        await _run(handlers, None)
        assert handlers.executor.session_configs[0].model is None

    async def test_the_routed_model_reaches_the_session_config(self) -> None:
        """**挑出来的那个模型名要被带进去** —— 否则路由白挑。"""
        handlers = StubHandlers()
        await _run(handlers, _RecordingResolver("strong-model"))
        assert handlers.executor.session_configs[0].model == "strong-model"

    async def test_the_resolver_is_asked_about_this_project_and_task(self) -> None:
        """**问的是"这个项目的这个任务该谁干"** —— 不是全局一个模型。"""
        resolver = _RecordingResolver()
        await _run(StubHandlers(), resolver)
        assert resolver.calls == [("p-demo", TaskType.FEATURE)]

    async def test_a_refusing_resolver_stops_the_run(self) -> None:
        """**挑不出来就报错** —— 不悄悄换一个模型跑完。"""
        with pytest.raises(ModelResolutionError):
            await _run(StubHandlers(), _RefusingResolver())

    async def test_a_refusing_resolver_never_opens_a_session(self) -> None:
        """**没跑成的执行不该留下会话** —— 否则那半截活会被人当成跑过了。"""
        handlers = StubHandlers()
        with pytest.raises(ModelResolutionError):
            await _run(handlers, _RefusingResolver())
        assert handlers.executor.session_configs == []


class TestThePortShape:
    """端口的形状 —— 结构化类型靠方法名对齐。"""

    def test_the_stub_satisfies_the_port(self) -> None:
        """**打桩件满足端口** —— 说明端口的方法名没写错。"""
        assert isinstance(_RecordingResolver(), ModelResolverPort)

    def test_the_refusing_stub_satisfies_the_port(self) -> None:
        assert isinstance(_RefusingResolver(), ModelResolverPort)
