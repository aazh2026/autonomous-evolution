# M1 单轮闭环 · 逐步走查（TASK-P1-081）

> **一句话**：把一条候选交给系统，从它被登记开始，一路走到"改动合入主分支、
> 独立验证通过、每一步都留了库里的行"，中间没有一个假动作 —— 唯一被替换掉的
> 是"写代码"那一段执行器，唯一被模拟的是"人按合并审批"那一按。

**验收对照**

| 口径 | 要求 | 在哪验 | 结果 |
| --- | --- | --- | --- |
| T1 | 回放 runner 跑通该链；每一步的 DB 记录可查 | `tests/integration/test_m1_cycle.py`（11 条） | ✅ |
| T2 | 单轮闭环端到端耗时与成本落在 P1-072 基线的 2 倍以内 | `apps/director/m1_budget.py` + 演示第 7 节 | ✅ 成本 1.00×、耗时 0.01× |
| T3 | 演示"从候选到验证通过"的完整链路 | `make demo-m1` → `scripts/demo_m1_cycle.py` | ✅ 退出码 0 |

---

## 1. 怎么跑

前置条件只有一个：**一个真的 PostgreSQL**。那些记录得真的在库里 —— 这是 T1 的
验收口径，不是"顺手落个库"。

```bash
make up          # 起本地全栈（PG + MinIO + API + worker）
make demo-m1     # 跑这一轮闭环
```

`make demo-m1` 等价于：

```bash
uv run python -m scripts.demo_m1_cycle
```

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--database-url` | PostgreSQL 连接串（默认读 `ASES_DATABASE_URL`，再默认 `postgresql+asyncpg://ases:ases@localhost:5432/ases`） |
| `--pilot` | 试点仓库源码目录（默认 `samples/pilot_repo`，会被**复制**成一份独立的 git 仓库） |
| `--work-root` | 工作根目录（默认在临时目录里建一个） |
| `--cycle-id` | 指定周期号（不给就每次跑造一个新的） |
| `--keep` | 保留工作目录（调试用） |

**跑完不清库是故意的。** "每一步的 DB 记录可查"这句话得有个东西可查。想清库
用 `make reset`。

---

## 2. 这一轮里什么是真的

| 环节 | 真 / 假 | 落在哪里 |
| --- | --- | --- |
| 观察 → 快照 | **真** | `orchestration/loop_b.py` |
| 差距分析 | **真**（差距内容来自手工注入的候选） | `apps/director/m1_cycle.py:584` |
| 候选登记 | **真** | `orchestration/loop_b.py:291` |
| 价值/风险/成本评分 | **真** | `apps/director/m1_cycle.py:622` |
| 决策（做 / 推迟 / 拒绝） | **真** | `PolicyDecisionMaker(DecisionPolicy())` |
| Critic 九问 | **真** | `agents/critic/` |
| 拆解成任务 | **真** | `core.planning.roadmap.TaskDecomposer` |
| 验收契约闸门（R-051） | **真** | `infrastructure/database/repositories.py:482` |
| **写代码** | **假** —— 按写死的补丁清单改文件 | `apps/director/m1_executor.py:104` |
| 独立验证 | **真** —— 真跑 pytest + 真跑 lint | `apps/director/m1_cycle.py:877` |
| 验收判定 | **真** | `core/acceptance/` |
| 人工审批 | **真**（"人"被模拟：批准人 `m1-approver`） | `core/approval/` + `PostgresApprovalStore` |
| 合并 | **真** —— 真的 `git merge` | `apps/director/m1_cycle.py:974` |
| 落库 | **真** —— 真的 PostgreSQL | `infrastructure/database/` |

**为什么只换执行器。** 链路上真正需要判断的地方（Critic 审查、契约判定、合并
闸门）全部保留；被换掉的"写代码"要么需要 API 凭据、要么需要网络 —— 两者都会
让"这条链能不能跑通"这个问题依赖于环境，而不是依赖于代码。**换掉执行器不改变
被验证的东西**：改完之后跑的仍是真 pytest、真的独立验证引擎、真的验收契约。

**候选本身是手工注入的。** M1 验的是"候选之后发生什么"，不是"候选是怎么找
出来的" —— 自主发现候选是 M2 的活（TASK-P1-082）。

---

## 3. 全链总览

**循环 B 的十五个节点**（`core/evolution/evolution_loop.py:57`）与它们各自
留下的库记录：

| # | 节点 | 落库 |
| --- | --- | --- |
| 1 | `observe_project` | `evolution_cycle`（开周期 —— 候选与决策要挂在它身上） |
| 2 | `build_project_snapshot` | 暂存在状态里（快照在第 14 步落库） |
| 3 | `analyze_goal_gap` | —— |
| 4 | `discover_candidates` | `evolution_candidate` |
| 5 | `gather_evidence` | —— |
| 6 | `evaluate_value_risk_cost` | —— |
| 7 | `worth_doing` | ——（分支点：值不值得做） |
| 8 | `record_defer_reject` | `evolution_decision` |
| 9 | `generate_evolution_plan` | ——（Critic 九问在这里跑） |
| 10 | `decompose_into_tasks` | `task`（初态 `APPROVED`） |
| 11 | `dependency_and_resource_scheduling` | —— |
| 12 | `execute_engineering_cycle` | **交给循环 A** —— 下面那串表 |
| 13 | `evaluate_actual_outcome` | ——（判定出 `no_change`） |
| 14 | `update_project_knowledge` | `project_snapshot` + `evolution_cycle` 收尾 |
| 15 | `update_goal_gap` | —— |

**循环 A 的十一个节点**（`core/task/execution_loop.py:60`）与任务状态的迁移：

| # | 节点 | 任务状态 |
| --- | --- | --- |
| 1 | `task_ready` | —— |
| 2 | `task_planner` | —— |
| 3 | `select_coding_agent` | —— |
| 4 | `create_isolated_workspace` | —— |
| 5 | `agent_executes` | `RUNNING` |
| 6 | `collect_diff_and_artifacts` | —— |
| 7 | `independent_verification` | `VERIFYING` |
| 8 | `acceptance_passed` | `PASSED` |
| 9 | `integration_and_merge_gate` | `INTEGRATING → MERGED` |
| 10 | `update_project_state` | —— |
| 11 | `execution_complete` | `COMPLETED` |

**任务行在库里的完整路径**（`apps/director/m1_cycle.py:1090` 的 `_MAIN_PATH`）：

```
APPROVED → PLANNED → READY → RUNNING → VERIFYING → PASSED → INTEGRATING → MERGED → COMPLETED
```

---

## 4. 逐步走查

### 4.0 准备：把试点仓库复制一份

演示**不碰 `samples/pilot_repo` 原目录**。`materialize_pilot_repo`
（`apps/director/m1_cycle.py:1336`）把源码复制到临时工作根目录，在副本里
`git init`、建 `main` 分支。**为什么必须复制**：这一轮会真的提交、真的合并 ——
在原目录上做这些事，等于让演示把仓库的 git 历史搅了。

紧接着 `write_test_report`（同文件 `:1368`）在副本上跑一遍试点仓库的复现套件，
产出 JUnit 报告 —— **这是 Critic 的 `problem_exists` 要读的东西**。没有这份
报告，"这个问题真的存在吗"就只能靠编。

### 4.1 观察 → 把这一轮开出来

`observe_project` 一边读项目定义（`samples/pilot_repo/project.yaml`），一边
**把周期开出来**（`orchestration/loop_b.py:250` → `evolution_cycle` 落一行）。

**周期必须在开始时就落库。** 候选与决策挂在它身上（`cycle_id` 外键）；事后
补记的周期挂不住那一轮已经产生的东西，那一轮的产出就永远算不进它的计数里。

### 4.2 快照：把观察定格

`build_project_snapshot` 取观察结果生成 `SnapshotView`，**版本号 = 最新版本 + 1**。
重复的版本号会被数据库的唯一约束挡下来（`project_snapshot (project_id, version)`
唯一约束）—— 悄悄覆盖掉上一份快照，事后没人知道它曾经存在过。

快照本身在这里**不落库**，它要到第 14 步（`update_project_knowledge`）才写进去。

### 4.3 差距 → 候选 → 证据

`analyze_goal_gap` 与 `discover_candidates` 用的都是手工注入的那一条候选
（`m1_candidate()`，`apps/director/m1_cycle.py:171`）：

```
候选    ：CLI 在空输入时抛未捕获的 ValueError
类别    ：quality
置信度  ：0.90
预计工作量：0.5d
证据    ：src/tinylib/cli.py（stats 分支没有空输入守卫）
          known_issues/test_known_issues.py（PILOT-001 复现用例）
假设    ：空输入是一句用法错误，不该是一段 traceback
```

**候选立刻落库**（`evolution_candidate`）—— 决策要挂在候选上（外键），而决策
是下一步作出的。等到那一步再回头补候选，这一轮的候选就挂不住决策了。

### 4.4 评分 → 决策

评分由 `_Evaluator`（`apps/director/m1_cycle.py:622`）算，决策由
`PolicyDecisionMaker(DecisionPolicy())` 作 —— **决策是算出来的，不是填报的**：

| 价值 | 影响 | 紧急度 | 成本 | 风险 | 置信度 |
| --- | --- | --- | --- | --- | --- |
| 0.70 | 0.60 | 0.50 | 0.30 | 0.20 | 0.90 |

落库的那一行（`evolution_decision`）：

```
决策   ：record（record = 记下来要做）
理由   ：价值 0.7、置信度 0.9、成本 0.3、风险 0.2 均达到门槛，按优先级推进
被推翻 ：（没有）
```

`record_defer_reject` 节点三种结局都要记 —— defer 与 reject 同样是合法结局。
只记"做了什么"而不记"为什么不做"，事后读的人分不清"判断为不值得"与"压根
没看见"。

### 4.5 Critic 的九问

`generate_evolution_plan` 里跑 Critic。九问是**三态**的：`passed`（查过了，通过）、
`failed`（查过了，不通过）、`unverified`（没查成）—— **把"没查成"报成"通过"，
Critic 就成了一台会点头的机器**。

这一轮的实际结果：

| 检查 | 判定 | 摘要 |
| --- | --- | --- |
| `problem_exists` | ⚠️ unverified | 测试报告里有 9 个失败的用例，但都与候选无关；没有失败用例能证明这个问题存在，而搜不到证明不了它不存在 |
| `evidence_sufficient` | ✅ passed | 2 条证据都指得到真的东西 |
| `not_already_solved` | ⚠️ unverified | 任务表里有 10 条相关任务（前几轮演示留下的）—— 是不是同一件事要人判断 |
| `respects_goals_and_constraints` | ✅ passed | 按项目状态查询，没有已确认违反的约束（项目声明了 5 条） |
| `has_lower_cost_alternative` | ⚠️ unverified | Git 历史里没有提到该问题的提交 —— 有没有更省的做法只能人判断 |
| `no_unnecessary_complexity` | ⚠️ unverified | 仓库里没有找到与该问题相关的文件 —— 命中多处不等于复杂 |
| `worth_doing_now` | ✅ passed | 还有关键结果没达成（4 个关键结果尚未达成） |
| `respects_non_goals` | ✅ passed | 逐条比对了 3 条 Non-goals，没有命中 |
| `does_not_trigger_stop_conditions` | ✅ passed | 没有触发任何停止条件 |

**零否决** —— 于是任务被派生出来。**四项 unverified 是这一版的真实水平，
不是 bug**（详见第 7 节）。

### 4.6 计划 → 拆解 → 排期

`TaskDecomposer` 把被放行的 `record` 决策拆成任务草稿，`land_roadmap`
（`infrastructure/database/repositories.py:356`）落成任务行。**只有 `record`
且被放行的决策才派生任务** —— 少一道门槛，任务就会从没有决策来源的地方冒出来。

任务行落库时的状态是 **`APPROVED`**（`create_from_decision` 给的初态），
执行器、分支、工作区此时都还是空。

`dependency_and_resource_scheduling` 之后有一道检查
（`check_resource_schedule`）：少派一个任务，那个任务就没人做 —— 而排期表看
上去仍然完整；让后继早于前驱开工，则是在一件还没做完的事上动土。两条都要断。

**排期顺手把执行器写进任务行。** 调度结果（谁、哪一波）只活在编排层的内存里；
任务行上不记，事后查一条任务就不知道它是哪个执行器做的 —— 会话行上倒是记着
执行器，可会话与任务是两件事，靠会话反推任务是**猜**。

### 4.7 循环 A 接手：十一个节点

`execute_engineering_cycle` **不执行代码** —— 它调用的是
`EngineeringCyclePort`，唯一的实现转手交给循环 A 的入口（`orchestration/loop_a.py`）。

| 事件 | 节点 | 这一步干了什么 |
| --- | --- | --- |
| #1 | `task_ready` | 任务就绪 |
| #2 | `task_planner` | 写施工说明（"在 stats 分支加上空输入守卫，返回退出码 2；把 PILOT-001 的复现用例搬回 `tests/` 作为常规回归"） |
| #3 | `select_coding_agent` | 选执行器 —— `m1-local-patcher` |
| #4 | `create_isolated_workspace` | 建独立工作区 + 分支 `agent/<task_id>` |
| #5 | `agent_executes` | **写代码**（补丁器按清单改文件、提交） |
| #6 | `collect_diff_and_artifacts` | 收改动：3 个文件 |
| #7 | `independent_verification` | **独立验证**：真跑 pytest + 真跑 lint |
| #8 | `acceptance_passed` | 验收契约判定 → 通过 |
| #9 | `integration_and_merge_gate` | 合并闸门 → 审批 → 真 `git merge` |
| #10 | `update_project_state` | 回写项目状态 |
| #11 | `execution_complete` | 收尾 |

### 4.8 验收契约闸门（R-051）

任务行从 `READY` 进 `RUNNING` 时，`TaskRepository.transition` 会检查
`acceptance_contract_bound == is_bound_to(task.acceptance_contract, str(task.id))`
—— **没有绑定契约的任务不许开工**。没绑就抛 `AcceptanceContractRequiredError`。

**这一道闸门在 M1 里是真过的。** 任务行落库那一刻契约还是空的，是 `_BindingJournal`
（`apps/director/m1_cycle.py:394`）在 `land_tasks` 之后**立刻**把契约绑上去的 ——
契约里的 `task_id` 必须等于任务行 id，而那个 id 在落库那一刻才确定，所以绑定只能
发生在落库之后。绕过去的话，任务就是"没有验收标准也开工"。

### 4.9 独立验证

验证器（`apps/director/m1_cycle.py:877`）用真的 `VerificationEngine`，在**副本
工作区**里跑：

| 检查 | 结论 |
| --- | --- |
| `unit_tests_pass` | ✅ 通过 |
| `lint_pass` | ✅ 通过 |

耗时 393 毫秒。落库两样东西：

- `verification_run`（结论 `passed`，验证者 `m1-independent-verifier`，挂在任务与周期上）
- `artifact`（`execution_trace`，`memory://m1/<run_id>/execution-trace`，65 字节，
  内容哈希 `d36ba20e…`）

### 4.10 验收 → 审批 → 合并

验收通过后进合并闸门。闸门按项目的自主权策略（`PolicyManager`）判断这一步
要不要人批 —— M1 的试点项目策略要批，于是生成 `approval_request`：

```
操作     ：merge_to_main
状态     ：approved
发起人   ：director
批准人   ：m1-approver
人看到的那份改动：
  known_issues/test_known_issues.py |  6 ------
   src/tinylib/cli.py                |  3 +++
   tests/test_cli.py                 | 14 ++++++++++++++
   3 files changed, 17 insertions(+), 6 deletions(-)
```

**审批看的是 diff，不是"任务成功"** —— 人看到的那份改动和 `git diff --stat`
逐字一致。人按下去之后，闸门真的 `git merge` 到 `main`，提交
`2e7c8d50b7c08c44ad1f7f10690b1c623f96b33e`。

### 4.11 快照 → 周期收尾 → 更新差距

`update_project_knowledge`（`orchestration/loop_b.py:471`）落快照与周期收尾：
`project_snapshot`（`main` 分支、观察时定格的那个提交）与 `evolution_cycle`
的收尾（状态 `completed`、结论 `no_change`、有起点有终点）。

`update_goal_gap` 是最后一个节点 —— 差距被更新，这一轮结束。

---

## 5. T2 的账：耗时与成本

`apps/director/m1_budget.py` 读 `docs/evaluation/baselines.yaml`（P1-072 采的
基线），拿**跨类别中位数**做基准，上限是基线的 2 倍：

| 指标 | 本轮 | 基线 | 倍数 | 上限 | 结论 |
| --- | --- | --- | --- | --- | --- |
| 成本 | 0.631445 USD | 0.631445 USD | 1.00× | 1.262890 USD | ✅ |
| 耗时 | 1.312 秒 | 105.024 秒 | 0.01× | 210.048 秒 | ✅ |

**成本是名义值，不是模型账单。** 补丁器不调模型，所以它报的是
`apps/director/m1_executor.py:50` 里写死的 `NOMINAL_COST`。**写死是为了可比**
—— T2 要拿它跟基线比，而这个数写在代码里而不是藏在某次运行里，换个环境就
不会换个答案。**它衡量的是"链路本身的开销能不能落在真执行器的量级内"，不是
"这一轮真花了多少钱"。**

**耗时 0.01× 也不该被当成"这个系统很快"。** 基线是"真 Coding Agent 干一件
真活"的时间；这一轮改的是三处已知位置的代码，且不调模型。两者的差主要是
**执行器换了**，不是链路变快了。

---

## 6. T1 的镜子：回放 runner

`tests/system/scripts/m1_cycle.yaml` 是 M1 那条链的**镜子**：同样的候选、同样
的置信度与风险、同样的四项评分、同样的任务标识与标题。

**它不写"结果"。** 决策由真的策略算，放行由真的
`agents.critic.approval.approval_for` 翻译 —— 脚本只提供"模型会给出的东西"。
这里唯一被换掉的是执行器（打桩）与验证器（打桩）：M1 那条链真的改了代码、
真的跑了 pytest；这一条不打桩就跑不起来。

`tests/integration/test_m1_cycle.py`（11 条）断言：

| 测试类 | 断言 |
| --- | --- |
| `TestTheChainLeavesAQueryableRowEverywhere` | 候选 / 决策 / 任务 / 执行器+分支+工作区 / 验证+产物 / 审批+快照+会话 / 周期 —— **每一张表都有一行查得到** |
| `TestTheWholeChainIsTraceable` | 循环 B 的 15 个节点、循环 A 的 11 条事件（序号 1…11）、任务状态序列一字不差 |
| `TestTheReplayRunnerReproducesTheSameChain` | 回放走出来的**形状**与 M1 一致：15 节点、11 事件、同样的状态迁移、同样的决策/审批/任务/结局 |
| `TestTheCostAndTheTimeStayWithinTwiceTheBaseline` | T2 —— 通过 `check_m1_budget` 判定 |

**一处必须说清的差别**：回放那一轮的任务行停在 `APPROVED`，而 M1 那一轮停在
`COMPLETED`。原因是**只有 M1 驱动的镜像适配器
（`_MirroringSessionJournal`）会把任务行推进状态机**；P1-065 的回放 runner 不
做这件事（它比的是**链路**，不是落库镜像）。测试把这个差别显式断言下来了，
而不是绕过去 —— 绕过去的话，两个 runner 的差异就永远没人知道。

---

## 7. 这一轮没做到的五件事

写下来是因为**验收要看到的是这一轮真实的样子**，不是一份理想清单。

### 7.1 Critic 的 `problem_exists` 是 UNVERIFIED

候选标题是中文（"CLI 在空输入时抛未捕获的 ValueError"），而测试报告里的
pytest node id 是英文（`known_issues/test_known_issues.py::test_the_cli_reports_an_empty_stats_input_instead_of_crashing`）。
`problem_exists` 用的是**子串匹配** —— 中文标题匹配不上英文 node id，于是判成
`unverified`。

**这不是误判，是"没查成"。** 而且 `unverified` **不否决** —— 一个查不成的检查
不该替一个真问题开绿灯，也不该把真问题毙掉。但它也**不构成证据**。

修它要改的是检查本身（用断言/失败信息而不是标题做匹配），不是改候选标题 ——
改标题能让检查"通过"，但那是**把问题藏进通过里**。

### 7.2 周期结局是 `no_change`

```
周期结局：no_change
          缺少可比的前后目标达成度，本轮没有可验证的改善
```

`classify_cycle`（`core/metrics/cycle.py`）要的是**可比的**前后达成度。这一轮
修掉了一条缺陷，但整体达成度没变 —— M1 的观察者不报达成度，所以没有可比的对
照物。**这不是失败**：一条缺陷修好了、系统如实说"这一轮没有可验证的整体改善"，
这正是"不夸大"该有的样子。**可计算的达成度差值是 M2 的验收项**
（TASK-P1-082 的 T2 ④）。

### 7.3 成本是名义值

见第 5 节。

### 7.4 `agent_session.status` 停在 `active`

`SessionJournal` 没有 close 方法，所以会话行不会被标成结束。这一轮的会话行
确实还在库里、状态是 `active`。**不影响验收**（会话行的用途是"查得出是谁在
哪台工作区上干的"），但它是一处未完成。

### 7.5 回放那一轮的任务行停在 `APPROVED`

见第 6 节。

---

## 8. 每一步的 DB 记录可查

演示跑完之后，库里留着这一轮的行。**下面每句都能直接跑**（把 `<cycle>` 换成
演示输出的周期号，`<task_id>` 换成任务 id）。

**先认一件容易踩的事：周期有两把号。** `evolution_cycle.cycle_id` 是业务号
（字符串，如 `cycle-m1-20261109-134259-85a730`），而 `evolution_cycle.id` 是 uuid
—— 后者才是 `evolution_candidate` / `evolution_decision` / `verification_run`
的 `cycle_id` 外键指向的东西。**`task` 表压根没有 `cycle_id` 列**，它挂在决策
上（`decision_id`），所以查任务得连着 join 两层。

```sql
-- 周期
select id, cycle_id, trigger, status, outcome, started_at, ended_at
from evolution_cycle where cycle_id = '<cycle>';

-- 候选（cycle_id 是 uuid 外键）
select c.id, c.category, c.title, c.status, c.confidence, c.estimated_effort
from evolution_candidate c
join evolution_cycle cy on cy.id = c.cycle_id
where cy.cycle_id = '<cycle>';

-- 决策
select d.id, d.candidate_id, d.decision, d.rationale, d.overridden_by
from evolution_decision d
join evolution_cycle cy on cy.id = d.cycle_id
where cy.cycle_id = '<cycle>';

-- 任务（含执行器、分支、工作区、契约绑定、起止时间）—— 没有 cycle_id 列
select t.id, t.title, t.type, t.status, t.executor, t.branch, t.workspace_id,
       t.repair_count, t.started_at, t.completed_at, t.acceptance_contract
from task t
join evolution_decision d on d.id = t.decision_id
join evolution_cycle cy on cy.id = d.cycle_id
where cy.cycle_id = '<cycle>';

-- 独立验证与产物
select id, task_id, cycle_id, result, verified_by, duration_ms, checks
from verification_run where task_id = '<task_id>';
select id, task_id, type, uri, hash, size_bytes
from artifact where task_id = '<task_id>';

-- 人工审批（含人看到的那份 diff）
select id, task_id, operation, status, requested_by, decided_by, diff_summary
from approval_request where task_id = '<task_id>';

-- 快照与会话
select id, project_id, version, commit, branch
from project_snapshot where project_id = 'pilot-tinylib' order by version desc limit 3;
select id, session_id, task_id, executor, agent_id, status
from agent_session where task_id = '<task_id>';

-- 循环 A 的十一个事件
select sequence, event_type, payload
from execution_event where task_id = '<task_id>' order by sequence;
```

---

## 9. 终端实录

下面是 `make demo-m1` 的一次完整输出（已去掉 uuid 之外的抖动；id 与 commit
每次运行都不同）。

```text
==============================================================================
ASES M1 单轮闭环演示（TASK-P1-081 · T3）
==============================================================================
  链路是真的：真的图、真的状态机、真的策略、真的落库、真的 pytest。
  执行器是本地补丁器（m1-local-patcher）：按写死的补丁改代码，
  不联网、不调模型、不花钱 —— M1 验的是链路，不是模型。
  唯一被模拟的是'人'：合并审批的那一按，演示替你按了。

──────────────────────────────────────────────────────────────────────────────
[1/7]  前置检查 —— 数据库在不在
──────────────────────────────────────────────────────────────────────────────
  数据库    ：postgresql+asyncpg://ases:ases@localhost:5432/ases
  版本      ：PostgreSQL 16.15 on aarch64-unknown-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit

──────────────────────────────────────────────────────────────────────────────
[2/7]  准备 —— 复制一份独立的试点仓库
──────────────────────────────────────────────────────────────────────────────
  试点源码  ：/Users/aaronzh/Documents/GitHub/autonomous-evolution/samples/pilot_repo
  工作根目录：/tmp/claude-501/ases-m1-demo-ntyrweaa
  原目录一个字都不动 —— 演示在副本上干活。

──────────────────────────────────────────────────────────────────────────────
[3/7]  手工注入候选 —— PILOT-001
──────────────────────────────────────────────────────────────────────────────
  候选      ：CLI 在空输入时抛未捕获的 ValueError
  证据      ：src/tinylib/cli.py（没有空输入守卫）
              known_issues/test_known_issues.py（复现用例）
  假设      ：空输入是一句用法错误，不该是一段 traceback
  候选是手工注入的 —— M1 验的是'候选之后发生什么'。

──────────────────────────────────────────────────────────────────────────────
[4/7]  走完闭环 —— 决策 → 任务 → 执行 → 独立验证 → 记录
──────────────────────────────────────────────────────────────────────────────
  周期号    ：cycle-m1-20261109-134259-85a730
  候选      ：CAND-M1-001
  决策      ：EVD-cycle-m1-20261109-134259-85a730-001
  任务      ：T-M1-001（903c3e9e-2664-4cef-9027-c9513303a856）
  验证结论  ：passed
    · unit_tests_pass：通过
    · lint_pass：通过
  验收      ：通过
  合并      ：已合入主分支
  合入提交  ：2e7c8d50b7c08c44ad1f7f10690b1c623f96b33e
  改动文件  ：3 个
  分支      ：agent/903c3e9e-2664-4cef-9027-c9513303a856
  周期结局  ：no_change
             缺少可比的前后目标达成度，本轮没有可验证的改善

──────────────────────────────────────────────────────────────────────────────
[4/7]  Critic 的九问 —— 放行凭据从这里来
──────────────────────────────────────────────────────────────────────────────
  ⚠️  problem_exists                 unverified
      查过测试报告：测试报告里有 9 个失败的用例，但都与「CLI 在空输入时抛未捕获的 ValueError」无关；没有失败用例能证明这个问题存在，而搜不到证明不了它不存在
  ✅ evidence_sufficient            passed
      2 条证据都指得到真的东西
  ⚠️  not_already_solved             unverified
      任务表里有相关任务：任务表里有 10 条与「CLI 在空输入时抛未捕获的 ValueError」相关的任务 ……
  ✅ respects_goals_and_constraints passed
      按项目状态查询，没有已确认违反的约束（项目声明了 5 条）
  ⚠️  has_lower_cost_alternative     unverified
      Git 历史里没有提到「CLI 在空输入时抛未捕获的 ValueError」的提交 —— 有没有更省的做法只能人判断
  ⚠️  no_unnecessary_complexity      unverified
      仓库里没有找到与「CLI 在空输入时抛未捕获的 ValueError」相关的文件 —— 命中多处不等于复杂，复杂不复杂只能人判断
  ✅ worth_doing_now                passed
      还有关键结果没达成（4 个关键结果尚未达成）
  ✅ respects_non_goals             passed
      逐条比对了 3 条 Non-goals，没有命中
  ✅ does_not_trigger_stop_conditions passed
      没有触发任何停止条件
  注：任务表查询会看到之前几轮演示留下的行 —— 演示不清库，
      那正是「每一步的 DB 记录可查」要留的东西。

──────────────────────────────────────────────────────────────────────────────
[5/7]  第 ① 步 · 候选落库 —— evolution_candidate
──────────────────────────────────────────────────────────────────────────────
  行 id      ：1e9917dc-80bd-4a3c-8ebb-e2a50c802a32
  类别      ：quality
  标题      ：CLI 在空输入时抛未捕获的 ValueError
  状态      ：accepted
  置信度    ：0.9000
  预计工作量：0.5d

──────────────────────────────────────────────────────────────────────────────
[5/7]  第 ② 步 · 决策落库 —— evolution_decision
──────────────────────────────────────────────────────────────────────────────
  行 id      ：6ca73f25-fbf8-44cf-ab38-8dadcdb86c7d
  挂在候选上：1e9917dc-80bd-4a3c-8ebb-e2a50c802a32
  决策      ：record（record = 记下来要做）
  理由      ：价值 0.7、置信度 0.9、成本 0.3、风险 0.2 均达到门槛，按优先级推进
  被推翻    ：（没有）

──────────────────────────────────────────────────────────────────────────────
[5/7]  第 ③ 步 · 任务落库 —— task
──────────────────────────────────────────────────────────────────────────────
  行 id      ：903c3e9e-2664-4cef-9027-c9513303a856
  标题      ：CLI 在空输入时抛未捕获的 ValueError
  类型      ：bugfix
  状态      ：COMPLETED
  执行器    ：m1-local-patcher
  分支      ：agent/903c3e9e-2664-4cef-9027-c9513303a856
  工作区    ：903c3e9e-2664-4cef-9027-c9513303a856
  返修次数  ：0
  开过工    ：是
  收过尾    ：是
  验收契约  ：已绑定

──────────────────────────────────────────────────────────────────────────────
[5/7]  第 ④ 步 · 独立验证落库 —— verification_run 与 artifact
──────────────────────────────────────────────────────────────────────────────
  行 id      ：3913d42d-4f00-4b41-8f8d-0ac03900e666
  挂在任务上：903c3e9e-2664-4cef-9027-c9513303a856
  挂在周期上：f6191e01-6302-4f10-8450-8fc8a88ec32c
  结论      ：passed
  验证者    ：m1-independent-verifier
  耗时      ：393 毫秒
  检查项    ：[{'check': 'unit_tests_pass', 'outcome': 'passed'}, {'check': 'lint_pass', 'outcome': 'passed'}]
  产物      ：execution_trace  memory://m1/3913d42d-4f00-4b41-8f8d-0ac03900e666/execution-trace  (65 字节)
  内容哈希  ：d36ba20e54086beadb007c7be4b91ddff70213260c66b31e7dd6704f58108f98

──────────────────────────────────────────────────────────────────────────────
[5/7]  第 ⑤ 步 · 人工审批落库 —— approval_request
──────────────────────────────────────────────────────────────────────────────
  行 id      ：77550941-b658-4087-81d2-e2a512fcb8f1
  挂在任务上：903c3e9e-2664-4cef-9027-c9513303a856
  操作      ：merge_to_main
  状态      ：approved
  发起人    ：director
  批准人    ：m1-approver
  人看到的那份改动：
    known_issues/test_known_issues.py |  6 ------
     src/tinylib/cli.py                |  3 +++
     tests/test_cli.py                 | 14 ++++++++++++++
     3 files changed, 17 insertions(+), 6 deletions(-)

──────────────────────────────────────────────────────────────────────────────
[5/7]  第 ⑥ 步 · 快照与会话 —— project_snapshot / agent_session
──────────────────────────────────────────────────────────────────────────────
  快照行 id  ：9043c665-2d30-4243-975b-9987b5a97bdf
  版本      ：10
  提交      ：ec42afa47853057c35cdcfee040a96e9d3224665
  分支      ：main
  会话行 id  ：8978fa4e-6d55-443f-9f06-168d675388c4
  会话      ：m1-session-001
  执行器    ：m1-local-patcher
  代理      ：director
  状态      ：active

──────────────────────────────────────────────────────────────────────────────
[5/7]  第 ⑦ 步 · 周期落库 —— evolution_cycle
──────────────────────────────────────────────────────────────────────────────
  行 id      ：f6191e01-6302-4f10-8450-8fc8a88ec32c
  周期号    ：cycle-m1-20261109-134259-85a730
  触发源    ：quality
  状态      ：completed
  结论      ：no_change
  起止      ：有起点 / 有终点

──────────────────────────────────────────────────────────────────────────────
[6/7]  循环 A 的事件轨迹 —— execution_event
──────────────────────────────────────────────────────────────────────────────
  #1   task_ready                       （没有状态）
  #2   task_planner                     （没有状态）
  #3   select_coding_agent              （没有状态）
  #4   create_isolated_workspace        （没有状态）
  #5   agent_executes                   RUNNING
  #6   collect_diff_and_artifacts       （没有状态）
  #7   independent_verification         VERIFYING
  #8   acceptance_passed                PASSED
  #9   integration_and_merge_gate       INTEGRATING → MERGED
  #10  update_project_state             （没有状态）
  #11  execution_complete               COMPLETED

  合计 11 条事件，序号 1…11

──────────────────────────────────────────────────────────────────────────────
[7/7]  T2 —— 耗时与成本
──────────────────────────────────────────────────────────────────────────────
  端到端耗时：1.312 秒
  本轮成本  ：0.631445 USD

  成本：0.631445 USD，基线 0.631445 USD（1.00 倍），上限 1.262890 USD —— 在基线 2 倍以内
  耗时：1.312 秒，基线 105.024 秒（0.01 倍），上限 210.048 秒 —— 在基线 2 倍以内
  ✅ 成本在基线 2 倍以内
  ✅ 耗时在基线 2 倍以内

==============================================================================
演示结束 —— 这一轮的行还留在库里，可以自己查
==============================================================================
  周期号：cycle-m1-20261109-134259-85a730
  任务  ：903c3e9e-2664-4cef-9027-c9513303a856
  成本  ：0.631445 USD（名义值，执行器写死在代码里）
```

**退出码 0。** 有任何一项超出基线 2 倍，演示会打印 `❌` 并返回 1。

---

## 10. 相关文件

| 文件 | 作用 |
| --- | --- |
| `apps/director/m1_cycle.py` | M1 的驱动 —— 组装循环 B / 循环 A 的 handlers，跑一整轮 |
| `apps/director/m1_executor.py` | 本地补丁执行器（写死的补丁清单 + 名义成本） |
| `apps/director/m1_budget.py` | T2 的账 —— 读基线，判 2 倍上限 |
| `infrastructure/database/repositories.py` | `assign_executor` / `assign_workspace` —— "谁来做""在哪儿干"记在**任务行**上 |
| `scripts/demo_m1_cycle.py` | T3 的演示脚本 |
| `tests/system/scripts/m1_cycle.yaml` | 回放用的镜子脚本（T1） |
| `tests/integration/test_m1_cycle.py` | T1 的 11 条断言 |
| `tests/integration/test_task_assignment.py` | 任务行那两栏的 7 条断言（不许被改写） |
| `docs/evaluation/baselines.yaml` | T2 的基线（P1-072 采的） |
