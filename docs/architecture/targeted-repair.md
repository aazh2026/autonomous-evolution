# 定向修复 —— 哪儿坏了、改哪儿、还修不修得起

设计依据：`docs/design.md` §6（失败诊断与有界修复；"修复失败必须有次数、成本和
时间上限"）；`docs/tasks.md` TASK-P2-015（交付物 `agents/diagnosis/failure_classifier.py`；
验收条件"遇到不同类型验证失败时，自动定位失败原因并生成 Targeted 修复指令，
**修复次数与预算严格受全局配额限制**"）、TASK-P1-063（三重上限全局共享）、
R-098、I-04。

> **阶段一回答了"能不能自己修"，没回答"改哪儿"。** `core/task/repair.py` 的
> `FailureKind` 有七类，其中"必检项没过"这一类里装着语法错、依赖缺失、断言
> 失败、类型不匹配 —— **改法完全不一样**。这一层把那一类拆开，并且**说清楚
> 改哪一行**。

## 0. 一句话

**"检查没通过，请修复"不是修复指令 —— 它是一句废话。**

"改 `src/pkg/thing.py:1` 那处没用的导入（F401）"和"检查没通过，请修复"，
返工成本差着几个数量级：前者是一次编辑，后者是一次搜索加一次猜测。
**定向的意思就是把位置和动作一起说出来。**

## 1. 三个文件，三件事

| 文件 | 干什么 | 不干什么 |
| --- | --- | --- |
| `agents/diagnosis/failure_classifier.py` | 归类：是什么错、改哪儿 | **不问账本** |
| `orchestration/targeted_repair.py` | 把归类接上有界修复 | **不落状态** |
| `core/task/repair.py`（阶段一） | 配额：能不能修、修几次、花多少 | 不管"是什么错" |

**分类器是纯函数。** 同样的失败记录，同样的结论 —— **不该因为"额度还有没有"
而变**。额度一变结论就变，等于让"修什么"取决于"还剩多少钱"：那不是分类，
那是调度。

**配额只在协调器那头生效。** 分类器压根不知道账本的存在，它给出的结论只是一句
"这是语法错，改这三行"。

## 2. 归类靠结构化的东西，不靠读人话

阶段一在这件事上的理由要再说一遍：**措辞是工具定的**，改一次措辞就改一次分类，
而分类决定"要不要自己修"。所以这里的输入是**工具的机器可读输出**：

| 工具 | 输出形态 | 解析器 |
| --- | --- | --- |
| ruff | `--output-format=json`（一个数组） | `parse_ruff_json` |
| mypy | `--output=json`（一行一个对象） | `parse_mypy_json` |
| pytest | `--junitxml`（JUnit XML） | `parse_junit_xml` |
| Python 自己 | `SyntaxError` / `ValueError` | `parse_python_syntax_error` |
| 其它 | `[{file, line, code, message}]` | `parse_structured_diagnostics` |

**新工具接进来只有两条路**：把自己的诊断码归一化进 `_CODE_TO_TYPE`（更准），
或者只接解析器、用工具的默认类别（粗一点）。**改一处就够** —— 归一化码到失败
类型的映射是一张表，不是一堆 `if`。

### 2.1 判不出就判不出

认不出来的输出归 `FailureType.UNKNOWN`，而"判不出"归**需人工**。

**猜出来的类别决定"要不要自己修"，猜错一次就是一次不该发生的返工。**
工具不认识就什么也不给（`parse_tool_output` 对未知工具返回空），而不是硬套
一个解析器。

### 2.2 细化了可能比粗类更保守

粗类"必检项没过"是可自动修的；而细化出来的**架构违规**、**依赖版本冲突**归
"需人工"。这是有意的：**知道得更具体时，收窄自动修复的范围比扩大它安全。**

| 细类 | 能不能自己修 | 为什么 |
| --- | --- | --- |
| 语法错 / 编译错 | 能 | 改完就能重跑 |
| 依赖缺失 | 能 | 装上就行 |
| 类型不匹配 | 能 | 改类型标注或实现 |
| 断言失败 | 能 | 改实现或改断言 |
| 测试自身出错 | 能 | 补夹具 |
| 规范违规 | 能 | 按规则改 |
| **架构违规** | **需人工** | 改依赖方向是设计决定 |
| **依赖版本冲突** | **需人工** | 选哪个版本是口径问题 |
| 环境不可用 / 超时 / 已取消 | 需人工 | 不是代码的事 |
| 前提不成立 | **不可修** | 验收标准与现状冲突，修也没用 |

### 2.3 主因有固定顺序

一次失败里可能同时有好几类错（一个文件既有语法错又有未使用的导入）。主因按
固定顺序挑，**语法错排在最前**：

> 模块根本解析不了的时候，"类型不匹配""规范违规"全是解析器的连锁反应，报上
> 去只会让人去改本来没错的地方。

`TEST_SETUP_ERROR` 排在 `TEST_ASSERTION` 前面，理由一样：**夹具坏了，那条用例
压根没跑 —— 它的断言失败是假象。**

## 3. 定向施工说明

`Classification.targeted_instruction()` 产出的是**排给 Agent 的施工说明**：

```
修掉 1 处规范违规：src/pkg/thing.py:1:8 —— 规范违规（F401）：`os` imported but unused
修掉 2 处语法错：src/pkg/broken.py:1:7 —— 语法错：Expected a parameter or the end of
the parameter list；src/pkg/broken.py:1:8 —— 语法错：Expected `)`, found newline
```

三样都在里面：**文件、行、要改的那个东西**。

- **工具自己的码要带上**（`F401` 比"规范违规"有用 —— 前者能直接查到规则，
  后者只能重跑一遍才知道）。
- **最多列 5 处**，超出的要说明超了多少（"另有 3 处同类问题"）—— 截断而不说，
  读的人会以为就这么多。
- **不能是空的**，也不能是"随便改改"。空的指令会让 Agent 拿到一份没有着力点
  的施工说明，而"随便改改"正是无限修复循环的起点。
- **路径要相对工作区**。`/tmp/xyz/src/pkg/thing.py:1` 说"改那个临时目录里的
  东西"，而 `src/pkg/thing.py:1` 才说"改哪儿"。

## 4. 配额：全局的那一份

**这一半是阶段一的**（TASK-P1-063），这一层**不重复实现** —— 把归类结果翻译成
阶段一那份 `FailureDiagnosis`，交给 `RepairCoordinator` 就完事了。

```
classify(failure)  →  Classification  →  to_diagnosis()  →  RepairCoordinator.plan_diagnosed()
     纯函数                    结论            翻译              配额在这头生效
```

### 4.1 三条规矩

**一、上限是全局共享的，不是每个任务各一份。** 两个任务各修 3 次，看着都没超，
合起来就是 6 次，而上限说的是 5 次。**每任务独立的上限不是上限，是配额。**
所以两个任务要共用同一个 `RepairCoordinator`（一本账）。

**二、不能自动修的压根不占额度。** 没修就占一格，等于凭空少了一截却没人真的
修过。协调器**先判归类、再问账本**，顺序反了就会白占一格。

**三、被拒了就不排任务。** 配额批了才有 `repair_task`，被拒时 `instruction`
是空的 —— 被拒了还排任务，等于修了两遍。

### 4.2 被拒要说清撞的是哪一条上限

```python
second = await planner.plan(request, failure=failure)
assert second.allowed is False
assert second.outcome.limit is RepairLimit.COUNT  # 次数 / 成本 / 时间
assert second.instruction == ""  # 被拒了就不排任务
```

"修复停了"没法行动；"全局次数已达上限 1 次"才知道该提配额还是认账停下。

## 5. 怎么跑

```bash
# 演示：真的起 ruff / mypy / pytest 跑一遍
.venv/bin/python scripts/demo_targeted_repair.py

# 单测
.venv/bin/python -m pytest tests/unit/agents/diagnosis/ \
                      tests/unit/orchestration/test_targeted_repair.py -q
```

演示会造一个坏在六个不同地方的工作区（未使用的导入、语法错、类型不匹配、
依赖缺失、断言失败、夹具缺失），真的跑工具、真的归类，然后演示：

1. 六种坏法给出**六个不同的结论**；
2. 每条施工说明都落到了**具体的文件与行**上；
3. 全局上限是 1 时，**第二个任务被拒**，理由说的是"次数已达全局上限"；
4. **已取消的任务不占额度** —— 它压根没修。

## 6. 边界

- **这一层不落状态。** 与 `orchestration/gates` 同一套做法：算结论、给迁移，
  写状态的是调用方。两边都写就会有一边写丢。
- **这一层不新立修复机制。** 三重上限、每任务闸门、全局账本都是阶段一的东西，
  这里只负责把"哪儿坏了"说清楚。
- **不解析人话。** 输入只有工具的机器可读输出与阶段一的结构化字段。
- **分类不保证"准"，只保证"可追溯"。** 每条结论都带证据
  （`Classification.evidence`：文件、行、工具自己的码），对不上时能查。

## 7. 相关

- 阶段一的归类与有界修复：`core/task/repair.py`、`orchestration/repair.py`
- 六层验证（失败从哪儿来）：`docs/architecture/six-layer-verification.md`
- 阶段一的失败诊断：`core/task/execution_loop.py`（`FailureDiagnosis`）
