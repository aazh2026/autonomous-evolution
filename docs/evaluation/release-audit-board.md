# 高风险放行审计 —— 真语料过一遍新闸

本看板的每一个数都来自**真库里的审批记录**（``approval_request`` 表），
判定走的是**生产的那条路**（``HighRiskReviewFlow`` 的四步）。

- 语料：**45** 条审批请求，来自 ``approval_request`` 表
- 涉及任务：**45** 个（**一个任务就是一次变更**）
- 操作分布：merge_to_main × 45
- 状态分布：approved × 45
- 提出人：director × 45
- 表态人：lr-approver × 32、m1-approver × 13
- **每个任务有几条审批**：1 × 45
- 时间范围：2026-11-09T13:25:45.039076+00:00 → 2026-11-10T09:38:37.053406+00:00

## 现行规矩（基线）

旧审批门只要求 somebody ≠ 提出人表态就放行 —— 不看有没有第二个人，
不看这个人懂不懂。

| 口径 | 条数 |
| --- | --- |
| 审批请求总数 | 45 |
| 按旧规矩放行 | 45 |
| **提出人自己批了自己**（旧规矩拦不住） | 0 |

## 三个场景

| 场景 | 性质 | 变更数 | 放行 | 拦住 |
| --- | --- | --- | --- | --- |
| as-is（照真语料原样跑（实测）） | **实测** | 45 | 0 | 45 |
| paired（补上另一位真实评审人（推演）） | 推演 | 45 | 45 | 0 |
| architecture（当作架构级改动（推演）） | 推演 | 45 | 0 | 45 |

**as-is 是实测** —— 照真语料原样送进新闸，量的是“按现在这样跑下去会怎样”。

**paired 与 architecture 是推演** —— 推演不是实测：它们回答的是
“闸门在该放的时候放不放”“资格那一层有没有牙齿”。

## 拦住的理由

### as-is（照真语料原样跑（实测））

| 规矩 | 条数 |
| --- | --- |
| 独立评审份数不够（`not_enough_reviews`） | 45 |

### paired（补上另一位真实评审人（推演））

| 规矩 | 条数 |
| --- | --- |
| 放行了（`passed`） | 45 |

### architecture（当作架构级改动（推演））

| 规矩 | 条数 |
| --- | --- |
| 评审资格不足（`missing_scope`） | 45 |

## 每一份变更的结论

### as-is（照真语料原样跑（实测））

| 变更 | 任务 | 风险档 | 要几份 | 批过的人 | 结论 |
| --- | --- | --- | --- | --- | --- |
| `336c18c4` | `336c18c4` | high | 2 | m1-approver | 独立评审份数不够 |
| `acd9d379` | `acd9d379` | high | 2 | m1-approver | 独立评审份数不够 |
| `e8c8f84a` | `e8c8f84a` | high | 2 | m1-approver | 独立评审份数不够 |
| `c950ba2e` | `c950ba2e` | high | 2 | m1-approver | 独立评审份数不够 |
| `65443e78` | `65443e78` | high | 2 | m1-approver | 独立评审份数不够 |
| `f6cb7280` | `f6cb7280` | high | 2 | m1-approver | 独立评审份数不够 |
| `d59dbf2a` | `d59dbf2a` | high | 2 | m1-approver | 独立评审份数不够 |
| `61ffb6d0` | `61ffb6d0` | high | 2 | m1-approver | 独立评审份数不够 |
| `2a6fdd22` | `2a6fdd22` | high | 2 | m1-approver | 独立评审份数不够 |
| `903c3e9e` | `903c3e9e` | high | 2 | m1-approver | 独立评审份数不够 |
| `7f5e9a4b` | `7f5e9a4b` | high | 2 | m1-approver | 独立评审份数不够 |
| `2f725c21` | `2f725c21` | high | 2 | m1-approver | 独立评审份数不够 |
| `dc7fbb8c` | `dc7fbb8c` | high | 2 | m1-approver | 独立评审份数不够 |
| `ff128c70` | `ff128c70` | high | 2 | lr-approver | 独立评审份数不够 |
| `2febb32f` | `2febb32f` | high | 2 | lr-approver | 独立评审份数不够 |
| `57d841ef` | `57d841ef` | high | 2 | lr-approver | 独立评审份数不够 |
| `4666642c` | `4666642c` | high | 2 | lr-approver | 独立评审份数不够 |
| `deac9a9b` | `deac9a9b` | high | 2 | lr-approver | 独立评审份数不够 |
| `f6d0aabe` | `f6d0aabe` | high | 2 | lr-approver | 独立评审份数不够 |
| `64c1ae75` | `64c1ae75` | high | 2 | lr-approver | 独立评审份数不够 |
| `f1a90a3e` | `f1a90a3e` | high | 2 | lr-approver | 独立评审份数不够 |
| `899c7efe` | `899c7efe` | high | 2 | lr-approver | 独立评审份数不够 |
| `7984b1e2` | `7984b1e2` | high | 2 | lr-approver | 独立评审份数不够 |
| `5ead672a` | `5ead672a` | high | 2 | lr-approver | 独立评审份数不够 |
| `a3edcc5a` | `a3edcc5a` | high | 2 | lr-approver | 独立评审份数不够 |
| `4a48bfe2` | `4a48bfe2` | high | 2 | lr-approver | 独立评审份数不够 |
| `2841f810` | `2841f810` | high | 2 | lr-approver | 独立评审份数不够 |
| `9d871f62` | `9d871f62` | high | 2 | lr-approver | 独立评审份数不够 |
| `127c6adf` | `127c6adf` | high | 2 | lr-approver | 独立评审份数不够 |
| `90565a0c` | `90565a0c` | high | 2 | lr-approver | 独立评审份数不够 |
| `bc5fe7db` | `bc5fe7db` | high | 2 | lr-approver | 独立评审份数不够 |
| `6159859c` | `6159859c` | high | 2 | lr-approver | 独立评审份数不够 |
| `08ee7b9f` | `08ee7b9f` | high | 2 | lr-approver | 独立评审份数不够 |
| `c41df0c6` | `c41df0c6` | high | 2 | lr-approver | 独立评审份数不够 |
| `f38420f4` | `f38420f4` | high | 2 | lr-approver | 独立评审份数不够 |
| `670a82b2` | `670a82b2` | high | 2 | lr-approver | 独立评审份数不够 |
| `44f61fc7` | `44f61fc7` | high | 2 | lr-approver | 独立评审份数不够 |
| `c7dd3eda` | `c7dd3eda` | high | 2 | lr-approver | 独立评审份数不够 |
| `643b3b45` | `643b3b45` | high | 2 | lr-approver | 独立评审份数不够 |
| `38a98afd` | `38a98afd` | high | 2 | lr-approver | 独立评审份数不够 |
| `1b391e41` | `1b391e41` | high | 2 | lr-approver | 独立评审份数不够 |
| `36098e07` | `36098e07` | high | 2 | lr-approver | 独立评审份数不够 |
| `688e2445` | `688e2445` | high | 2 | lr-approver | 独立评审份数不够 |
| `1ed02ef8` | `1ed02ef8` | high | 2 | lr-approver | 独立评审份数不够 |
| `6f9f1f55` | `6f9f1f55` | high | 2 | lr-approver | 独立评审份数不够 |

### paired（补上另一位真实评审人（推演））

推演场景 —— **45 份变更的逐条结论在台账里**（按 ``scenario`` 字段筛）。

### architecture（当作架构级改动（推演））

推演场景 —— **45 份变更的逐条结论在台账里**（按 ``scenario`` 字段筛）。

## 没测的，和已知的不足

- **语料里没有“改动种类”这一项** —— 所以“资格那一层”在真数据上答不了这个
  问题，只能推演（architecture 场景）。
- **语料里没有“评审人资格”这一项** —— 名册是按语料里出现过的人**一律**按最
  松的一档（general）登记的。**这是对新闸最不利的假设**：在这个假设下它都
  拦不住，那才是真的拦不住。
- **旧审批没有绑内容摘要** —— 它绑的是任务。回放时把那份结论绑到这份变更的
  摘要上：这份评审说的就是这一份改动，但“绑摘要”是新规矩加的那道绑定，
  **不是这次量出来的东西**。
- **这批语料里没有拒过的、没有过期的** —— “拒绝是终态”“过期盖不住拒绝”两
  条在真数据上没有被触发，只有单元测试覆盖。
- **这一轮只量了闸门** —— 闸门拦住之后流程怎么走（谁被通知、怎么补评审）
  不在这次范围内。

## 复现

```
python -m scripts.run_release_audit
```

台账：``docs/evaluation/release-ledger.jsonl``（135 行，每行是一份变更在一个场景下的结论）
