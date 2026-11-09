"""TASK-P1-062 —— 验收契约准入的领域契约。

**这里验的是规则本身，不依赖数据库。** 判据有三样：契约的**版本指纹**、
契约**绑的是谁**、以及三种拒绝各自的**理由**。它们都该能在没有库的情况下
说清楚 —— 而"说清楚"是闸门能给出可执行结论的前提。

**为什么版本指纹要单独立一组用例。** "下一次迁移用新版本"这条要求，全靠
指纹把两次判定连起来：内容没变 → 同一个版本；内容变了 → 版本变了。指纹要是
不稳定（同样的内容算出两个值），或者不敏感（内容变了算出来没变），这条要求
就都是空的 —— 而它**看上去**还是成立的：两次判定都能通过。

**为什么键序要单独立一条。** 契约存在数据库的 JSON 列里，而数据库可能重排
键。指纹若把键序算进去，同一份契约从库里读出来两次可能算出两个版本 ——
于是"改没改过"这件事就说不清了。
"""

from __future__ import annotations

import pytest

from core.task.admission import (
    CONTRACT_GATE,
    AdmissionDecision,
    AdmissionOutcome,
    ContractBindingMismatchError,
    ContractUnreadableError,
    RefusalReason,
    bound_task_of,
    contract_fingerprint,
    is_bound_to,
)
from core.task.state_machine import TransitionGateError


class TestTheFingerprint:
    """契约的版本指纹。"""

    def test_the_same_content_gives_the_same_version(self) -> None:
        """**同一份内容永远是同一个版本。** 不然"改没改过"就说不清了。"""
        document = {"task_id": "TASK-001", "required": ["unit_tests_pass"]}

        assert contract_fingerprint(document) == contract_fingerprint(dict(document))

    def test_different_content_gives_a_different_version(self) -> None:
        """**内容变了，版本必须跟着变** —— 否则"下一次迁移用新版本"落空。"""
        before = {"task_id": "TASK-001", "required": ["unit_tests_pass"]}
        after = {"task_id": "TASK-001", "required": ["unit_tests_pass", "lint_pass"]}

        assert contract_fingerprint(before) != contract_fingerprint(after)

    def test_the_key_order_does_not_change_the_version(self) -> None:
        """**键序不算进版本。** 契约存在 JSON 列里，数据库可能重排键 ——
        键序要是算进去，同一份契约读两次能算出两个版本。"""
        one = {"task_id": "TASK-001", "required": ["unit_tests_pass"], "forbidden": []}
        other = {"forbidden": [], "required": ["unit_tests_pass"], "task_id": "TASK-001"}

        assert contract_fingerprint(one) == contract_fingerprint(other)

    def test_the_version_is_not_the_document_itself(self) -> None:
        """**指纹是短哈希，不是原文。** 原文可能很长，而版本只需要能比出
        "一样 / 不一样"。"""
        document = {"task_id": "TASK-001", "required": ["unit_tests_pass"]}

        version = contract_fingerprint(document)

        assert len(version) == 16
        assert version != str(document)

    def test_a_value_that_is_not_json_raises(self) -> None:
        """**存不进去的东西不该被悄悄当成"另一个版本"。** 那样一份坏契约
        就会有一个看起来很正常的版本号。"""
        with pytest.raises(TypeError):
            contract_fingerprint({"task_id": "TASK-001", "bad": object()})


class TestTheBinding:
    """契约绑的是哪个任务。"""

    def test_no_contract_binds_to_nothing(self) -> None:
        assert bound_task_of(None) is None

    def test_a_contract_without_a_task_id_binds_to_nothing(self) -> None:
        """**答不出"这是谁的契约"，在准入这件事上就是不行。**"""
        assert bound_task_of({"required": ["unit_tests_pass"]}) is None

    def test_a_blank_task_id_binds_to_nothing(self) -> None:
        """空白与没有是一回事 —— 空白 id 通过了，契约就能被贴到任何任务上。"""
        assert bound_task_of({"task_id": "   "}) is None

    def test_the_bound_task_is_read_out(self) -> None:
        assert bound_task_of({"task_id": "TASK-001"}) == "TASK-001"

    def test_a_contract_bound_elsewhere_does_not_pass(self) -> None:
        """**拿 A 任务的契约放行 B 任务，等于没有契约。**"""
        assert is_bound_to({"task_id": "TASK-002"}, "TASK-001") is False

    def test_a_contract_bound_here_passes(self) -> None:
        assert is_bound_to({"task_id": "TASK-001"}, "TASK-001") is True

    def test_no_contract_never_passes(self) -> None:
        assert is_bound_to(None, "TASK-001") is False

    def test_an_empty_task_id_never_passes(self) -> None:
        """**空任务 id 不能当"匹配"。** 否则任何契约都能配上一个空 id 而
        通过。"""
        assert is_bound_to({"task_id": "TASK-001"}, "") is False


class TestTheDecision:
    """准入结论。"""

    def test_an_admitted_decision_says_so(self) -> None:
        decision = AdmissionDecision.admit(reason="可以开工", contract_version="abc123")

        assert decision.admitted is True
        assert decision.outcome is AdmissionOutcome.ADMITTED
        assert decision.kind is None
        assert decision.contract_version == "abc123"

    def test_a_refused_decision_carries_its_kind(self) -> None:
        """**机器可读的理由** —— 调用方要据此分支，不能去解析人话。"""
        decision = AdmissionDecision.refuse(reason="没有契约", kind=RefusalReason.NO_CONTRACT)

        assert decision.admitted is False
        assert decision.outcome is AdmissionOutcome.REFUSED
        assert decision.kind is RefusalReason.NO_CONTRACT

    def test_a_refused_decision_carries_no_version(self) -> None:
        """拒绝时没有"被放行的那一版" —— 有的话读的人会以为它算数。"""
        decision = AdmissionDecision.refuse(reason="没有契约", kind=RefusalReason.NO_CONTRACT)

        assert decision.contract_version == ""


class TestTheRefusalErrors:
    """三种拒绝，三种错。"""

    def test_the_binding_mismatch_carries_the_task_it_bound_to(self) -> None:
        """**要能说出它绑的是谁** —— 否则看日志的人不知道该去改哪一条。"""
        error = ContractBindingMismatchError(task_id="TASK-001", bound_to="TASK-002")

        assert error.task_id == "TASK-001"
        assert error.bound_to == "TASK-002"

    def test_the_unreadable_contract_carries_the_detail(self) -> None:
        """坏数据要说得清坏在哪 —— "读不出来"三个字查不出问题。"""
        error = ContractUnreadableError(task_id="TASK-001", detail="required 不得为空")

        assert error.task_id == "TASK-001"
        assert error.detail == "required 不得为空"

    @pytest.mark.parametrize(
        "error",
        [
            ContractBindingMismatchError(task_id="TASK-001", bound_to="TASK-002"),
            ContractUnreadableError(task_id="TASK-001", detail="坏了"),
        ],
        ids=["bound_elsewhere", "unreadable"],
    )
    def test_every_refusal_is_a_transition_gate_error(self, error: TransitionGateError) -> None:
        """**三种拒绝共用一个基类** —— 调用方一把抓住即可，不必逐个列。"""
        assert isinstance(error, TransitionGateError)

    @pytest.mark.parametrize(
        "error",
        [
            ContractBindingMismatchError(task_id="TASK-001", bound_to="TASK-002"),
            ContractUnreadableError(task_id="TASK-001", detail="坏了"),
        ],
        ids=["bound_elsewhere", "unreadable"],
    )
    def test_every_refusal_names_the_same_gate(self, error: TransitionGateError) -> None:
        """**它们把守的是同一件事** —— 闸门的名字要一样，否则读日志的人得
        自己去猜"这两条是不是一个地方拦的"。"""
        assert error.gate == CONTRACT_GATE
