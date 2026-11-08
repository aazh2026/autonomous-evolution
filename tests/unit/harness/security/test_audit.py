"""TASK-P1-045 —— 工具调用审计日志。

**审计表里少一条记录，比多一条更危险。** 少记一条，就少了一次"谁在什么
任务上动了什么"的凭据；多记一条顶多是噪音。所以这里盯三件事：

- **该记的都记下来了**（T1 ①）—— 一次任务执行之后，它的每一次工具调用
  都在表里，每条都带着主体 / 任务 / 资源 / 结果四要素；
- **写不下来的绝不放行**（T1 ②）—— 审计写失败就阻断操作，不允许"审计
  失败就放行"；
- **绕不过去** —— 业务代码拿到的是一个只有"记一次调用"的窄接口，拿不到
  存储；而存储协议里只有 append，没有 update、没有 delete。

**为什么这些断言要写成这样。** "记录存在"很容易写成"表里有东西" ——
那是一份空表也能过的检查。这里每条断言都指向一个具体的字段或一次具体的
写入失败，因为审计的价值全在细节里：一条没有主体的记录，等于没有记录。
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.policy.autonomy import Operation
from harness.security.audit import (
    NOT_APPLICABLE,
    AuditDecision,
    AuditEvent,
    AuditLog,
    AuditOutcome,
    AuditQuery,
    AuditRecorder,
    AuditResult,
    AuditStore,
    AuditWriteError,
    InMemoryAuditStore,
    JsonlAuditStore,
)
from harness.security.authorization import (
    AuthorizationRegistry,
    AuthorizationRequest,
    AuthorizationService,
    Capability,
    TaskAuthorization,
)

#: 固定的"现在" —— 时间戳不该随测试跑动而变。
NOW = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


class Clock:
    """可拨的时钟。"""

    def __init__(self, moment: datetime) -> None:
        self.now = moment

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class BrokenStore:
    """写不进去的存储 —— 磁盘满了、文件被锁、连接断了……"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error if error is not None else OSError("磁盘满了")
        self.attempts = 0

    def append(self, event: AuditEvent) -> None:
        self.attempts += 1
        raise self.error

    def query(self, query: AuditQuery) -> tuple[AuditEvent, ...]:
        return ()


def _protocol_members(protocol: type) -> set[str]:
    """一个协议对外暴露哪些方法。"""
    return {
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_") and callable(value)
    }


def _event(**overrides: Any) -> AuditEvent:
    """一条默认的审计记录，测试按需改字段。"""
    values: dict[str, Any] = {
        "event_id": "evt-1",
        "agent_id": "coder-01",
        "task_id": "TASK-042",
        "project_id": "my-product",
        "workspace_id": "ws-7",
        "resource": "src/main.py",
        "operation": "write_file",
        "result": AuditResult.SUCCESS,
        "decision": AuditDecision.ALLOW,
        "reason": "授权范围内",
        "timestamp": NOW,
    }
    values.update(overrides)
    return AuditEvent(**values)


def _log(store: AuditStore | None = None, clock: Clock | None = None) -> AuditLog:
    """一个装好存储的审计口。"""
    counter = iter(f"evt-{index}" for index in range(1, 1000))
    return AuditLog(
        store if store is not None else InMemoryAuditStore(),
        clock=clock or Clock(NOW),
        id_factory=lambda: next(counter),
    )


class TestTheFourElements:
    """主体、任务、资源、结果 —— 缺一条就构造不出来。"""

    def test_every_record_carries_the_four_elements(self) -> None:
        event = _event()
        assert event.agent_id == "coder-01"
        assert event.task_id == "TASK-042"
        assert event.resource == "src/main.py"
        assert event.result is AuditResult.SUCCESS

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("agent_id", ""),
            ("task_id", ""),
            ("resource", ""),
            ("agent_id", "   "),
            ("task_id", "  "),
        ],
    )
    def test_a_record_without_an_element_is_refused(self, field: str, value: str) -> None:
        """四要素不是"建议填" —— 缺一条就构造不出来。"""
        with pytest.raises(ValueError, match=field):
            _event(**{field: value})

    def test_the_record_is_frozen(self) -> None:
        """记下来的东西不许改 —— 要改就再记一条。"""
        event = _event()
        with pytest.raises(FrozenInstanceError):
            event.result = AuditResult.FAILURE  # type: ignore[misc]

    def test_the_record_has_no_method_to_rewrite_it(self) -> None:
        """审计表里不该有"更新结果"这种入口。"""
        mutators = [
            name
            for name in dir(AuditEvent)
            if not name.startswith("_") and callable(getattr(AuditEvent, name, None))
        ]
        assert mutators == [], f"审计记录不该有这些方法：{mutators}"


class TestTheWritePathCannotBeBypassed:
    """绕不过去 —— 写路径只有一条，且只有一条。"""

    def test_the_recorder_exposes_exactly_one_way_to_write(self) -> None:
        """业务代码拿到的是写入口：只有"记一次调用"。"""
        assert _protocol_members(AuditRecorder) == {"record_tool_call"}

    def test_the_store_cannot_change_what_was_written(self) -> None:
        """存储协议里只有 append —— 没有 update，也没有 delete。"""
        assert _protocol_members(AuditStore) == {"append", "query"}

    def test_the_log_does_not_hand_out_the_store(self) -> None:
        """存储是审计模块的事，业务代码拿不到。"""
        log = _log()
        public = {name for name in dir(log) if not name.startswith("_")}
        assert "store" not in public
        assert not hasattr(log, "store")

    def test_the_log_is_a_recorder(self) -> None:
        log = _log()
        assert isinstance(log, AuditRecorder)

    def test_the_business_side_only_needs_the_recorder(self) -> None:
        """把日志当成窄接口用，是类型层面就能保证的。"""
        recorder: AuditRecorder = _log()
        event = recorder.record_tool_call(
            agent_id="coder-01",
            task_id="TASK-042",
            project_id="my-product",
            workspace_id="ws-7",
            resource="src/main.py",
            operation="write_file",
            result=AuditResult.SUCCESS,
        )
        assert event.task_id == "TASK-042"


class TestTheFailClosed:
    """T1 ② —— 审计写失败就阻断，不允许"审计失败就放行"。"""

    def test_a_failing_write_raises(self) -> None:
        store = BrokenStore()
        log = _log(store)
        with pytest.raises(AuditWriteError):
            log.record_tool_call(
                agent_id="coder-01",
                task_id="TASK-042",
                project_id="my-product",
                workspace_id="ws-7",
                resource="src/main.py",
                operation="write_file",
                result=AuditResult.SUCCESS,
            )
        assert store.attempts == 1

    def test_the_original_failure_is_kept_as_the_cause(self) -> None:
        """不能只说一句"写失败了" —— 得知道是为什么失败的。"""
        log = _log(BrokenStore(OSError("磁盘满了")))
        with pytest.raises(AuditWriteError) as caught:
            log.record_tool_call(
                agent_id="coder-01",
                task_id="TASK-042",
                project_id="my-product",
                workspace_id="ws-7",
                resource="src/main.py",
                operation="write_file",
                result=AuditResult.SUCCESS,
            )
        assert isinstance(caught.value.__cause__, OSError)

    def test_a_failing_audit_blocks_the_authorization(self) -> None:
        """判定留痕写不下去 → 判定为拒绝，操作压根不开始。"""
        service = _authorized_service(sink=_log(BrokenStore()))
        decision = service.authorize(_code_change_request())
        assert decision.allowed is False
        assert decision.audit_recorded is False
        assert "留痕失败" in decision.reason

    def test_the_audited_call_refuses_to_swallow_the_exception(self) -> None:
        """块里出错了，异常照旧抛出去 —— 审计不吞异常。"""
        store = InMemoryAuditStore()
        log = _log(store)
        with (
            pytest.raises(RuntimeError, match="工具炸了"),
            log.audited_call(
                agent_id="coder-01",
                task_id="TASK-042",
                project_id="my-product",
                workspace_id="ws-7",
                resource="src/main.py",
                operation="write_file",
            ),
        ):
            raise RuntimeError("工具炸了")
        events = log.query(AuditQuery(task_id="TASK-042"))
        assert len(events) == 1
        assert events[0].result is AuditResult.FAILURE

    def test_the_audited_call_raises_when_the_record_cannot_be_written(self) -> None:
        """连失败记录都写不下去 —— 那就抛，原始异常挂在 cause 上。"""
        log = _log(BrokenStore())
        with (
            pytest.raises(AuditWriteError) as caught,
            log.audited_call(
                agent_id="coder-01",
                task_id="TASK-042",
                project_id="my-product",
                workspace_id="ws-7",
                resource="src/main.py",
                operation="write_file",
            ),
        ):
            raise RuntimeError("工具炸了")
        assert isinstance(caught.value.__cause__, RuntimeError)


class TestTheTaskTrail:
    """T1 ① —— 一次任务执行之后，它的每一次工具调用都在表里。"""

    def test_after_a_task_execution_every_tool_call_is_recorded(self) -> None:
        log = _log()
        with log.audited_call(
            agent_id="coder-01",
            task_id="TASK-042",
            project_id="my-product",
            workspace_id="ws-7",
            resource="src/main.py",
            operation="read_file",
        ):
            pass
        log.record_tool_call(
            agent_id="coder-01",
            task_id="TASK-042",
            project_id="my-product",
            workspace_id="ws-7",
            resource="src/main.py",
            operation="write_file",
            result=AuditResult.SUCCESS,
        )
        with log.audited_call(
            agent_id="coder-01",
            task_id="TASK-042",
            project_id="my-product",
            workspace_id="ws-7",
            resource="tests/test_main.py",
            operation="run_tests",
        ) as outcome:
            outcome.result = AuditResult.FAILURE
            outcome.reason = "有一条用例没过"

        events = log.query(AuditQuery(task_id="TASK-042"))
        assert len(events) == 3
        assert [event.operation for event in events] == [
            "read_file",
            "write_file",
            "run_tests",
        ]

    def test_every_record_carries_the_four_elements(self) -> None:
        log = _log()
        log.record_tool_call(
            agent_id="coder-01",
            task_id="TASK-042",
            project_id="my-product",
            workspace_id="ws-7",
            resource="src/main.py",
            operation="write_file",
            result=AuditResult.SUCCESS,
        )
        event = log.query(AuditQuery(task_id="TASK-042"))[0]
        assert event.agent_id == "coder-01"
        assert event.task_id == "TASK-042"
        assert event.resource == "src/main.py"
        assert event.result is AuditResult.SUCCESS

    def test_the_records_keep_the_writing_order(self) -> None:
        log = _log()
        for index in range(5):
            log.record_tool_call(
                agent_id="coder-01",
                task_id="TASK-042",
                project_id="my-product",
                workspace_id="ws-7",
                resource=f"src/file{index}.py",
                operation="write_file",
                result=AuditResult.SUCCESS,
            )
        events = log.query(AuditQuery(task_id="TASK-042"))
        assert [event.resource for event in events] == [f"src/file{index}.py" for index in range(5)]

    def test_another_task_is_not_mixed_in(self) -> None:
        log = _log()
        log.record_tool_call(
            agent_id="coder-01",
            task_id="TASK-042",
            project_id="my-product",
            workspace_id="ws-7",
            resource="src/main.py",
            operation="write_file",
            result=AuditResult.SUCCESS,
        )
        log.record_tool_call(
            agent_id="coder-01",
            task_id="TASK-043",
            project_id="my-product",
            workspace_id="ws-7",
            resource="src/other.py",
            operation="write_file",
            result=AuditResult.SUCCESS,
        )
        events = log.query(AuditQuery(task_id="TASK-042"))
        assert len(events) == 1
        assert events[0].resource == "src/main.py"

    def test_the_judgement_is_recorded_too(self) -> None:
        """判定也是一次"发生了什么" —— 放行与拒绝都记。"""
        log = _log()
        service = _authorized_service(sink=log)
        decision = service.authorize(_code_change_request())
        assert decision.allowed is True
        events = log.query(AuditQuery(task_id="TASK-042"))
        assert len(events) == 1
        assert events[0].decision is AuditDecision.ALLOW

    def test_the_judgement_and_the_call_are_both_there(self) -> None:
        """一次真实链路：判定 → 调用 → 表里两样都有。"""
        log = _log()
        service = _authorized_service(sink=log)
        service.authorize(_code_change_request())
        log.record_tool_call(
            agent_id="coder-01",
            task_id="TASK-042",
            project_id="my-product",
            workspace_id="ws-7",
            resource="src/main.py",
            operation="write_file",
            result=AuditResult.SUCCESS,
        )
        events = log.query(AuditQuery(task_id="TASK-042"))
        assert [event.operation for event in events] == ["code_changes", "write_file"]
        assert events[0].decision is AuditDecision.ALLOW
        assert events[0].capability == "repo:write"

    def test_a_refusal_is_recorded_as_a_denial(self) -> None:
        log = _log()
        service = _authorized_service(sink=log)
        decision = service.authorize(
            AuthorizationRequest(
                agent_id="coder-01",
                task_id="TASK-042",
                project_id="my-product",
                workspace_id="ws-7",
                operation=Operation.MERGE_TO_MAIN,
            )
        )
        assert decision.allowed is False
        events = log.query(AuditQuery(task_id="TASK-042"))
        assert len(events) == 1
        assert events[0].decision is AuditDecision.DENY
        assert events[0].result is AuditResult.DENIED


class TestTheQuery:
    """查询接口 —— 精确匹配，留空即不限。"""

    @pytest.fixture()
    def log(self) -> AuditLog:
        log = _log()
        for index in range(4):
            log.record_tool_call(
                agent_id=f"coder-{index}",
                task_id="TASK-042" if index < 3 else "TASK-099",
                project_id="my-product",
                workspace_id="ws-7",
                resource=f"src/file{index}.py",
                operation="write_file" if index % 2 == 0 else "read_file",
                result=AuditResult.SUCCESS,
                session_id="sess-1" if index < 2 else None,
            )
        return log

    def test_an_empty_query_returns_everything(self, log: AuditLog) -> None:
        assert len(log.query()) == 4

    def test_it_filters_by_task(self, log: AuditLog) -> None:
        assert len(log.query(AuditQuery(task_id="TASK-042"))) == 3

    def test_it_filters_by_agent(self, log: AuditLog) -> None:
        assert len(log.query(AuditQuery(agent_id="coder-1"))) == 1

    def test_it_filters_by_operation(self, log: AuditLog) -> None:
        assert len(log.query(AuditQuery(operation="read_file"))) == 2

    def test_it_filters_by_session(self, log: AuditLog) -> None:
        assert len(log.query(AuditQuery(session_id="sess-1"))) == 2
        assert log.query(AuditQuery(session_id="sess-9")) == ()

    def test_it_filters_by_project(self, log: AuditLog) -> None:
        assert len(log.query(AuditQuery(project_id="my-product"))) == 4
        assert log.query(AuditQuery(project_id="other-product")) == ()

    def test_it_filters_by_resource(self, log: AuditLog) -> None:
        assert len(log.query(AuditQuery(resource="src/file2.py"))) == 1

    def test_the_match_is_exact(self, log: AuditLog) -> None:
        """前缀不算命中 —— 否则"查一个任务"会查出半个仓库。"""
        assert log.query(AuditQuery(task_id="TASK-04")) == ()
        assert log.query(AuditQuery(agent_id="coder")) == ()

    def test_it_filters_by_time(self, log: AuditLog) -> None:
        assert len(log.query(AuditQuery(since=NOW))) == 4
        assert len(log.query(AuditQuery(since=NOW + timedelta(seconds=1)))) == 0
        assert len(log.query(AuditQuery(until=NOW - timedelta(seconds=1)))) == 0
        assert len(log.query(AuditQuery(since=NOW, until=NOW))) == 4

    def test_an_unknown_task_finds_nothing(self, log: AuditLog) -> None:
        assert log.query(AuditQuery(task_id="TASK-000")) == ()


class TestTheDurableStore:
    """文件实现 —— 写成功就是写成功了。"""

    def test_the_file_holds_one_line_per_record(self, tmp_path: Path) -> None:
        store = JsonlAuditStore(tmp_path / "audit.jsonl")
        log = _log(store)
        for index in range(3):
            log.record_tool_call(
                agent_id="coder-01",
                task_id="TASK-042",
                project_id="my-product",
                workspace_id="ws-7",
                resource=f"src/file{index}.py",
                operation="write_file",
                result=AuditResult.SUCCESS,
            )
        lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3

    def test_the_records_survive_a_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = _log(JsonlAuditStore(path))
        log.record_tool_call(
            agent_id="coder-01",
            task_id="TASK-042",
            project_id="my-product",
            workspace_id="ws-7",
            resource="src/main.py",
            operation="write_file",
            result=AuditResult.FAILURE,
            reason="编译没过",
            capability="repo:write",
            session_id="sess-1",
        )
        events = _log(JsonlAuditStore(path)).query(AuditQuery(task_id="TASK-042"))
        assert len(events) == 1
        restored = events[0]
        assert restored.agent_id == "coder-01"
        assert restored.resource == "src/main.py"
        assert restored.result is AuditResult.FAILURE
        assert restored.reason == "编译没过"
        assert restored.capability == "repo:write"
        assert restored.session_id == "sess-1"
        assert restored.timestamp == NOW

    def test_appending_never_rewrites_what_was_written(self, tmp_path: Path) -> None:
        """只追加 —— 之前那几行一个字都不能变。"""
        path = tmp_path / "audit.jsonl"
        log = _log(JsonlAuditStore(path))
        log.record_tool_call(
            agent_id="coder-01",
            task_id="TASK-042",
            project_id="my-product",
            workspace_id="ws-7",
            resource="src/first.py",
            operation="write_file",
            result=AuditResult.SUCCESS,
        )
        first = path.read_text(encoding="utf-8")
        log.record_tool_call(
            agent_id="coder-01",
            task_id="TASK-042",
            project_id="my-product",
            workspace_id="ws-7",
            resource="src/second.py",
            operation="write_file",
            result=AuditResult.SUCCESS,
        )
        assert path.read_text(encoding="utf-8").startswith(first)

    def test_a_broken_path_is_reported_as_a_write_failure(self, tmp_path: Path) -> None:
        """写不进去就得说写不进去 —— 不能装作记下来了。"""
        directory = tmp_path / "not-a-file"
        directory.mkdir()
        log = _log(JsonlAuditStore(directory))
        with pytest.raises(AuditWriteError):
            log.record_tool_call(
                agent_id="coder-01",
                task_id="TASK-042",
                project_id="my-product",
                workspace_id="ws-7",
                resource="src/main.py",
                operation="write_file",
                result=AuditResult.SUCCESS,
            )

    def test_a_corrupted_line_is_reported(self, tmp_path: Path) -> None:
        """坏行即错 —— 跳过它等于给篡改者留后门。"""
        path = tmp_path / "audit.jsonl"
        path.write_text("这不是 JSON\n", encoding="utf-8")
        with pytest.raises(ValueError, match="不是合法 JSON"):
            _log(JsonlAuditStore(path)).query()

    def test_a_record_missing_a_field_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text(json.dumps({"agent_id": "coder-01"}) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="event_id"):
            _log(JsonlAuditStore(path)).query()

    def test_querying_a_missing_file_finds_nothing(self, tmp_path: Path) -> None:
        assert _log(JsonlAuditStore(tmp_path / "audit.jsonl")).query() == ()


class TestTheModelGuardsItself:
    """模型自己守住边界。"""

    def test_the_result_is_coerced_into_the_known_values(self) -> None:
        event = _event(result="success")
        assert event.result is AuditResult.SUCCESS

    def test_an_unknown_result_is_refused(self) -> None:
        with pytest.raises(ValueError):
            _event(result="半成功")

    def test_an_unknown_decision_is_refused(self) -> None:
        with pytest.raises(ValueError):
            _event(decision="也许")

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        event = _event(timestamp=datetime(2026, 3, 1, 10, 0))
        assert event.timestamp == NOW

    def test_the_decision_keeps_the_judgement_time(self) -> None:
        """判定的时间是判定自己的时间，不是留痕的时间。"""
        clock = Clock(NOW + timedelta(hours=3))
        log = _log(clock=clock)
        service = _authorized_service(sink=log, clock=Clock(NOW))
        service.authorize(_code_change_request())
        event = log.query(AuditQuery(task_id="TASK-042"))[0]
        assert event.timestamp == NOW

    def test_the_outcome_defaults_to_success(self) -> None:
        assert AuditOutcome().result is AuditResult.SUCCESS

    def test_the_placeholder_for_a_missing_resource(self) -> None:
        """没有具体资源时要写明"没有"，不能留空。"""
        log = _log()
        service = _authorized_service(sink=log)
        service.authorize(
            AuthorizationRequest(
                agent_id="coder-01",
                task_id="TASK-042",
                project_id="my-product",
                workspace_id="ws-7",
                operation=Operation.PLAN_TASKS,
            )
        )
        event = log.query(AuditQuery(task_id="TASK-042"))[0]
        assert event.resource == NOT_APPLICABLE

    def test_the_store_protocol_is_runtime_checkable(self) -> None:
        assert isinstance(InMemoryAuditStore(), AuditStore)
        assert isinstance(JsonlAuditStore(Path("audit.jsonl")), AuditStore)


def _code_change_request() -> AuthorizationRequest:
    return AuthorizationRequest(
        agent_id="coder-01",
        task_id="TASK-042",
        project_id="my-product",
        workspace_id="ws-7",
        operation=Operation.CODE_CHANGES,
    )


def _authorized_service(*, sink: Any, clock: Clock | None = None) -> AuthorizationService:
    """一个"改代码"操作会被放行的判定器。"""
    registry = AuthorizationRegistry()
    registry.register(
        TaskAuthorization(
            agent_id="coder-01",
            task_id="TASK-042",
            project_id="my-product",
            workspace_id="ws-7",
            allowed_operations=frozenset({Operation.CODE_CHANGES, Operation.PLAN_TASKS}),
            granted_scopes=frozenset({Capability.REPO_WRITE.value}),
        )
    )
    return AuthorizationService(
        registry,
        sink=sink,
        workspace_roots={"ws-7": Path("/tmp/ws-7")},
        clock=clock,
    )
