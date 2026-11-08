# 审计字段字典

> 适用范围：`harness/security/audit.py` 写入的**全部**审计记录。
> 关联任务：`docs/tasks.md` TASK-P1-045、R-062。
>
> 这份文档由 `tests/unit/harness/security/test_audit_schema.py` 钉在代码上 ——
> 字段增删会有人被测试拦下，而不是等到某次复盘时才发现"这个字段没了"。

---

## 1. 审计表是什么

审计表回答一个问题：**"那天到底是谁、在哪个任务上、对哪个资源做了什么、
结果如何。"**

它**不参与判断**。"准不准"是授权模块的事，"合不合规"是安全门的事。审计只
把已经发生的事实原样记下来。

**只追加。** 表里的东西不许改、不许删 —— 能改的记录等于没有记录，因为事后
没人能相信它还是当时写下的那样。存储协议 `AuditStore` 里只有 `append`，
没有 `update`、没有 `delete`：拿到这个协议代码，无论怎么写都改不了已有
的记录。

## 2. 每条记录都有的字段

| 字段 | 类型 | 必填 | 来源 | 说明 |
| --- | --- | --- | --- | --- |
| `event_id` | `string` | 是 | 审计口生成 | 这条记录的标识。审计表的主键 |
| `agent_id` | `string` | 是 | 调用方 | **主体** —— 干活的 Agent。**不能**用它替代 `task_id`（R-063） |
| `task_id` | `string` | 是 | 调用方 | **任务** —— 这次动作属于哪个任务。授权绑定的是它 |
| `project_id` | `string` | 是 | 调用方 | 项目 |
| `workspace_id` | `string` | 是 | 调用方 | 工作空间 |
| `resource` | `string` | 是 | 调用方 | **资源** —— 这次动作针对的东西（文件路径、命令、远端对象……） |
| `operation` | `string` | 是 | 调用方 | **操作** —— 做了什么（`write_file` / `merge_to_main`……） |
| `result` | `string` | 是 | 调用方 | **结果** —— 成没成。取值见 §4 |
| `decision` | `string` | 是 | 审计口 | 判定 —— 允许还是拒绝。取值见 §4 |
| `timestamp` | `string` | 是 | 调用方 / 判定 | 时间戳，**ISO-8601，UTC** |
| `reason` | `string` | 否 | 调用方 / 判定 | 理由 —— 为什么是这个结果。判定类记录一定非空 |
| `capability` | `string` | 否 | 审计口 | 这次判定涉及的资源级能力（如 `repo:merge`） |
| `session_id` | `string` | 否 | 调用方 | 执行会话。同一会话里的调用属于同一次 Agent 执行 |

**四要素**（R-062）是 `agent_id` / `task_id` / `resource` / `result` —— 这四个
**必填，且缺一条就构造不出来**：

- 少了 `agent_id`，就不知道是谁干的；
- 少了 `task_id`，就不知道这件事**是不是被授权干过**；
- 少了 `resource`，就不知道动了什么 —— "改了点东西"没法复盘；
- 少了 `result`，就不知道成没成 —— 半成品的调用最危险，因为它看上去一切
  正常。

这不是"建议填"，而是 `AuditEvent` 构造时就会拒绝的空值校验。

## 3. 判定类记录与调用类记录

审计表里两种记录，字段相同，填法不同：

| | 判定类记录 | 调用类记录 |
| --- | --- | --- |
| 什么时候写 | 授权判定之后 | 工具调用之后 |
| `operation` | 被判定的操作（`code_changes`……） | 实际执行的动作（`write_file`……） |
| `resource` | 被判定的资源；没有就写 `none` | 实际碰到的资源 |
| `decision` | `allow` / `deny` | 通常是 `allow`（调用被拒了就不会发生） |
| `result` | `success`（放行）/ `denied`（拒绝） | `success` / `failure` |
| `timestamp` | **判定的时间**（`decided_at`） | 调用发生的时间 |

**判定的时间用判定自己的时间**，不是"记下来的时间" —— 后者会随留痕延迟
漂移，同一次判定在不同机器上会给出不同的时间，事后对不上。

## 4. 枚举取值

### `result`

| 取值 | 含义 |
| --- | --- |
| `success` | 做成了 |
| `failure` | 开始了，但没成 |
| `denied` | **压根没开始**（被拒绝）。与"开始了但失败"不是一回事 —— 前者没有副作用，后者可能有 |

只有这三种。"部分成功"、"超时未返回"、"不知道"都不在表里 —— 拿不准的时候
记成 `failure`：审计表宁可多记一条失败，也不要出现一条说不清状态的记录。

### `decision`

| 取值 | 含义 |
| --- | --- |
| `allow` | 允许 |
| `deny` | 拒绝 |

**拒绝比放行更该记。** 放行会留下做事的结果，拒绝只留下一句"没做"，而
"为什么没做"往往正是后来要复盘的东西。

### 占位值

`resource` 与 `operation` 在"不适用"时写 `none`（`NOT_APPLICABLE`）——
例如"拆分任务"这种不碰文件的操作。**不能留空**：留空会让"这次操作不针对
具体资源"和"这个字段忘了填"没法区分。

## 5. 存储格式

`JsonlAuditStore` 写的是 **JSON Lines**：一行一条记录，字段与 §2 一一对应。

```
{"agent_id":"coder-01","capability":"repo:write","decision":"allow","event_id":"evt-1","operation":"code_changes","project_id":"my-product","reason":"授权有效","resource":"none","result":"success","session_id":null,"task_id":"TASK-042","timestamp":"2026-03-01T10:00:00+00:00","workspace_id":"ws-7"}
```

**为什么本体是文件而不是数据库。** 审计要在动作前后落盘才谈得上 fail-closed，
而"数据库写成功"不等于"这条记录不会消失" —— 事务回滚时它跟着没了，"写下
来了"就退化成"大概写下来了"。文件追加 + `fsync` 没有这条尾巴。

**读到坏行就报错。** 审计文件被改过（或写坏了）的时候，宁可查不了，也不能
装作没事 —— 一份会跳过坏行的审计表等于给了篡改者一个后门。

## 6. 查询维度

`AuditQuery` 的每一项都是**精确匹配**，留空即不限；时间范围是闭区间。
没有模糊匹配、没有正则：审计查询要能复现，同一个问题今天和下个月必须查出
同一个答案。

| 条件 | 匹配什么 |
| --- | --- |
| `task_id` | 任务 |
| `agent_id` | 主体 |
| `project_id` | 项目 |
| `workspace_id` | 工作空间 |
| `resource` | 资源 |
| `operation` | 操作 |
| `session_id` | 执行会话 |
| `since` | 时间下界（含） |
| `until` | 时间上界（含） |

## 7. 写失败即阻断

**审计写不下来 = 这件事没发生过 = 不能当作发生了。** 链上有两道闸，位置
不同是有意的：

| 闸 | 位置 | 写失败时 |
| --- | --- | --- |
| 判定留痕 | 动作**之前** | 判定为**拒绝**，操作不开始 |
| 调用记录 | 动作**之后** | 抛 `AuditWriteError`，这次调用**不算做成了** |

前者能真正地"不让他做"，后者保证"做过的事不会没有记录"。绝不允许"审计
失败就放行"—— 那会让审计表变成一份只记成功、不记失败的名册，比没有审计
更坏。

## 8. 检索示例

| 问题 | 过滤条件 |
| --- | --- |
| 这个任务做过什么 | `task_id = "TASK-042"` |
| 这个 Agent 在这个任务上碰过哪些资源 | `task_id = "TASK-042" and agent_id = "coder-01"` |
| 哪些调用没成功 | `task_id = "TASK-042" and result = "failure"` |
| 哪些操作被拒过 | `decision = "deny"` |
| 某次执行会话里的全部动作 | `session_id = "sess-1"` |
| 某段时间内的全部记录 | `since = "2026-03-01T00:00:00+00:00" and until = "2026-03-02T00:00:00+00:00"` |
| 动过某个文件的所有调用 | `resource = "src/main.py"` |

## 9. 相关

- `harness/security/audit.py` —— 实现
- `harness/security/authorization.py` —— 判定与授权（判定类记录的来源）
- `docs/observability/log-schema.md` —— 日志字段字典（观测用，与审计不是一回事）
- `docs/adr/0001-config-and-secret-handling.md` —— 密钥分类的边界
- `docs/architecture/data-model.md` —— `task_id` / `project_id` 对应的表与外键
