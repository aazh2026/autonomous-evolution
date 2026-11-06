"""执行器契约里的记录类型 —— 它们的**守卫**。

验证（T1 ①的延伸）：契约不只由七个方法组成，还有"方法之间的记录"——
:class:`~executors.base.ExecutionTask`、:class:`~executors.base.SessionConfig`、
:class:`~executors.base.ExecutionEvent`。这些记录横跨编排层与执行器，所以它们的
守卫尤其要紧：**守卫写错了，错误会出现在离原因很远的地方。**

三条守卫，每条都对应一个具体的坑：

1. ``attempt`` 从 1 起算 —— 第 0 次尝试不存在，而幂等键 ``task_id:attempt``
   一旦允许 0，"第 0 次"与"没有第 0 次"就分不开了。
2. ``timeout_seconds`` 必须为正 —— **超时与失败是两个状态**（R-097 / I-03），
   而"忘了设超时"会让这个区别在真正出事时才暴露，那时已经晚了。
3. 记录不可变 —— 落库之后的东西不该被就地改写；改写等于"没发生过"。

**还有一条是本模块对 design.md §3.4 的补全**：设计里 ``execute(task)`` 不带
会话参数，"任务落在哪个会话上"就没有地方说。适配器若只记"最近一个会话"，
并发跑两个任务时必然张冠李戴。所以会话由任务自己带过来 ——
:class:`~executors.base.ExecutionTask` 的 ``session_id`` 是为此存在的。
七个方法的签名一个没动。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from executors.base import (
    CancellationState,
    Capability,
    ErrorCategory,
    ExecutionEvent,
    ExecutionTask,
    SessionConfig,
    build_idempotency_key,
)


def _task(**overrides: object) -> ExecutionTask:
    base: dict[str, object] = {
        "session_id": "session-1",
        "task_id": "TASK-1",
        "title": "改一行 README",
        "instruction": "把第一行的错别字改掉",
        "timeout_seconds": 60,
    }
    base.update(overrides)
    return ExecutionTask(**base)  # type: ignore[arg-type]


class TestTheTaskCarriesItsSession:
    """``execute(task)`` 不带会话参数 —— 那会话就得在任务身上。"""

    def test_the_session_id_is_part_of_the_task(self) -> None:
        assert _task(session_id="session-42").session_id == "session-42"

    def test_two_tasks_can_name_different_sessions(self) -> None:
        """并发跑两个任务时，"最近一个会话"这种记法必然张冠李戴。"""
        first = _task(session_id="session-1")
        second = _task(session_id="session-2")

        assert first.session_id != second.session_id


class TestTheAttemptStartsAtOne:
    def test_a_zero_attempt_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="attempt 必须从 1 起算"):
            _task(attempt=0)

    def test_a_negative_attempt_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="attempt 必须从 1 起算"):
            _task(attempt=-1)

    def test_the_first_attempt_is_allowed(self) -> None:
        assert _task(attempt=1).attempt == 1

    def test_the_idempotency_key_rejects_a_zero_attempt(self) -> None:
        """幂等键是 ``task_id + attempt`` —— 键的构造与任务上的守卫同源，
        各自都要守，否则两处会给出不同的答案。"""
        with pytest.raises(ValueError, match="attempt 必须从 1 起算"):
            build_idempotency_key("TASK-1", 0)

    def test_the_key_is_the_task_id_and_the_attempt(self) -> None:
        assert build_idempotency_key("TASK-1", 3) == "TASK-1:3"


class TestTheTimeoutMustBePositive:
    """超时要显式声明，且必须为正。"""

    def test_a_zero_timeout_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timeout_seconds 必须为正"):
            _task(timeout_seconds=0)

    def test_a_negative_timeout_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timeout_seconds 必须为正"):
            _task(timeout_seconds=-5)

    def test_a_positive_timeout_is_kept(self) -> None:
        assert _task(timeout_seconds=360).timeout_seconds == 360


class TestTheRecordsAreImmutable:
    def test_the_task_cannot_be_mutated(self) -> None:
        task = _task()

        with pytest.raises(FrozenInstanceError):
            task.task_id = "TASK-2"  # type: ignore[misc]

    def test_the_session_config_cannot_be_mutated(self) -> None:
        config = SessionConfig(workspace="/tmp/ws")

        with pytest.raises(FrozenInstanceError):
            config.workspace = "/tmp/other"  # type: ignore[misc]

    def test_the_event_cannot_be_mutated(self) -> None:
        event = ExecutionEvent(
            session_id="session-1",
            task_id="TASK-1",
            seq=1,
            kind="progress",
            occurred_at=datetime.now(UTC),
        )

        with pytest.raises(FrozenInstanceError):
            event.seq = 2  # type: ignore[misc]


class TestTheDefaultsAreEmptyRatherThanMissing:
    """没有就是没有 —— 别用一个"看起来像名字的空值"占位。"""

    def test_the_session_config_has_no_environment_by_default(self) -> None:
        assert dict(SessionConfig(workspace="/tmp/ws").env) == {}

    def test_the_event_has_no_payload_by_default(self) -> None:
        event = ExecutionEvent(
            session_id="session-1",
            task_id="TASK-1",
            seq=1,
            kind="progress",
            occurred_at=datetime.now(UTC),
        )

        assert dict(event.payload) == {}

    def test_the_session_config_has_no_credential_by_default(self) -> None:
        """凭证**引用**默认没有 —— 更不会有什么长期密钥。"""
        assert SessionConfig(workspace="/tmp/ws").credential_ref is None


class TestTheVocabulariesAreClosed:
    """枚举是封闭集合 —— 拼错的值要在这里就露出来。"""

    def test_the_capabilities(self) -> None:
        assert {capability.value for capability in Capability} == {
            "stream_events",
            "interrupt",
            "parallel_sessions",
            "structured_output",
        }

    def test_the_error_categories(self) -> None:
        """三类而不是两类 —— "不可重试"与"该由人来看"是两种后续动作。"""
        assert {category.value for category in ErrorCategory} == {
            "retryable",
            "non_retryable",
            "needs_human",
        }

    def test_the_cancellation_states(self) -> None:
        assert {state.value for state in CancellationState} == {
            "requested",
            "confirmed",
            "refused",
        }
