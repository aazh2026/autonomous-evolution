"""取消传播链 —— 任务层 → Adapter 层 → 进程层 → 容器层。

设计依据：docs/design.md §6（"取消必须能传递到执行器和 Sandbox"）、
docs/tasks.md TASK-P1-033（R-096 / I-02：每层确认收到后才向下一层推进）、
§2 隐含工程约束清单（取消传播四层链）。

**取消不是一个动作，是一条链。** 因为一次执行留下的东西不止一样：执行器
的会话、它背后的进程、装它的容器。只停最里面那个（会话），容器还在跑，
里面可能还挂着半死不活的东西；只停最外面那个（容器），执行器永远等不到
确认，链只能报"取消失败"—— 而容器其实早就没了。四层各管一件事，从内到
外依次收干净。

**每一层的动作是"让它停"，确认是"它真的不在跑了"。** 这两件事分开，是因为
"请求发出去了"与"停下来了"之间的差别，正是取消链存在的全部理由
（I-02）。链不问"你收到了吗"，它问"你还在跑吗"。

**推进规则：每层给出结论后才走下一层，但一层失败不挡住后面的层。**

这两半是一件事的两面，缺一不可：

- **先有结论再推进** —— 不许在上一层还悬着的时候就去动下一层。反过来做
  的后果很具体：容器先停，执行器就再也给不出确认，于是链报"取消失败"，
  而容器早就没了 —— 结论与事实相反。
- **失败不挡路** —— 一层失败之后，后面的层**仍然要试**。因为"这一层没
  停下来"绝不等于"后面的层不用管"：容器还在跑，它占着内存、占着端口、
  占着工作空间，而工作空间是**别的任务也要用**的。跳过它等于把一次
  取消变成一次泄漏。

所以失败记在**结论**里，不挡**动作**。链报"已取消"的条件是**每一层都确认**
（:attr:`~core.task.cancellation.CancellationReport.cancelled`），少一层
都不算。

**每层都有期限，所以链一定会收敛。** 一层超时记 ``FAILED``，然后继续往
下走。整条链的时间上限是"层数 × 每层期限"—— 有限，这是"取消不会挂死"
这句话的全部含义。

**顺序固定，且不可配置。** 任务 → Adapter → 进程 → 容器，**从内到外**。
可配置就会有人把容器排在前面，而那正是上面说的那种"结论与事实相反"。

**这一层不 import 执行器。** 链通过 :mod:`core.task.cancellation` 里的
端口跟执行器说话（那边是"怎么问"的协议），所以 ``harness/`` 不必依赖
``executors/`` —— 方向仍然是"适配层 → 领域"（待裁决 13）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final, Protocol

from core.task.cancellation import (
    CancellationReport,
    CancellationTarget,
    ContainerStopper,
    Interrupter,
    LayerName,
    LayerOutcome,
    LayerState,
    ProcessStopper,
    StopReport,
)

#: 每层的期限（秒）。
#:
#: 刻意小于任务书 T1 ① 说的"30 秒" —— 那是**整条链**的预算，四层各占一
#: 份的话，每层给 30 秒就等于给 120 秒，那条断言就永远过不了。
DEFAULT_LAYER_TIMEOUT_SECONDS: Final[float] = 10.0


class CancellationLayer(Protocol):
    """取消链上的一层。

    :meth:`stop` 的**后置条件**是"这一类资源已经不在跑了" —— 链据此判断
    这一层有没有完成。返回 :class:`~core.task.cancellation.LayerOutcome`，
    而不是 ``None`` 或布尔值：理由要跟结论一起留下来。
    """

    @property
    def name(self) -> LayerName:
        """这一层是哪一层。"""

    async def stop(self, target: CancellationTarget) -> LayerOutcome:
        """让这一层认领的资源停下来，并回报结果。"""


@dataclass(frozen=True, slots=True)
class TaskLayer:
    """任务层 —— 取消请求的受理处。

    这一层没有"动作"：它要做的只是让请求**有了一个确定的落点**。把它显式
    放进链里，是为了链跑完之后能回答"这次取消到底有没有被受理"—— 而这条
    记录在报告里是**第一条**：受理之前发生的一切都还没算数。
    """

    @property
    def name(self) -> LayerName:
        return LayerName.TASK

    async def stop(self, target: CancellationTarget) -> LayerOutcome:
        return LayerOutcome(
            LayerName.TASK,
            LayerState.CONFIRMED,
            f"取消请求已受理（任务 {target.task_id}，会话 {target.session_id}）",
        )


@dataclass(frozen=True, slots=True)
class AdapterLayer:
    """Adapter 层 —— 让执行器自己停下来。

    问的是 :meth:`~executors.base.CodingAgentAdapter.interrupt`，因为只有
    执行器知道自己的会话是什么状态。回执里的状态按**字符串**比对，而不是
    import 执行器的枚举 —— 这样 ``harness/`` 不必依赖 ``executors/``。

    **不认识的状态一律按"没确认"处理。** 执行器要是换了一套状态取值，我
    们宁可说"没停干净"也不能说"停了"（fail-closed）。
    """

    interrupter: Interrupter

    @property
    def name(self) -> LayerName:
        return LayerName.ADAPTER

    async def stop(self, target: CancellationTarget) -> LayerOutcome:
        ack = await self.interrupter.interrupt(target.session_id)
        state = str(ack.state)
        detail = ack.detail
        if state == "confirmed":
            return LayerOutcome(LayerName.ADAPTER, LayerState.CONFIRMED, detail)
        if state == "refused":
            return LayerOutcome(LayerName.ADAPTER, LayerState.REFUSED, detail)
        if state == "requested":
            return LayerOutcome(
                LayerName.ADAPTER,
                LayerState.UNCONFIRMED,
                f"执行器只回了「请求已发出」，没有确认停下：{detail}",
            )
        return LayerOutcome(
            LayerName.ADAPTER,
            LayerState.UNCONFIRMED,
            f"执行器回了不认识的状态 {state!r}，不能当作已停止：{detail}",
        )


@dataclass(frozen=True, slots=True)
class ProcessLayer:
    """进程层 —— 确保执行器背后那个进程真的不在运行。

    这一层与 Adapter 层**不是同一件事**。Adapter 层问的是"执行器说你停了
    吗"，进程层问的是"那个进程还在吗"。分开的理由：执行器可能只停了它
    管的**主**进程，而它 spawn 出来的子进程还在跑 —— 那时执行器报的是
    "已停止"，而工作空间里还有东西在动。
    """

    stopper: ProcessStopper

    @property
    def name(self) -> LayerName:
        return LayerName.PROCESS

    async def stop(self, target: CancellationTarget) -> LayerOutcome:
        report = await self.stopper.ensure_process_stopped(target.session_id)
        state = LayerState.CONFIRMED if report.stopped else LayerState.FAILED
        return LayerOutcome(LayerName.PROCESS, state, report.detail)


@dataclass(frozen=True, slots=True)
class SandboxLayer:
    """容器层 —— 确保沙箱容器不再运行。

    **没有容器时报 ``CONFIRMED``。** 后置条件是"沙箱容器不在运行"，没有
    容器时这个条件**天然成立** —— 报失败才是撒谎（"没有容器"不是"容器没
    停掉"）。
    """

    stopper: ContainerStopper

    @property
    def name(self) -> LayerName:
        return LayerName.SANDBOX

    async def stop(self, target: CancellationTarget) -> LayerOutcome:
        container_id = target.container_id
        if container_id is None:
            return LayerOutcome(LayerName.SANDBOX, LayerState.CONFIRMED, "这次执行没有沙箱容器")
        report = await self.stopper.ensure_container_stopped(container_id)
        state = LayerState.CONFIRMED if report.stopped else LayerState.FAILED
        return LayerOutcome(LayerName.SANDBOX, state, report.detail)


class ContainerLauncher(Protocol):
    """能停容器、也能回答"它还在不在跑"的启动器。

    **为什么是协议而不是 :class:`~harness.sandbox.launcher.SandboxLauncher`
    本身。** 取消链只用到启动器的两个方法，而"怎么停容器"可以有别的实现
    —— 别的容器运行时、别的测试替身。写成协议，链就只依赖它真正用到的那
    两个方法。

    :meth:`container_running` 返回三态（``bool | None``）不是两态：``None``
    是"问不话"，与"没在跑"必须分开。
    """

    def stop(self, container_id: str, *, grace_seconds: int = ...) -> None:
        """停掉容器。"""

    def container_running(self, container_id: str) -> bool | None:
        """容器还在不在跑。问不话时返回 ``None``。"""


@dataclass(frozen=True, slots=True)
class LauncherContainerStopper:
    """用 :class:`~harness.sandbox.launcher.SandboxLauncher` 停容器。

    :attr:`grace_seconds` 是 ``docker stop`` 给容器自己收尾的时间。刻意
    比启动器默认的 30 秒短：取消是**用户等着的**操作，让容器慢慢收尾
    等于让用户多等。收尾不干净也不要紧 —— 后面还有 ``rm -f``。
    """

    launcher: ContainerLauncher
    grace_seconds: int = 5

    async def ensure_container_stopped(self, container_id: str) -> StopReport:
        await asyncio.to_thread(self.launcher.stop, container_id, grace_seconds=self.grace_seconds)
        running = await asyncio.to_thread(self.launcher.container_running, container_id)
        if running is None:
            # 没问上话 —— 不是"没在跑"，是"不知道"。说成"停了"就是撒谎。
            return StopReport(
                stopped=False,
                detail=f"停掉了容器 {container_id}，但没能确认它不再运行（docker 不可用）",
            )
        if running:
            return StopReport(stopped=False, detail=f"容器 {container_id} 停不下来")
        return StopReport(stopped=True, detail=f"容器 {container_id} 已停止")


@dataclass(frozen=True, slots=True)
class NoProcessStopper:
    """给"没有独立进程"的执行器用的进程层。

    有些执行器不 fork 子进程 —— 它就在当前进程里跑（内联执行）。那时
    进程层的后置条件**天然成立**：没有独立进程，也就没有独立进程还在跑。

    报 ``CONFIRMED`` 而不是"跳过"，是为了让四层的形状保持一致 —— 报告里
    每一层都有一句话，事后对账时不会缺一条。
    """

    detail: str = "这个执行器没有独立的子进程"

    async def ensure_process_stopped(self, session_id: str) -> StopReport:
        return StopReport(stopped=True, detail=self.detail)


class CancellationChain:
    """取消传播链。

    :param layers: 依次执行的层。**顺序就是语义** —— 链不会自己排序。
    :param layer_timeout_seconds: 每层的期限。一层超时记 ``FAILED`` 然后
        继续往下走，所以整条链的时间上限是"层数 × 每层期限"。
    """

    def __init__(
        self,
        layers: tuple[CancellationLayer, ...],
        *,
        layer_timeout_seconds: float = DEFAULT_LAYER_TIMEOUT_SECONDS,
    ) -> None:
        self._layers = layers
        self._layer_timeout = layer_timeout_seconds

    @property
    def layers(self) -> tuple[CancellationLayer, ...]:
        """链上的层。"""
        return self._layers

    async def cancel(self, target: CancellationTarget) -> CancellationReport:
        """跑一遍整条链。

        **不会抛异常** —— 每一层的失败都记进报告里。取消失败是一个**结论**，
        不是一个崩溃：调用方需要知道"哪一层没停下来"才能处置，而一个异常
        会把这条信息丢掉。
        """
        outcomes: list[LayerOutcome] = []
        for layer in self._layers:
            outcomes.append(await self._stop_layer(layer, target))
        return CancellationReport(target=target, outcomes=tuple(outcomes))

    async def _stop_layer(
        self, layer: CancellationLayer, target: CancellationTarget
    ) -> LayerOutcome:
        """跑一层，把它变成一条结论。

        **超时与报错都变成 ``FAILED``，不变成异常。** 这一层失败不代表
        后面的层不用试 —— 容器还在跑就是还在跑。
        """
        try:
            return await asyncio.wait_for(layer.stop(target), timeout=self._layer_timeout)
        except TimeoutError:
            return LayerOutcome(
                layer.name,
                LayerState.FAILED,
                f"这层在 {self._layer_timeout:.0f} 秒内没有给出结论",
            )
        except Exception as exc:
            # 失败要变成**结论**，不是异常 —— 见上面这段的说明。
            return LayerOutcome(layer.name, LayerState.FAILED, f"这层报错：{exc}")


@dataclass(frozen=True, slots=True)
class NoContainerStopper:
    """给"没有容器"的场景用的容器层。

    与 :class:`NoProcessStopper` 同理：没有容器时后置条件天然成立。
    """

    detail: str = "这次执行没有沙箱容器"

    async def ensure_container_stopped(self, container_id: str) -> StopReport:
        return StopReport(stopped=True, detail=self.detail)


@dataclass(frozen=True, slots=True)
class ChainResourceReclaimer:
    """把取消链包装成"回收器"。

    **为什么要有这层包装。** 超时处置（``executors/recovery.py``）要"把这次
    执行留下的资源收干净"，而收干净这件事**就是**取消链在做的事 —— 只是
    动机不同：取消是用户提的要求，回收是处置，两者的**后置条件**完全一样
    （每一类资源都不在了）。

    执行器那一侧不能直接 import 沙箱（待裁决 13），所以那边把"怎么回收"
    写成一个端口，这边提供一个**结构上满足**它的实现。两边不需要共享同一
    份声明 —— 需要共享的只有 :mod:`core.task.cancellation` 里的那些词汇。

    **它跑的就是同一条链**，所以四层的顺序与"失败不挡路"那些规矩一条都
    不打折。
    """

    chain: CancellationChain

    async def reclaim(self, target: CancellationTarget) -> CancellationReport:
        """收干净，并如实回报每一层的结论。"""
        return await self.chain.cancel(target)


def build_cancellation_chain(
    *,
    interrupter: Interrupter,
    process_stopper: ProcessStopper | None = None,
    container_stopper: ContainerStopper | None = None,
    layer_timeout_seconds: float = DEFAULT_LAYER_TIMEOUT_SECONDS,
) -> CancellationChain:
    """造一条标准链。

    **四层齐全，顺序固定。** ``process_stopper`` 与 ``container_stopper``
    缺省时用"这一类资源本来就没有"的实现 —— 那样这一层报 ``CONFIRMED``，
    而不是让链缺一层。链缺一层看起来更省事，但事后对账时"这层怎么没有
    记录"要靠猜。

    :param interrupter: 执行器。任何一个实现
        :meth:`~executors.base.CodingAgentAdapter.interrupt` 的对象都算。
    :param process_stopper: 停进程的一方。缺省用
        :class:`NoProcessStopper`。
    :param container_stopper: 停容器的一方。缺省用
        :class:`NoContainerStopper`。
    """
    return CancellationChain(
        (
            TaskLayer(),
            AdapterLayer(interrupter),
            ProcessLayer(process_stopper or NoProcessStopper()),
            SandboxLayer(container_stopper or NoContainerStopper()),
        ),
        layer_timeout_seconds=layer_timeout_seconds,
    )


__all__ = [
    "DEFAULT_LAYER_TIMEOUT_SECONDS",
    "AdapterLayer",
    "CancellationChain",
    "CancellationLayer",
    "ChainResourceReclaimer",
    "ContainerLauncher",
    "LauncherContainerStopper",
    "NoContainerStopper",
    "NoProcessStopper",
    "ProcessLayer",
    "SandboxLayer",
    "TaskLayer",
    "build_cancellation_chain",
]
