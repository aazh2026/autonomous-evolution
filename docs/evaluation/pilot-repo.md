# 试点仓库 —— tinylib

> 交付物：docs/tasks.md **TASK-P1-083**（试点仓库选取与误报率标注基线）。
> 验证方式：T1（pytest）· T3（人工评审）· T4（本文档）。

## 为什么选它

任务书给的选择标准有四条，tinylib 逐条对得上：

| 要求 | tinylib 的情况 |
| --- | --- |
| **有可运行的自动化测试** | 自带 43 条用例，`python -m pytest` 一条命令跑完，**零外部依赖**（只用标准库） |
| **有明确目标可量化** | 目标写死在 [`samples/pilot_repo/project.yaml`](../../samples/pilot_repo/project.yaml)：四个关键结果全部可机器判定（覆盖率、缺陷修复数、用例数、模块覆盖率） |
| **规模适中** | 库本体 7 个模块、208 行可执行语句 —— 一轮演进的上下文装得下，一次任务改不动全局 |
| **依赖已安装** | **没有任何第三方依赖**。沙箱里不需要装任何东西，也不需要联网 |

它还有两条别的仓库不容易有的性质：

- **缺陷是真的。** 本文列出的 12 条有效发现全部**跑得出来** —— 不是"看着像
  bug"，是真的能复现、能断言。
- **它有"故意留下的可改点"。** `strings.py` 刻意没写类型标注、`schedule.py`
  刻意把条件嵌套在一起 —— 这两处是"补类型标注""重构但不改变行为"这类任务
  的对象。**一个全是毛病的仓库只能产出"修 bug"这一类任务**；要演练任务
  分类的宽度，仓库里得有一些"改进了但没变坏"的地方。

## 目标 YAML

下面这份是 `samples/pilot_repo/project.yaml` 的**逐字副本** ——
`tests/system/test_pilot_labels.py` 会核对两者一致，改了一份忘了另一份会被
测出来。

```yaml
# 试点仓库的项目目标 —— **六要素齐全，缺哪一项载入时就报错**。
#
# 对应 docs/design.md §3.1 的目标模型（core/project/models.py 的
# ``ProjectGoal``）。这份文件是 TASK-P1-082（M2 自主闭环）的输入：
# **只给目标，不给任务** —— 系统自己去找问题、自己排优先级。
#
# 三条使用约定（与 docs/examples/project.yaml 同源）：
#   1. metric / unit / eval_method 是自由字符串，系统不硬编码任何指标名；
#   2. target 必须是可比较表达式（">= 0.95" / "<= 250" / "== true"），
#      解析不使用 eval()，配置里的表达式没有代码执行权；
#   3. budget 的 currency 可配置 —— 这里用 USD，因为成本基线
#      （docs/evaluation/baselines.yaml）也是 USD 记的。
#
# 基线数字来自 **2026-11-09 在本仓库上实测**（不是估的）：
#   整行覆盖率 0.740385（154/208 行）
#   测试数量 43
#   cli 模块覆盖率 0.000000
#   已确认缺陷 12 条（见 docs/issues.md）

project:
  id: pilot-tinylib
  name: tinylib（试点仓库）

  # ---------- 六要素之一：Objective ----------
  objective:
    - id: OBJ-001
      description: 修掉已知缺陷清单里的全部有效发现，并补齐对应的回归用例
    - id: OBJ-002
      description: 把 cli 模块从零覆盖提到有意义的水平
    - id: OBJ-003
      description: 在不新增功能、不改公开 API 的前提下提高整体质量

  # ---------- 六要素之二：Key Results ----------
  key_results:
    - id: KR-001
      metric: line_coverage
      target: ">= 0.90"
      unit: ratio
      metric_type: percentage
      eval_method: ci_measurement
      direction: higher_is_better
      baseline: "0.740385"
      description: 整仓行覆盖率 —— 由 pytest --cov 实测

    - id: KR-002
      metric: cli_module_coverage
      target: ">= 0.80"
      unit: ratio
      metric_type: percentage
      eval_method: ci_measurement
      direction: higher_is_better
      baseline: "0.000000"
      description: cli 模块覆盖率 —— 唯一对外接口，现在是 0

    - id: KR-003
      metric: known_defects_fixed
      target: ">= 12"
      unit: count
      metric_type: count
      eval_method: automated_test
      direction: higher_is_better
      baseline: "0"
      description: 已知缺陷清单里被修掉的有效发现条数 —— 分母是 12

    - id: KR-004
      metric: test_count
      target: ">= 70"
      unit: count
      metric_type: count
      eval_method: automated_test
      direction: higher_is_better
      baseline: "43"
      description: 仓库自有测试套件的用例数 —— 复现用例不进这个数

  # ---------- 六要素之三：Constraints ----------
  constraints:
    - id: C-001
      description: 所有代码变更必须通过仓库自带的自动化测试
      kind: all_changes_must_pass_tests
      machine_checkable: true
      expression: tests_passed == true

    - id: C-002
      description: 覆盖率不得低于当前基线 —— 修缺陷不能靠删用例
      kind: custom
      machine_checkable: true
      expression: line_coverage >= 0.740385

    - id: C-003
      description: 不引入任何第三方运行时依赖
      kind: custom
      machine_checkable: true
      expression: runtime_dependencies == 0

    - id: C-004
      description: 不改公开 API 的签名与语义
      kind: custom
      machine_checkable: true
      expression: public_api_changed == false

    - id: C-005
      description: 不绕过权限和安全策略
      kind: no_bypass_security_policy
      machine_checkable: true
      expression: policy_violations == 0

  # ---------- 六要素之四：Non-goals ----------
  non_goals:
    - id: NG-001
      description: 不新增功能 —— 这一轮只修缺陷、补用例
    - id: NG-002
      description: 不做与已知缺陷清单无关的重构
    - id: NG-003
      description: 不改动验收标准与权限策略本身

  # ---------- 六要素之五：Autonomy Policy ----------
  autonomy:
    rules:
      analyze_project:
        level: L4
        permission: autonomous
      plan_tasks:
        level: L4
        permission: autonomous
      code_changes:
        level: L4
        permission: autonomous
      test_execution:
        level: L4
        permission: autonomous
      branch_creation:
        level: L4
        permission: autonomous
      merge_to_main:
        level: L2
        permission: approval_required
      production_deployment:
        level: L1
        permission: advisory_only
      modify_governance:
        level: L1
        permission: advisory_only

  # ---------- 六要素之六：Stop Conditions ----------
  stop_conditions:
    - id: SC-001
      description: 全部关键结果达成
      kind: all_key_results_met

    - id: SC-002
      description: 预算耗尽
      kind: budget_exhausted

    - id: SC-003
      description: 连续 3 轮演进没有产生新的可验证改善
      kind: consecutive_failures_exceeded
      threshold: "3"

    - id: SC-004
      description: 累计演进时长超过 1 天
      kind: time_elapsed
      threshold: "1440"

  # ---------- 预算 ----------
  # 上限按实测成本基线（docs/evaluation/baselines.yaml）留出余量：
  # 单轮最贵的一次任务是 1.05 USD，一轮最多十几轮。
  budget:
    currency: USD
    max_daily_cost: "30"
    max_total_cost: "200"
    max_model_calls: 2000
    max_token_usage: 20000000
    max_wall_clock_minutes: 1440
    max_parallel_agents: 2

  # ---------- 仓库与环境 ----------
  repository:
    url: file://samples/pilot_repo
    default_branch: main
    local_path: samples/pilot_repo

  tech_stack:
    - python
```

目标要点：

- **只给目标，不给任务**（TASK-P1-082 的输入）。四个关键结果里只有目标值和
  基线，没有"先做 A 再做 B"。
- **约束里有两条是防"作弊"的**：`覆盖率不得低于当前基线`（修缺陷不能靠删
  用例）、`不改公开 API 的签名与语义`。
- **预算按实测成本基线留余量**：单轮最贵的一次任务是 1.05 USD（见
  [基线表](baselines.md)），日上限给到 30 USD。

## 初始质量基线

**2026-11-09 在本仓库实测**（`python -m pytest --cov=src/tinylib`）：

| 指标 | 数值 | 怎么来的 |
| --- | --- | --- |
| **整仓行覆盖率** | **0.740385** | 154 / 208 行 |
| **测试数量** | **43** | `pytest` 收集到的用例数（复现用例不在其中，见下） |
| **cli 模块覆盖率** | **0.000000** | 唯一对外接口，一条用例都没有 |
| **已确认缺陷** | **12** | 见 `samples/pilot_repo/docs/issues.md`（9 条可复现 + 3 条测试缺口） |
| **误报（无效发现）** | **8** | 同上，人工评审判定 |
| **运行时依赖** | **0** | 只用标准库 |

### 按模块的覆盖率

| 模块 | 覆盖率 | 未覆盖的行 |
| --- | --- | --- |
| `tinylib/__init__.py` | 100% | — |
| `tinylib/cli.py` | **0%** | 6-63 |
| `tinylib/config.py` | 100% | — |
| `tinylib/money.py` | 87% | 24, 29, 33, 37, 49 |
| `tinylib/schedule.py` | 91% | 31, 33, 35 |
| `tinylib/stats.py` | 87% | 26, 41, 46, 60, 69 |
| `tinylib/strings.py` | 94% | 38, 44 |
| **合计** | **74%** | **54 行** |

**`cli.py` 的 0% 不是一个数字，是一份指控。** 命令行是仓库唯一的对外接口，
而它一条用例都没有 —— 下面 9 条可复现缺陷里有 4 条就落在它身上，**一条都
没被现有用例挡住**。这不是巧合。

### 已知缺陷清单

完整清单在 [`samples/pilot_repo/docs/issues.md`](../../samples/pilot_repo/docs/issues.md)，
共 20 条：**12 条有效发现 + 8 条无效发现**。其中 9 条有效发现有**会失败的
复现用例**，放在 `samples/pilot_repo/known_issues/test_known_issues.py`。

**那个目录不在仓库默认的测试路径里**（`testpaths = ["tests"]`）。理由是两条：

1. 仓库自己的套件必须保持绿 —— 那些用例是**故意失败**的，它们复现的是还没
   修的缺陷；
2. **已发布的覆盖率基线不能被它弄脏** —— `baselines.yaml` 里那份基线是
   仓库**自有套件**跑出来的，把会失败的用例混进去，基线测的就不是同一个
   东西了。

复现用例用 `xfail(strict=True)` 标着：**缺陷修好之后它们会变成"意外通过"，
而 strict 把意外通过算作失败** —— 那正是要的效果：缺陷修好了，就把对应的
`xfail` 去掉，让它回到 `tests/` 里成为常规回归。

## 标注样本集

**这就是误报率的分母。** 机读版：
[`evaluation/labels/pilot_labels.jsonl`](../../evaluation/labels/pilot_labels.jsonl)。

- **条数恰为 20**（`evaluation.labels.pilot.EXPECTED_LABEL_COUNT`）。
- 每条五个字段：`issue_ref` / `is_valid_finding` / `category` / `annotated_by`
  / `annotated_at`。
- `is_valid_finding` 是**布尔**，不是 0/1 —— 写错类型时载入就报错。
- **谁标的、什么时候标的**都记着 —— 没有这两项，标注就没法复核。

### 样本集一览

| 编号 | 位置 | 类别 | 判定 | 证据 |
| --- | --- | --- | --- | --- |
| PILOT-001 | `cli.py` | runtime_error | 有效发现 | repro |
| PILOT-002 | `cli.py` | runtime_error | 有效发现 | repro |
| PILOT-003 | `cli.py` | cli_semantics | 有效发现 | repro |
| PILOT-004 | `cli.py` | api_contract | 有效发现 | repro |
| PILOT-005 | `money.py` | immutability | 有效发现 | repro |
| PILOT-006 | `config.py` | input_validation | 有效发现 | repro |
| PILOT-007 | `config.py` | input_validation | 有效发现 | repro |
| PILOT-008 | `strings.py` | algorithm | 有效发现 | repro |
| PILOT-009 | `schedule.py` | consistency | 有效发现 | repro |
| PILOT-010 | `cli.py` | test_gap | 有效发现 | review |
| PILOT-011 | `money.py` | test_gap | 有效发现 | review |
| PILOT-012 | `stats.py` | test_gap | 有效发现 | review |
| PILOT-013 | `strings.py` | documented_behavior | 无效发现 | review |
| PILOT-014 | `stats.py` | documented_behavior | 无效发现 | review |
| PILOT-015 | `stats.py` | documented_behavior | 无效发现 | review |
| PILOT-016 | `money.py` | design_intent | 无效发现 | review |
| PILOT-017 | `config.py` | documented_behavior | 无效发现 | review |
| PILOT-018 | `stats.py` | documented_behavior | 无效发现 | review |
| PILOT-019 | `money.py` | documented_behavior | 无效发现 | review |
| PILOT-020 | `config.py` | documented_behavior | 无效发现 | review |

**两类证据不许混。**

- `repro` —— 有一条**会失败的用例**（9 条）。缺陷是真的，而且可复现。
- `review` —— **人工评审**结论（11 条）。要么是行为与文档写明的意图冲突，
  要么是某处根本没有用例。**这类结论没有会失败的用例可写** —— 它们复现的
  是"缺一样东西"，不是"行为不对"。硬给它造一条会失败的用例，造出来的只会
  是**一条假的缺陷**。

### 类别分布

| 类别 | 条数 | 有效 | 无效 |
| --- | --- | --- | --- |
| runtime_error | 2 | 2 | 0 |
| cli_semantics | 1 | 1 | 0 |
| api_contract | 1 | 1 | 0 |
| immutability | 1 | 1 | 0 |
| input_validation | 2 | 2 | 0 |
| algorithm | 1 | 1 | 0 |
| consistency | 1 | 1 | 0 |
| test_gap | 3 | 3 | 0 |
| documented_behavior | 7 | 0 | 7 |
| design_intent | 1 | 0 | 1 |
| **合计** | **20** | **12** | **8** |

### 为什么八条"无效发现"比十二条"有效发现"更值钱

**全是真问题的清单会让误报率永远是 0** —— 那是个没有意义的数：它不区分
"系统很准"与"这个仓库没什么可误报的"。

误报率的分母之所以可信，靠的就是**有人认真看过并认定它们不是问题**。八条
无效发现里没有一条是"看错了"，它们各有各的根据 —— 文档写明了这是有意取舍
（PILOT-013/014/015/017/018），或者既有用例已经把这个行为钉住了
（PILOT-019/020），或者这个模块里根本没有汇率表、报它等于凭空造需求
（PILOT-016）。

**标注人和标注时间都记着**，就是为了这个：标注人换了、或者三个月后回头看，
这些结论都得能被重新审视一遍。

## 误报率怎么用

```python
from evaluation.evolution_evaluation import DecisionRecord, false_positive_rate_from_labels
from evaluation.labels import pilot_label_store

decisions = [
    DecisionRecord(decision_id="d-1", candidate_id="PILOT-001", decision="record"),
    DecisionRecord(decision_id="d-2", candidate_id="PILOT-013", decision="record"),
]
result = false_positive_rate_from_labels(decisions, pilot_label_store())
```

**三条规矩，改一条就毁掉这个指标：**

1. **分母只从标注集来。** 决策记录自己带的 `candidate_label` 一律被清掉
   （`apply_labels`）—— 让被评价的一方决定分母，那个分母就不可信了。
2. **标注集里没有的候选不进分母。** 没被审过的不能说它错，也不能说它对。
3. **标注集不在 → 报"基线未建立"，不报 0。** "还没标"与"全是无效"是相反
   的两件事；报 0 会让看板显示"误报率 0%"，读的人会以为系统很准。

## T3 评审记录：测试套件在沙箱内稳定运行

**2026-11-09，人工评审 + 机器复核。**

- **零外部依赖**：`tinylib` 只用标准库。沙箱里不需要装任何东西，也不需要
  联网 —— 这条是"能在沙箱内稳定运行"的**前提**，不是事后补的说明。
- **自测套件**：43 条用例，**0.01 秒**跑完，无随机性、无时钟依赖、无网络。
- **复现套件**：9 条用例全部落在 `xfail` 上 —— 机器复核过（见
  `tests/system/test_pilot_labels.py`）：**9 条全 xfail，没有一条 xpass、
  也没有一条 error**。全 xpass 意味着缺陷其实不存在（标注错了），有 error
  意味着复现本身坏了 —— 两种都得查。
- **结论**：试点仓库的测试套件**可以在沙箱内稳定运行**。T3 通过。
