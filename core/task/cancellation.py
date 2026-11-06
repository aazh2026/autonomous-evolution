"""取消传播的词汇表 —— 四层链共享的类型与端口。

设计依据：docs/design.md §6（"取消必须能传递到执行器和 Sandbox"）、
docs/tasks.md TASK-P1-033（R-096 / I-02：每层确认收到才推进）、
§2 的隐含工程约束清单（取消传播四层链）。

**为什么这些类型住在 ``core/``。** 取消链的一头是**执行器**
（``executors/``），另一头是**沙箱**（``harness/``）。链要同时驱动两边，
而这两边**互不 import** —— 沙箱不该知道执行器是什么，执行器也不该知道
沙箱的存在（待裁决 13 与 ``tests/unit/harness/test_layering.py`` 守着这条
边界）。

于是"链怎么跟两边说话"这个约定必须住在**第三处**：``core/``。这不是为了
省事 —— 放在任何一边，那一边就得依赖另一边；放在 ``core/``，方向就仍然是
"适配层 → 领域"，两边都只依赖领域。这与 :mod:`core.task.outcome` 是同一个
道理：判定规则住在领域，执行结果住在执行器，两边靠一份协议接上。

**端口只声明"怎么问"，不声明"怎么答"。** 怎么停下来是各自的事：执行器
知道自己的进程怎么杀，沙箱知道容器怎么停。这条边界在 TASK-P1-031 的
codex 骨架上已经显过形 —— 底层协议各不相同，而系统级契约不变。取消也是
一样：链认的是"后置条件成立与否"，不认"你是怎么停的"。

**每一层的后置条件是"这一类资源已经不在跑了"。** 不是"收到请求了"，也不
是"尝试过了" —— 是**真的不在跑了**。只有这样，"已取消"这个结论才是关于
**事实**的，而不是关于**流程**的：流程走完了但东西还在跑，那不叫取消。

**四层各自的后置条件**：

1. 任务层 —— 取消请求已被受理；
2. Adapter 层 —— 执行器确认它已经停下来了；
3. 进程层 —— 执行器背后那个进程已经不在运行；
4. 容器层 —— 沙箱容器已经不在运行。

**"确认"与"收到"不是一件事。** :class:`LayerState` 因此有四种状态而不是
两种：``CONFIRMED``（后置条件成立）、``REFUSED``（这一层明确停不下来）、
``FAILED``（试了但没成 —— 超时或报错）、``UNCONFIRMED``（只回了"请求已
收到"，没给出结论）。把后两种都算成"确认"，就是拿"问过了"冒充"停住了"。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class LayerName(StrEnum):
    """取消链上的一层。

    顺序固定：任务 → Adapter → 进程 → 容器。**先内后外** —— 先让执行器
    自己收尾（它可能顺手把工作空间整理好），再强制停进程，最后才动容器。
    反过来做的话，容器先停，执行器就再也给不出确认了 —— 那时候链只能报
    "取消失败"，而容器其实早就没了。
    """

    #: 任务层 —— 取消请求的受理处。
    TASK = "task"
    #: Adapter 层 —— 执行器确认自己停下来了。
    ADAPTER = "adapter"
    #: 进程层 —— 执行器背后的进程不在运行了。
    PROCESS = "process"
    #: 容器层 —— 沙箱容器不在运行了。
    SANDBOX = "sandbox"


class LayerState(StrEnum):
    """一层取消的结果。

    四态而不是两态，因为"确认停住"与"确认没停住"之间还有两个真实的
    情况：**停不下来**（执行器拒绝、容器停不掉）与**没结论**（这一层只
    回了"请求已收到"）。把它们都塞进"失败"，就分不清"再试一次可能行"与
    "它根本不想停"。
    """

    #: 后置条件成立 —— 这一类资源确实已经不在跑了。
    CONFIRMED = "confirmed"
    #: 这一层明确表示停不下来（执行器拒绝中断、容器拒绝停止）。
    REFUSED = "refused"
    #: 试了，但没成（超时、报错）。
    FAILED = "failed"
    #: 没有给出结论 —— 只回了"请求已收到"。
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True)
class LayerOutcome:
    """一层取消的结果与理由。

    :attr:`detail` 记的是**为什么** —— 不带理由的"失败"在人面前只是一句
    没有线索的结论，而取消失败最需要线索（是没停下来，还是没问上话）。
    """

    layer: LayerName
    state: LayerState
    detail: str = ""


@dataclass(frozen=True, slots=True)
class StopReport:
    """ "某个东西停了没有"的回执。

    :attr:`stopped` 是**事实**：它停了就是停了。:attr:`detail` 是给人看的
    线索（退出码是什么、docker 说什么）。

    **不叫 ``stopped: bool | None``** —— "没停"与"没问上话"必须分开。问不
    上话的时候说"没停"，会把"我们没停掉它"与"它本来就没在跑"混成一句
    话，而这两件事的后果完全不同。
    """

    stopped: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CancellationTarget:
    """要取消的是哪一次执行。

    :attr:`container_id` 可以是 ``None`` —— 不是每一次执行都在沙箱容器里
    跑。没有容器时容器层的后置条件**天然成立**（没有容器在跑），所以它
    报 ``CONFIRMED`` 而不是"失败"。
    """

    task_id: str
    session_id: str
    container_id: str | None = None


@dataclass(frozen=True, slots=True)
class CancellationReport:
    """一次取消传播的完整结果。

    :attr:`outcomes` 按链的顺序记下每一层的结论 —— **四层各一条**，包括
    任务层。留痕是为了事后能回答"到底哪一层没停下来"，而不是只得到一个
    说不清缘由的"取消失败"。
    """

    target: CancellationTarget
    outcomes: tuple[LayerOutcome, ...]

    @property
    def cancelled(self) -> bool:
        """是不是**每一层**都确认了。

        只有全部确认才算取消。少一层就是没取消 —— 因为那意味着有一类资源
        还在跑，而"还有东西在跑"与"已经取消"不能同时成立。

        空的报告不算取消（fail-closed）：没跑过链就不能说取消过。
        """
        if not self.outcomes:
            return False
        return all(outcome.state is LayerState.CONFIRMED for outcome in self.outcomes)

    @property
    def unfinished(self) -> tuple[LayerOutcome, ...]:
        """没确认的那些层。"""
        return tuple(
            outcome for outcome in self.outcomes if outcome.state is not LayerState.CONFIRMED
        )


class InterruptionAck(Protocol):
    """执行器对取消请求的回执。

    **刻意只声明两个成员。** 链需要的是"状态"与"理由"，不需要知道回执是
    哪个类 —— :class:`~executors.base.CancellationAck` 结构上满足这个协
    议，于是 ``harness/`` 不必 import ``executors/``（那会让方向反过来：
    沙箱去依赖执行器）。
    """

    @property
    def state(self) -> str:
        """取消状态。取值与 :class:`~executors.base.CancellationState` 一致。"""

    @property
    def detail(self) -> str:
        """理由。"""


class Interrupter(Protocol):
    """能接受取消请求的执行器。"""

    async def interrupt(self, session_id: str) -> InterruptionAck:
        """中断一次执行，并给出确认。"""


class ProcessStopper(Protocol):
    """能停掉执行器背后那个进程的一方。

    实现者是**拥有那个进程的执行器** —— 别人替它杀进程，它无从知道。
    """

    async def ensure_process_stopped(self, session_id: str) -> StopReport:
        """确保这个会话背后的进程已经不在运行。"""


class ContainerStopper(Protocol):
    """能停掉沙箱容器的一方。"""

    async def ensure_container_stopped(self, container_id: str) -> StopReport:
        """确保这个容器已经不在运行。"""


__all__ = [
    "CancellationReport",
    "CancellationTarget",
    "ContainerStopper",
    "Interrupter",
    "InterruptionAck",
    "LayerName",
    "LayerOutcome",
    "LayerState",
    "ProcessStopper",
    "StopReport",
]
