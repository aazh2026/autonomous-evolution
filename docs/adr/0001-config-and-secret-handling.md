# ADR 0001 —— 配置分类与密钥处理

- **状态**：已采纳
- **日期**：2026-11-05
- **关联任务**：TASK-P1-002（`docs/tasks.md`）
- **设计依据**：design.md §3.5D（凭证用短期令牌，不向 Prompt 暴露长期密钥）、R-060、I-08

---

## 1. 背景

ASES 会同时运行 Coding Agent、对象存储、Langfuse、OTel collector 和 PostgreSQL。
这些依赖各自需要不同的配置，其中一部分是**治理策略**，一部分是**运行时参数**，
还有一部分是**凭证**。三者的性质完全不同：治理策略由人类定义、变更需审批；
运行时参数可以随时调整；凭证必须保密且短期。

如果三类配置混在一个字典里，加载逻辑无法区分"改错了要重启"和"改错了要重新审批"，
更无法区分"这个值可以打进日志"和"这个值绝对不能进日志"。分类不是洁癖 ——
它决定了哪些值能改、由谁改、以及泄露时要承担什么后果。

## 2. 决策

配置分三类，边界由 `infrastructure/config/settings.py` 强制：

| 类别 | 例子 | 存放位置 | 变更方式 | 能否进日志 |
| --- | --- | --- | --- | --- |
| **外部配置**（治理类） | 权限策略、验收规则、预算上限、目标定义 | YAML 文件或数据库，路径由 `Settings.governance_*` 字段指向 | **走审批流程**，系统不得自主修改 | 可（不含密钥时） |
| **运行时配置** | 模型名、超时、并发数、日志级别、数据库连接串 | 环境变量 / `.env`，带代码默认值 | 改环境变量后重启生效 | 可 |
| **密钥** | S3 密钥、Langfuse 密钥、Claude Code API Key | **只从环境注入**，类型为 `SecretStr` | 轮换 | **绝对不可** |

### 2.1 三条硬规则

1. **治理配置不得硬编码。**
   理由：design.md §7 要求"系统不得自主修改控制自身的治理策略"。
   若权限矩阵写死在 Python 里，"修改策略需审批"这句话就没有技术强制 ——
   改一行代码就绕过去了。所以 `PolicyManager` 从外部文件读取，
   缺失时**回落到保守基线**而不是宽松默认（见 `core/policy/autonomy.py`）。

2. **密钥只进不出，且不落盘。**
   所有密钥字段声明为 `SecretStr`：它的 `repr()`、`str()` 与日志输出统一为
   `**********`。`.env` 已被 `.gitignore` 排除；仓库里只保留不含真实值的
   `.env.example`。`Settings` 的 `model_config` 设了 `frozen=True`，
   运行期无法就地改写已加载的凭证。

3. **缺凭证就启动失败，不带着不确定行为运行。**
   非本地环境缺必需凭证时 `Settings` 构造直接抛 `ValidationError`，
   错误信息指名缺哪一项。宁可进程起不来，也不要"先跑起来再说" ——
   后者的代价是运行到一半才发现没配。

### 2.2 环境相关的额外约束

`Settings` 的 `model_validator` 施加两条按环境收紧的规则：

- **任何非 local 环境禁止使用默认 MinIO 凭证 `minioadmin/minioadmin`**。
  本地开发的默认口令不能被带到共享环境 —— 这是最常见的"忘了改"事故。
- **production 环境启用 Langfuse 时必须同时提供 public/secret key**，
  且必须提供对象存储密钥。生产环境的可观测性不能是"尽力而为"。

## 3. 后果

- **正面**：治理配置与运行参数解耦，"改策略"与"改参数"在代码路径上就是两条路；
  密钥泄露面收敛到 `SecretStr` 这一种类型上，由一处代码统一处理。
- **负面**：多了一层间接 —— 判定器要读文件、文件要按 schema 校验。
  换来的是"外部配置改错了会明确报错"，而不是"悄悄按默认值跑"。
- **代价**：轮换密钥需要重启进程。阶段一接受这个代价；
  阶段二引入 secret manager 时由它承担热更新。

## 4. 被否决的备选方案

| 备选 | 否决理由 |
| --- | --- |
| 全部配置放一个 `dict[str, str]` | 无法区分治理配置与运行时配置，"治理需审批"落不了地；也无法对密钥做统一脱敏 |
| 密钥写进数据库的 `project` 表 | 密钥与治理数据同库 = 一次 SQL 注入泄露全部凭证；且数据库备份即密钥泄露 |
| 用 `.env` 之外的自定义加密配置文件 | 需要自己实现密钥管理。短期令牌 + 环境注入是 §3.5D 的既定路径，自造轮子不增加安全性 |
| 在 `Settings` 里给密钥用普通 `str` | `repr()` 会带出明文，一次 `print(settings)` 或异常回溯就泄露 |

## 5. 验证

- **T1 ①**：缺必需密钥时构造 `Settings` 抛 `ValidationError`，
  错误信息点名缺失项 —— `tests/unit/infrastructure/test_settings.py`
- **T1 ②**：全库扫描无硬编码密钥字面量 ——
  `tests/unit/infrastructure/test_no_hardcoded_secrets.py`（进 CI 门禁）
- **T1 ③**：密钥字段均为 `SecretStr`，`repr()` 不泄露 ——
  `tests/unit/infrastructure/test_settings.py`

## 6. 相关

- `docs/tasks.md` §2.1（TASK-P1-002）、§6 待裁决 8
- `infrastructure/config/settings.py`
- `core/policy/autonomy.py`（治理配置的消费方）
- `docs/architecture/data-model.md`（数据库不存密钥的理由）
