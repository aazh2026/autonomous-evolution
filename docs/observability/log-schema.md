# 日志字段字典

> 适用范围：`infrastructure/observability/logging.py` 输出的**全部**日志事件。
> 关联任务：`docs/tasks.md` TASK-P1-005。
>
> 这份文档由 `tests/unit/infrastructure/test_log_schema.py` 钉在代码上 ——
> 字段增删会有人被测试拦下，而不是等到某次排障时才发现"这个字段没了"。

---

## 1. 输出格式

| 格式 | 何时用 | 形态 |
| --- | --- | --- |
| `json` | 默认。采集、检索、告警规则都依赖它 | 每条一行 JSON，字段扁平 |
| `console` | 本地调试 | `structlog.dev.ConsoleRenderer`，带颜色 |

格式由 `Settings.log_format` 决定，**不靠环境变量在运行时切换** ——
同一份代码在不同部署下行为一致，比"到处都能改"重要。

## 2. 每条日志都有的字段

| 字段 | 类型 | 来源 | 说明 |
| --- | --- | --- | --- |
| `event` | `string` | 调用方传入 | 事件名。用动宾短语：`task_started`、`verification_failed` |
| `level` | `string` | `structlog.processors.add_log_level` | `debug` / `info` / `warning` / `error` / `critical` |
| `timestamp` | `string` | `structlog.processors.TimeStamper` | ISO-8601，**UTC**（`utc=True`） |
| `service` | `string` | `Settings.service_name` | 服务名，默认 `ases`。多服务部署时靠它区分来源 |

## 3. 保留的上下文字段（可检索维度）

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `task_id` | `string` | 任务 ID。**最常用的检索维度** —— "这个任务做过什么" |
| `cycle_id` | `string` | 演进周期 ID。"这一轮演进有哪些动作" |
| `project_id` | `string` | 项目 ID。跨项目对比时用 |

这三个字段是**保留键**（`logging.RESERVED_CONTEXT_KEYS`）：它们不强制出现在
每条日志里，但一旦绑定了上下文，后续日志都会自动带上。

绑定方式：

```python
from infrastructure.observability.logging import bind_task_context

bind_task_context(task_id="TASK-042", cycle_id="CYCLE-007", project_id="my-product")
```

`bind_task_context` **只绑定非 `None` 的项** —— 绑定空值会留下
`"task_id": null`，让"这个任务没有 id"和"这个字段没写"没法区分。

也可以直接 `bind_context(**kwargs)` 绑任意键，但保留键走专用函数更清楚。

## 4. 事件字段（调用方自定义）

`event` 之外的所有关键字参数都会原样进入日志。命名约定：

| 约定 | 例子 | 理由 |
| --- | --- | --- |
| 布尔量用 `is_` / `has_` 前缀 | `is_degraded`、`has_contract` | 检索时能一眼看出是布尔量 |
| 数量用 `_count` / `_ms` / `_bytes` 后缀 | `token_count`、`duration_ms` | 单位明确，不靠猜 |
| 枚举值全小写 | `result="passed"` | 大小写不一致的枚举没法做精确匹配 |

## 5. 脱敏

`logging.SENSITIVE_KEYS` 里的键名，其值一律被替换成 `***`：

```
api_key, apikey, access_key, secret_key, secret, password, passwd,
token, authorization, auth, private_key, credential, credentials,
session_token
```

匹配是**键名精确匹配**（大小写不敏感），不是子串匹配 —— 否则
`token_count` 这种无害字段也会被抹掉。

值本身还有三种状态：

| 输入 | 输出 | 含义 |
| --- | --- | --- |
| 有值 | `***` | 配了，但不打出来 |
| `""` | `empty` | 配了但是空串 |
| `None` / 缺省 | `unset` | 压根没配 |

区分"空"和"没配"是有意义的：前者通常是配置写错了，后者是没这个需求。

**连接串也是密钥**。`Settings.database_url` 是 `SecretStr`，
`Settings.database_dsn` 才会给出明文 —— 明文不要进日志。

## 6. 观测降级标记

Langfuse / OTel 不可用时，日志里会出现这些字段：

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `degraded` | `true` | 本条事件对应的能力已降级 |
| `langfuse_state` | `degraded` | Langfuse 转入降级状态 |
| `reason` | 异常类名 | 降级原因（不记异常全文，避免带出内部地址） |
| `endpoint` | URL | OTel collector 地址 |

降级**只在进入时告警一次**，恢复时记一条 `langfuse_recovered`。
持续故障时反复刷屏会把真正的日志淹掉 —— 而观测故障时日志正是最要紧的。

排查"这段时间为什么没有 Langfuse 数据"时，先按 `degraded=true` 过滤。

## 7. 检索示例

| 问题 | 过滤条件 |
| --- | --- |
| 某个任务做过什么 | `task_id = "TASK-042"` |
| 某轮演进周期的全部动作 | `cycle_id = "CYCLE-007"` |
| 所有失败 | `level in (error, critical)` |
| 观测降级时段 | `degraded = true` |
| 某任务里的密钥脱敏是否生效 | `task_id = "TASK-042" and api_key = "***"` |

## 8. 相关

- `infrastructure/observability/logging.py` —— 实现
- `infrastructure/observability/langfuse.py` —— Langfuse 客户端（降级逻辑）
- `infrastructure/observability/tracing.py` —— OTel 骨架
- `docs/adr/0001-config-and-secret-handling.md` —— 密钥分类的边界
- `docs/architecture/data-model.md` —— `task_id` / `cycle_id` 对应的表与外键
