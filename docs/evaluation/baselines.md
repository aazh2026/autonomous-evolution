# 基线表 —— 成本、时延与覆盖率

> 本文件由 `scripts/collect_baselines.py` 生成，**请勿手工编辑**。
> 机读版：[`baselines.yaml`](baselines.yaml)。

## 试点仓库

- **试点**：`pilot_repo`
- **记账币种**：USD
- **样本数**：10
- **任务类别数**：10

## 各类任务的基线（中位数）

| 任务类别 | 指标 | 中位数 | 单位 | 样本数 | 来源 |
| --- | --- | --- | --- | --- | --- |
| api_extension | 金额 | 0.419528 USD | 金额 | 1 | claude-code:MiniMax-M3:completed |
| api_extension | token | 284444 | token | 1 | claude-code:MiniMax-M3:completed |
| api_extension | 秒 | 39.600 | 秒 | 1 | claude-code:MiniMax-M3:completed |
| api_extension | 次 | 15 | 次 | 1 | claude-code:MiniMax-M3:completed |
| api_extension | 比例 | 0.740385 | 比例 | 1 | claude-code:MiniMax-M3:completed |
| bug_fix | 金额 | 1.014637 USD | 金额 | 1 | claude-code:MiniMax-M3:completed |
| bug_fix | token | 652272 | token | 1 | claude-code:MiniMax-M3:completed |
| bug_fix | 秒 | 341.575 | 秒 | 1 | claude-code:MiniMax-M3:completed |
| bug_fix | 次 | 30 | 次 | 1 | claude-code:MiniMax-M3:completed |
| bug_fix | 比例 | 0.746411 | 比例 | 1 | claude-code:MiniMax-M3:completed |
| changelog | 金额 | 0.345965 USD | 金额 | 1 | claude-code:MiniMax-M3:completed |
| changelog | token | 190921 | token | 1 | claude-code:MiniMax-M3:completed |
| changelog | 秒 | 33.105 | 秒 | 1 | claude-code:MiniMax-M3:completed |
| changelog | 次 | 9 | 次 | 1 | claude-code:MiniMax-M3:completed |
| changelog | 比例 | 0.740385 | 比例 | 1 | claude-code:MiniMax-M3:completed |
| doc_update | 金额 | 0.620042 USD | 金额 | 1 | claude-code:MiniMax-M3:completed |
| doc_update | token | 333462 | token | 1 | claude-code:MiniMax-M3:completed |
| doc_update | 秒 | 128.882 | 秒 | 1 | claude-code:MiniMax-M3:completed |
| doc_update | 次 | 15 | 次 | 1 | claude-code:MiniMax-M3:completed |
| doc_update | 比例 | 0.740385 | 比例 | 1 | claude-code:MiniMax-M3:completed |
| error_handling | 金额 | 0.667132 USD | 金额 | 1 | claude-code:MiniMax-M3:completed |
| error_handling | token | 446059 | token | 1 | claude-code:MiniMax-M3:completed |
| error_handling | 秒 | 79.673 | 秒 | 1 | claude-code:MiniMax-M3:completed |
| error_handling | 次 | 26 | 次 | 1 | claude-code:MiniMax-M3:completed |
| error_handling | 比例 | 0.753425 | 比例 | 1 | claude-code:MiniMax-M3:completed |
| feature_add | 金额 | 0.988107 USD | 金额 | 1 | claude-code:MiniMax-M3:completed |
| feature_add | token | 708674 | token | 1 | claude-code:MiniMax-M3:completed |
| feature_add | 秒 | 210.770 | 秒 | 1 | claude-code:MiniMax-M3:completed |
| feature_add | 次 | 28 | 次 | 1 | claude-code:MiniMax-M3:completed |
| feature_add | 比例 | 0.765217 | 比例 | 1 | claude-code:MiniMax-M3:completed |
| perf_tweak | 金额 | 基线未建立 | 金额 | 0 | — |
| perf_tweak | token | 基线未建立 | token | 0 | — |
| perf_tweak | 秒 | 900.062 | 秒 | 1 | claude-code:MiniMax-M3:timed_out |
| perf_tweak | 次 | 21 | 次 | 1 | claude-code:MiniMax-M3:timed_out |
| perf_tweak | 比例 | 0.743363 | 比例 | 1 | claude-code:MiniMax-M3:timed_out |
| refactor | 金额 | 1.050437 USD | 金额 | 1 | claude-code:MiniMax-M3:completed |
| refactor | token | 942845 | token | 1 | claude-code:MiniMax-M3:completed |
| refactor | 秒 | 216.636 | 秒 | 1 | claude-code:MiniMax-M3:completed |
| refactor | 次 | 33 | 次 | 1 | claude-code:MiniMax-M3:completed |
| refactor | 比例 | 0.751174 | 比例 | 1 | claude-code:MiniMax-M3:completed |
| test_add | 金额 | 0.378746 USD | 金额 | 1 | claude-code:MiniMax-M3:completed |
| test_add | token | 250919 | token | 1 | claude-code:MiniMax-M3:completed |
| test_add | 秒 | 55.812 | 秒 | 1 | claude-code:MiniMax-M3:completed |
| test_add | 次 | 12 | 次 | 1 | claude-code:MiniMax-M3:completed |
| test_add | 比例 | 0.740385 | 比例 | 1 | claude-code:MiniMax-M3:completed |
| type_annotate | 金额 | 0.631445 USD | 金额 | 1 | claude-code:MiniMax-M3:completed |
| type_annotate | token | 514200 | token | 1 | claude-code:MiniMax-M3:completed |
| type_annotate | 秒 | 81.165 | 秒 | 1 | claude-code:MiniMax-M3:completed |
| type_annotate | 次 | 30 | 次 | 1 | claude-code:MiniMax-M3:completed |
| type_annotate | 比例 | 0.741627 | 比例 | 1 | claude-code:MiniMax-M3:completed |

## 覆盖率分布（按模块）

| 模块 | 覆盖率中位数 | 样本数 |
| --- | --- | --- |
| tinylib | 1.000000 | 10 |
| tinylib.cli | 0.000000 | 10 |
| tinylib.config | 1.000000 | 10 |
| tinylib.money | 0.868421 | 10 |
| tinylib.schedule | 0.906250 | 10 |
| tinylib.stats | 0.871795 | 10 |
| tinylib.strings | 0.935484 | 10 |

## 成本预算校准

- 每日任务数假设：20
- 安全系数：2
- 结论：单任务中位成本 0.631445 USD，按每天 20 个任务、2 倍安全系数，max_daily_cost = 25.257800 USD
