"""TASK-P1-062 —— 验收契约准入闸门（编排层）。

**这里验的是门的行为。** 没有契约时任务开不了工（T1 ①）、补上契约就能开工
（T1 ②）、契约改了之后下一次判定用的是新版本且改动留了审计（T1 ③）—— 三件
事都在这里（design.md §3.5B、§6）。

**为什么用假的存储而不是真的数据库。** 这一层要证明的是"门的判定与留痕"，
不是"库里的行长什么样" —— 后者由 ``tests/integration/test_admission_gate.py``
在真库上证明。两边都做，是因为它们证明的不是一回事：这里证明逻辑，那里证明
接线。少一边就有一类洞没人管。

**为什么三种拒绝要分开测。** "没有契约"、"绑在别的任务上"、"契约读不出来" ——
三者的补救动作分别是**补一份 / 改回去 / 重写**。要是都抛同一个错，调用方就没
法分别处理，而"分别处理"正是分类的全部意义。

**为什么"幂等重投"要单独测。** 任务已经在 RUNNING 时又收到一次 START，要的是
"别重复执行副作用"，不是"再审一遍准入"。这一条要是没守住，消息重投就会变成
一次任务失败 —— 而重投恰恰是最该被容忍的情况。
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from core.project.models import ComparisonOperator, TargetValue
from core.task.admission import (
    ContractAuditError,
    ContractBindingMismatchError,
    ContractUnreadableError,
    contract_fingerprint,
)
from core.task.state_machine import (
    AcceptanceContractRequiredError,
    TaskState,
    TransitionGateError,
)
from harness.security.audit import (
    AuditDecision,
    AuditEvent,
    AuditLog,
    AuditQuery,
    AuditResult,
    InMemoryAuditStore,
)
from harness.verification.contract import AcceptanceContract, RequiredCheck, Threshold
from orchestration.gates import BIND_OPERATION, UPDATE_OPERATION, AdmissionGate

_TASK_ID = "TASK-001"
_PROJECT_ID = "PROJECT-001"
_AGENT_ID = "AGENT-001"


class _FakeContractStore:
    """契约的存取口（假的）。

    **它只做一件事：记住谁绑了什么。** 门拿到的信息只有这些 —— 门不知道数据
    存在哪儿，也不需要知道。
    """

    def __init__(self) -> None:
        self.documents: dict[str, Mapping[str, object]] = {}
        self.bind_calls: list[str] = []
        self.unbind_calls: list[str] = []

    async def contract_of(self, task_id: str) -> Mapping[str, object] | None:
        return self.documents.get(task_id)

    async def bind_contract(self, task_id: str, document: Mapping[str, object]) -> None:
        self.documents[task_id] = dict(document)
        self.bind_calls.append(task_id)

    async def unbind_contract(self, task_id: str) -> None:
        self.documents.pop(task_id, None)
        self.unbind_calls.append(task_id)


class _BrokenAuditStore:
    """写不下来的审计存储。

    **它代表的是磁盘满了、目录没了、权限被改** —— 这类故障在生产上是真实
    的，而"审计写不下来"时会发生什么，正是这里要钉住的。

    :attr:`attempted` 记下"试图写过" —— 写不进去不等于没试过；它证明门确实
    走到了审计这一步（而不是在别的地方就退出了）。
    """

    def __init__(self) -> None:
        self.attempted: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self.attempted.append(event)
        raise OSError("审计存储写不进去")

    def query(self, query: AuditQuery) -> tuple[AuditEvent, ...]:
        """**按任务号查** —— 够用了：这个存储只用来证明"写不下来"。"""
        if query.task_id is None:
            return tuple(self.attempted)
        return tuple(event for event in self.attempted if event.task_id == query.task_id)


def _contract(*, task_id: str = _TASK_ID, coverage: str = "0.85") -> AcceptanceContract:
    """一份正常的契约。

    **阈值故意放在里面。** 一份没有阈值的契约改起来看不出差别 —— 而"改了标准"
    这件事正是要验的，得有东西可改。
    """
    return AcceptanceContract(
        task_id=task_id,
        required=(RequiredCheck.UNIT_TESTS_PASS, RequiredCheck.LINT_PASS),
        thresholds={
            "coverage": Threshold(
                operator=ComparisonOperator.GTE,
                value=TargetValue.parse(coverage),
            )
        },
    )


def _gate(
    store: _FakeContractStore | None = None,
    *,
    audit_store: InMemoryAuditStore | _BrokenAuditStore | None = None,
) -> tuple[AdmissionGate, _FakeContractStore, InMemoryAuditStore | _BrokenAuditStore]:
    """搭一套门 + 存储 + 审计。"""
    the_store = store if store is not None else _FakeContractStore()
    the_audit_store = audit_store if audit_store is not None else InMemoryAuditStore()
    gate = AdmissionGate(the_store, audit=AuditLog(the_audit_store))
    return gate, the_store, the_audit_store


class TestATaskWithoutAContractCannotStart:
    """T1 ① —— 无契约的任务无法从 READY 迁移到 RUNNING。"""

    async def test_the_start_is_refused(self) -> None:
        """**没有契约就没有验收标准**，没有验收标准的执行只是"做完了"。"""
        gate, _, _ = _gate()

        with pytest.raises(AcceptanceContractRequiredError) as exc_info:
            await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.READY)

        assert exc_info.value.task_id == _TASK_ID

    async def test_the_refusal_happens_before_anything_is_written(self) -> None:
        """**拒绝时什么都没动。** 门不写契约、不写状态 —— 它只给结论。"""
        gate, store, audit_store = _gate()

        with pytest.raises(AcceptanceContractRequiredError):
            await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.READY)

        assert store.documents == {}
        assert store.bind_calls == []
        assert audit_store.query(AuditQuery(task_id=_TASK_ID)) == ()

    async def test_the_refusal_is_a_transition_gate_error(self) -> None:
        """**它和另外两种拒绝共用一个基类** —— 调用方一把抓住即可。"""
        gate, _, _ = _gate()

        with pytest.raises(TransitionGateError):
            await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.READY)


class TestWithAContractTheTaskCanStart:
    """T1 ② —— 补上契约后迁移成功。"""

    async def test_the_start_is_allowed(self) -> None:
        """**契约齐了就能开工** —— 门不额外卡人。"""
        gate, store, _ = _gate()
        await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(),
        )

        result = await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.READY)

        assert result.transition.changed is True
        assert result.transition.previous is TaskState.READY
        assert result.transition.current is TaskState.RUNNING
        assert _TASK_ID in store.documents

    async def test_the_decision_carries_the_version(self) -> None:
        """**放行的是"这一版"契约** —— 版本要能被指出来，否则事后说不清
        "它当时验的是哪一版标准"。"""
        gate, _, _ = _gate()
        binding = await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(),
        )

        result = await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.READY)

        assert result.decision.admitted is True
        assert result.decision.contract_version == binding.version

    async def test_the_binding_left_an_audit_record(self) -> None:
        """**契约的每一次改动都留审计** —— 谁改的、改的什么、什么时候。"""
        gate, _, audit_store = _gate()
        await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(),
        )

        events = audit_store.query(AuditQuery(task_id=_TASK_ID))

        assert len(events) == 1
        assert events[0].operation == BIND_OPERATION
        assert events[0].agent_id == _AGENT_ID
        assert events[0].project_id == _PROJECT_ID
        assert events[0].result is AuditResult.SUCCESS
        assert events[0].decision is AuditDecision.ALLOW

    async def test_the_audit_names_the_resource(self) -> None:
        """**资源名要指向这份契约** —— 审计里全是别的任务时，得能挑出来。"""
        gate, _, audit_store = _gate()
        await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(),
        )

        events = audit_store.query(AuditQuery(task_id=_TASK_ID))

        assert events[0].resource == f"acceptance_contract:{_TASK_ID}"

    async def test_binding_a_model_and_binding_a_document_agree(self) -> None:
        """**两种入参走同一条路。** 给模型还是给字典，不该影响"能不能绑" ——
        两边各走一套校验，早晚会对不上。"""
        gate, _, _ = _gate()

        from_model = await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(),
        )
        from_document = await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract().to_document(),
        )

        assert from_model.version == from_document.version


class TestTheContractCanBeChangedAndTheChangeIsAudited:
    """T1 ③ —— 契约在任务开始后被修改 → 下一次迁移用新版本，且修改留审计。"""

    async def test_the_change_leaves_a_second_audit_record(self) -> None:
        """**补一份与改一份分开记** —— 事后复盘时这是两件事。"""
        gate, _, audit_store = _gate()
        await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(),
        )
        await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(coverage="0.60"),
        )

        events = audit_store.query(AuditQuery(task_id=_TASK_ID))

        assert len(events) == 2
        assert events[0].operation == BIND_OPERATION
        assert events[1].operation == UPDATE_OPERATION

    async def test_the_audit_names_the_version_it_replaced(self) -> None:
        """**"从哪一版换到哪一版"要说得出来** —— 只说"现在是这一版"，就查不出
        门槛是什么时候被放低的。"""
        gate, _, audit_store = _gate()
        first = await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(),
        )
        second = await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(coverage="0.60"),
        )

        events = audit_store.query(AuditQuery(task_id=_TASK_ID))

        assert first.version in events[1].reason
        assert second.version in events[1].reason

    async def test_the_binding_reports_the_replaced_version(self) -> None:
        """**调用方也要知道换掉了哪一版** —— 它要写进自己的记录里。"""
        gate, _, _ = _gate()
        first = await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(),
        )

        second = await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(coverage="0.60"),
        )

        assert second.replaced_version == first.version

    async def test_the_first_binding_replaces_nothing(self) -> None:
        """**首次绑定没有"被换掉的那一版"** —— 有的话读的人会以为漏了什么。"""
        gate, _, _ = _gate()

        binding = await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(),
        )

        assert binding.replaced_version == ""

    async def test_the_next_admission_uses_the_new_version(self) -> None:
        """**下一次迁移用新版本。** 这是 T1 ③ 的正脸：改完之后判定看到的必须是
        新内容，不能是上次那份。"""
        gate, _, _ = _gate()
        await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(),
        )
        before = await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.READY)

        await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(coverage="0.60"),
        )
        after = await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.READY)

        assert before.decision.contract_version != after.decision.contract_version

    async def test_the_new_version_is_the_new_content(self) -> None:
        """**新版本就是新内容的指纹** —— 不是行号、不是时间戳。"""
        gate, store, _ = _gate()
        await gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(coverage="0.60"),
        )

        result = await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.READY)

        document = store.documents[_TASK_ID]
        assert document is not None
        assert result.decision.contract_version == contract_fingerprint(dict(document))


class TestTheThreeRefusals:
    """三种拒绝，三种错，三种补救。"""

    async def test_a_contract_bound_to_another_task_is_refused(self) -> None:
        """**拿 A 任务的契约放行 B 任务，等于没有契约。**"""
        gate, store, _ = _gate()
        store.documents[_TASK_ID] = _contract(task_id="TASK-OTHER").to_document()

        with pytest.raises(ContractBindingMismatchError) as exc_info:
            await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.READY)

        assert exc_info.value.task_id == _TASK_ID
        assert exc_info.value.bound_to == "TASK-OTHER"

    async def test_an_unreadable_contract_is_refused(self) -> None:
        """**坏数据不能被当成"没有契约"** —— 那等于绕过闸门。"""
        gate, store, _ = _gate()
        store.documents[_TASK_ID] = {"task_id": _TASK_ID, "required": []}

        with pytest.raises(ContractUnreadableError) as exc_info:
            await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.READY)

        assert exc_info.value.task_id == _TASK_ID
        assert exc_info.value.detail

    async def test_every_refusal_is_a_transition_gate_error(self) -> None:
        """**三种拒绝共用一个基类** —— 调用方一把抓住即可。"""
        gate, store, _ = _gate()
        store.documents[_TASK_ID] = _contract(task_id="TASK-OTHER").to_document()

        with pytest.raises(TransitionGateError):
            await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.READY)

    async def test_binding_the_wrong_task_is_rejected(self) -> None:
        """**绑错了当场报错** —— 绑上去就是给别的任务发了一张假通行证。"""
        gate, store, _ = _gate()

        with pytest.raises(ContractBindingMismatchError):
            await gate.bind_contract(
                task_id=_TASK_ID,
                project_id=_PROJECT_ID,
                agent_id=_AGENT_ID,
                contract=_contract(task_id="TASK-OTHER"),
            )

        assert store.documents == {}


class TestTheGateDoesNotCacheTheContract:
    """门每次判定都重新读契约。"""

    async def test_a_change_behind_the_gate_is_seen(self) -> None:
        """**缓存会让"改松了标准"在判定上看不出来** —— 而那正是最需要被看见
        的一种改动。"""
        gate, store, _ = _gate()
        store.documents[_TASK_ID] = _contract().to_document()

        before = await gate.check(task_id=_TASK_ID)

        store.documents[_TASK_ID] = _contract(coverage="0.60").to_document()
        after = await gate.check(task_id=_TASK_ID)

        assert before.contract_version != after.contract_version

    async def test_repeated_checks_agree(self) -> None:
        """**同一个契约，两次判定给出同一个版本** —— 版本要可复算，否则
        "改没改过"就说不清了。"""
        gate, store, _ = _gate()
        store.documents[_TASK_ID] = _contract().to_document()

        first = await gate.check(task_id=_TASK_ID)
        second = await gate.check(task_id=_TASK_ID)

        assert first.contract_version == second.contract_version


class TestTheChangeIsRolledBackWhenTheAuditFails:
    """审计写不下来 → 这次改动就不算数。"""

    async def test_the_first_binding_is_rolled_back(self) -> None:
        """**改回去了、审计没写成，两件事都还是真的。**"""
        broken = _BrokenAuditStore()
        gate, store, _ = _gate(audit_store=broken)

        with pytest.raises(ContractAuditError):
            await gate.bind_contract(
                task_id=_TASK_ID,
                project_id=_PROJECT_ID,
                agent_id=_AGENT_ID,
                contract=_contract(),
            )

        assert store.documents == {}
        assert store.unbind_calls == [_TASK_ID]
        # 门确实走到了审计这一步 —— 失败发生在写痕上，不是在别的什么地方。
        assert len(broken.attempted) == 1

    async def test_the_previous_contract_is_restored(self) -> None:
        """**改写失败要把原来那版放回去** —— 而不是把任务留在"契约是新写的那
        版、审计里却查不到"的中间态。"""
        store = _FakeContractStore()
        working_gate, _, _ = _gate(store)
        first = await working_gate.bind_contract(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            agent_id=_AGENT_ID,
            contract=_contract(),
        )

        broken_gate, _, _ = _gate(store, audit_store=_BrokenAuditStore())
        with pytest.raises(ContractAuditError):
            await broken_gate.bind_contract(
                task_id=_TASK_ID,
                project_id=_PROJECT_ID,
                agent_id=_AGENT_ID,
                contract=_contract(coverage="0.60"),
            )

        restored = store.documents[_TASK_ID]
        assert restored is not None
        assert contract_fingerprint(dict(restored)) == first.version

    async def test_the_audit_failure_is_reported_as_such(self) -> None:
        """**它不是"契约坏了"** —— 是"这件事没留下痕"。两者的处置不同。"""
        gate, _, _ = _gate(audit_store=_BrokenAuditStore())

        with pytest.raises(ContractAuditError) as exc_info:
            await gate.bind_contract(
                task_id=_TASK_ID,
                project_id=_PROJECT_ID,
                agent_id=_AGENT_ID,
                contract=_contract(),
            )

        assert "审计" in str(exc_info.value)


class TestTheRedeliveryIsANoop:
    """幂等重投。"""

    async def test_a_running_task_starting_again_does_not_raise(self) -> None:
        """**重投要的是"别重复执行副作用"** —— 不是"再审一遍准入"。"""
        gate, _, _ = _gate()

        result = await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.RUNNING)

        assert result.transition.changed is False
        assert result.transition.current is TaskState.RUNNING

    async def test_the_redelivery_does_not_execute_the_start(self) -> None:
        """**空操作就意味着调用方不会重复执行副作用** —— 这是幂等的全部意义。"""
        gate, _, _ = _gate()

        result = await gate.admit_start(task_id=_TASK_ID, current_state=TaskState.RUNNING)

        assert result.transition.is_idempotent_noop is True
        assert result.transition.previous is TaskState.RUNNING
