"""状态序列化的单元测试（TASK-P1-050）。

设计依据：docs/tasks.md TASK-P1-050（"图状态可持久化（checkpoint）"）、
docs/design.md §5。

**这里验的是"存进去还能原样读出来"。** checkpoint 的价值全在这一点上：
存进去的是一份状态，读出来还得是**同一份**状态 —— 同样的类型、同样的值。
读出来的东西"看起来差不多"，checkpoint 就成了负资产：它让人以为崩溃之后
能接着跑，实际上接着跑的是一份走样的状态。

**白名单为什么是"算出来"的。** 手写清单早晚会漏，而漏掉的症状是"跑到一半
说某个类型不让反序列化" —— 跟当初新加的那个模型八竿子打不着。这里从状态
模式本身把类型图走一遍，所以新加模型时清单自动跟着长。

**白名单是被强制执行的。** 实测：``JsonPlusSerializer`` 传了显式白名单之后，
不在名单里的类型读回来会**降级成普通字典** —— 不报错，只是不再是那个类型了。
（这是最坏的一种失败：看起来没事。）所以白名单少一个类型，checkpoint 就会
**悄悄**变形。这条正是本文件存在的主要理由。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

from core.evolution.assessment import ProjectAssessment
from core.planning.roadmap import EvolutionRoadmap
from core.planning.stages import PipelineState, Stage
from orchestration.langgraph.serialization import (
    collect_state_types,
    state_allowlist,
    state_serializer,
)
from tests.support.decision_stages import (
    assessment,
    decision_outcome,
    discovery_result,
    gap_analysis,
    project_context,
    roadmap,
)


class Foreign(BaseModel):
    """一个不在状态里的模型 —— 用来试白名单的边界。

    **必须是模块级的。** 反序列化是按"模块名 + 类名"把类找回来的 —— 定义在
    函数体里的类，模块里根本没有这个名字，于是**即使在白名单里也找不回来**。
    这一点本身就说明了白名单的机制：它放行的不是一个类，而是"模块里的某个
    具名类"。
    """

    value: int


def _full_state() -> PipelineState:
    """一份五个产物都在的状态 —— 状态能装的最满的一种。"""
    return PipelineState(
        cycle_id="cycle-0007",
        context=project_context(),
        assessment=assessment(),
        gaps=gap_analysis(),
        discovery=discovery_result(),
        outcome=decision_outcome(),
        roadmap=roadmap(),
        completed_stages=(
            Stage.UNDERSTAND,
            Stage.DIAGNOSE,
            Stage.EXPLORE,
            Stage.DECIDE,
            Stage.PLAN,
        ),
    )


class TestTheAllowlistCoversTheState:
    """白名单要盖住状态里出现的每一个自定义类型。"""

    def test_every_reachable_type_is_in_the_allowlist(self) -> None:
        """**一个都不能少。** 少一个，那个类型读回来就降级成字典。"""
        reachable = collect_state_types(PipelineState)
        listed = set(state_allowlist())

        missing = {(item.__module__, item.__name__) for item in reachable} - listed

        assert missing == set()

    @pytest.mark.parametrize(
        ("module", "name"),
        [
            ("core.planning.stages", "PipelineState"),
            ("core.planning.stages", "ProjectContext"),
            ("core.planning.stages", "GapAnalysis"),
            ("core.planning.stages", "DecisionOutcome"),
            ("core.evolution.assessment", "ProjectAssessment"),
            ("core.evolution.candidate", "DiscoveryResult"),
            ("core.planning.roadmap", "EvolutionRoadmap"),
        ],
    )
    def test_the_important_types_are_listed(self, module: str, name: str) -> None:
        """这几个是链上每个阶段的产物 —— 漏掉哪一个，链就在那一段断掉。"""
        assert (module, name) in state_allowlist()

    def test_the_enums_are_listed_too(self) -> None:
        """枚举也是自定义类型 —— 它们同样要被原样读回来。

        **少了枚举，读回来的是字符串。** 而字符串在多数场合"也能用"，于是
        类型走样这件事会一路溜到最后才暴露。
        """
        listed = {name for _, name in state_allowlist()}

        assert "OverallStatus" in listed
        assert "DecisionState" in listed
        assert "Stage" in listed

    def test_the_allowlist_is_sorted(self) -> None:
        """排序不是洁癖 —— 清单要能被人 diff。"""
        assert list(state_allowlist()) == sorted(state_allowlist())


class TestTheAllowlistIsDerived:
    """白名单是从模式**算出来**的，不是手写的。"""

    def test_a_new_model_shows_up_in_the_allowlist(self) -> None:
        """给状态加一个新模型类型，白名单自动加上它。

        **这条是"手写清单会漏"的正面回答。** 手写清单时，新加模型要人记得
        去补 —— 而人会忘，忘了之后 checkpoint 读回来会静默变形。
        """

        class NewThing(BaseModel):
            name: str

        class WiderState(BaseModel):
            thing: NewThing

        listed = state_allowlist(WiderState)

        assert (NewThing.__module__, "NewThing") in listed

    def test_a_nested_model_is_reached(self) -> None:
        """嵌套的模型也要走到 —— 注解里套着注解。"""

        class Inner(BaseModel):
            value: int

        class Middle(BaseModel):
            inner: Inner

        class Outer(BaseModel):
            middle: Middle
            items: list[Inner]

        listed = state_allowlist(Outer)

        assert (Inner.__module__, "Inner") in listed
        assert (Middle.__module__, "Middle") in listed

    def test_the_allowlist_of_a_different_root_is_different(self) -> None:
        """换个根类型，清单跟着换 —— 它确实是从根算的。"""

        class Narrow(BaseModel):
            value: int

        assert state_allowlist(Narrow) != state_allowlist(PipelineState)


class TestTheRoundTrip:
    """存进去，读出来，还是同一份状态。"""

    def test_the_state_survives_the_round_trip(self) -> None:
        serializer = state_serializer()
        state = _full_state()

        kind, blob = serializer.dumps_typed(state)
        restored = serializer.loads_typed((kind, blob))

        assert restored == state

    def test_the_types_survive_too(self) -> None:
        """**值一样不够，类型也得一样。** 值一样而类型变了的状态，下游读它
        的方式就变了 —— 而那不会报错。
        """
        serializer = state_serializer()
        state = _full_state()

        kind, blob = serializer.dumps_typed(state)
        restored = serializer.loads_typed((kind, blob))

        assert isinstance(restored, PipelineState)
        assert isinstance(restored.assessment, ProjectAssessment)
        assert isinstance(restored.roadmap, EvolutionRoadmap)
        assert isinstance(restored.completed_stages[0], Stage)

    def test_the_decimals_stay_decimals(self) -> None:
        """``Decimal`` 不能变成 ``float``。

        **这是最容易被悄悄改掉的一个。** ``0.9`` 写成浮点再读回来是
        ``0.9000000000000000222...`` —— 比较时看着相等，累加之后差得离谱。
        而它是优先级评分里的因子。
        """
        serializer = state_serializer()
        state = _full_state()

        kind, blob = serializer.dumps_typed(state)
        restored = serializer.loads_typed((kind, blob))

        assert restored.outcome is not None
        decision = restored.outcome.decisions.decisions[0]
        assert isinstance(decision.factors.value, Decimal)
        assert decision.factors.value == Decimal("0.8")

    def test_the_tuples_stay_tuples(self) -> None:
        """元组不能变成列表。**顺序与不可变性都是语义的一部分。**"""
        serializer = state_serializer()
        state = _full_state()

        kind, blob = serializer.dumps_typed(state)
        restored = serializer.loads_typed((kind, blob))

        assert isinstance(restored.completed_stages, tuple)
        assert isinstance(restored.assessment.goal_gap, tuple)

    def test_an_empty_state_survives_too(self) -> None:
        """空状态也要能存 —— 刚开始跑、什么都还没产出的时候也要落 checkpoint。"""
        serializer = state_serializer()
        state = PipelineState(cycle_id="cycle-0007", context=project_context())

        kind, blob = serializer.dumps_typed(state)
        restored = serializer.loads_typed((kind, blob))

        assert restored == state
        assert restored.assessment is None


class TestTheAllowlistIsLoadBearing:
    """**白名单少一个类型，checkpoint 就会悄悄变形。**

    这一节是整个文件存在的理由：上面那些"往返一致"的用例，只有在白名单
    **完整**时才成立。而白名单完整与否，在这里是被**实测**的。
    """

    def test_a_type_outside_the_allowlist_comes_back_degraded(self) -> None:
        """不在白名单里的模型，读回来是**普通字典** —— 不是模型。

        **不报错，只是不再是那个类型了。** 这就是"猜错类型的那份 checkpoint
        比没有 checkpoint 更糟"的机制：它看起来好好的。
        """

        serializer = JsonPlusSerializer(allowed_msgpack_modules=(("decimal", "Decimal"),))

        kind, blob = serializer.dumps_typed(Foreign(value=1))
        restored = serializer.loads_typed((kind, blob))

        # 顺序有意义：先问"它不是那个模型吗"，再问"它是字典吗"。
        assert not isinstance(restored, Foreign)
        assert isinstance(restored, dict)

    def test_the_same_type_survives_when_it_is_listed(self) -> None:
        """同一份数据，只要在白名单里，就还是那个类型。

        **两条用例放在一起才说得清问题。** 单独看第一条会以为"msgpack 就是
        这么存的"；单独看第二条会以为"白名单只是个摆设"。
        """

        serializer = JsonPlusSerializer(allowed_msgpack_modules=((Foreign.__module__, "Foreign"),))

        kind, blob = serializer.dumps_typed(Foreign(value=1))
        restored = serializer.loads_typed((kind, blob))

        assert isinstance(restored, Foreign)
        assert restored.value == 1

    def test_the_state_serializer_would_degrade_without_the_allowlist(self) -> None:
        """把白名单摘掉，本项目的状态就读不回来了。

        **这一条把前两条与本项目连起来。** 前两条说的是"机制"；这一条说的
        是"我们这个状态**正好**会被这个机制咬到" —— 所以白名单必须算出来，
        不能省。
        """
        without_allowlist = JsonPlusSerializer(allowed_msgpack_modules=None)

        kind, blob = without_allowlist.dumps_typed(_full_state())
        restored = without_allowlist.loads_typed((kind, blob))

        assert not isinstance(restored, PipelineState)


class TestPickleIsNotUsed:
    """**不用 pickle。**"""

    def test_the_serializer_does_not_fall_back_to_pickle(self) -> None:
        """pickle 能存任何东西，但读回来会**执行任意代码**。

        checkpoint 是"从库里读出来"的字节 —— 库里那份要是被人改过，读它就
        等于执行了改它的人写的代码。msgpack 只重建白名单里的类型，不会有
        这个问题。
        """
        assert state_serializer().pickle_fallback is False

    def test_the_state_is_stored_as_msgpack(self) -> None:
        """存下去的类型标签是 ``msgpack``，不是 ``pickle``。"""
        kind, _ = state_serializer().dumps_typed(_full_state())

        assert kind == "msgpack"
