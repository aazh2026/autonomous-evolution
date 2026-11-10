"""多任务依赖图与拓扑结构（TASK-P2-012）。

设计依据：
- docs/design.md §3.2 七种任务类型与任务编排；
- docs/design.md §3.5C 单任务与多任务集成路径；
- docs/tasks.md TASK-P2-012（R-056 依赖图调度、DAG 执行、依赖失败的传播）。

核心概念：
1. **纯有向无环图（Acyclic Directed Graph）**：
   任务作为图的节点，任务间的前置依赖作为有向边（``predecessor -> successor``）。
   自环与多跳环路在图构建或校验时被严格检出，抛出包含完整环路链的 :class:`DependencyCycleError`。
2. **拓扑分层（Topological Layering）**：
   将任务按依赖深度划分为多个执行波次（Waves / Layers）。
   同一波次内的所有任务互不依赖，可以安全并发执行；下一波次必须在上一波次完成后方可调度。
3. **因果可达性（Reachability & Closure）**：
   提供快速查询直接前驱/后继、以及全部传递前驱（Ancestors / Upstream）与
   全部传递后继（Descendants / Downstream）的能力，为调度器失败阻断级联传播提供支持。
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from core.planning.roadmap import (
    DependencyCycleError,
    DependencyType,
    EvolutionRoadmap,
)
from core.task.state_machine import TaskState

if TYPE_CHECKING:
    from core.entities.task import Task, TaskDependency


class DAGError(Exception):
    """依赖图基类异常。"""


class UnknownNodeError(DAGError):
    """节点不存在。"""

    def __init__(self, node_id: str) -> None:
        super().__init__(f"任务节点 {node_id!r} 不在依赖图中")
        self.node_id = node_id


class DuplicateNodeError(DAGError):
    """节点重复添加。"""

    def __init__(self, node_id: str) -> None:
        super().__init__(f"任务节点 {node_id!r} 已存在于依赖图中")
        self.node_id = node_id


@dataclass(frozen=True, slots=True)
class DAGTaskNode:
    """依赖图中的任务节点。"""

    task_id: str
    title: str
    task_type: str
    priority: Decimal = field(default_factory=lambda: Decimal(0))
    status: TaskState = TaskState.PLANNED
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DAGEdge:
    """依赖图中的有向边（predecessor 必须先于 successor 完成）。"""

    predecessor_id: str
    successor_id: str
    dependency_type: DependencyType = DependencyType.FINISH_TO_START


class TaskDAG:
    """多任务依赖图（DAG）。

    维护节点集合与前驱/后继邻接表，支持环路检测、拓扑排序、分层调度波次与影响域分析。
    """

    def __init__(self) -> None:
        self._nodes: dict[str, DAGTaskNode] = {}
        # predecessor -> successors (下游)
        self._forward: dict[str, dict[str, DAGEdge]] = defaultdict(dict)
        # successor -> predecessors (上游)
        self._backward: dict[str, dict[str, DAGEdge]] = defaultdict(dict)

    @property
    def node_count(self) -> int:
        """节点总数。"""
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        """边总数。"""
        return sum(len(edges) for edges in self._forward.values())

    def add_node(self, node: DAGTaskNode) -> None:
        """向图中添加任务节点。"""
        if node.task_id in self._nodes:
            raise DuplicateNodeError(node.task_id)
        self._nodes[node.task_id] = node

    def get_node(self, task_id: str) -> DAGTaskNode:
        """获取任务节点。"""
        node = self._nodes.get(task_id)
        if node is None:
            raise UnknownNodeError(task_id)
        return node

    def list_nodes(self) -> tuple[DAGTaskNode, ...]:
        """获取所有任务节点。"""
        return tuple(self._nodes.values())

    def add_dependency(
        self,
        predecessor_id: str,
        successor_id: str,
        dependency_type: DependencyType = DependencyType.FINISH_TO_START,
    ) -> None:
        """添加有向依赖边：predecessor -> successor。"""
        if predecessor_id not in self._nodes:
            raise UnknownNodeError(predecessor_id)
        if successor_id not in self._nodes:
            raise UnknownNodeError(successor_id)
        if predecessor_id == successor_id:
            raise DependencyCycleError((predecessor_id, successor_id))

        edge = DAGEdge(
            predecessor_id=predecessor_id,
            successor_id=successor_id,
            dependency_type=dependency_type,
        )
        self._forward[predecessor_id][successor_id] = edge
        self._backward[successor_id][predecessor_id] = edge

    def direct_predecessors(self, task_id: str) -> frozenset[str]:
        """获取任务的直接前驱任务标识集。"""
        if task_id not in self._nodes:
            raise UnknownNodeError(task_id)
        return frozenset(self._backward[task_id].keys())

    def direct_successors(self, task_id: str) -> frozenset[str]:
        """获取任务的直接后继任务标识集。"""
        if task_id not in self._nodes:
            raise UnknownNodeError(task_id)
        return frozenset(self._forward[task_id].keys())

    def all_ancestors(self, task_id: str) -> frozenset[str]:
        """获取任务的全部传递上游任务标识集（直接 + 间接）。"""
        if task_id not in self._nodes:
            raise UnknownNodeError(task_id)

        visited: set[str] = set()
        queue: deque[str] = deque([task_id])
        while queue:
            curr = queue.popleft()
            for pred in self._backward[curr]:
                if pred not in visited:
                    visited.add(pred)
                    queue.append(pred)
        return frozenset(visited)

    def all_descendants(self, task_id: str) -> frozenset[str]:
        """获取任务的全部传递下游任务标识集（直接 + 间接）。"""
        if task_id not in self._nodes:
            raise UnknownNodeError(task_id)

        visited: set[str] = set()
        queue: deque[str] = deque([task_id])
        while queue:
            curr = queue.popleft()
            for succ in self._forward[curr]:
                if succ not in visited:
                    visited.add(succ)
                    queue.append(succ)
        return frozenset(visited)

    def find_cycle(self) -> tuple[str, ...] | None:
        """检测图中是否存在环路；若存在则返回闭环路径节点元组，否则返回 None。"""
        # 0: 未访问, 1: 正在当前路径访问, 2: 已完全访问
        visited: dict[str, int] = dict.fromkeys(self._nodes, 0)
        path: list[str] = []

        def dfs(node: str) -> list[str] | None:
            visited[node] = 1
            path.append(node)
            for succ in self._forward[node]:
                state = visited[succ]
                if state == 1:
                    # 发现回路
                    cycle_start = path.index(succ)
                    return [*path[cycle_start:], succ]
                if state == 0:
                    found = dfs(succ)
                    if found is not None:
                        return found
            path.pop()
            visited[node] = 2
            return None

        for n in sorted(self._nodes.keys()):
            if visited[n] == 0:
                cycle = dfs(n)
                if cycle is not None:
                    return tuple(cycle)
        return None

    def validate_acyclic(self) -> None:
        """校验图必须是无环的；若存在环路则抛出 DependencyCycleError。"""
        cycle = self.find_cycle()
        if cycle is not None:
            raise DependencyCycleError(cycle)

    def topological_sort(self) -> tuple[str, ...]:
        """返回拓扑排序序列（前驱排在后继前面）。"""
        self.validate_acyclic()

        # 入度表 (只统计直接前驱数量)
        in_degrees: dict[str, int] = {k: len(self._backward[k]) for k in self._nodes}
        # 初始入度为 0 的节点（按优先级降序、标识升序排列保持确定性）
        def node_sort_key(node_id: str) -> tuple[Decimal, str]:
            node = self._nodes[node_id]
            return (-node.priority, node_id)

        ready = [k for k, d in in_degrees.items() if d == 0]
        ready.sort(key=node_sort_key)

        result: list[str] = []
        while ready:
            curr = ready.pop(0)
            result.append(curr)
            for succ in self._forward[curr]:
                in_degrees[succ] -= 1
                if in_degrees[succ] == 0:
                    ready.append(succ)
                    ready.sort(key=node_sort_key)

        return tuple(result)

    def topological_layers(self) -> tuple[tuple[str, ...], ...]:
        """将所有任务划分为分层执行波次（Execution Waves）。

        第 0 层的任务无任何前驱，可立即并行运行；
        第 k 层的任务仅依赖第 0 ~ k-1 层的任务。
        """
        self.validate_acyclic()

        in_degrees: dict[str, int] = {k: len(self._backward[k]) for k in self._nodes}
        layers: list[tuple[str, ...]] = []

        current_layer = [k for k, d in in_degrees.items() if d == 0]
        while current_layer:
            current_layer.sort(key=lambda nid: (-self._nodes[nid].priority, nid))
            layers.append(tuple(current_layer))

            next_layer: list[str] = []
            for curr in current_layer:
                for succ in self._forward[curr]:
                    in_degrees[succ] -= 1
                    if in_degrees[succ] == 0:
                        next_layer.append(succ)
            current_layer = next_layer

        return tuple(layers)

    @classmethod
    def from_roadmap(cls, roadmap: EvolutionRoadmap) -> TaskDAG:
        """从阶段一产出的路线图实体构建依赖图。"""
        dag = cls()
        for pt in roadmap.tasks:
            dag.add_node(
                DAGTaskNode(
                    task_id=pt.key,
                    title=pt.title,
                    task_type=pt.task_type.value,
                    priority=pt.priority,
                    metadata={"decision_id": pt.decision_id},
                )
            )

        for dep in roadmap.dependencies:
            dag.add_dependency(
                predecessor_id=dep.predecessor_key,
                successor_id=dep.successor_key,
                dependency_type=dep.dependency_type,
            )
        dag.validate_acyclic()
        return dag

    @classmethod
    def from_tasks_and_dependencies(
        cls,
        tasks: Iterable[Task],
        dependencies: Iterable[TaskDependency],
    ) -> TaskDAG:
        """从数据库实体列表构建依赖图。"""
        dag = cls()
        for t in tasks:
            dag.add_node(
                DAGTaskNode(
                    task_id=str(t.id),
                    title=t.title,
                    task_type=t.type,
                    priority=t.priority,
                    status=TaskState(t.status),
                    metadata={"project_id": t.project_id, "executor": t.executor},
                )
            )

        for d in dependencies:
            pred_id = str(d.predecessor_id)
            succ_id = str(d.successor_id)
            dep_type = DependencyType(d.dependency_type)
            dag.add_dependency(pred_id, succ_id, dep_type)

        dag.validate_acyclic()
        return dag
