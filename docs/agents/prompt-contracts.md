# 角色化 Prompt 集 —— 职责边界与输入输出契约

设计依据：`docs/design.md` §3.2（"我建议不要只使用一个通用 Director Prompt，
而是构建四个逻辑角色"）、§5（"每一阶段都应该有结构化输出，并且允许下游独立
验证"）；`docs/tasks.md` TASK-P1-057（T4：每套 Prompt 的职责边界与输入输出）。

权威来源是代码，不是本文档：`agents/prompts/`（六套 Prompt 与六个输出模型）、
`orchestration/prompt_runtime.py`（运行配置 → 模型设置的那座桥）。
`tests/unit/agents/prompts/test_prompt_sets.py` 盯的就是下面这几张表。

## 0. 一句话说清这六套 Prompt 做什么

**六个角色，六套正文，六种输出形状。**

它们不是六个进程 —— `design.md` §3.2 明说"四个角色不一定是四个常驻 Agent……
建议初期作为四种独立的决策任务，由同一个 Director 调用不同模型或 Prompt 完成"。
所以这里有的是**六种看法**，不是六个长期驻留的对象。

## 1. 六个角色的契约表

| 角色 | 职责（`purpose`） | 边界（`boundary`） | 输入 | 输出 schema | 下游落点 |
| --- | --- | --- | --- | --- | --- |
| `analyst` | 理解项目现状，识别实现差距、缺陷与机会。 | 不决定做不做，不产出解法，不做质量评审，不列任务清单。 | 项目 id 与快照、项目目标（目标、关键结果、约束、非目标、停止条件、验收标准）、项目状态与历史 | `AnalystOutput` | `ProjectAssessment` |
| `strategist` | 判断产品价值、需求优先级与功能演进方向。 | 不决定做不做，不做技术选型，不做质量评审，不写任务与实现方案。 | 项目评估（目标差距与演进机会）、项目目标与关键结果、用户反馈与需求记录、已有的产品能力清单 | `StrategistOutput` | `EvolutionCandidate`（类别 `product`） |
| `architect` | 判断架构质量、技术债、技术选型与重构必要性。 | 不决定做不做，不判断产品价值，不做质量评审，不写任务与实现方案。 | 项目评估（目标差距与演进机会）、项目目标与架构约束、技术栈清单、模块与依赖结构、架构规则与既有的架构决策记录 | `ArchitectOutput` | `EvolutionCandidate`（类别 `architecture`） |
| `quality` | 评估质量、可靠性、安全性与测试充分性。 | 不决定做不做，不判断产品价值，不评审架构，不写任务与实现方案。 | 项目评估（目标差距与演进机会）、测试报告与覆盖率、缺陷记录、线上反馈与事故、可观测性数据、项目目标里的验收标准 | `QualityOutput` | `EvolutionCandidate`（类别 `quality`） |
| `director` | 对每个候选作出最终演进决策，并说清依据。 | 不找候选，不写任务，不做事实核验，不改项目目标。 | 项目评估与差距分析、探索阶段产出的候选清单、项目目标与约束、预算与成本上限、这一轮的资源与风险偏好 | `DirectorOutput` | `DecisionRound` |
| `critic` | 逐项做九项检查，给出一份读者能复核的报告。 | 不打分，不排序，不改候选，不决定最终做不做。 | 被审的候选、仓库与 Git 历史、测试报告与失败用例、任务表里已有的任务、产物登记处里已登记的证据、项目目标与关键结果、约束、Non-goals、停止条件 | `CriticOutput` | `CriticReport` |

**"边界"这一列不是客套话。** 六个角色看着像是在讨论同一件事（这个项目该往哪儿
走），一旦边界不清，它们就会互相覆盖：架构师说"这个功能不值得做"，策略师说
"这个架构该重写"，总监于是拿到两份互相矛盾的结论，却分不清是谁越界了。

所以边界**同时写在两处**：文档（这张表）与 `PromptSet.boundary`（代码里的字段），
而后者会被**写进模型看得见的正文** —— 只写在文档里没用，没人看文档，但模型每次
都看正文。

## 2. 三个提候选的角色共用一份输出形状

`strategist` / `architect` / `quality` 的输出结构**一模一样**（`CandidateRoleOutput`），
差别只有两处：看的角度（正文）与产出的候选归哪一类。

| 角色 | 钉死的类别 | `ClassVar` |
| --- | --- | --- |
| `strategist` | `product` | `StrategistOutput.category` |
| `architect` | `architecture` | `ArchitectOutput.category` |
| `quality` | `quality` | `QualityOutput.category` |

**类别由角色定，不由模型定。** `core.evolution.taxonomy` 把机会分成产品 / 架构 /
工程 / 质量 / 技术五类，为的是让它们**互相可比** —— 而可比的前提是每一类都由
同一双眼睛看出来的。模型自己填 `category` 的话，"质量策略师报了一条产品机会"
就会发生，而那两类机会的排期口径根本不同（产品的依据是用户价值，质量的依据是
缺陷率），放在一起比就是拿两把尺子量同一张桌子。

所以 `CandidateConclusion` **没有** `category` 字段。模型多说一句会被丢掉
（`CandidateConclusion._drop_the_category`），不影响结果 —— 但它也不作数。

## 3. 输出形状（字段级）

### 3.1 公共骨架

`RoleOutput`（`agents/prompts/base.py`）是所有角色输出的基类：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `model_config` | — | `frozen=True, extra="forbid"` —— **schema 之外的字段一律拒绝** |

`Conclusion` 是"一句话 + 凭什么这么说"的公共形状：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `str` | 下游靠它引用（机会回应哪些差距、决策针对哪个候选） |
| `statement` | `str` | 主张本身。一句话 |
| `evidence` | `tuple[Evidence, ...]` | **空了就意味着"没有依据"** |
| `status` | `ConclusionStatus` | **算出来的属性**，不是字段 |

### 3.2 `AnalystOutput`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `project_id` | `str` | 项目 id |
| `snapshot` | `str` | 评估所依据的项目快照 —— 这份评估的**时点** |
| `overall_status` | `OverallStatus` | `evolving` / `at_target` / `blocked` / `stalled` |
| `gaps` | `tuple[GapConclusion, ...]` | 差距（`severity` 四档） |
| `opportunities` | `tuple[OpportunityConclusion, ...]` | 机会（`category` / `priority` / `addresses`） |
| `recommendations` | `tuple[str, ...]` | 机会 id，**按推荐顺序排** |

### 3.3 `CandidateRoleOutput`（三个提候选的角色共用）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `candidates` | `tuple[CandidateConclusion, ...]` | 候选清单 |

`CandidateConclusion` 在 `Conclusion` 之上多几栏 —— 因为"值得做"这句话本身
信息量太小：一条只有主张的候选没法排期。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `hypothesis` | `str` | 假设：做了这件事会怎样 |
| `expected_outcome` | `tuple[str, ...]` | 预期结果，**至少一条** —— 事后拿它跟实际收益比 |
| `risk` | `RiskLevel` | 风险档（低 / 中 / 高） |
| `confidence` | `Decimal` | **证据的置信度**，0~1；不是"我觉得有把握" |
| `estimated_effort` | `str` | 预估成本（自由字符串 —— 成本单位不统一是常态） |
| `benefit` | `ProjectBenefit \| None` | 对本项目的实际收益论证 |

### 3.4 `DirectorOutput`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `cycle_id` | `str` | 这一轮演进的周期 id |
| `decisions` | `tuple[DecisionConclusion, ...]` | 这一轮的决策 |

`DecisionConclusion` 在 `Conclusion` 之上：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `candidate_id` | `str` | 被决策的候选 id |
| `decision` | `DecisionState` | `record` / `defer` / `reject` —— **没有第四态** |
| `requested_analysis` | `tuple[FollowUpAnalysis, ...]` | **只有 defer 非空** |
| `factors` | `PriorityFactors` | 六个因子，全部 0~1 |

**决策的"那句话"就是 `statement`**，落到领域模型里叫 `rationale`。这里不再单开
一栏 `rationale` —— 两栏散文迟早会说岔，而说岔了没人看得出来。

### 3.5 `CriticOutput`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `candidate_id` | `str` | 被审的候选 id |
| `outcomes` | `tuple[CriticConclusion, ...]` | **九项检查各一条**（`CriticReport` 会当场验） |

`CriticConclusion` 的字段是 `check` / `verdict` / `summary` / `evidence` /
`vetoes`。**它不继承 `Conclusion`** —— 证据词表不一样（见 §4）。

## 4. 两套证据词表

| 词表 | 定义在 | `kind` 取值 | 字段 |
| --- | --- | --- | --- |
| `Evidence` | `core/evolution/evidence.py` | `artifact` / `test_report` / `issue` / `file` | `kind` / `ref` / `note` |
| `CriticEvidence` | `agents/critic/checks.py` | 11 种（见下） | `kind` / `reference` / `detail` |

**为什么 Critic 要另一套。** 它要引用**非目标、约束、停止条件、关键结果** ——
那些东西在 `core` 的词表里压根没有（那儿只有产物、测试报告、问题单、文件路径
四种）。硬塞进 `core` 那一套，等于让领域层的证据词表为某一个角色的需要开口子。

`CriticEvidence` 的 11 种 `kind`：`repository` / `git_history` / `test_report` /
`project_status` / `task` / `artifact` / `non_goal` / `constraint` /
`stop_condition` / `goal` / `candidate`。

## 5. "有没有依据"是算出来的，不是模型说的

**这是这套 Prompt 集最重要的一条契约。**

一条结论有没有依据，看的是它**带了几条证据引用** —— 这是能数出来的事实。
如果让模型自己填 `status`，那么"这条结论一条证据都没有、模型却说它有依据"就
成了一种可能，而那正是这套系统要消灭的东西：一份**看起来有依据**的结论比一份
明说"没依据"的结论危险得多 —— 后者会被下游排除，前者会被当成事实用。

所以 `Conclusion.status` 是**属性**不是字段，由 `evidence` 算出来，模型填不了，
也填不掉。

```
有至少一条 evidence  →  supported
一条都没有            →  unsupported
```

**模型多说一句 `status` 怎么办。** 收下但不采用 —— `parse_output` 会把它从原始
JSON 里剔掉（`_strip_system_fields`，递归）。模型没读输出契约是事实，但那一份
输出里其余部分可能全是对的；为一句话毙掉一次调用（一次调用的钱、一次跑偏的分析）
太贵。

**真正毙掉输出的是契约本身**：`extra="forbid"` 意味着 schema 之外的字段会让解析
失败。字段名对不上是"模型没读契约"的硬证据 —— 那就该当场停下。

## 6. 没有依据的结论在下游怎么办

| 角色 | 转换方法 | 没依据的那些 |
| --- | --- | --- |
| `analyst` | `to_assessment()` | 差距与机会被丢掉；指向它们的推荐被丢掉；机会指向已掉队差距的引用被摘掉 |
| `strategist` / `architect` / `quality` | `to_candidates()` | 候选不进入领域层 |
| `director` | `to_decision_round()` | 决策不进入决策轮 —— **不派生任务** |
| `critic` | `to_report()` | 该项降级为 `unverified`，**不否决任何东西**（但仍然出现在报告里） |

**"排除"排除的是它的作用，不是它的存在。** 没依据的结论仍然留在角色的输出里 ——
"这里有一条拿不出证据的结论"本身就是下游要知道的事。抹掉它，下游就只看到一份
"看起来齐整"的输出，看不出哪一处是没底气的。

## 7. 解析契约

`parse_output(prompt_set, raw)` 三步，缺一不可：

1. **去掉围栏** —— ` ```json ` … ` ``` `。模型爱加围栏，加围栏**不是错误**。
   只脱围栏，不脱围栏里的散文：围栏里不是 JSON 时照样报错 ——
   "从散文里抠 JSON"那条路只会把一份不合格的输出变成一份**看起来合格**的输出。
2. **解析 JSON** —— 不是 JSON 就抛 `PromptParseError`。
3. **按这个角色自己的 schema 校验** —— 六套输出互相套不上（`AnalystOutput` 塞进
   `DirectorOutput` 会被 `extra="forbid"` 当场挡下）。

**`PromptParseError` 不是"调用失败"。** 调用成功了，模型也答了 —— 只是答得不合格。
两者的补救动作完全不同：调用失败要查网络与额度，解析失败要改 Prompt 或换模型。
混成一种错误，读日志的人就会去查错的方向。

## 8. 模型与温度是运行配置，不是代码

| 配置项 | 环境变量 | 默认值 |
| --- | --- | --- |
| `model_name` | `ASES_MODEL_NAME` | `claude-sonnet-4-6` |
| `model_temperature` | `ASES_MODEL_TEMPERATURE` | `0.2` |
| `model_max_tokens` | `ASES_MODEL_MAX_TOKENS` | `4096` |

配置住在 `infrastructure/config/settings.py`，桥住在
`orchestration/prompt_runtime.py`：

```python
configured = with_configured_model(ANALYST_PROMPT_SET, settings)
```

**桥为什么在编排层。** 放 `agents/` —— `agents/` 就要 import `infrastructure/`，
而 `agents/` 是角色层，它不关心配置是从环境变量来的还是从文件来的；放
`infrastructure/` —— `infrastructure/` 就要 import `agents/`，那是反过来的依赖：
基础设施层不该知道"角色化 Prompt"这种上层的东西。编排层是**把东西接起来**的
那一层，桥就在这儿。

**换模型不动 Prompt。** `with_configured_model` 返回**新的一套**，正文一个字都不改
—— 于是"同一套 Prompt 在两个模型上跑出的结果不一样"这件事是可比的，而不是在
两次调用之间被别处的改动悄悄改掉了。

**默认温度低。** 这套系统的输出要能被下游独立核验，而"发散"正是核验的对立面 ——
一份每次跑出来不一样的结论，没法被拿去跟别的结论比。
