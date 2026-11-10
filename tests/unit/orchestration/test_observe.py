"""TASK-P3-013 —— 观察编排：六路各读各的，拼成项目此刻的样子。

**测的是"拼"这件事，不是"读"。** 读取在 ``tests/unit/observe/`` 里。

这一份测试守住三件事：

1. **"读不成"不往上抛** —— 一路坏了，其余五路照跑；
2. **三态分得开** —— 空、失败、观察到，是三件不同的事；
3. **同类多路合成一行** —— 但坏掉的那一路不能被藏起来。
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.observe import (
    Observation,
    ObservationKind,
    ObservationSnapshot,
    ObservationSource,
    SourceOutcome,
    SourceStatus,
)
from orchestration.observe import Observer, build_observer


def _status(snapshot: ObservationSnapshot, kind: ObservationKind) -> SourceStatus:
    """取某一类的状态。

    **读不到就报错** —— 断言里的 ``None`` 会让下一步无从下手，而"这一类
    根本没有状态"本身就是另一条要单独断言的事。
    """
    status = snapshot.status_of(kind)
    assert status is not None, kind
    return status


class _Stub:
    """一个可控的观察来源。"""

    def __init__(self, kind: ObservationKind, result: object) -> None:
        self._kind = kind
        self._result = result
        self.calls = 0

    @property
    def kind(self) -> ObservationKind:
        """这一路是哪一类。"""
        return self._kind

    def observe(self) -> tuple[Observation, ...]:
        """按剧本演。"""
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result  # type: ignore[return-value]


def _observation(kind: ObservationKind, subject: str) -> Observation:
    """造一条观察。"""
    return Observation(
        kind=kind,
        subject=subject,
        fact=f"{subject} 有问题",
        evidence=(Evidence(kind=EvidenceKind.FILE, ref="src/tinylib/money.py"),),
    )


class TestTheSixSources:
    """六路是同一个形状。"""

    def test_they_all_satisfy_the_port(self) -> None:
        """**六路是同一个形状** —— 编排那一侧才不用写六遍。"""
        sources = [
            _Stub(ObservationKind.CHANGES, ()),
            _Stub(ObservationKind.TESTS, ()),
            _Stub(ObservationKind.STATIC, ()),
            _Stub(ObservationKind.RUNTIME, ()),
            _Stub(ObservationKind.FEEDBACK, ()),
            _Stub(ObservationKind.DOCS, ()),
        ]
        for source in sources:
            assert isinstance(source, ObservationSource)

    def test_the_snapshot_has_one_row_per_kind(self) -> None:
        """**六路都要在，一个都不能少** —— 少一路就看不出"没读到"与"读到
        了但是空的"。
        """
        observer = Observer(
            sources=(
                _Stub(ObservationKind.CHANGES, ()),
                _Stub(ObservationKind.TESTS, ()),
                _Stub(ObservationKind.STATIC, ()),
                _Stub(ObservationKind.RUNTIME, ()),
                _Stub(ObservationKind.FEEDBACK, ()),
                _Stub(ObservationKind.DOCS, ()),
            ),
            project_id="P1",
        )
        snapshot = observer.observe()
        assert len(snapshot.statuses) == 6
        assert {item.kind for item in snapshot.statuses} == set(ObservationKind)


class TestTheThreeStates:
    """三态分得开。"""

    def test_a_source_that_produced_something_is_observed(self) -> None:
        observation = _observation(ObservationKind.CHANGES, "a.py")
        observer = Observer(
            sources=(_Stub(ObservationKind.CHANGES, (observation,)),),
            project_id="P1",
        )
        snapshot = observer.observe()
        assert _status(snapshot, ObservationKind.CHANGES).outcome is SourceOutcome.OBSERVED

    def test_a_source_that_produced_nothing_is_empty(self) -> None:
        """**空是"读到了，没有东西"** —— 不是失败。"""
        observer = Observer(
            sources=(_Stub(ObservationKind.CHANGES, ()),),
            project_id="P1",
        )
        snapshot = observer.observe()
        assert _status(snapshot, ObservationKind.CHANGES).outcome is SourceOutcome.EMPTY

    def test_a_source_that_broke_is_failed(self) -> None:
        """**失败是"压根没读成"** —— 与空是两件事。"""
        observer = Observer(
            sources=(_Stub(ObservationKind.TESTS, RuntimeError("测试套件起不来")),),
            project_id="P1",
        )
        snapshot = observer.observe()
        status = _status(snapshot, ObservationKind.TESTS)
        assert status.outcome is SourceOutcome.FAILED
        assert "测试套件起不来" in status.detail

    def test_the_failure_says_what_kind_of_failure_it_was(self) -> None:
        """**"没读成"要说得清是"没读成"** —— 异常类型与消息一起留着。"""
        observer = Observer(
            sources=(_Stub(ObservationKind.TESTS, ValueError("退出码 4")),),
            project_id="P1",
        )
        snapshot = observer.observe()
        assert "ValueError" in _status(snapshot, ObservationKind.TESTS).detail
        assert "退出码 4" in _status(snapshot, ObservationKind.TESTS).detail


class TestTheFailuresDoNotSpread:
    """一路坏了，其余照跑。"""

    def test_the_other_sources_still_run(self) -> None:
        """**六路里有五路正常的时候，报告"这一路坏了"比整个流程崩掉有用
        得多。**
        """
        good = _Stub(ObservationKind.CHANGES, (_observation(ObservationKind.CHANGES, "a.py"),))
        observer = Observer(
            sources=(good, _Stub(ObservationKind.TESTS, RuntimeError("炸了"))),
            project_id="P1",
        )
        snapshot = observer.observe()
        assert _status(snapshot, ObservationKind.CHANGES).outcome is SourceOutcome.OBSERVED
        assert _status(snapshot, ObservationKind.TESTS).outcome is SourceOutcome.FAILED
        assert len(snapshot.observations) == 1

    def test_the_broken_source_produces_no_observations(self) -> None:
        """**失败的那一路不能贡献半截观察。**"""
        observer = Observer(
            sources=(_Stub(ObservationKind.TESTS, RuntimeError("炸了")),),
            project_id="P1",
        )
        snapshot = observer.observe()
        assert snapshot.observations == ()


class TestSeveralSourcesOfOneKind:
    """同一类接了好几个来源。"""

    def test_they_are_merged_into_one_row(self) -> None:
        """**同一类观察的状态只有一个** —— 快照里六行，不多不少。"""
        observer = Observer(
            sources=(
                _Stub(ObservationKind.TESTS, (_observation(ObservationKind.TESTS, "a.py::x"),)),
                _Stub(ObservationKind.TESTS, (_observation(ObservationKind.TESTS, "b.py::y"),)),
            ),
            project_id="P1",
        )
        snapshot = observer.observe()
        assert len(snapshot.statuses) == 1
        assert _status(snapshot, ObservationKind.TESTS).outcome is SourceOutcome.OBSERVED
        assert len(snapshot.observations) == 2

    def test_one_reading_is_enough_to_call_it_observed(self) -> None:
        """**有一个读到了就是读到了** —— 另一个套件坏掉这件事另说。"""
        observer = Observer(
            sources=(
                _Stub(ObservationKind.TESTS, (_observation(ObservationKind.TESTS, "a.py::x"),)),
                _Stub(ObservationKind.TESTS, RuntimeError("另一个套件坏了")),
            ),
            project_id="P1",
        )
        snapshot = observer.observe()
        assert _status(snapshot, ObservationKind.TESTS).outcome is SourceOutcome.OBSERVED

    def test_the_broken_one_is_not_hidden(self) -> None:
        """**"读到了"与"全都坏了"不能混成一句话** —— 坏掉那路的细节要留着。"""
        observer = Observer(
            sources=(
                _Stub(ObservationKind.TESTS, (_observation(ObservationKind.TESTS, "a.py::x"),)),
                _Stub(ObservationKind.TESTS, RuntimeError("另一个套件坏了")),
            ),
            project_id="P1",
        )
        snapshot = observer.observe()
        assert "另一个套件坏了" in _status(snapshot, ObservationKind.TESTS).detail

    def test_all_broken_means_failed(self) -> None:
        """**全都坏了就是失败** —— 不是空。"""
        observer = Observer(
            sources=(
                _Stub(ObservationKind.TESTS, RuntimeError("炸了")),
                _Stub(ObservationKind.TESTS, RuntimeError("也炸了")),
            ),
            project_id="P1",
        )
        snapshot = observer.observe()
        assert _status(snapshot, ObservationKind.TESTS).outcome is SourceOutcome.FAILED


class TestTheSnapshotItself:
    """快照本身。"""

    def test_it_carries_the_project_and_the_time(self) -> None:
        """**快照说"这是谁在什么时候看到的"。**"""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        observer = build_observer(
            (_Stub(ObservationKind.CHANGES, ()),),
            project_id="P1",
            clock=lambda: now,
        )
        snapshot = observer.observe()
        assert snapshot.project_id == "P1"
        assert snapshot.observed_at == now

    def test_each_source_is_read_exactly_once(self) -> None:
        """**只跑一遍** —— 跑一遍 pytest 要几百毫秒，判"有没有东西"和取内容
        分成两次调用，这一路就要读两遍。
        """
        source = _Stub(ObservationKind.CHANGES, (_observation(ObservationKind.CHANGES, "a.py"),))
        observer = Observer(sources=(source,), project_id="P1")
        observer.observe()
        assert source.calls == 1
