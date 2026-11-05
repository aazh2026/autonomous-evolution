"""TASK-P1-020 —— 十二个核心实体的 schema 断言（不需要数据库）。

这些断言在**元数据**层面检查结构：字段存在、约束存在、外键指向正确。
它们不连库 —— 连库的版本在 ``tests/integration/test_entity_crud.py``。

之所以两层都要：元数据断言跑得快、每次都跑，能第一时间发现"有人改了
模型但忘了改迁移"；集成断言连真库，确认约束在 PostgreSQL 里真的生效。
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Table

from core.entities import ALL_ENTITIES, Base
from core.task.state_machine import TaskState

EXPECTED_TABLES = {
    "project",
    "project_snapshot",
    "evolution_candidate",
    "evolution_decision",
    "task",
    "task_dependency",
    "agent_session",
    "execution_event",
    "verification_run",
    "artifact",
    "architecture_decision",
    "evolution_cycle",
}


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _constraint_texts(table_name: str) -> list[str]:
    table = _table(table_name)
    return [
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]


class TestEntitySet:
    """实体与表。"""

    def test_twelve_entities_are_defined(self) -> None:
        assert len(ALL_ENTITIES) == 12

    def test_twelve_tables_are_mapped(self) -> None:
        tables = set(Base.metadata.tables) - {"alembic_version"}
        assert tables == EXPECTED_TABLES

    def test_every_entity_has_a_table(self) -> None:
        for entity in ALL_ENTITIES:
            assert entity.__tablename__ in Base.metadata.tables

    def test_table_names_are_snake_case(self) -> None:
        for name in EXPECTED_TABLES:
            assert name == name.lower()
            assert " " not in name


class TestKeyFields:
    """design.md §3.6 表格里点名的字段。"""

    def test_candidate_has_evidence_value_and_risk(self) -> None:
        columns = _table("evolution_candidate").columns
        assert "evidence" in columns
        assert "value" in columns
        assert "risk" in columns

    def test_decision_has_decision_and_rationale(self) -> None:
        columns = _table("evolution_decision").columns
        assert "decision" in columns
        assert "rationale" in columns

    def test_artifact_has_uri_hash_and_type(self) -> None:
        columns = _table("artifact").columns
        assert "uri" in columns
        assert "hash" in columns
        assert "type" in columns

    def test_snapshot_has_version_commit_and_metrics(self) -> None:
        columns = _table("project_snapshot").columns
        assert "version" in columns
        assert "commit" in columns
        assert "metrics" in columns

    def test_task_dependency_has_predecessor_and_successor(self) -> None:
        columns = _table("task_dependency").columns
        assert "predecessor_id" in columns
        assert "successor_id" in columns

    def test_task_has_type_priority_and_status(self) -> None:
        columns = _table("task").columns
        assert "type" in columns
        assert "priority" in columns
        assert "status" in columns

    def test_agent_session_has_executor_and_session_id(self) -> None:
        columns = _table("agent_session").columns
        assert "executor" in columns
        assert "session_id" in columns

    def test_execution_event_has_event_type_and_payload(self) -> None:
        columns = _table("execution_event").columns
        assert "event_type" in columns
        assert "payload" in columns

    def test_verification_run_has_checks_and_result(self) -> None:
        columns = _table("verification_run").columns
        assert "checks" in columns
        assert "result" in columns

    def test_architecture_decision_has_context_decision_tradeoff(self) -> None:
        columns = _table("architecture_decision").columns
        assert "context" in columns
        assert "decision" in columns
        assert "tradeoff" in columns

    def test_evolution_cycle_has_cycle_id_and_outcome(self) -> None:
        columns = _table("evolution_cycle").columns
        assert "cycle_id" in columns
        assert "outcome" in columns

    def test_project_has_goal_and_policy(self) -> None:
        columns = _table("project").columns
        assert "goal" in columns
        assert "policy" in columns


class TestThreeTableSeparation:
    """TASK-P1-022 —— 三表分离的**结构**断言。"""

    def test_task_decision_id_is_not_null(self) -> None:
        assert _table("task").columns["decision_id"].nullable is False

    def test_task_decision_id_references_evolution_decision(self) -> None:
        targets = {
            foreign_key.target_fullname
            for constraint in _table("task").constraints
            if isinstance(constraint, ForeignKeyConstraint)
            for foreign_key in constraint.elements
        }
        assert "evolution_decision.id" in targets

    def test_decision_candidate_id_is_not_null(self) -> None:
        assert _table("evolution_decision").columns["candidate_id"].nullable is False

    def test_decision_candidate_id_references_evolution_candidate(self) -> None:
        targets = {
            foreign_key.target_fullname
            for constraint in _table("evolution_decision").constraints
            if isinstance(constraint, ForeignKeyConstraint)
            for foreign_key in constraint.elements
        }
        assert "evolution_candidate.id" in targets

    def test_candidate_has_no_direct_task_reference(self) -> None:
        """候选不能直接指向任务 —— 那样就绕过了决策。"""
        columns = _table("evolution_candidate").columns
        assert "task_id" not in columns


class TestAcceptanceContractGate:
    """R-051 —— 验收契约准入的 DB 级约束。"""

    def test_task_has_acceptance_contract_column(self) -> None:
        assert "acceptance_contract" in _table("task").columns

    def test_acceptance_contract_is_required_for_executing_states(self) -> None:
        texts = _constraint_texts("task")
        gate = next((text for text in texts if "acceptance_contract IS NOT NULL" in text), None)
        assert gate is not None, "task 表缺少验收契约准入约束"
        for state in ("RUNNING", "VERIFYING", "PASSED", "REPAIRING"):
            assert state in gate

    def test_pre_execution_states_do_not_require_contract(self) -> None:
        """还没开始干的任务不需要验收契约。"""
        gate = next(
            text for text in _constraint_texts("task") if "acceptance_contract IS NOT NULL" in text
        )
        for state in ("APPROVED", "PLANNED", "READY"):
            assert state not in gate


class TestStateConstraints:
    """状态与决策的取值约束。"""

    def test_task_status_check_lists_all_fourteen_states(self) -> None:
        texts = _constraint_texts("task")
        status_check = next((text for text in texts if "status IN (" in text), None)
        assert status_check is not None
        for state in TaskState:
            assert f"'{state.value}'" in status_check

    def test_decision_state_is_constrained_to_three_values(self) -> None:
        texts = _constraint_texts("evolution_decision")
        decision_check = next((text for text in texts if "decision IN (" in text), None)
        assert decision_check is not None
        assert "'record'" in decision_check
        assert "'defer'" in decision_check
        assert "'reject'" in decision_check

    def test_defer_requires_requested_analysis(self) -> None:
        texts = _constraint_texts("evolution_decision")
        defer_check = next(
            (text for text in texts if "requested_analysis" in text),
            None,
        )
        assert defer_check is not None
        assert "defer" in defer_check

    def test_normalized_value_range_is_enforced(self) -> None:
        texts = _constraint_texts("evolution_candidate")
        for metric in ("value", "risk", "confidence"):
            assert any(f"{metric} >= 0 AND {metric} <= 1" in text for text in texts)

    def test_artifact_hash_must_be_sha256_length(self) -> None:
        assert any("char_length(hash) = 64" in text for text in _constraint_texts("artifact"))


class TestEvidenceProtection:
    """TASK-P1-024 —— 证据表的保护。"""

    def test_evidence_tables_exist(self) -> None:
        assert "verification_run" in Base.metadata.tables
        assert "artifact" in Base.metadata.tables

    def test_evidence_rows_reference_tasks_with_restrict(self) -> None:
        """证据不能因为任务被删而消失。"""
        table = _table("verification_run")
        for constraint in table.constraints:
            if isinstance(constraint, ForeignKeyConstraint):
                for element in constraint.elements:
                    if element.target_fullname == "task.id":
                        assert element.ondelete == "RESTRICT"


class TestConstraintNaming:
    """约束必须具名 —— Alembic 的 downgrade 靠名字定位。"""

    def test_all_constraints_have_names(self) -> None:
        for table in Base.metadata.tables.values():
            for constraint in table.constraints:
                assert constraint.name, f"{table.name} 上有未命名的约束"

    def test_indexes_have_names(self) -> None:
        for table in Base.metadata.tables.values():
            for index in table.indexes:
                assert index.name
