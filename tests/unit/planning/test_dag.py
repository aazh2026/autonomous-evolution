"""多任务依赖图单元测试（TASK-P2-012）。

覆盖要求：
- R-056：依赖图调度、拓扑排序、分层波次与环路死锁检测；
- T1：自动化验证节点与边管理、自环与多跳环检测、因果闭包查询及路线图构建。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from core.entities.task import Task, TaskDependency
from core.planning.dag import (
    DAGTaskNode,
    DuplicateNodeError,
    TaskDAG,
    UnknownNodeError,
)
from core.planning.roadmap import (
    DependencyCycleError,
    EvolutionRoadmap,
    PlannedTask,
    ScheduleSlot,
    TaskType,
)
from core.planning.roadmap import (
    TaskDependency as RoadmapDependency,
)


class TestDAGConstructionAndValidation:
    """DAG 构建、节点管理与环路检测。"""

    def test_add_nodes_and_edges(self) -> None:
        dag = TaskDAG()
        n1 = DAGTaskNode(task_id="t1", title="任务 1", task_type=TaskType.FEATURE.value)
        n2 = DAGTaskNode(task_id="t2", title="任务 2", task_type=TaskType.BUGFIX.value)
        dag.add_node(n1)
        dag.add_node(n2)

        assert dag.node_count == 2
        assert dag.get_node("t1").title == "任务 1"

        dag.add_dependency("t1", "t2")
        assert dag.edge_count == 1
        assert dag.direct_predecessors("t2") == frozenset({"t1"})
        assert dag.direct_successors("t1") == frozenset({"t2"})

    def test_duplicate_node_error(self) -> None:
        dag = TaskDAG()
        n1 = DAGTaskNode(task_id="t1", title="任务 1", task_type=TaskType.FEATURE.value)
        dag.add_node(n1)
        with pytest.raises(DuplicateNodeError):
            dag.add_node(n1)

    def test_unknown_node_error(self) -> None:
        dag = TaskDAG()
        n1 = DAGTaskNode(task_id="t1", title="任务 1", task_type=TaskType.FEATURE.value)
        dag.add_node(n1)
        with pytest.raises(UnknownNodeError):
            dag.add_dependency("t1", "t_unknown")
        with pytest.raises(UnknownNodeError):
            dag.get_node("t_unknown")

    def test_self_dependency_raises_cycle_error(self) -> None:
        dag = TaskDAG()
        n1 = DAGTaskNode(task_id="t1", title="任务 1", task_type=TaskType.FEATURE.value)
        dag.add_node(n1)
        with pytest.raises(DependencyCycleError) as exc_info:
            dag.add_dependency("t1", "t1")
        assert "t1" in exc_info.value.cycle

    def test_multi_node_cycle_detection(self) -> None:
        dag = TaskDAG()
        for i in range(1, 4):
            dag.add_node(
                DAGTaskNode(
                    task_id=f"t{i}",
                    title=f"任务 {i}",
                    task_type=TaskType.FEATURE.value,
                )
            )
        dag.add_dependency("t1", "t2")
        dag.add_dependency("t2", "t3")
        dag.add_dependency("t3", "t1")  # 形成环路 t1 -> t2 -> t3 -> t1

        assert dag.find_cycle() is not None
        with pytest.raises(DependencyCycleError) as exc_info:
            dag.validate_acyclic()
        assert len(exc_info.value.cycle) >= 3


class TestTopologicalSortAndLayers:
    """拓扑排序与分层波次验证。"""

    def test_diamond_graph_sort_and_layers(self) -> None:
        """菱形依赖图:

             t1
            /  \\
           t2   t3
            \\  /
             t4
        """
        dag = TaskDAG()
        dag.add_node(DAGTaskNode("t1", "根任务", TaskType.FEATURE.value, priority=Decimal("0.9")))
        dag.add_node(DAGTaskNode("t2", "左子任务", TaskType.BUGFIX.value, priority=Decimal("0.5")))
        dag.add_node(DAGTaskNode("t3", "右子任务", TaskType.TEST.value, priority=Decimal("0.8")))
        dag.add_node(DAGTaskNode("t4", "汇聚任务", TaskType.DOCS.value, priority=Decimal("0.1")))

        dag.add_dependency("t1", "t2")
        dag.add_dependency("t1", "t3")
        dag.add_dependency("t2", "t4")
        dag.add_dependency("t3", "t4")

        # 校验拓扑排序合法性
        order = dag.topological_sort()
        assert order[0] == "t1"
        assert order[-1] == "t4"
        assert order.index("t1") < order.index("t2") < order.index("t4")
        assert order.index("t1") < order.index("t3") < order.index("t4")

        # 校验分层波次
        layers = dag.topological_layers()
        assert len(layers) == 3
        assert layers[0] == ("t1",)
        # 第二层两个节点由于 t3 priority (0.8) > t2 priority (0.5)，t3 排在前面
        assert layers[1] == ("t3", "t2")
        assert layers[2] == ("t4",)

    def test_ancestor_and_descendant_closure(self) -> None:
        """传递因果闭包查询。"""
        dag = TaskDAG()
        for i in range(1, 6):
            dag.add_node(
                DAGTaskNode(
                    task_id=f"t{i}",
                    title=f"任务 {i}",
                    task_type=TaskType.FEATURE.value,
                )
            )
        # 链条: t1 -> t2 -> t3 -> t4; t5 独立
        dag.add_dependency("t1", "t2")
        dag.add_dependency("t2", "t3")
        dag.add_dependency("t3", "t4")

        assert dag.all_ancestors("t4") == frozenset({"t1", "t2", "t3"})
        assert dag.all_descendants("t1") == frozenset({"t2", "t3", "t4"})
        assert dag.all_ancestors("t1") == frozenset()
        assert dag.all_descendants("t4") == frozenset()
        assert dag.all_ancestors("t5") == frozenset()
        assert dag.all_descendants("t5") == frozenset()


class TestDAGFactories:
    """从路线图及持久化实体构建 DAG。"""

    def test_from_roadmap(self) -> None:
        roadmap = EvolutionRoadmap(
            cycle_id="cycle-001",
            tasks=(
                PlannedTask(
                    key="task-a",
                    title="任务 A",
                    task_type=TaskType.FEATURE,
                    candidate_id="cand-1",
                    decision_id="dec-1",
                    priority=Decimal("0.7"),
                ),
                PlannedTask(
                    key="task-b",
                    title="任务 B",
                    task_type=TaskType.TEST,
                    candidate_id="cand-1",
                    decision_id="dec-1",
                    priority=Decimal("0.5"),
                ),
            ),
            dependencies=(
                RoadmapDependency(
                    predecessor_key="task-a",
                    successor_key="task-b",
                ),
            ),
            schedule=(
                ScheduleSlot(key="task-a", position=1, wave=1),
                ScheduleSlot(key="task-b", position=2, wave=2),
            ),
        )

        dag = TaskDAG.from_roadmap(roadmap)
        assert dag.node_count == 2
        assert dag.edge_count == 1
        assert dag.direct_predecessors("task-b") == frozenset({"task-a"})

    def test_from_db_entities(self) -> None:
        id1 = uuid.uuid4()
        id2 = uuid.uuid4()
        t1 = Task(
            project_id="proj",
            decision_id=uuid.uuid4(),
            title="数据库任务 1",
            type="bugfix",
            priority=Decimal("0.6"),
            status="PLANNED",
        )
        t1.id = id1
        t2 = Task(
            project_id="proj",
            decision_id=uuid.uuid4(),
            title="数据库任务 2",
            type="docs",
            priority=Decimal("0.4"),
            status="PLANNED",
        )
        t2.id = id2

        dep = TaskDependency(
            project_id="proj",
            predecessor_id=id1,
            successor_id=id2,
            dependency_type="finish_to_start",
        )

        dag = TaskDAG.from_tasks_and_dependencies([t1, t2], [dep])
        assert dag.node_count == 2
        assert dag.direct_successors(str(id1)) == frozenset({str(id2)})
