"""TASK-P3-013 —— 观察层契约：一条观察得站得住。

**测的是"观察的形状"，不是"观察到了什么"。** 真正的读取在
``tests/unit/observe/test_sources.py`` 与 ``tests/system/`` 里。

这一份测试守住三件事：

1. **观察必须有证据** —— 没有证据的那条不是观察，是说法；
2. **每一类观察只能带它该带的那种证据** —— 把两件事混成一件的，契约层要
   报错；
3. **"读到了没有"是三态** —— 空与失败不能混。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.observe import (
    OBSERVATION_EVIDENCE_KINDS,
    Observation,
    ObservationContractError,
    ObservationKind,
    ObservationSnapshot,
    ObservationSource,
    SourceOutcome,
    SourceStatus,
    group_by_kind,
)


def _status(snapshot: ObservationSnapshot, kind: ObservationKind) -> SourceStatus:
    """取某一类的状态。

    **读不到就报错** —— 断言里的 ``None`` 会让下一步无从下手，而"这一类
    根本没有状态"本身就是另一条要单独断言的事。
    """
    status = snapshot.status_of(kind)
    assert status is not None, kind
    return status


def _file(ref: str = "src/tinylib/money.py") -> Evidence:
    """一条文件证据。"""
    return Evidence(kind=EvidenceKind.FILE, ref=ref)


class TestTheVocabulary:
    """六类观察，一类不多一类不少。"""

    def test_there_are_exactly_six_kinds(self) -> None:
        """**六类不能少也不能多。** 少一类，"六类数据源各接入 ≥1 个真实来源"
        就是空话；多一类，那一类就有自己的一套规则，词表对不上。
        """
        assert len(list(ObservationKind)) == 6

    def test_the_kinds_are_the_ones_design_md_lists(self) -> None:
        """**与 design.md §3 第一层那六条一一对应。**"""
        assert {kind.value for kind in ObservationKind} == {
            "changes",
            "tests",
            "static",
            "runtime",
            "feedback",
            "docs",
        }

    def test_every_kind_has_an_evidence_rule(self) -> None:
        """**每一类观察都有明确的证据规则** —— 少一类的意思是"这一类的证据
        形态没人说了算"。
        """
        assert set(OBSERVATION_EVIDENCE_KINDS) == set(ObservationKind)

    def test_the_outcome_has_three_states(self) -> None:
        """**三态，不是两态。** 空与失败混成一种，缺实测值就变成了 0。"""
        assert {outcome.value for outcome in SourceOutcome} == {"observed", "empty", "failed"}


class TestTheObservation:
    """一条观察。"""

    def test_it_needs_evidence(self) -> None:
        """**没有证据的观察不是观察，是说法。**

        这一条是 R-028 与 R-029 的落点 —— 说"有证据"就必须真的拿得出。
        """
        with pytest.raises(ObservationContractError, match="至少一条证据"):
            Observation(
                kind=ObservationKind.CHANGES,
                subject="src/tinylib/money.py",
                fact="这个文件最近改得很频繁",
                evidence=(),
            )

    def test_the_subject_cannot_be_blank(self) -> None:
        """**没有主体，所有观察都挤成一团。**"""
        with pytest.raises(ObservationContractError, match="subject"):
            Observation(
                kind=ObservationKind.CHANGES,
                subject="   ",
                fact="这个文件最近改得很频繁",
                evidence=(_file(),),
            )

    def test_the_fact_cannot_be_blank(self) -> None:
        with pytest.raises(ObservationContractError, match="fact"):
            Observation(
                kind=ObservationKind.CHANGES,
                subject="src/tinylib/money.py",
                fact="  ",
                evidence=(_file(),),
            )

    def test_the_evidence_kind_has_to_fit_the_observation_kind(self) -> None:
        """**一条"代码改动"的观察带着问题单编号，说明生产者把两件事混成
        了一件** —— 那时候没人说得清这条观察到底在说什么。
        """
        with pytest.raises(ObservationContractError, match="不能带"):
            Observation(
                kind=ObservationKind.CHANGES,
                subject="src/tinylib/money.py",
                fact="这个文件最近改得很频繁",
                evidence=(Evidence(kind=EvidenceKind.ISSUE, ref="PILOT-001"),),
            )

    def test_the_feedback_kind_takes_issues(self) -> None:
        """**反馈这一类就该带问题单编号** —— 那正是它的落点。"""
        observation = Observation(
            kind=ObservationKind.FEEDBACK,
            subject="PILOT-001",
            fact="`cli.py` 的 `stats` 子命令收到空输入时抛未捕获的异常",
            evidence=(Evidence(kind=EvidenceKind.ISSUE, ref="PILOT-001"),),
        )
        assert observation.evidence[0].identifier == "PILOT-001"

    def test_the_detail_is_optional(self) -> None:
        """**补充说明不能替代事实与证据** —— 但它可以没有。"""
        observation = Observation(
            kind=ObservationKind.CHANGES,
            subject="src/tinylib/money.py",
            fact="这个文件最近改得很频繁",
            evidence=(_file(),),
        )
        assert observation.detail == ""

    def test_the_fingerprint_is_kind_plus_subject(self) -> None:
        """**同一条观察反复被观察到，身份不变。** 这是观察与事件最不一样的地方。"""
        observation = Observation(
            kind=ObservationKind.CHANGES,
            subject="src/tinylib/money.py",
            fact="这个文件最近改得很频繁",
            evidence=(_file(),),
        )
        assert observation.fingerprint == ("changes", "src/tinylib/money.py")

    def test_two_observations_of_the_same_thing_have_the_same_fingerprint(self) -> None:
        """**同一件事被观察到两次，是同一条事实，不是两件事。**"""
        first = Observation(
            kind=ObservationKind.CHANGES,
            subject="src/tinylib/money.py",
            fact="改了三次",
            evidence=(_file(),),
        )
        second = Observation(
            kind=ObservationKind.CHANGES,
            subject="src/tinylib/money.py",
            fact="改了五次",
            evidence=(_file(),),
        )
        assert first.fingerprint == second.fingerprint


class TestTheSnapshot:
    """一次观察的结果。"""

    def _snapshot(self) -> ObservationSnapshot:
        """一份快照：三路观察到，一路空，一路失败。"""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return ObservationSnapshot(
            project_id="P1",
            observed_at=now,
            observations=(
                Observation(
                    kind=ObservationKind.CHANGES,
                    subject="a.py",
                    fact="改得频繁",
                    evidence=(_file("a.py"),),
                ),
                Observation(
                    kind=ObservationKind.TESTS,
                    subject="t.py::test_x",
                    fact="没通过",
                    evidence=(_file("t.py"),),
                ),
            ),
            statuses=(
                SourceStatus(kind=ObservationKind.CHANGES, outcome=SourceOutcome.OBSERVED),
                SourceStatus(kind=ObservationKind.TESTS, outcome=SourceOutcome.OBSERVED),
                SourceStatus(kind=ObservationKind.RUNTIME, outcome=SourceOutcome.EMPTY),
                SourceStatus(
                    kind=ObservationKind.STATIC,
                    outcome=SourceOutcome.FAILED,
                    detail="炸了",
                ),
            ),
        )

    def test_it_answers_by_kind(self) -> None:
        snapshot = self._snapshot()
        assert len(snapshot.by_kind(ObservationKind.CHANGES)) == 1
        assert len(snapshot.by_kind(ObservationKind.FEEDBACK)) == 0

    def test_it_answers_the_status_of_a_kind(self) -> None:
        """**"没读到"与"读到了但是空的"必须分得开。**"""
        snapshot = self._snapshot()
        assert _status(snapshot, ObservationKind.RUNTIME).outcome is SourceOutcome.EMPTY
        assert _status(snapshot, ObservationKind.STATIC).outcome is SourceOutcome.FAILED

    def test_a_kind_that_was_not_read_at_all_has_no_status(self) -> None:
        """**没接的那一路与接了但空的那一路不是一回事。**"""
        snapshot = self._snapshot()
        assert snapshot.status_of(ObservationKind.FEEDBACK) is None

    def test_the_covered_kinds_are_the_ones_that_produced_something(self) -> None:
        snapshot = self._snapshot()
        assert snapshot.kinds_covered == (ObservationKind.CHANGES, ObservationKind.TESTS)

    def test_the_missing_kinds_include_both_empty_and_failed(self) -> None:
        """**缺的那几类必须看得见** —— 少了一路而没人知道，"六类数据源各接入
        ≥1 个真实来源"这句话就随时会不成立。
        """
        snapshot = self._snapshot()
        assert set(snapshot.kinds_missing) == {
            ObservationKind.RUNTIME,
            ObservationKind.STATIC,
        }

    def test_the_failed_kinds_are_only_the_ones_that_broke(self) -> None:
        """**"读到了但是空的"不算失败** —— 两者要分得开。"""
        snapshot = self._snapshot()
        assert snapshot.failed_kinds == (ObservationKind.STATIC,)


class TestGrouping:
    """按种类分桶。"""

    def test_it_covers_all_six_kinds(self) -> None:
        """**分桶的结果要确定** —— 同样的观察分两次，桶的顺序不能变。"""
        buckets = group_by_kind(())
        assert list(buckets) == list(ObservationKind)

    def test_the_observations_land_in_the_right_bucket(self) -> None:
        observation = Observation(
            kind=ObservationKind.DOCS,
            subject="0001-first",
            fact="第一个决策",
            evidence=(_file("docs/adr/0001-first.md"),),
        )
        buckets = group_by_kind([observation])
        assert buckets[ObservationKind.DOCS] == (observation,)
        assert buckets[ObservationKind.CHANGES] == ()


class TestThePort:
    """观察来源的形状。"""

    def test_a_source_only_needs_a_kind_and_an_observe(self) -> None:
        """**它只负责读。** 不判断这是不是机会，也不决定要不要开一轮。"""

        class Source:
            @property
            def kind(self) -> ObservationKind:
                return ObservationKind.CHANGES

            def observe(self) -> tuple[Observation, ...]:
                return ()

        assert isinstance(Source(), ObservationSource)

    def test_a_source_that_is_not_a_source_is_rejected(self) -> None:
        """**形状对不上就是形状对不上** —— 不给"差不多就行"。"""

        class Nope:
            pass

        assert not isinstance(Nope(), ObservationSource)
