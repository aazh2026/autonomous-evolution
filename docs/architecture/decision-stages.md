# Director 五阶段决策图 —— 节点输入输出契约

设计依据：`docs/design.md` §5（把 Director 的决策拆成五个阶段）、
`docs/tasks.md` TASK-P1-050（T4：节点输入输出契约表）。

权威来源是代码，不是本文档：`core/planning/stages.py`（契约）、
`orchestration/langgraph/decision_graph.py`（图）。
`tests/unit/planning/test_stages.py` 与 `tests/unit/orchestration/test_decision_graph.py`
盯的就是下面这张表里的每一格。

## 0. 一句话说清这张图做什么

```
START → understand → diagnose → explore → decide → plan → END
```

**它只做两件事：按固定次序调用五个阶段，以及把状态存下来。**
"这一步的产出对不对"由领域层判 —— 见 §4。

## 1. 节点输入输出契约表

| # | 节点名 | 阶段 | 输入（状态字段） | 输入类型 | 输出（状态字段） | 输出类型 | 处理器方法 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `understand` | 认知 | `context` | `ProjectContext` | `assessment` | `ProjectAssessment` | `Understander.understand(context)` |
| 2 | `diagnose` | 诊断 | `assessment` | `ProjectAssessment` | `gaps` | `GapAnalysis` | `Diagnoser.diagnose(assessment)` |
| 3 | `explore` | 探索 | `gaps` | `GapAnalysis` | `discovery` | `DiscoveryResult` | `Explorer.explore(gaps)` |
| 4 | `decide` | 决策 | `discovery` | `DiscoveryResult` | `outcome` | `DecisionOutcome` | `Decider.decide(discovery)` |
| 5 | `plan` | 规划 | `outcome` | `DecisionOutcome` | `roadmap` | `EvolutionRoadmap` | `Planner.plan(outcome)` |

**每个节点还写一个字段：`completed_stages`**（`tuple[Stage, ...]`）。
它记的是"跑到哪了" —— 少了它，从 checkpoint 恢复时会把整条链重跑一遍。

**"输入（状态字段）"这一列是契约的一部分，不是实现细节。** 取错字段是最难发现的
一类错：类型往往还对得上（`GapAnalysis` 与 `ProjectAssessment` 不会混），但内容
不对题 —— 下游于是拿着一份"看起来没问题"的输入继续算。

所以这张表**由领域层持有**，节点不自己写死读哪个字段：`core/planning/stages.py` 的
`_STAGE_INPUT_FIELDS` 记着对应关系，`stage_input(state, stage)` 按它取，取不到就
就地报错。节点的写法因此是 `handlers.diagnose.diagnose(stage_input(state, Stage.DIAGNOSE))`
—— 少一个字段都不行，而类型由 `stage_input` 的逐阶段重载写死（传错类型，mypy 直接
报错）。

| 阶段 | 输入字段 | `_STAGE_INPUT_FIELDS` |
| --- | --- | --- |
| 认知 | `context` | `Stage.UNDERSTAND: "context"` |
| 诊断 | `assessment` | `Stage.DIAGNOSE: "assessment"` |
| 探索 | `gaps` | `Stage.EXPLORE: "gaps"` |
| 决策 | `discovery` | `Stage.DECIDE: "discovery"` |
| 规划 | `outcome` | `Stage.PLAN: "outcome"` |

| 输入类型 | 定义在 | 它装什么 |
| --- | --- | --- |
| `ProjectContext` | `core/planning/stages.py` | 项目 id、快照、**目标**（必填）、历史 |
| `ProjectAssessment` | `core/evolution/assessment.py` | 状态、目标差距[]、演进机会[]、建议[]、暂缓[] |
| `GapAnalysis` | `core/planning/stages.py` | 评估的**收窄**：状态、差距[]、暂缓[] |
| `DiscoveryResult` | `core/evolution/candidate.py` | 候选[]、被丢弃的趋势[] |
| `DecisionOutcome` | `core/planning/stages.py` | 决策轮 + 每条决策的放行凭据 |
| `EvolutionRoadmap` | `core/planning/roadmap.py` | 任务[]、依赖[]、排期[] |

## 2. 状态字段

`PipelineState`（`core/planning/stages.py`）是流水线累积的状态：

| 字段 | 类型 | 谁写它 | 初值 |
| --- | --- | --- | --- |
| `cycle_id` | `str` | 调用方 | 必填 |
| `context` | `ProjectContext` | 调用方 | 必填 |
| `assessment` | `ProjectAssessment \| None` | 认知 | `None` |
| `gaps` | `GapAnalysis \| None` | 诊断 | `None` |
| `discovery` | `DiscoveryResult \| None` | 探索 | `None` |
| `outcome` | `DecisionOutcome \| None` | 决策 | `None` |
| `roadmap` | `EvolutionRoadmap \| None` | 规划 | `None` |
| `completed_stages` | `tuple[Stage, ...]` | 每个节点 | `()` |

**状态是冻结的 Pydantic 模型，不是 dict。** `advance()` 返回**新**状态而不是
就地改 —— 就地改的话，"上一次的状态"就没了，而从 checkpoint 恢复时正需要那一份。

## 3. 阶段次序

`STAGE_ORDER`（`core/planning/stages.py`）把次序写死：

```
understand → diagnose → explore → decide → plan
```

**次序不是约定，是依赖。** 后一段的输入是前一段的产出：诊断读认知的结论、
探索读诊断的差距、决策读探索的候选、规划读决策的结论。次序乱了，产物就对不上 ——
而对不上时**不会报错**，只会得到一份看起来完整的错误计划。

## 4. 契约：谁在判"产出对不对"

**编排器不校验状态。** 实测：把一句自由文本塞进 `assessment` 字段，LangGraph
照跑不误，最后返回一份类型不对的状态（见
`tests/unit/orchestration/test_decision_graph.py::TestTheOrchestratorDoesNotValidateTheState`）。

所以判据在领域层 —— `core/planning/stages.py` 的 `advance()` 与 `stage_input()`。
它们守五条：

| # | 契约 | 谁守 | 破了会怎样 | 不守会怎样 |
| --- | --- | --- | --- | --- |
| 1 | **次序**：这个阶段必须是下一个该跑的 | `advance()` | `StageContractError` | 后面那份产物建在空地基上 |
| 2 | **类型**：产物必须是对应阶段的类型 | `advance()` | `StageContractError` | 得到一份"结构完整但内容错位"的状态 |
| 3 | **只跑一次**：同一个阶段不跑第二遍 | `advance()` | `StageContractError` | 已定下的结论被换成另一个样子，前一份就此消失 |
| 4 | **不越级**：跑完之后产物必须真的在状态里 | `advance()` | `StageContractError` | 状态与事实对不上 |
| 5 | **不缺输入**：这一阶段的输入必须在状态里 | `stage_input()` | `StageContractError` | 接着跑一份坏掉的 checkpoint，得到一份**看起来完整**的错计划 |

**第 5 条治的是哪一种坏状态。** "认知阶段标记为已完成，评估却不在状态里" —— 那种
状态只能来自一份坏掉的 checkpoint。接着跑它不会得到一份错计划（那样反而好），
它会得到一份**看起来完整**的错计划：五个阶段都跑了，路线图也出来了，只是诊断
阶段拿的是上一份陈年评估。

**为什么契约不写在图里。** 编排器要是对状态负责，就得在每个节点里重复一遍类型
判断 —— 而那些判断是**领域知识**（这条差距有没有证据、这份决策是不是 `record`），
写在领域层才能被单独校验与复用，也能给非 LangGraph 的调用方用。

**输入字段的对应关系也在领域层**（`_STAGE_INPUT_FIELDS`）。同样的道理：节点要是
自己写死读哪个字段，五处各写一遍，迟早有一处跟领域层那份对不上 —— 而对不上时
不会报错，只会读到另一个字段。

## 5. 持久化（checkpoint）

| 东西 | 在哪 | 说明 |
| --- | --- | --- |
| checkpoint 本体 | `graph_checkpoint` 表，BYTEA | 序列化类型标签单独存一列 `serde_type` |
| 节点写下的中间结果 | `graph_checkpoint_write` 表 | 按 `writes_sort_key` 的顺序读 —— 顺序不是细节 |
| 存储实现 | `infrastructure/database/checkpointer.py` | `SqlAlchemyCheckpointSaver`，只实现异步那一半 |
| 序列化 | `orchestration/langgraph/serialization.py` | msgpack，白名单**从状态模式算出来** |

**为什么存 BYTEA 而不是 JSONB。** checkpoint 里装的是 Pydantic 模型、`Decimal`、
元组 —— 存成 JSONB 就得在存之前把它们降级成 JSON，读回来再猜原来是什么类型。
**猜错类型的那份 checkpoint 比没有 checkpoint 更糟**：它看起来好好的。

**为什么白名单是算出来的。** 手写一份清单早晚会漏，而漏掉的症状是"跑到一半说
某个类型不让反序列化"，跟当初新加的那个模型八竿子打不着。
`state_allowlist()` 从状态模式出发把类型图走一遍。

**为什么不用 pickle。** pickle 能存任何东西，但读回来会**执行任意代码** ——
checkpoint 是"从库里读出来"的字节，库里那份要是被人改过，读它就等于执行了
改它的人写的代码。

**排序靠 checkpoint_id，不靠时间戳。** LangGraph 的 checkpoint id 是 uuid6，
高位是毫秒级时间戳，所以它的**字符串序就是时间序**。于是"取最新一个
checkpoint"是一条干净的 `ORDER BY checkpoint_id DESC LIMIT 1`。

**恢复时怎么接着跑。** LangGraph 认这个约定：给了输入就是开新一轮，
`invoke(None, config)` 就是续上一次。`run_decision_graph(graph, None, config=...)`
走的是后者 —— **跑完的阶段一次都不会重跑**（对"调模型"这样的节点，重跑不是
白干一遍，是又花钱又可能给出另一个结论）。

## 6. 节点是注入的

`build_decision_graph(handlers, checkpointer=...)` 只负责**接线**。五个处理器
（`StageHandlers`）由调用方给：

| 实现 | 用途 |
| --- | --- |
| 打桩（`tests/support/decision_stages.py`） | 跑通整图，端到端验产物 |
| 角色化 Prompt 实现（TASK-P1-057） | 生产路径 |

**图不该知道处理器是谁。** 换一个实现，图照样跑通，只是结果不同。

## 7. 用法

```python
from orchestration.langgraph.decision_graph import (
    build_decision_graph,
    run_decision_graph,
)
from infrastructure.database.checkpointer import SqlAlchemyCheckpointSaver
from orchestration.langgraph.serialization import state_serializer

saver = SqlAlchemyCheckpointSaver(session_factory, serde=state_serializer())
graph = build_decision_graph(handlers, checkpointer=saver)

# 第一次跑
final = await run_decision_graph(graph, state, config={"configurable": {"thread_id": "cycle-0007"}})

# 中途崩了之后接着跑（已完成的阶段不重跑）
final = await run_decision_graph(graph, None, config={"configurable": {"thread_id": "cycle-0007"}})
```

`run_decision_graph` 返回的是**领域状态**（`PipelineState`）。
LangGraph 1.x 的 `invoke` 返回的是通道字典 —— 编排层把这一步补上，
出了这张图状态仍然是领域对象。
