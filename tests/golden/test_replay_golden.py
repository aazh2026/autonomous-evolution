"""编排层金标 —— 一整轮演进的**形状**，存下来。

**这一层与负金标矩阵是两种东西。** 矩阵验的是"坏的时候会不会失败关闭"；
这一层验的是"好的时候，这一轮到底长什么样"。

**为什么编排层需要一份存下来的期望产物。** 循环 B 一次要走十五个节点 ——
走过哪些、什么顺序、决策是什么、派了哪些任务、结论是什么、下一轮的差距
是什么。**这些事实散在十几个断言里，就没有人能一眼看出"这一轮变了什么"。**
存成一份产物，PR 的 diff 里就能直接看到：多了哪个节点、少了哪条决策、任务
从 1 个变成 0 个、下一轮的差距换没换。

**四条路，四份金标。**

1. ``loop_b_worth_doing`` —— 值得做：走完十五个节点，派了一个任务。
2. ``loop_b_deferred`` —— 不值得做：走到"记录/推迟/拒绝"就拐弯，**一个任务
   都不派**，但知识照样更新、下一轮的差距照样算。
3. ``loop_b_vetoed`` —— 决策是 ``record``，审查却否决了：任务同样不派，而且
   **否决的理由被记进路线图** —— 不做也要说清楚为什么。
4. ``continuous_rounds`` —— 连续多轮怎么跑、撞到预算墙怎么停下来：
   **停下来是一个结论，不是一个异常**；撞墙那一笔**不记进账**，但"被拦过"
   这件事留在结论里。

**金标里没有时间戳、没有 uuid。** 它们天生不可重复 —— 收进金标就等于给金标
塞了一个永远会变的字段，于是金标很快就变成噪声。

**金标里记着决策的理由 —— 那是文案。** 改了文案要连金标一起改，于是那次
改动在 PR 的 diff 里看得见。**这正是要的效果**：这个系统的主张是"每一步都要
说得出为什么"，那么"为什么"就该是被存下来、被比对的东西，而不是散落在
临时日志里的话。

跑法::

    uv run pytest tests/golden/test_replay_golden.py -v

    # 改了预期行为之后重新生成
    ASES_GOLDEN_UPDATE=1 uv run pytest tests/golden/test_replay_golden.py -v
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from core.evolution.evolution_loop import EvolutionPlan, LoopBState
from core.metrics.cost import CostAmount
from core.planning.roadmap import DecisionApproval, TaskDraft, TaskType
from core.policy.budget import Budget
from core.policy.resource import ResourceGate, ResourceUsage
from orchestration.continuous import ContinuousRunner
from orchestration.loop_b import build_loop_b_graph, run_loop_b
from tests.golden.support import GoldenStore, assert_golden
from tests.support.evolution_loop import (
    TASK_KEY,
    StubHandlers,
    defer_all,
    demo_candidate,
    initial_state,
)

#: 固定时刻 —— **不取"现在"**，否则金标会随跑的时刻变化。
_START: Final[datetime] = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

#: 四份金标的名字 —— **一处列出**，免得生成了一份就忘了另外三份。
GOLDEN_NAMES: Final[tuple[str, ...]] = (
    "loop_b_worth_doing",
    "loop_b_deferred",
    "loop_b_vetoed",
    "continuous_rounds",
)


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------


def _cycle_facts(stub: StubHandlers, state: LoopBState) -> dict[str, object]:
    """把一轮的事实采成一份能存下来的字典。

    **只采"从外面能读到、而且读出来就说明问题"的那些。** 不采内部字段、不采
    对象地址、不采时间戳 —— 那些进了金标就再也静不下来。

    **顺序敏感的地方不加 ``[]`` 后缀**（走过的节点、执行次序）；**集合的
    地方加**（下一轮还差哪些）。这两者的区别是金标能不能活下去的关键：
    把顺序敏感的东西当集合比，行为变了也不报；把集合当顺序比，无关的排序
    抖动会伪装成行为变化。
    """
    decisions = state.decisions
    roadmap = state.roadmap
    execution = state.execution
    outcome = state.outcome
    knowledge = state.knowledge
    next_gaps = state.next_gaps

    return {
        "worth_doing": state.worth_doing,
        "visited_nodes": [str(node) for node in state.visited_nodes],
        "decisions": [
            {
                "id": item.id,
                "candidate_id": item.candidate_id,
                "decision": item.decision.value,
                "rationale": item.rationale,
                "requested_analysis": [
                    {"kind": analysis.kind.value, "description": analysis.description}
                    for analysis in item.requested_analysis
                ],
            }
            for item in (decisions.decisions if decisions is not None else ())
        ],
        # 下一轮要补什么 —— defer 攒下来的待办。
        "pending_analysis": [
            {"kind": item.kind.value, "description": item.description}
            for item in (decisions.pending_analysis if decisions is not None else ())
        ],
        "roadmap": {
            "tasks": [
                {
                    "key": task.key,
                    "decision_id": task.decision_id,
                    "candidate_id": task.candidate_id,
                    "task_type": task.task_type.value,
                    "title": task.title,
                }
                for task in (roadmap.tasks if roadmap is not None else ())
            ],
            # 没进路线图的决策 —— **连同理由**。
            "skipped": [
                {"decision_id": item.decision_id, "reason": item.reason}
                for item in (roadmap.skipped if roadmap is not None else ())
            ],
            "execution_order": list(roadmap.execution_order) if roadmap is not None else [],
        },
        "execution": [
            {
                "task_key": item.task_key,
                "task_id": item.task_id,
                "accepted": item.accepted,
                "outcome": item.outcome.value if item.outcome is not None else None,
                "reason": item.reason,
            }
            for item in (execution.tasks if execution is not None else ())
        ],
        "outcome": (
            {
                "status": outcome.status.value,
                "verdict": outcome.verdict.outcome.value if outcome.verdict is not None else None,
                "verdict_reason": outcome.verdict.reason if outcome.verdict is not None else None,
                "attainment_before": (
                    str(outcome.attainment_before)
                    if outcome.attainment_before is not None
                    else None
                ),
                "attainment_after": (
                    str(outcome.attainment_after) if outcome.attainment_after is not None else None
                ),
            }
            if outcome is not None
            else None
        ),
        # 这一轮在库里留下的两样东西 —— 周期记录与项目快照。
        "knowledge": (
            {
                "cycle_ref": knowledge.cycle_ref,
                "snapshot_ref": knowledge.snapshot_ref,
                "snapshot_version": knowledge.snapshot_version,
                "status": knowledge.status.value,
            }
            if knowledge is not None
            else None
        ),
        # 下一轮的诊断 —— 这一轮做完之后，项目还差什么。
        "next_gaps": (
            {
                "snapshot": next_gaps.snapshot,
                "overall_status": next_gaps.overall_status.value,
                "gap_ids[]": [gap.id for gap in next_gaps.gaps],
            }
            if next_gaps is not None
            else None
        ),
        # 这一轮往库里写了什么 —— 五样都要能对账。
        "journal_calls": dict(sorted(stub.journal.calls.items())),
    }


async def _run_cycle(stub: StubHandlers) -> dict[str, object]:
    """跑一轮循环 B，把事实采回来。"""
    graph = build_loop_b_graph(stub.handlers)
    state = await run_loop_b(graph, initial_state())
    return _cycle_facts(stub, state)


# --------------------------------------------------------------------------
# 三个场景的处理器
# --------------------------------------------------------------------------


@dataclass
class VetoingHandlers(StubHandlers):
    """审查否决的处理器 —— **决策是 record，但凭据不放行**。

    **这是治理外置那条规则在编排层的落点。** 决策那一层说"值得做"，审查
    那一层说"不放行" —— **两道门槛都要过**，少一道任务就会从没有决策来源
    的地方冒出来。

    **它继承打桩件而不是重写一套。** 重写一套就等于另造一条链 —— 那条链上
    的行为与真实的不一样，金标记下来的就是假形状。
    """

    async def plan(self, state: LoopBState) -> EvolutionPlan:
        decisions = state.decisions
        if decisions is None:
            raise AssertionError("规划之前必须先有决策")
        recorded = decisions.recorded
        if not recorded:
            return EvolutionPlan()
        first = recorded[0]
        return EvolutionPlan(
            drafts=(
                TaskDraft(
                    key=TASK_KEY,
                    decision_id=first.id,
                    task_type=TaskType.FEATURE,
                    title=self.candidate.title,
                    description="把落库口补齐",
                    rationale=first.rationale,
                ),
            ),
            approvals=(
                DecisionApproval(
                    decision_id=first.id,
                    candidate_id=first.candidate_id,
                    approved=False,
                    reason="审查发现证据不成立 —— 否决也要说清楚为什么",
                ),
            ),
        )


# --------------------------------------------------------------------------
# 连续多轮 —— "下一轮"那个站点
# --------------------------------------------------------------------------


def _gate(limit: str) -> ResourceGate:
    """搭一个闸门 —— **上限只有一个出处**。"""
    return ResourceGate(
        Budget(max_daily_cost=Decimal(limit)),
        scope="pilot",
        clock=lambda: _START,
    )


def _spending(
    amount: str,
    *,
    seen: list[str] | None = None,
) -> Callable[[str], Awaitable[ResourceUsage]]:
    """一轮的活 —— 干完把这一轮的用量报上来。

    **账由闸门记，驱动方只报用量** —— 同一笔钱记两遍，审计时得先分辨哪一遍
    才是真的。
    """

    async def _run(cycle_id: str) -> ResourceUsage:
        if seen is not None:
            seen.append(cycle_id)
        return ResourceUsage(
            cost=CostAmount(amount=Decimal(amount), currency="USD"),
            tokens=100,
        )

    return _run


async def _continuous_facts() -> dict[str, object]:
    """连续多轮的事实 —— **撞墙怎么停，账上还剩多少。**

    **上限 5，每轮花 3。** 于是：第一轮记进账（3）；第二轮要花 3，累计会变成
    6 > 5，**那一笔被拦下** —— 不记进累计额，但流水留着；第三轮开跑前，闸门
    看见"今天撞过一次"，于是不再开新周期。

    **三件事在同一个结论里：** 跑了两轮、被拦过一次、账上是 3。
    """
    gate = _gate("5")
    runner = ContinuousRunner(gate=gate, max_cycles=20)
    seen: list[str] = []

    result = await runner.run(_spending("3", seen=seen))

    return {
        "cycles": list(result.cycles),
        "cycle_count": result.count,
        "stopped": result.stopped,
        "reason": result.reason,
        "total": str(result.total),
        "blocked": result.blocked,
        "daily_spend": str(await gate.daily_spend()),
        # 跑过几轮 —— 撞墙那轮**也算跑过了**（它确实跑了，确实花了钱）。
        "seen_cycles": seen,
    }


# --------------------------------------------------------------------------
# 测试
# --------------------------------------------------------------------------


class TestTheCycleShape:
    """一轮演进的形状 —— 三条路各存一份。"""

    async def test_the_worth_doing_cycle(self) -> None:
        assert_golden("loop_b_worth_doing", await _run_cycle(StubHandlers()))

    async def test_the_deferred_cycle(self) -> None:
        """证据不足 —— **这一轮什么都不派，但下一轮的差距照样算。**"""
        stub = StubHandlers(decisions=defer_all(demo_candidate()))
        assert_golden("loop_b_deferred", await _run_cycle(stub))

    async def test_the_vetoed_cycle(self) -> None:
        """决策说"值得做"，审查说不放行 —— **任务不派，理由记进路线图。**"""
        assert_golden("loop_b_vetoed", await _run_cycle(VetoingHandlers()))


class TestTheNextRoundStation:
    """连续多轮 —— **停下来是一个结论，不是一个异常。**"""

    async def test_the_rounds_run_until_the_budget_wall(self) -> None:
        assert_golden("continuous_rounds", await _continuous_facts())


class TestTheGoldenItself:
    """金标作为数据，它自己得是干净的。"""

    def test_every_golden_file_exists(self) -> None:
        """**金标不存在就是没测。**

        少一份金标，这条路就没人守 —— 而"少一份"在 CI 里是**绿的**（少跑的
        用例不会让 pytest 变红）。所以这里显式查一遍。
        """
        store = GoldenStore()
        missing = [name for name in GOLDEN_NAMES if not store.path_for(name).exists()]
        assert not missing, (
            f"这些金标不存在：{'、'.join(missing)}\n"
            f"生成它们：ASES_GOLDEN_UPDATE=1 uv run pytest tests/golden/test_replay_golden.py"
        )

    async def test_the_deferred_cycle_visits_fewer_nodes(self) -> None:
        """**defer 那条路必须真的少走几个节点。**

        这条不是金标的补充，是金标的**反面**：如果 defer 那条路走的节点跟
        值得做那条一样多，那金标里记下的"分岔"就是假的 —— 而假的分岔比没有
        分岔更坏。
        """
        worth = await _run_cycle(StubHandlers())
        stub = StubHandlers(decisions=defer_all(demo_candidate()))
        deferred = await _run_cycle(stub)

        assert len(deferred["visited_nodes"]) < len(worth["visited_nodes"])  # type: ignore[arg-type]
        assert deferred["roadmap"]["tasks"] == []  # type: ignore[index]
        assert deferred["worth_doing"] is False

    async def test_the_vetoed_cycle_records_why_it_did_not_plan(self) -> None:
        """**否决的理由必须留下。** 不做也要说清楚为什么。"""
        facts = await _run_cycle(VetoingHandlers())
        skipped = facts["roadmap"]["skipped"]  # type: ignore[index]
        assert skipped, "否决了却没记下理由 —— 事后分不清『判断为不值得』与『压根没看见』"

    async def test_the_continuous_rounds_stop_without_raising(self) -> None:
        """**撞墙是结论，不是异常。**

        这一条不是金标的补充，是它的**反面**：如果"停下来"是靠抛异常实现的，
        那么正在跑的东西就被掀掉了 —— 跑完的成果没了，没跑完的也没了。
        """
        facts = await _continuous_facts()
        assert facts["stopped"] is True
        assert facts["blocked"] is True
        # 撞墙那一笔不记进账 —— 账上是 3，不是 6。
        assert Decimal(str(facts["total"])) == Decimal("3")
        assert Decimal(str(facts["daily_spend"])) == Decimal("3")
