"""TASK-P4-040 —— 自适应调度的离线回放。

验证六件事：

1. **不许看未来** —— 第 i 位上的决定只能看第 i 位之前的记录。
2. **冷启动要有话说** —— 没有历史就按显式声明的偏好来，并如实标成冷启动。
3. **挑不出来就是挑不出来** —— 不悄悄退回一个默认组合。
4. **没跑成不是做不对** —— 它不进成功率的分母。
5. **配对要按位号配** —— 少一位两位就配错了，而配错的对检验出来的 p 值是假的。
6. **历史有两种口径** —— "所有格子的历史"与"自己跑过的历史"，结论不同。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from core.policy.scheduling import DEFAULT_TOLERANCE, SchedulingPolicy
from evaluation.agents.scheduling_replay import (
    HISTORY_CHOSEN,
    HISTORY_SHARED,
    RecordedRun,
    ReplaySlot,
    RoundRobinBaseline,
    StaticBaseline,
    build_slots,
    recorded_runs,
    replay,
)
from evaluation.executors.benchmark import PriceTable, RunOutcome, RunRecord, TokenUsage

#: 价目表 —— 每百万 token 1 美元，测成本折算用的。
_TABLE = PriceTable(
    input=Decimal("1"),
    output=Decimal("1"),
    cache_read=Decimal("0"),
    cache_write=Decimal("0"),
)


def _run(
    executor: str = "e1",
    task_id: str = "t1",
    *,
    succeeded: bool = True,
    executed: bool = True,
    cost: str | None = "1.0",
) -> RecordedRun:
    """造一次真实运行 —— **模型固定是 m1**（这一篇只关心执行器那一维）。"""
    return RecordedRun(
        executor=executor,
        model="m1",
        task_id=task_id,
        attempt=1,
        succeeded=succeeded,
        executed=executed,
        cost=Decimal(cost) if cost is not None else None,
        duration=Decimal("10"),
    )


def _slots(*runs: RecordedRun) -> tuple[ReplaySlot, ...]:
    """把记录排成决策位。"""
    return build_slots(runs)


def _policy(
    preference: tuple[str, ...] = ("e1/m1",),
    *,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> SchedulingPolicy:
    """默认的调度规矩 —— **冷启动偏好是显式声明的**。"""
    return SchedulingPolicy(preference=preference, tolerance=tolerance)


def _record(
    executor: str = "e1",
    model: str = "m1",
    task_id: str = "t1",
    outcome: RunOutcome = RunOutcome.SUCCEEDED,
    tokens: int | None = None,
) -> RunRecord:
    """造一条评测记录。"""
    return RunRecord(
        task_id=task_id,
        executor=executor,
        attempt=1,
        outcome=outcome,
        model=model,
        tokens=TokenUsage(input=tokens or 0, output=0, cache_read=0, cache_write=0)
        if tokens is not None
        else None,
    )


class TestSlots:
    """决策位 —— **次序就是因果顺序**。"""

    def test_runs_are_grouped_by_attempt_and_task(self) -> None:
        slots = _slots(
            _run(executor="a", task_id="t1"),
            _run(executor="b", task_id="t1"),
            _run(executor="a", task_id="t2"),
        )
        assert len(slots) == 2
        assert slots[0].cells == ("a/m1", "b/m1")
        assert slots[1].task_id == "t2"

    def test_the_slot_index_is_sequential(self) -> None:
        slots = _slots(
            _run(task_id="t1"),
            _run(task_id="t2"),
            _run(task_id="t3"),
        )
        assert [slot.index for slot in slots] == [0, 1, 2]

    def test_the_order_follows_the_first_appearance(self) -> None:
        """**先跑出来的在前** —— 回放的因果性靠这个建立。"""
        slots = _slots(
            _run(task_id="t2"),
            _run(task_id="t1"),
            replace(_run(task_id="t2"), attempt=2),
        )
        assert [(slot.attempt, slot.task_id) for slot in slots] == [(1, "t2"), (1, "t1"), (2, "t2")]


class TestCausality:
    """不许看未来。"""

    def test_the_first_decision_cannot_see_the_first_slot(self) -> None:
        """**第一个位上还没有历史** —— 只能冷启动。

        便宜的那格在第一个位上会做错，而贵的会做对。**如果它能看见第一个位的结果
        就会挑贵的那格** —— 那正是"看未来"。它没有看见，所以挑了声明的偏好；等到
        第二个位，它才拿第一个位的账去挑。
        """
        slots = _slots(
            _run(executor="cheap", task_id="t1", succeeded=False, cost="1"),
            _run(executor="pricey", task_id="t1", cost="10"),
            _run(executor="cheap", task_id="t2", succeeded=False, cost="1"),
            _run(executor="pricey", task_id="t2", cost="10"),
        )
        report = replay(slots, policy=_policy(), baselines=[])

        assert report.decisions[0].cell == "e1/m1"
        assert report.decisions[0].cold_start is True
        assert report.decisions[1].cell == "pricey/m1"
        assert report.decisions[1].cold_start is False

    def test_the_prefix_of_a_replay_is_the_same(self) -> None:
        """**前 N 位不受后面的位影响** —— 这是因果性的硬证据。

        跑两遍：一遍只有前两个位，一遍有四个位。前两个位上的决定必须一模一样 ——
        不一样就说明后面的位漏进了前面。
        """
        runs: list[RecordedRun] = []
        for task in ("t1", "t2", "t3", "t4"):
            runs.append(_run(executor="cheap", task_id=task, cost="1"))
            runs.append(_run(executor="pricey", task_id=task, cost="10"))
        short = replay(_slots(*runs[:4]), policy=_policy(), baselines=[])
        long = replay(_slots(*runs), policy=_policy(), baselines=[])

        assert len(short.decisions) == 2
        assert [decision.cell for decision in short.decisions] == [
            decision.cell for decision in long.decisions[:2]
        ]

    def test_the_reason_is_recorded_with_the_decision(self) -> None:
        """**理由要跟挑的结果一起留着** —— 事后才答得出"为什么是它"。"""
        slots = _slots(_run())
        report = replay(slots, policy=_policy(), baselines=[])
        assert "冷启动" in report.decisions[0].reason


class TestColdStart:
    """冷启动 —— **没有历史也得能开工**。"""

    def test_without_a_preference_it_refuses(self) -> None:
        """**挑不出来就明说挑不出来** —— 不悄悄退回一个默认组合。"""
        slots = _slots(_run())
        report = replay(slots, policy=SchedulingPolicy(), baselines=[])
        assert report.decisions[0].cell == ""
        assert report.adaptive is not None
        assert report.adaptive.undecided == 1

    def test_the_cold_start_is_counted(self) -> None:
        slots = _slots(_run())
        report = replay(slots, policy=_policy(), baselines=[])
        assert report.adaptive is not None
        assert report.adaptive.cold_starts == 1


class TestBaselines:
    """静态对照线。"""

    def test_a_static_baseline_always_uses_its_cell(self) -> None:
        slots = _slots(
            _run(executor="a", task_id="t1"),
            _run(executor="b", task_id="t2"),
        )
        report = replay(
            slots,
            policy=_policy(),
            baselines=[StaticBaseline(name="static", cell="b/m1")],
        )
        line = report.line("static")
        assert line is not None
        assert {outcome.cell for outcome in line.outcomes} == {"b/m1"}

    def test_the_round_robin_rotates(self) -> None:
        """**轮转是事先说得清的分配方式** —— 它不是随机。"""
        slots = _slots(
            _run(executor="a", task_id="t1"),
            _run(executor="b", task_id="t2"),
            _run(executor="c", task_id="t3"),
        )
        report = replay(
            slots,
            policy=_policy(),
            baselines=[RoundRobinBaseline(name="rr", cells=("a/m1", "b/m1", "c/m1"))],
        )
        line = report.line("rr")
        assert line is not None
        assert [outcome.cell for outcome in line.outcomes] == ["a/m1", "b/m1", "c/m1"]


class TestMissingData:
    """缺数据 —— **空缺不是失败**。"""

    def test_a_slot_without_a_record_is_skipped(self) -> None:
        """**挑了但这一位上没有实测记录** —— 那一位不进统计，不算失败。"""
        slots = _slots(_run(executor="a", task_id="t1"))
        report = replay(
            slots,
            policy=_policy(preference=("missing/m1",)),
            baselines=[],
        )
        assert report.adaptive is not None
        assert report.adaptive.missing == 1
        assert report.adaptive.slots == 0
        assert report.adaptive.successes == 0


class TestThreeStates:
    """三态 —— **没跑成不是做不对**。"""

    def test_not_run_is_not_in_the_success_denominator(self) -> None:
        """跑不成的那次不进成功率的分母。

        两位上，同一条线做对一次、跑不起来一次。**成功率是 1/1 不是 1/2** ——
        跑不起来是环境问题，不该算进能力。
        """
        slots = _slots(
            _run(executor="b", task_id="t1", succeeded=True),
            _run(executor="b", task_id="t2", succeeded=False, executed=False),
        )
        report = replay(
            slots,
            policy=_policy(),
            baselines=[StaticBaseline(name="static", cell="b/m1")],
        )
        line = report.line("static")
        assert line is not None
        assert line.executed == 1
        assert line.successes == 1
        assert line.success_rate == Decimal(1)
        assert line.overall_rate == Decimal("0.5")

    def test_a_line_without_any_execution_has_no_rate(self) -> None:
        """**全都没跑成 —— 成功率没有分母** —— 是"没有"，不是 0。"""
        slots = _slots(_run(executor="b", task_id="t1", succeeded=False, executed=False))
        report = replay(
            slots,
            policy=_policy(),
            baselines=[StaticBaseline(name="static", cell="b/m1")],
        )
        line = report.line("static")
        assert line is not None
        assert line.success_rate is None
        assert line.overall_rate == Decimal(0)


class TestCosts:
    """成本 —— **没测出来不是 0**。"""

    def test_without_a_price_table_the_cost_is_unknown(self) -> None:
        """**没测出来不许拿 0 顶上** —— 0 会被当成免费。"""
        runs = recorded_runs([_record(tokens=1000)], prices=None)
        assert runs[0].cost is None

    def test_the_cost_comes_from_the_price_table(self) -> None:
        runs = recorded_runs([_record(tokens=1000)], prices={"m1": _TABLE})
        assert runs[0].cost == Decimal("0.001")

    def test_runs_without_a_model_are_dropped(self) -> None:
        """**没记模型的运行切不进任何格子** —— 与能力矩阵同一套规矩。"""
        record = RunRecord(task_id="t1", executor="a", attempt=1, outcome=RunOutcome.SUCCEEDED)
        assert recorded_runs([record], prices=None) == ()

    def test_a_line_without_costs_has_no_mean(self) -> None:
        slots = _slots(_run(cost=None))
        report = replay(
            slots,
            policy=_policy(),
            baselines=[StaticBaseline(name="static", cell="e1/m1")],
        )
        line = report.line("static")
        assert line is not None
        assert line.mean_cost is None
        assert line.cost_samples == 0


class TestPairedComparison:
    """配对比较 —— **按位号配**。"""

    def test_a_slot_the_baseline_did_not_run_is_not_paired(self) -> None:
        """**一边"没跑成"，这一位就不进成功率的检验** —— 拿不齐的一对去比，比出来的是编的。"""
        slots = _slots(
            _run(executor="a", task_id="t1", cost="1"),
            _run(executor="b", task_id="t1", cost="2"),
            _run(executor="a", task_id="t2", cost="1"),
            _run(executor="b", task_id="t2", succeeded=False, executed=False, cost=None),
        )
        report = replay(
            slots,
            policy=_policy(preference=("a/m1",)),
            baselines=[StaticBaseline(name="static", cell="b/m1")],
        )
        comparison = report.comparisons[0]
        assert comparison.success_pairs == 1
        assert comparison.cost_pairs == 1

    def test_the_cost_pairs_need_both_costs(self) -> None:
        """**成本有一边没量到，这一位就不进成本的检验。**"""
        slots = _slots(
            _run(executor="a", task_id="t1", cost="1"),
            _run(executor="b", task_id="t1", cost="2"),
            _run(executor="a", task_id="t2", cost="1"),
            _run(executor="b", task_id="t2", cost=None),
        )
        report = replay(
            slots,
            policy=_policy(preference=("a/m1",)),
            baselines=[StaticBaseline(name="static", cell="b/m1")],
        )
        comparison = report.comparisons[0]
        assert comparison.success_pairs == 2
        assert comparison.cost_pairs == 1

    def test_a_difference_in_success_is_detected(self) -> None:
        """**自适应赢了对子** —— McNemar 只看不一致的那几对。"""
        slots = _slots(
            _run(executor="a", task_id="t1"),
            _run(executor="b", task_id="t1", succeeded=False),
        )
        report = replay(
            slots,
            policy=_policy(preference=("a/m1",)),
            baselines=[StaticBaseline(name="static", cell="b/m1")],
        )
        comparison = report.comparisons[0]
        assert comparison.success is not None
        assert comparison.success.detail["only_first"] == 1
        assert comparison.success.detail["discordant"] == 1


class TestBudget:
    """预算 —— **同样的钱能做对几件事**。"""

    def test_only_measured_costs_count(self) -> None:
        """**没量到成本的那一位不进预算内** —— 不知道花了多少就不能说没超。"""
        slots = _slots(
            _run(executor="a", task_id="t1", cost="1"),
            _run(executor="a", task_id="t2", cost=None),
        )
        report = replay(slots, policy=_policy(preference=("a/m1",)), baselines=[])
        line = report.adaptive
        assert line is not None
        assert line.within_budget(Decimal("2")) == (1, 1)

    def test_the_ceiling_excludes_the_expensive_ones(self) -> None:
        slots = _slots(
            _run(executor="a", task_id="t1", cost="1"),
            _run(executor="a", task_id="t2", cost="10"),
        )
        report = replay(slots, policy=_policy(preference=("a/m1",)), baselines=[])
        line = report.adaptive
        assert line is not None
        assert line.within_budget(Decimal("2")) == (1, 1)
        assert line.within_budget(Decimal("20")) == (2, 2)


class TestHistoryModes:
    """历史口径 —— **两种，结论不同**。"""

    def test_the_shared_history_lets_the_policy_switch(self) -> None:
        """**账本里有所有格子的历史** —— 第一个位之后就能挑到便宜的那格。"""
        slots = _slots(
            _run(executor="cheap", task_id="t1", cost="1"),
            _run(executor="pricey", task_id="t1", cost="10"),
            _run(executor="cheap", task_id="t2", cost="1"),
            _run(executor="pricey", task_id="t2", cost="10"),
        )
        report = replay(slots, policy=_policy(preference=("pricey/m1",)), baselines=[])
        assert report.decisions[0].cell == "pricey/m1"
        assert report.decisions[1].cell == "cheap/m1"

    def test_the_chosen_history_never_discovers_an_unmeasured_cell(self) -> None:
        """**只用自己的历史，就永远发现不了没试过的组合。**

        这不是 bug，是结论：**历史必须来自离线评测**（能力矩阵），不能指望边跑
        边学 —— 没跑过的格子根本没有观测，调度器连它存在都不知道。
        """
        slots = _slots(
            _run(executor="cheap", task_id="t1", cost="1"),
            _run(executor="pricey", task_id="t1", cost="10"),
            _run(executor="cheap", task_id="t2", cost="1"),
        )
        report = replay(
            slots,
            policy=_policy(preference=("pricey/m1",)),
            baselines=[],
            history=HISTORY_CHOSEN,
        )
        assert {decision.cell for decision in report.decisions} == {"pricey/m1"}

    def test_the_shared_mode_is_the_default(self) -> None:
        slots = _slots(_run())
        report = replay(slots, policy=_policy(), baselines=[])
        assert report.history == HISTORY_SHARED

    def test_an_unknown_history_mode_is_refused(self) -> None:
        slots = _slots(_run())
        try:
            replay(slots, policy=_policy(), baselines=[], history="whatever")
        except ValueError as error:
            assert "历史口径" in str(error)
        else:
            raise AssertionError("未知的历史口径应当被拒绝")


class TestReproducibility:
    """可复现 —— **否则"这次为什么用这个组合"永远答不上来**。"""

    def test_the_same_input_gives_the_same_decisions(self) -> None:
        slots = _slots(
            _run(executor="cheap", task_id="t1", cost="1"),
            _run(executor="pricey", task_id="t1", cost="10"),
        )
        first = replay(slots, policy=_policy(), baselines=[])
        second = replay(slots, policy=_policy(), baselines=[])
        assert [decision.cell for decision in first.decisions] == [
            decision.cell for decision in second.decisions
        ]
