"""DAG 多任务依赖调度器单元测试（TASK-P2-012）。

覆盖要求：
- R-056：依赖未满足的任务不启动；上游失败时下游进入正确状态而非误跑；
- T1：前置未完成拦截、波次解锁、级联阻断与原因追溯、数据库同步。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.entities.task import Task
from core.planning.dag import DAGTaskNode, TaskDAG
from core.task.state_machine import TaskAction, TaskState
from orchestration.dag_scheduler import (
    DAGScheduler,
    IllegalExecutionError,
)


def _build_test_dag() -> TaskDAG:
    """构建标准测试依赖图:

         t1 (根)
        /  \\
       t2   t3
        \\  /
         t4
         |
         t5 (链条末端)

       t6 (完全独立任务)
    """
    dag = TaskDAG()
    dag.add_node(DAGTaskNode("t1", "根功能", "feature", priority=Decimal("0.9")))
    dag.add_node(DAGTaskNode("t2", "核心修复", "bugfix", priority=Decimal("0.8")))
    dag.add_node(DAGTaskNode("t3", "辅助测试", "test", priority=Decimal("0.7")))
    dag.add_node(DAGTaskNode("t4", "集成文档", "docs", priority=Decimal("0.5")))
    dag.add_node(DAGTaskNode("t5", "发布上线", "feature", priority=Decimal("0.4")))
    dag.add_node(DAGTaskNode("t6", "独立优化", "performance", priority=Decimal("0.6")))

    dag.add_dependency("t1", "t2")
    dag.add_dependency("t1", "t3")
    dag.add_dependency("t2", "t4")
    dag.add_dependency("t3", "t4")
    dag.add_dependency("t4", "t5")

    return dag


class TestPreconditionEnforcement:
    """R-056 核心判据 ①：依赖未满足的任务不启动。"""

    def test_initial_state_only_independent_tasks_ready(self) -> None:
        dag = _build_test_dag()
        scheduler = DAGScheduler(dag)

        # 初始时只有无前驱的 t1 和 t6 自动进入 READY
        assert scheduler.get_state("t1") is TaskState.READY
        assert scheduler.get_state("t6") is TaskState.READY
        # 依赖未满足的 t2, t3, t4, t5 仍保持 PLANNED
        assert scheduler.get_state("t2") is TaskState.PLANNED
        assert scheduler.get_state("t3") is TaskState.PLANNED
        assert scheduler.get_state("t4") is TaskState.PLANNED
        assert scheduler.get_state("t5") is TaskState.PLANNED

        runnable = [n.task_id for n in scheduler.get_runnable_tasks()]
        assert runnable == ["t1", "t6"]

    def test_cannot_start_before_predecessor_completes(self) -> None:
        dag = _build_test_dag()
        scheduler = DAGScheduler(dag)

        assert not scheduler.can_start("t2")
        with pytest.raises(IllegalExecutionError) as exc_info:
            scheduler.mark_running("t2")

        err = exc_info.value
        assert err.task_id == "t2"
        assert "t1" in err.uncompleted_predecessors


class TestSuccessProgressionAndUnlocking:
    """前置完成时逐步解锁后继任务。"""

    def test_predecessor_completion_unlocks_successors(self) -> None:
        dag = _build_test_dag()
        scheduler = DAGScheduler(dag)

        scheduler.mark_running("t1")
        assert scheduler.get_state("t1") is TaskState.RUNNING

        # t1 完成后，直接后继 t2 和 t3 解锁进入 READY
        unlocked = scheduler.mark_completed("t1")
        assert set(unlocked) == {"t2", "t3"}
        assert scheduler.get_state("t1") is TaskState.COMPLETED
        assert scheduler.get_state("t2") is TaskState.READY
        assert scheduler.get_state("t3") is TaskState.READY
        # t4 仍需等待 t2 与 t3 同时完成，保持 PLANNED
        assert scheduler.get_state("t4") is TaskState.PLANNED

    def test_diamond_convergence_requires_all_parents(self) -> None:
        dag = _build_test_dag()
        scheduler = DAGScheduler(dag)

        scheduler.mark_completed("t1")
        # t2 完成，但 t3 尚未完成
        scheduler.mark_completed("t2")
        assert scheduler.get_state("t4") is TaskState.PLANNED
        assert not scheduler.can_start("t4")

        # t3 完成，t4 的所有前驱均已完成，t4 成功解锁为 READY
        unlocked = scheduler.mark_completed("t3")
        assert "t4" in unlocked
        assert scheduler.get_state("t4") is TaskState.READY
        assert scheduler.can_start("t4")


class TestCascadingFailurePropagation:
    """R-056 核心判据 ②：上游失败时下游进入正确状态而非误跑。"""

    def test_failure_cascades_to_all_transitive_downstream(self) -> None:
        dag = _build_test_dag()
        scheduler = DAGScheduler(dag)

        # t1 顺利完成，解锁 t2 和 t3
        scheduler.mark_completed("t1")
        assert scheduler.get_state("t2") is TaskState.READY

        # t2 执行过程中失败（例如独立验证未通过）
        blocked_downstream = scheduler.mark_failed("t2", reason="Pytest 契约测试挂起")

        # t2 自身变为 BLOCKED
        assert scheduler.get_state("t2") is TaskState.BLOCKED

        # 依赖 t2 的下游节点 t4 和间接下游 t5 必须全部被级联阻断为 BLOCKED！
        assert set(blocked_downstream) == {"t4", "t5"}
        assert scheduler.get_state("t4") is TaskState.BLOCKED
        assert scheduler.get_state("t5") is TaskState.BLOCKED

        # 验证阻断追溯信息
        b4 = scheduler.get_blockage("t4")
        assert b4 is not None
        assert b4.blocked_by == "t2"
        assert "Pytest 契约测试挂起" in b4.reason

        b5 = scheduler.get_blockage("t5")
        assert b5 is not None
        assert b5.blocked_by == "t2"

        # 下游绝不可能再被启动
        assert not scheduler.can_start("t4")
        assert not scheduler.can_start("t5")

        # 独立分支 t3 和 t6 绝不被误伤
        assert scheduler.get_state("t3") is TaskState.READY
        assert scheduler.get_state("t6") is TaskState.READY

    def test_root_failure_blocks_entire_subgraph(self) -> None:
        dag = _build_test_dag()
        scheduler = DAGScheduler(dag)

        # 根任务 t1 失败
        blocked = scheduler.mark_failed("t1", reason="架构合规性否决")

        assert scheduler.get_state("t1") is TaskState.BLOCKED
        # t2, t3, t4, t5 全部阻断
        assert set(blocked) == {"t2", "t3", "t4", "t5"}
        for tid in ["t2", "t3", "t4", "t5"]:
            assert scheduler.get_state(tid) is TaskState.BLOCKED
            assert not scheduler.can_start(tid)

        # 独立任务 t6 正常运行
        assert scheduler.can_start("t6")


class TestProgressAndPersistence:
    """进度统计与数据库同步。"""

    def test_progress_tracking(self) -> None:
        dag = _build_test_dag()
        scheduler = DAGScheduler(dag)

        p0 = scheduler.progress()
        assert p0.total == 6
        assert p0.ready == 2
        assert p0.planned == 4
        assert not p0.is_finished

        # 完成 t1, t6
        scheduler.mark_completed("t1")
        scheduler.mark_completed("t6")
        # t2 失败阻断 t4, t5
        scheduler.mark_failed("t2", reason="修复超限")
        # t3 完成
        scheduler.mark_completed("t3")

        p_final = scheduler.progress()
        assert p_final.completed == 3  # t1, t6, t3
        assert p_final.blocked == 3    # t2, t4, t5
        assert p_final.is_finished
        assert not p_final.is_successful

    @pytest.mark.asyncio
    async def test_sync_to_repository(self) -> None:
        dag = TaskDAG()
        t1_id = uuid.uuid4()
        t2_id = uuid.uuid4()

        dag.add_node(DAGTaskNode(str(t1_id), "任务 1", "bugfix"))
        dag.add_node(DAGTaskNode(str(t2_id), "任务 2", "test"))
        dag.add_dependency(str(t1_id), str(t2_id))

        scheduler = DAGScheduler(dag)
        # t1 失败阻断 t2
        scheduler.mark_failed(str(t1_id), reason="执行器崩溃")

        # 模拟 DB 任务
        db_t1 = Task(
            project_id="p1",
            decision_id=uuid.uuid4(),
            title="任务 1",
            type="bugfix",
            status=TaskState.PLANNED.value,
            extra={},
        )
        db_t1.id = t1_id

        db_t2 = Task(
            project_id="p1",
            decision_id=uuid.uuid4(),
            title="任务 2",
            type="test",
            status=TaskState.PLANNED.value,
            extra={},
        )
        db_t2.id = t2_id

        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(side_effect=lambda uid: db_t1 if uid == t1_id else db_t2)
        mock_repo.transition = AsyncMock()

        await scheduler.sync_to_repository(mock_repo)

        # 校验 transition 被正确调用为 TaskAction.BLOCK
        mock_repo.transition.assert_any_await(t1_id, TaskAction.BLOCK)
        mock_repo.transition.assert_any_await(t2_id, TaskAction.BLOCK)

        assert "blockage_record" in db_t2.extra
        rec = db_t2.extra["blockage_record"]
        assert isinstance(rec, dict)
        assert rec["blocked_by"] == str(t1_id)
