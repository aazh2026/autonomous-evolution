# ASES 用户使用指南

> 面向使用者（开发者 / 运维 / 试点负责人）的操作手册。
> 架构与取舍见 [`design.md`](design.md)，任务清单见 [`tasks.md`](tasks.md)，数据模型见 [`architecture/data-model.md`](architecture/data-model.md)。
>
> 本文中所有代码片段均已在当前代码库上实际执行验证。

---

## 目录

1. [系统是什么](#1-系统是什么)
2. [当前能力边界](#2-当前能力边界)
3. [安装与启动](#3-安装与启动)
4. [配置参考](#4-配置参考)
5. [核心概念](#5-核心概念)
6. [操作手册](#6-操作手册)
7. [ASES 控制舱界面使用手册（Cockpit）](#7-ases-控制舱界面使用手册cockpit)
8. [API 参考](#8-api-参考)
9. [故障排查](#9-故障排查)
10. [已知缺口](#10-已知缺口)
11. [开发者附录](#11-开发者附录)

---

## 1. 系统是什么

**ASES（Autonomous Software Evolution System，自主软件演进系统）** 不是一个"帮你写代码的自动化工具"，而是一套**长期承担软件项目演进责任的 AI 研发组织**的工程骨架。

它的核心命题是：

> 人类定义项目的目标、边界和约束；系统持续观察项目、理解现状、识别目标差距、自主决策演进方向、组织不同的 Coding Agent 实施变更，并通过**独立于执行环节**的验证决定是否接受变更。

### 1.1 两个独立循环

| | 循环 A · 工程执行循环 | 循环 B · 项目演进循环 |
|---|---|---|
| 输入 | 已确定的任务 | 项目当前状态 |
| 目标 | 完成任务 | 找到值得完成的任务 |
| 核心判断 | **怎么实现** | **应该实现什么** |
| 输出 | 代码和验收结果 | 下一轮演进计划 |
| 周期 | 分钟 ~ 小时 | 小时 ~ 天 |
| 责任主体 | Coding Agent + QA | Director |

这两个循环**不得合并成一个无限循环的 Prompt**——那正是本项目要解决、而不是要采用的架构。

### 1.2 八条核心不变量

这些约束不是口号，它们由数据库约束、类型系统和单元测试强制执行：

1. **候选 ≠ 任务**。`EvolutionCandidate → EvolutionDecision → Task` 三表分离，`Task.decision_id NOT NULL` 由外键强制。
2. **验收契约先于执行**。验收契约必须在 `READY → RUNNING` 之前存在并绑定 `task_id`，是**准入闸门**而不是事后报告。
3. **验证独立于执行**。`self_assessment` 只是 Agent 的自我报告，不参与验收判定——类型上就够不着。
4. **身份不可混用**。授权键 = `(agent_id, task_id, project_id, workspace_id, expiry)`。
5. **治理外置**。权限策略、验收规则、预算上限来自外部配置且变更需审批；系统不得自主修改控制自身的策略。
6. **证据不可丢失**。`VerificationRun` / `Artifact` 为 append-only，跨演进周期保留。
7. **审计写失败即阻断**（fail-closed），不允许"审计失败就放行"。
8. **密钥不入代码、不入库**。仅从环境注入，长效密钥不进入 Prompt。

---

## 2. 当前能力边界

**请先读这一节。** 系统目前处于**阶段一（Thin Slice）**，下面是"现在能用什么"和"还不能用什么"的准确清单。

### ✅ 已经可用（有实现、有测试）

| 能力 | 位置 | 说明 |
|---|---|---|
| 项目定义与目标六要素 | `core/project/` | YAML 载入、六要素校验、目标达成度计算、停止条件判定 |
| 项目注册表 | `core/project/registry.py` | 注册 / 审批替换 / 变更审计 |
| 14 态任务状态机 | `core/task/state_machine.py` | 含准入闸门、修复预算闸门、幂等语义 |
| 任务结果判定 | `core/task/outcome.py` | 类型级隔离自我报告；超时 ≠ 失败 |
| 四层取消链 | `core/task/cancellation.py` + `harness/sandbox/cancellation.py` | task → adapter → process → container |
| 自主权策略 L1–L4 | `core/policy/autonomy.py` | 8 个操作全覆盖，缺一条即拒绝加载，**无修改接口** |
| 预算管理 | `core/policy/budget.py` | 超限显式抛错；Postgres 账本并发安全 |
| 验收契约与判定 | `harness/verification/contract.py` | 三组规则、纯 AND 逻辑、三条"无法判定 ≠ 通过" |
| 独立验证引擎 | `harness/verification/engine.py` | 独立进程、白名单环境、并发执行、进程组超时 |
| Claude Code 执行器 | `executors/claude_code/` | 完整可用：NDJSON 事件流、超时、中断、幂等键 |
| 工作空间 / 租约 / 回收 | `harness/workspace/` | Git Worktree + 磁盘租约 + GC |
| 沙箱隔离 | `harness/sandbox/` | 白名单环境重建、挂载校验、容器启动与停止 |
| 安全门 | `harness/security/gate.py` | 密钥 / 依赖 / 公共 API 三项 forbidden 检查（CLI） |
| 网络出口白名单 | `harness/security/network.py` | 连接前校验，拒绝零次连接尝试 |
| 任务级授权 | `harness/security/authorization.py` | 五元组身份、过期即失效、操作 + 能力双闸、高风险需独立审批 |
| 工具调用审计 | `harness/security/audit.py` | 四要素构造时校验、只追加、写失败即阻断、JSON Lines 落盘 |
| 14 张数据表 + 迁移 | `core/entities/`、`migrations/` | append-only 触发器、CHECK 约束全部落库 |
| 8 个演进指标 | `evaluation/` | 4 个层级，指标 ID 主动拒绝"活动量"类指标 |
| 结构化日志 / OTel / Langfuse | `infrastructure/observability/` | 密钥自动脱敏、降级不抛异常 |

### ❌ 尚未实现（不要按"应该有"来用）

| 能力 | 现状 | 计划 |
|---|---|---|
| HTTP 业务接口 | 只有 `/healthz`、`/readyz` | TASK-P1-060/061/062 |
| 循环 A / 循环 B 编排 | `orchestration/` 全空 | TASK-P1-060/061 |
| Director 与各角色 Agent | `agents/` 全空（8 个 0 字节包） | TASK-P1-051~057 |
| 项目知识库 / 架构认知 | `knowledge/` 全空 | TASK-P2-016 |
| 集成引擎（多任务合并） | `harness/integration/` 全空 | TASK-P1-047 |
| 人工审批门 | 目录不存在 | TASK-P1-046 |
| Codex 执行器 | 骨架：事件表为空，**每次运行必然 FAILED** | 见 §6.7 |
| OpenCode / OpenHands / Gemini CLI | 0 字节空包 | 阶段二 |
| 覆盖率 / 成本 / 时延基线 | 未建立 | TASK-P1-072 |

> **一句话**：今天这套系统是"一个护栏严密、但还没有接线缆的领域内核 + 一套能跑起来的基础设施"。你可以放心地用它来**定义项目、编排任务、判定验收、隔离执行、留存证据**；但它**还不能自己发现问题并驱动演进**。

---

## 3. 安装与启动

### 3.1 前置条件

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | ≥ 3.12 | `pyproject.toml` 声明；本地 venv 实测 3.14.5 |
| uv | 最新 | 包管理与虚拟环境 |
| Docker + Compose v2 | 最新 | 本地依赖（PostgreSQL / MinIO）与沙箱 |
| git | 任意 | 工作空间依赖 `git worktree` |

### 3.2 安装

```bash
git clone <repo> && cd autonomous-evolution
make install          # uv sync --extra dev
```

需要 S3/MinIO 后端时额外安装 `s3` extra：

```bash
uv sync --extra dev --extra s3
```

否则 `S3ObjectStore` 在构造时会直接抛 `ObjectStoreUnavailableError`，提示 `uv sync --extra s3`。

### 3.3 配置

```bash
cp .env.example .env
```

`.env` 已被 `.gitignore` 排除，**绝不要提交**。最小可运行配置：

```ini
ASES_ENVIRONMENT=local
ASES_LOG_LEVEL=INFO
ASES_LOG_FORMAT=json

ASES_DATABASE_URL=postgresql+asyncpg://ases:ases@localhost:5432/ases

ASES_S3_ENDPOINT_URL=http://localhost:9100
ASES_S3_ACCESS_KEY=minioadmin
ASES_S3_SECRET_KEY=minioadmin
```

> **端口说明**：宿主机上的 MinIO 暴露在 **9100**（控制台 9101），不是默认的 9000/9001——9000 常被其它 S3 服务占用。容器内部仍走 9000。
>
> **注意**：docker-compose 里的 `api`/`worker`/`migrate` 服务会用自己的 `x-app-environment` 覆盖 `.env`（例如 `ASES_S3_ENDPOINT_URL=http://minio:9000`）。`.env` 影响的是**你本机直接跑**的进程。

### 3.4 启动本地全栈

```bash
make up            # postgres + minio + migrate + api + worker
make ps            # 查看状态
make health        # 调 /healthz
make logs          # 跟日志
```

`make up` 会等所有健康检查通过才返回（上限 120 秒，由 `COMPOSE_WAIT_TIMEOUT` 调整）。启动顺序是 `postgres(healthy) → minio(healthy) → migrate(completed) → api/worker`，即**迁移必须先跑完**，容器才起 API。

带观测栈（Langfuse）：

```bash
make up-observability     # 额外拉起 langfuse-web/worker/clickhouse/redis
```

Langfuse 控制台在 http://localhost:3000。它使用**独立的数据库**（`LANGFUSE_DB_NAME=langfuse`）——共用 ASES 的库会让它的 prisma migrate 报 P3005 拒绝执行。

### 3.5 停止与清理

```bash
make down      # 停止，保留数据卷
make reset     # 停止并清空数据卷（不可逆）
```

### 3.6 不使用 Docker 的本地运行

```bash
make migrate                                   # alembic upgrade head
uv run uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
uv run python -m apps.worker.main              # 前台 worker
```

### 3.7 数据库迁移

```bash
make migrate                 # upgrade head
make migrate-down            # 回滚一级
make revision m="加个索引"     # autogenerate 新迁移
make psql                    # 打开 psql CLI
```

迁移链：`0001_initial_schema` → `0002_project_environment` → `0003_budget_ledger` → `0004_cycle_outcome`。
`alembic.ini` 里的 `sqlalchemy.url` **故意留空**——凭证不落在仓库里，迁移从 `ASES_DATABASE_URL` 取。

---

## 4. 配置参考

### 4.1 环境变量总表

全部前缀 `ASES_`。`Settings` 是 `frozen=True` 的——进程内不可改配置，只能改环境变量重启。

**运行时**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASES_ENVIRONMENT` | `local` | `local` / `test` / `ci` / `staging` / `production` |
| `ASES_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `ASES_LOG_FORMAT` | `json` | `json` / `console` |
| `ASES_SERVICE_NAME` | `ases` | 写入每条日志的 `service` 字段 |

**数据库**

| 变量 | 默认值 |
|---|---|
| `ASES_DATABASE_URL` | `postgresql+asyncpg://ases:ases@localhost:5432/ases` |
| `ASES_DATABASE_POOL_SIZE` | `10` |
| `ASES_DATABASE_MAX_OVERFLOW` | `20` |
| `ASES_DATABASE_ECHO` | `False` |

**对象存储**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASES_S3_ENDPOINT_URL` | `None` | **有值即启用 S3/MinIO 后端**；`None` 则用本地文件系统后端 |
| `ASES_S3_REGION` | `us-east-1` | |
| `ASES_S3_BUCKET` | `ases-artifacts` | |
| `ASES_S3_ACCESS_KEY` / `ASES_S3_SECRET_KEY` | `None` | `SecretStr`；production 必填 secret |
| `ASES_S3_FORCE_PATH_STYLE` | `True` | MinIO 必须为 `True` |
| `ASES_LOCAL_STORE_ROOT` | `/tmp/ases-artifacts` | 本地后端根目录 |

**观测**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASES_OTEL_ENABLED` | `False` | 关闭时 `configure_tracing` 返回 `None`，不报错 |
| `ASES_OTEL_ENDPOINT` | `http://localhost:4318` | OTLP HTTP |
| `ASES_LANGFUSE_ENABLED` | `False` | |
| `ASES_LANGFUSE_HOST` | `http://localhost:3000` | |
| `ASES_LANGFUSE_PUBLIC_KEY` / `ASES_LANGFUSE_SECRET_KEY` | `None` | production 且启用 Langfuse 时必填 |

**Worker**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASES_WORKER_HEARTBEAT_FILE` | `/tmp/ases-worker/heartbeat` | 健康检查读它的新鲜度 |

**执行器 / 沙箱**

| 变量 | 默认值 |
|---|---|
| `ASES_EXECUTOR_DEFAULT` | `claude_code` |
| `ASES_CLAUDE_CODE_BIN` | `claude` |
| `ASES_CLAUDE_CODE_API_KEY` | `None` |
| `ASES_CODEX_BIN` | `codex` |
| `ASES_SANDBOX_IMAGE` | `ases/sandbox:latest` |
| `ASES_SANDBOX_NETWORK` | `none` |
| `ASES_WORKSPACE_ROOT` | `/tmp/ases-workspaces` |
| `ASES_WORKSPACE_LEASE_TTL_SECONDS` | `3600`（≥ 60） |

**治理**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASES_GOVERNANCE_DIR` | `<repo>/config/governance` | 该目录**不存在**，需自行创建 |
| `ASES_POLICY_FILE` | `None` | 外部自主权策略 YAML（**只有这个真正被读取**） |
| `ASES_ACCEPTANCE_RULES_FILE` | `None` | 目前**无消费方**，预留 |
| `ASES_BUDGET_FILE` | `None` | 目前**无消费方**，预算写在 `project.yaml` 里 |

> `.env.example` 未收录 `ASES_SERVICE_NAME` / `ASES_DATABASE_ECHO` / `ASES_S3_FORCE_PATH_STYLE` / `ASES_WORKER_HEARTBEAT_FILE` / `ASES_GOVERNANCE_DIR` / `ASES_POLICY_FILE` 等。以本表为准。

### 4.2 环境相关的红线（启动即校验）

`Settings` 会在构造时校验，**不通过直接 `ValidationError` 抛错，不降级**：

- 非 `local` 环境使用 `minioadmin/minioadmin` → **拒绝启动**（防止把开发凭证带上生产）。
- `production` + 启用 Langfuse 但缺 public/secret key → **拒绝启动**。
- `production` 缺 `ASES_S3_SECRET_KEY` → **拒绝启动**。

### 4.3 三类配置的边界（见 ADR-0001）

| 类别 | 存放位置 | 变更方式 |
|---|---|---|
| **治理类**（权限策略、验收规则、预算上限、目标） | YAML 文件或数据库 | 需审批；系统不得自行放宽 |
| **运行时类**（模型名、超时、并发、日志级别） | 环境变量 / `.env` | 重启生效 |
| **密钥** | 仅环境注入，`SecretStr` 承载 | 永不入库、永不进日志 |

---

## 5. 核心概念

这一节是你操作前必须理解的心智模型。

### 5.1 目标六要素

项目目标不是一段自然语言，而是**六个缺一不可**的结构化元素。载入时逐项校验，缺哪一项直接报错并指出。

| 元素 | 含义 | 缺失后果 |
|---|---|---|
| `objective` | 想实现什么 | `GoalValidationError(missing_elements=[...])` |
| `key_results` | 用哪些指标判断实现了 | 同上 |
| `constraints` | 哪些条件不能违反 | 同上 |
| `non_goals` | 明确**不做**什么 | 同上 |
| `autonomy` | 允许系统自主做哪些事 | 同上 |
| `stop_conditions` | 满足什么条件后停止演进 | 同上 |

### 5.2 三表分离：候选 → 决策 → 任务

```
EvolutionCandidate ──(candidate_id, NOT NULL)──> EvolutionDecision ──(decision_id, NOT NULL)──> Task
```

- **Candidate** = 系统发现了什么值得改进（一个待评估的机会）。
- **Decision** = 系统为什么决定做 / 不做。
- **Task** = 系统最终决定执行什么。

分离的意义：避免 Agent 把"发现的问题"直接变成"代码修改"。**不行动也是一个合法决策**：

| 决策 | 语义 | 是否产生 Task |
|---|---|---|
| `record` | 记录并执行 | 是 |
| `defer` | 推迟（**必须**至少列出 1 项后续分析请求，否则数据库 CHECK 拒绝） | 否 |
| `reject` | 拒绝（必须给理由） | 否 |

### 5.3 任务状态机（14 态）

```
DISCOVERED → EVALUATING → APPROVED → PLANNED → READY → RUNNING
→ VERIFYING → PASSED → INTEGRATING → MERGED → COMPLETED
```

完整状态集：`DISCOVERED` `EVALUATING` `DEFERRED` `APPROVED` `PLANNED` `READY` `RUNNING` `VERIFYING` `PASSED` `REPAIRING` `BLOCKED` `INTEGRATING` `MERGED` `COMPLETED`。

两条硬闸门：

1. **进入 `RUNNING` 前必须有验收契约** → 否则抛 `AcceptanceContractRequiredError`。
2. **进入 `REPAIRING` 前修复次数未超限**（默认 3）→ 否则抛 `RepairBudgetExhaustedError`。

非法迁移抛 `IllegalTransitionError`（**不静默兜底**）；重复提交同一动作返回 `changed=False` 的幂等空操作（消息重投安全）。

> `docs/design.md` §6 的状态图导出时丢失了全部边，**以 `core/task/state_machine.py` 的 `_TRANSITION_TABLE` 为权威**（见 `docs/tasks.md` 待裁决项 7）。

### 5.4 验收契约（Acceptance Contract）

**"怎样才算完成"必须在执行前定义。** 三组规则：

| 组 | 语义 | 缺证据时 |
|---|---|---|
| `required` | 必检项（`unit_tests_pass` / `integration_tests_pass` / `lint_pass` / `architecture_rules_pass`） | **失败** |
| `thresholds` | 阈值（如覆盖率 `>= 0.85`） | **失败** |
| `forbidden` | 禁止项（`secrets_in_diff` / `unauthorized_dependency_change` / `public_api_breaking_change`） | 命中任一即整体失败（一票否决） |

判定是**纯 AND 逻辑**——"禁止项一票否决"是规则的推论，而不是特例。三条"**无法判定 ≠ 通过**"：

- 必检项没有结果 → 失败
- 阈值没有实测值 → 失败
- 类型不匹配的比较 → 失败

契约绑定的 `task_id` 与证据的 `task_id` 不一致 → 直接失败。

### 5.5 自主权四级

| 级别 | 含义 |
|---|---|
| `L1` | 分析自主：分析、诊断、生成建议 |
| `L2` | 开发自主：创建分支、修改代码、测试 |
| `L3` | 集成自主：自动修复、集成、合并指定范围 |
| `L4` | 运营自主：自动发布、监控、回滚 |

八个操作**必须全部配置**，缺一条即拒绝加载（缺规则 = 权限漏洞，不是省事）。

默认基线：

| 操作 | 级别 | 权限 |
|---|---|---|
| `analyze_project` | L4 | `autonomous` |
| `plan_tasks` | L4 | `autonomous` |
| `code_changes` | L4 | `autonomous` |
| `test_execution` | L4 | `autonomous` |
| `branch_creation` | L4 | `autonomous` |
| `merge_to_main` | L2 | `approval_required` |
| `production_deployment` | L1 | `advisory_only` |
| `modify_governance` | L1 | `advisory_only` |

> **系统不得自主修改控制自身的策略。** `PolicyManager` **没有提供任何修改策略的方法**——策略是 `frozen` 的 `FrozenDict`。想改，只能改外部 YAML 走审批流程。

### 5.6 预算

指标：`daily_cost` `total_cost` `token_usage` `model_calls` `compute_minutes` `wall_clock_minutes`。
币种可配置（不写死 USD）。超限**显式抛 `BudgetExhausted`**，不静默截断；被拒绝的尝试仍以 `status=blocked` 记入账本供审计。累加器**只增不减**（数据库触发器保证），跨天滚动靠新增行，不靠删除。

### 5.7 证据与产物

- `VerificationRun` / `Artifact` 是 **append-only**——数据库触发器直接拒绝 `UPDATE` / `DELETE` / `TRUNCATE`。
- 产物按 **sha256 内容寻址**（key 形如 `sha256:<hex>`），**读取时**校验哈希（写入时正确只证明"当时正确"）。
- 重复写入同一内容是幂等的，且**不会覆盖**已存在的对象——即使那个对象已损坏，损坏必须保持可见。

---

## 6. 操作手册

### 6.1 定义并载入项目

完整示例见 [`docs/examples/project.yaml`](examples/project.yaml)。最小版本：

```yaml
project:
  id: my-product
  name: 在线PPT制作平台

  objective:
    - id: OBJ-001
      description: 建成完整的在线PPT制作平台

  key_results:
    - id: KR-001
      metric: e2e_success_rate
      target: ">= 0.95"
      unit: ratio
      metric_type: percentage
      eval_method: automated_test
      direction: higher_is_better
      baseline: "0.72"

  constraints:
    - id: C-001
      description: 所有代码变更必须通过自动化测试
      kind: all_changes_must_pass_tests
      machine_checkable: true
      expression: tests_passed == true

  non_goals:
    - id: NG-001
      description: 不做移动端原生适配

  autonomy:
    rules:
      analyze_project: { level: L4, permission: autonomous }
      plan_tasks:       { level: L4, permission: autonomous }
      code_changes:     { level: L4, permission: autonomous }
      test_execution:   { level: L4, permission: autonomous }
      branch_creation:  { level: L4, permission: autonomous }
      merge_to_main:    { level: L2, permission: approval_required }
      production_deployment: { level: L1, permission: advisory_only }
      modify_governance:    { level: L1, permission: advisory_only }

  stop_conditions:
    - id: SC-001
      description: 全部关键结果达成
      kind: all_key_results_met

  budget:
    currency: USD
    max_daily_cost: "50"
    max_parallel_agents: 4

  repository:
    url: https://github.com/example/my-product
    default_branch: main
```

载入：

```python
from pathlib import Path
from core.project.models import load_project

project = load_project(Path("docs/examples/project.yaml"))
print(project.id, project.name)  # my-product 在线PPT制作平台
print(project.goal.key_result("KR-001"))
```

**约定**：

1. `metric` / `unit` / `eval_method` 是自由字符串，系统不硬编码任何指标名。
2. `target` 必须是可比较表达式（`">= 0.95"` / `"<= 250"` / `"== true"`）。解析**不使用 `eval()`**——配置里的表达式没有代码执行权。
3. `budget.currency` 可配置。

`key_results` 的 `direction` 必须显式声明（`higher_is_better` / `lower_is_better`），判定方向不会猜错。`metric_type` 必须与 target 的值类型匹配（`boolean` ↔ 布尔，其余 ↔ 数值）。

### 6.2 注册项目与审批替换

```python
from core.project.registry import ProjectRegistry

registry = ProjectRegistry([project])
registry.get("my-product")
registry.list_all()
registry.by_repository("https://github.com/example/my-product")

# 替换定义需要审批引用
registry.replace(new_project, approval_ref="APPROVAL-2026-11-08-001")
registry.changes()  # 变更审计：谁、何时、旧定义、审批号
```

重复注册同一 ID 抛 `DuplicateProjectError`（**永不覆盖**）；替换不带 `approval_ref` 抛 `MissingApprovalError`。

### 6.3 判定目标达成度与停止条件

```python
from core.project.models import TargetValue
from core.project.stop_conditions import StopConditionEvaluator, GoalAttainmentCalculator

calc = GoalAttainmentCalculator(project.goal)
att = calc.evaluate({"KR-001": TargetValue.parse("0.96")})
print(att.overall, att.coverage, att.satisfied)
# 0.25 0.25 frozenset({'KR-001'})

evaluator = StopConditionEvaluator(project.goal)
verdict = evaluator.evaluate(
    {"budget_exhausted": False, "consecutive_failures": 1},
    actuals={"KR-001": TargetValue.parse("0.96")},
)
print(verdict.stopped, verdict.triggered_ids, verdict.undecidable)
```

要点：

- **`actuals` 的键是 Key Result 的 id（`KR-001`），不是 metric 名**——metric 名可以重复，KR id 不会。
- **未测量的 Key Result 记 0 分**（没有免费通过），并列入 `unmeasured`；`coverage` 告诉你实际测了多少。上例 4 个 KR 只测了 1 个，所以 `overall=0.25`、`coverage=0.25`。
- **无法判定 ≠ 未触发**：缺少事实或实测值进入 `undecidable`，不会混进 `triggered`。
- 停止条件引用未注册指标 → 构造时直接抛错（"启动即失败"），而不是运行时静默。

### 6.4 自主权策略

外部 YAML（`ASES_POLICY_FILE` 指向）：

```yaml
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
```

> 八个操作必须全部列出，缺一条即拒绝加载。
> YAML 里 `key: { level: ... }` 这种紧凑写法**必须留空格**（`key: {` 而不是 `key:{`），否则解析器报错。

```python
from core.policy.autonomy import PolicyManager, Operation, TaskContext, load_policy

pm = PolicyManager.from_file(Path("config/governance/policy.yaml"))
# 或 PolicyManager.default()（内置保守基线）

d = pm.check(Operation.MERGE_TO_MAIN)
print(d.kind, d.reason)
# DecisionKind.REQUIRE_APPROVAL  操作 merge_to_main 需人工审批（L2）

d2 = pm.check(Operation.CODE_CHANGES)
print(d2.kind)  # DecisionKind.ALLOW

# 带任务上下文（授权键五元组）
ctx = TaskContext(
    agent_id="agent-1",
    task_id="TASK-0001",
    project_id="my-product",
    workspace_id="TASK-0001",
    expiry=None,
    allowed_operations=frozenset(),
)
print(pm.check(Operation.CODE_CHANGES, ctx).kind)  # DecisionKind.ALLOW
```

两种加载方式的区别：

| 方式 | 文件缺失时 |
|---|---|
| `PolicyManager.from_file(path)` | 抛 `FileNotFoundError`（显式失败） |
| `load_policy(path)` | **回退到内置保守基线**（不会因为缺文件就放开权限） |

判定顺序是**先任务后策略**：过期 → `permits()` → 策略规则。`allowed_operations` 为空集表示"**不收窄**"，不是"什么都不允许"。

`advisory_only` 返回 `DENY`（不是"允许但附建议"）。

### 6.5 预算

```python
import asyncio
from decimal import Decimal
from core.policy.budget import Budget, BudgetManager, BudgetMetric, BudgetExhausted

budget = Budget(currency="USD", max_daily_cost="50", max_parallel_agents=4)
mgr = BudgetManager(budget, scope="my-product")


async def main():
    await mgr.consume(BudgetMetric.DAILY_COST, Decimal("12.5"))
    print(await mgr.remaining(BudgetMetric.DAILY_COST))  # 37.5

    try:
        await mgr.consume(BudgetMetric.DAILY_COST, Decimal("999"))
    except BudgetExhausted as e:
        print("超限:", e.metric, e.requested, e.limit)  # daily_cost 999 50

    print(await mgr.blocked_attempts())  # 被拒绝的尝试也在账本里，status=blocked


asyncio.run(main())
```

> `consume` 的金额是 **`Decimal`**（或数值），传字符串会抛 `TypeError`。

生产环境请用 `infrastructure.database.budget.PostgresBudgetLedger`（跨进程原子，靠 `SELECT ... FOR UPDATE`），而不是默认的进程内 `InMemoryBudgetLedger`。

### 6.6 准备隔离工作空间

```python
from pathlib import Path
from harness.workspace.manager import WorkspaceManager
from harness.workspace.leases import LeaseStore
from harness.workspace.git import SubprocessGitClient
from harness.workspace.models import SystemClock

mgr = WorkspaceManager(
    repo_path=Path("/path/to/repo"),
    workspace_root=Path("/tmp/ases-workspaces"),
    git=SubprocessGitClient(),
    clock=SystemClock(),
    lease_ttl_seconds=3600,
)

ws = mgr.acquire("TASK-0001", holder="worker-1")
print(ws.workspace_id, ws.branch, ws.path)
# TASK-0001 agent/TASK-0001 /tmp/ases-workspaces/TASK-0001

mgr.renew("TASK-0001", holder="worker-1")  # 续租（返回新租约，不原地改）
mgr.reset("TASK-0001")  # 重置：reset --hard + clean -fd（重试路径）
mgr.release("TASK-0001")  # 释放：删 worktree，**保留分支**（分支是交付物）
```

要点：

- 命名规则：分支 = `agent/<task_id>`，工作空间目录 = `<workspace_root>/<task_id>`。`task_id` 非法（超 64 字符、含 `..`）直接**拒绝**而不是改写——被悄悄改写的 ID 会让日志和数据库对不上。
- 租约文件存在 `<workspace_root>/.ases/leases/`，**在所有 worktree 之外**——Agent 提交不到它，"有没有主人"也无歧义。
- `acquire()` 会先落盘租约**再**建 worktree：中途崩溃留下的是"有主的孤儿"而不是"无主孤儿"。
- 租约被别的持有者占用时抛 `WorkspaceBusyError`。

回收：

```python
from harness.workspace.gc import WorkspaceGC

gc = WorkspaceGC(repo_path=..., workspace_root=..., git=SubprocessGitClient(), clock=SystemClock())
print(gc.candidates())  # 租约过期 / 无租约（孤儿）
print(gc.collect())  # 执行回收
```

### 6.7 调用 Coding Agent

**Claude Code（当前唯一完整可用的执行器）**：

```python
from executors.claude_code.adapter import ClaudeCodeAdapter
from executors.base import SessionConfig, ExecutionTask

adapter = ClaudeCodeAdapter(binary="claude", permission_mode="default")
print(adapter.name)  # claude-code

session = await adapter.create_session(
    SessionConfig(workspace="/tmp/ases-workspaces/TASK-0001", branch="agent/TASK-0001")
)

handle = await adapter.execute(
    ExecutionTask(
        session_id=session.session_id,
        task_id="TASK-0001",
        title="修复登录超时",
        instruction="在 auth/session.py 中把 token 有效期从 3600 改为 7200，并补一个测试。",
        timeout_seconds=1800,
    )
)

result = await adapter.collect_result(session.session_id)
print(result.status, result.artifacts, result.execution)
```

实际拼出的命令行：

```
claude --print --output-format stream-json --verbose --session-id <UUID> \
       [--model <model>] [--permission-mode default] "<title>\n\n<instruction>"
```

三个必须知道的坑：

1. **`--verbose` 是必需的**——`--print` 配 `--output-format stream-json` 时不加 `--verbose`，CLI 会以退出码 2 退出。
2. **`--session-id` 必须是合法 UUID**——非法值 CLI 会静默替换成它自己的 ID，导致会话对不上（适配器会抛 `SessionMismatchError`）。
3. **不加 `env_provider` 时，适配器会整体继承宿主机 `os.environ`**。要隔离就必须显式传入环境构造函数（见 §6.8）。这正是"沙箱隔离必须先于执行器接入"的由来。

凭证只能通过 `credential_provider` + `credential_env_name` 注入——`credential_ref` 是**引用**不是密钥本身，令牌只落到子进程环境里，不进 argv、不进 Prompt。

**Codex（当前不可用）**：`executors/codex/events.py` 的事件表为空，`map_event()` 恒返回 `unhandled`，`summarize()` 恒返回空结果——**每次运行都会以 `FAILED` 结束**（"无证据即无结论"，是刻意的设计而非 bug）。启用它的三个步骤：安装 `codex` → 抓取真实 stdout 与 argv → 补全事件表。`CodexInvocation` 里的四个参数与 `OPENAI_API_KEY` 目前都标注为**未验证**。

**OpenCode / OpenHands / Gemini CLI**：当前是 0 字节空包。

**能力声明**：适配器通过 `capabilities` 声明支持 `STREAM_EVENTS` / `INTERRUPT` / `PARALLEL_SESSIONS` / `STRUCTURED_OUTPUT`；请求不支持的能力会抛 `UnsupportedCapabilityError`。

**结果判定不在适配器里**。适配器只报告"执行器做了什么"，"算不算成功"由 §6.11 的 `resolve_outcome` 决定。

### 6.8 在沙箱里跑命令

沙箱的环境是**从白名单重建**的，不是继承来的：

```python
from harness.sandbox.policy import (
    build_sandbox_env,
    default_sandbox_policy,
    build_workspace_sandbox_spec,
)
from harness.sandbox.launcher import SandboxLauncher
from pathlib import Path

policy = default_sandbox_policy()
env = build_sandbox_env({"HOME": "/Users/x", "PATH": "/usr/bin", "AWS_SECRET_ACCESS_KEY": "leak"})
print(sorted(env))
# ['CI', 'GIT_CONFIG_NOSYSTEM', 'GIT_TERMINAL_PROMPT', 'HOME', 'LANG', 'LC_ALL',
#  'NPM_CONFIG_UPDATE_NOTIFIER', 'PATH', 'PIP_DISABLE_PIP_VERSION_CHECK',
#  'PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED', 'TERM', 'TZ']
```

`AWS_SECRET_ACCESS_KEY` **没有出现**——环境是重建的，不是过滤的。

安全规则：

- 名字里带 `SECRET` / `TOKEN` / `PASSWORD` / `CREDENTIAL` / `KEY` / `APIKEY` 的变量**一律拒绝**——凭证应当以**只读挂载**进入，而不是环境变量。
- 挂载源不能是 `~/.ssh`、`~/.aws`、`~/.kube`、`~/.docker`、`~/.gnupg`、`.netrc`、`.pgpass` 等凭证目录；`/root`、`/etc/ssh`、`/var/run/docker.sock` 永远禁止。
- 含凭证目录的**路径值**也会被拒绝（防止 `HOME=/Users/x/.aws` 这类夹带）。
- 数据库 DSN 只允许指向 `allowed_db_hosts` 里的主机，且主机名含 `prod` / `production` 词元时拒绝。
- 容器默认 `--cap-drop ALL` + `--security-opt no-new-privileges` + `--network none`。

启动：

```python
launcher = SandboxLauncher(docker_binary="docker", timeout_seconds=1800)

spec = build_workspace_sandbox_spec(
    workspace_path=Path("/tmp/ases-workspaces/TASK-0001"),
    task_id="TASK-0001",
    image="ases/sandbox:latest",
    network="none",
)
result = launcher.run(spec)  # 同步执行，返回 exit_code/stdout/stderr
cid = launcher.start(spec)  # 或后台启动，返回 container_id
launcher.stop(cid, grace_seconds=30)
print(launcher.container_running(cid))  # True / False / None（None = docker 不可达）
```

`container_running` 用 `docker ps --format {{.State}}` 而非 `docker inspect`，以便区分"容器没了"（空输出）和"docker 坏了"（非零退出）。

### 6.9 安全门与网络白名单

安全门有三项 forbidden 检查：密钥进入 diff、依赖清单变更、公共 API 破坏性变更。

```bash
# 从 diff 文件
uv run python -m harness.security.gate --diff-file /tmp/change.diff

# 从标准输入
git diff main...agent/TASK-0001 | uv run python -m harness.security.gate --stdin

# 带工作区（可正确解析依赖清单）
uv run python -m harness.security.gate --diff-file /tmp/change.diff \
    --workspace /tmp/ases-workspaces/TASK-0001 \
    --baseline-workspace /path/to/baseline \
    --authorized-dependency requests \
    --protected-package mypkg \
    --protected-symbol mypkg.public_api
```

退出码：`0` = 未命中，`1` = 命中，`2` = 用法 / 读取错误。

实际输出示例：

```
注意：本次未对规则 public_api_breaking_change 产出结论
安全门：命中 1 处
  - [secrets_in_diff] app.py：第 1 行命中凭据形状（AKIA…（共 20 字符））
  - 未产出结论的规则：public_api_breaking_change
```

要点：

- **未产出结论的规则会显式提示**（走 stderr）。退出码 0 不等于"所有规则都检查过了"——没配置 `protected_packages` 时公共 API 检查无法进行。
- 命中详情**只回显形状和长度，不回显内容**。
- 含 `example` / `changeme` / `placeholder` / `dummy` / `fake` / `todo` / `fixme` / `your-` 等标记的值视为占位符，**跳过**（AWS 官方文档示例 key 因此不会误报）。
- 锁文件（`lock` / `poetry.lock` / `package-lock.json`）**不算新增依赖声明**——锁文件变动是后果，不是新声明。
- 不支持的清单（`setup.py` / `Pipfile` / `Cargo.toml` / `Gemfile` / `composer.json` / `pom.xml` / `build.gradle*` / `environment.yml`）按**未授权**处理，而不是静默跳过。

程序化调用：

```python
from harness.security.gate import SecurityGate, SecurityPolicy

gate = SecurityGate(SecurityPolicy(authorized_dependencies=frozenset({"requests"})))
report = gate.check(diff_text)
print(report.passed, report.describe())
```

网络出口：

```python
from harness.security.network import NetworkPolicy, check_url

policy = NetworkPolicy(allowed_hosts=frozenset({"api.example.com", "*.github.com"}))
host, port = check_url("https://api.example.com/v1/models", policy=policy)
```

**默认白名单为空 = 默认拒绝一切**。校验发生在**连接之前**——被拒的 URL 产生零次连接尝试。`file://` 和 URL 内嵌凭证一律拒绝；重定向**不自动跟随**（`follow_redirects=False`），交回调用方重新校验。

任务级授权判定与工具调用审计见 [§6.19](#619-任务级授权与工具调用审计)。

### 6.10 独立验证

**构造验收契约**：

```python
from harness.verification.contract import (
    AcceptanceContract,
    RequiredCheck,
    ForbiddenRule,
    ArtifactKind,
    Threshold,
)
from core.project.models import TargetValue, ComparisonOperator

contract = AcceptanceContract(
    task_id="TASK-0001",
    required=(RequiredCheck.UNIT_TESTS_PASS, RequiredCheck.LINT_PASS),
    thresholds={
        "test_coverage": Threshold(operator=ComparisonOperator.GTE, value=TargetValue.parse("0.85"))
    },
    forbidden=(ForbiddenRule.SECRETS_IN_DIFF,),
    artifacts=(ArtifactKind.GIT_DIFF, ArtifactKind.TEST_REPORT),
)
```

**判定（纯函数，可单测）**：

```python
from harness.verification.contract import (
    ContractEvidence,
    CheckResult,
    CheckOutcome,
    evaluate_contract,
)

evidence = ContractEvidence(
    task_id="TASK-0001",
    checks={
        RequiredCheck.UNIT_TESTS_PASS: CheckResult(
            check=RequiredCheck.UNIT_TESTS_PASS, outcome=CheckOutcome.PASSED
        )
    },
    metrics={},
    violations=frozenset(),
    produced_artifacts=frozenset({ArtifactKind.GIT_DIFF}),
)
result = evaluate_contract(contract, evidence)
print(result.passed, result.failures, result.missing)
```

**跑真实验证**：

```python
import asyncio
from pathlib import Path
from harness.verification.engine import (
    VerificationEngine,
    VerificationPlan,
    CheckSpec,
    VerificationRequest,
)

plan = VerificationPlan(
    checks=(
        CheckSpec(
            check=RequiredCheck.UNIT_TESTS_PASS,
            command=("python", "-m", "pytest", "-q"),
            timeout_seconds=900,
        ),
        CheckSpec(
            check=RequiredCheck.LINT_PASS, command=("ruff", "check", "."), timeout_seconds=300
        ),
    ),
    model="claude-opus-4-8",  # 独立的验证模型，与代码生成模型分开
)

engine = VerificationEngine()
run = asyncio.run(
    engine.verify(
        VerificationRequest(
            task_id="TASK-0001",
            workspace=Path("/tmp/ases-workspaces/TASK-0001"),
            contract=contract,
            plan=plan,
        )
    )
)
print(run.evaluation)  # ContractEvaluation(passed=True, ...)
print(run.to_report().verdict)  # passed
```

隔离保证（由测试固定，不只是文档承诺）：

- `VerificationRequest` **没有任何字段能携带 `ExecutionResult`**——"参考 Agent 说的话"在类型上就写不出来。
- 检查在**独立进程**中运行（`create_subprocess_exec`，**从不** `shell=True`，所以命令里不能有管道）。
- 环境用白名单重建，`HOME` 指向一次性临时目录（不是宿主机 home），所以 `~/.claude/projects/<session>.jsonl` 够不着。
- 检查**并发**执行，结果按 plan 顺序重排。
- 超时杀整个**进程组**（SIGTERM → 5 秒宽限 → SIGKILL）。
- 扫描类检查用**退出码**作信号（0 = 干净，非零 = 命中）；扫描跑不起来**不算命中**，但也不算干净——它不产出产物，契约随后会把它记为 `missing`。
- `to_report()` 三分：证据不完整 → `ERROR`；证据完整但契约没过 → `FAILED`；完整且通过 → `PASSED`。

### 6.11 判定任务结果（自我报告不参与）

```python
from core.task.outcome import ExecutionStatus, VerificationReport, resolve_outcome


class _R:
    status = ExecutionStatus.COMPLETED


resolution = resolve_outcome(_R(), VerificationReport(verdict="passed"))
print(resolution.outcome, resolution.reason)
```

| 执行状态 | 验证结论 | 判定结果 |
|---|---|---|
| `COMPLETED` | 无报告 | `VERIFICATION_ERROR`（"没验证" ≠ "验证通过"，fail-closed） |
| `COMPLETED` | `passed` | `COMPLETED` |
| `COMPLETED` | `failed` | `FAILED` |
| `COMPLETED` | `error` | `VERIFICATION_ERROR`（需要人，不自动修复） |
| `FAILED` | — | `FAILED` |
| `CANCELLED` | — | `CANCELLED` |
| `TIMED_OUT` | — | `TIMED_OUT`（**超时不是失败的一种**） |

`resolve_outcome` 的第一个参数是 `StatusSource` Protocol——它**只有一个成员 `status`**。`self_assessment` 在类型上不可达，mypy 会直接拒绝读取。这是"Agent 不能给自己打分"的**结构性**保证，不是纪律要求。

### 6.12 取消与恢复

**四层取消链**（顺序固定：task → adapter → process → sandbox，由内向外）：

```python
from core.task.cancellation import CancellationTarget
from harness.sandbox.cancellation import build_cancellation_chain

chain = build_cancellation_chain(
    interrupter=adapter,  # Interrupter Protocol：interrupt(session_id) -> ack
    process_stopper=adapter,  # ProcessStopper Protocol：ensure_process_stopped
    container_stopper=launcher_stopper,  # ContainerStopper Protocol：ensure_container_stopped
)

report = await chain.cancel(
    CancellationTarget(
        task_id="TASK-0001",
        session_id=session.session_id,
        container_id=cid,
    )
)
print(report.cancelled)  # 只有全部层 CONFIRMED 才为 True
print(report.unfinished)  # 未确认的层
```

每层的"完成"含义是"**这一类资源不再运行**"。某一层超时或报错记为 `FAILED`，链**继续往下走**——一层失败不会阻断下一层。状态按**字符串**比较（未知状态 → `UNCONFIRMED`，fail-closed），因此 `harness/` 不需要 import `executors/`。

先停执行器再停容器——反过来执行器就永远无法确认了。

**超时恢复**：

```python
from executors.recovery import recover_from_timeout, RecoveryPorts

plan = await recover_from_timeout(
    task,
    ports=RecoveryPorts(
        interrupter=adapter,
        probe=adapter,
        reclaimer=chain_resource_reclaimer,
    ),
)
print(plan.outcome, plan.action)  # action ∈ {RESUME, RETRY, FAIL}
```

判定顺序固定：**interrupt → reclaim → probe → decide**。

| 条件 | 结果 |
|---|---|
| 未能中断 | `FAILED` |
| 未能回收资源 | `FAILED` |
| 错误类别为 `NEEDS_HUMAN` | `FAILED`（系统不自我授权） |
| 探测到可恢复 | `RESUME`（同 attempt，**同一**幂等键） |
| 可重试 | `RETRY`（attempt+1，**新**幂等键） |
| 其余 | `FAILED` |

### 6.13 任务状态迁移

```python
from core.task.state_machine import apply_action, TransitionContext, TaskState, TaskAction

# 进入 RUNNING 前必须带上验收契约已绑定的信号
result = apply_action(
    TaskState.READY,
    TaskAction.START,
    TransitionContext(task_id="TASK-0001", acceptance_contract_bound=True),
)
print(result.previous, "→", result.current, "changed:", result.changed)
```

不满足闸门时：

```python
apply_action(
    TaskState.READY,
    TaskAction.START,
    TransitionContext(task_id="TASK-0001", acceptance_contract_bound=False),
)
# AcceptanceContractRequiredError: 任务 TASK-0001 在进入 RUNNING 前必须已有绑定 task_id 的验收契约
```

查某状态允许的动作：

```python
from core.task.state_machine import legal_actions, happy_path

print(sorted(a.value for a in legal_actions(TaskState.VERIFYING)))
print(happy_path())
```

### 6.14 持久化与仓储

下面是一段**已在真实数据库上跑通**的完整链路：建项目 → 登记候选 → 记录决策 → 派生任务 → 绑验收契约 → 推进状态机。

```python
import uuid
from infrastructure.database.engine import get_session_factory, session_scope
from infrastructure.database.repositories import (
    CandidateRepository,
    DecisionRepository,
    TaskRepository,
)
from core.entities.project import Project
from core.task.state_machine import TaskAction
from harness.verification.contract import AcceptanceContract, RequiredCheck

factory = get_session_factory()  # 进程级单例，不需要传 engine
project_id = f"demo-{uuid.uuid4().hex[:8]}"

# 1) 建项目（目前没有 ProjectRepository，直接用 ORM 实体）
async with session_scope(factory) as s:
    s.add(
        Project(
            id=project_id,
            name="演示项目",
            goal={},
            policy={},
            budget={},
            repository_url="https://example.com/x",
            default_branch="main",
        )
    )

# 2) 候选 → 决策 → 任务 → 绑契约 → 推进状态
async with session_scope(factory) as s:
    candidate = await CandidateRepository(s).create(
        project_id=project_id,
        category="quality",
        title="补齐登录测试",
        evidence=[{"ref": "cov-001", "kind": "coverage"}],  # evidence 不能为空
    )
    decision = await DecisionRepository(s).record(
        candidate_id=candidate.id,
        decision="record",
        rationale="证据充分",
    )
    tasks = TaskRepository(s)
    task = await tasks.create_from_decision(
        decision_id=decision.id,
        title="补齐登录测试",
        task_type="test_improvement",
    )
    print(task.status)  # APPROVED —— 从决策派生出来的任务默认已批准

    contract = AcceptanceContract(task_id=str(task.id), required=(RequiredCheck.UNIT_TESTS_PASS,))
    await tasks.bind_acceptance_contract(task_id=task.id, contract=contract.to_document())

    for action in (TaskAction.PLAN, TaskAction.READY, TaskAction.START):
        task = await tasks.transition(task_id=task.id, action=action)
    print(task.status)  # RUNNING
```

非法迁移会带出可操作的信息：

```python
await tasks.transition(task_id=task.id, action=TaskAction.COMPLETE)
# IllegalTransitionError: 状态 RUNNING 下不允许动作 complete（允许的动作：block, verify）
```

要点：

- `TaskRepository.create_from_decision(...)` 是**唯一**构造 Task 的入口——数据库外键 `decision_id NOT NULL` 是第二道保险。
- `bind_acceptance_contract` 会拒绝 `task_id` 不匹配的契约。
- `transition` 会自动从任务当前状态构造 `TransitionContext`（契约是否已绑、`repair_count`、修复上限）。
- `session_scope` 在异常时回滚，**不做隐式提交**；提交发生在退出 `async with` 时。
- `get_session_factory()` 是进程级 `@cache` 单例，**不接受参数**（`get_engine()` 同理）。

### 6.15 产物存储（内容寻址）

```python
from infrastructure.storage.object_store import build_content_addressed_store

store = build_content_addressed_store()  # 有 ASES_S3_ENDPOINT_URL → S3/MinIO，否则本地文件系统
obj = await store.put_bytes(b"report content")
print(obj.key, obj.uri, obj.digest, obj.skipped)
# sha256:... s3://ases-artifacts/sha256/...

data = await store.get_bytes(obj.key)  # 读取时校验哈希，不一致 → ArtifactHashMismatchError
print(await store.verify(obj.key))
```

同一内容重复写入是幂等的，**不覆盖**已有对象——即使已存对象已损坏，损坏也必须保持可见。

### 6.16 评估指标

```python
from decimal import Decimal
from evaluation.task_evaluation.delivery import TaskOutcome, task_success_rate, rework_rate
from evaluation.evolution_evaluation.discovery import Discovery, valid_discovery_rate
from evaluation.project_evaluation.attainment import CycleAttainment, attainment_delta

print(
    task_success_rate(
        [
            TaskOutcome(task_id="T1", completed=True),
            TaskOutcome(task_id="T2", completed=False, repair_count=2),
        ]
    ).value
)  # 0.5

print(
    attainment_delta(
        [
            CycleAttainment(
                cycle_id="c1", attainment_before=Decimal("0.70"), attainment_after=Decimal("0.78")
            ),
        ]
    ).value
)  # 0.08
```

> `attainment_before` / `attainment_after` 是 **`Decimal`**，传字符串会在做减法时抛 `TypeError`。

**指标 ID 会被主动拒绝**：一个 metric id 只有在**同时**包含"活动对象词"（task / token / commit）和"数量词"（count / total / usage / num …）时才被拒——`task_success_rate` 合法（`rate` 刻意不算数量词），而 `token_count` / `num_tasks` / `commits_total` 在注册时就被拒。

**分母为 0 时返回 `None` 而不是 0**——"没有样本"必须和"指标为零"区分开（状态是 `NO_SAMPLES` / `BASELINE_MISSING`）。

### 6.17 观测

**日志**：

```python
from infrastructure.observability.logging import configure_logging, bind_task_context, get_logger
from infrastructure.config.settings import get_settings

configure_logging(get_settings())  # 幂等，可重复调用
bind_task_context(task_id="TASK-0001", project_id="my-product")  # 只绑定非 None 的字段
get_logger("ases.worker").info("任务完成")
```

输出（JSON 模式）：

```json
{"event": "任务完成", "project_id": "my-product", "task_id": "TASK-0001",
 "level": "info", "timestamp": "2026-11-08T15:42:44.759038Z", "service": "ases"}
```

- 每行必含：`event`、`level`、`timestamp`（ISO-8601 UTC）、`service`。
- 保留上下文键：`task_id`、`cycle_id`、`project_id`。
- **自动脱敏**：键名命中 `api_key` / `secret` / `password` / `token` / `authorization` / `private_key` / `credential` … 的字段值被替换为 `***`（空串 → `empty`，None/缺失 → `unset`）。

**追踪与 Langfuse**：

```python
from infrastructure.observability.tracing import configure_tracing, get_tracer
from infrastructure.observability.langfuse import LangfuseClient

configure_tracing(get_settings())  # 未启用返回 None，不报错
with get_tracer("ases").start_as_current_span("verify") as span:
    span.set_attribute("task_id", task_id)

lf = LangfuseClient(get_settings())
lf.record_trace("task_execution", trace_id=task_id, input_data={...}, output_data={...})
```

Langfuse 客户端**从不抛异常**：失败时进入 `DEGRADED` 状态并记一条 `langfuse_unavailable`（只记一次），下次成功时记 `langfuse_recovered`。请求超时 2 秒。

### 6.18 记录演进周期

```python
from decimal import Decimal
from datetime import datetime, timezone
from core.metrics.cycle import EvolutionCycle, CycleCounts, CycleStatus, classify_cycle

counts = CycleCounts(candidates=5, decisions=3, tasks=2, completed_tasks=2)

verdict = classify_cycle(
    CycleStatus.COMPLETED,
    counts,
    attainment_before=Decimal("0.70"),
    attainment_after=Decimal("0.78"),
)
print(verdict.outcome, verdict.reason)
# improved 目标达成度从 0.70 提升到 0.78（差值 +0.08）

now = datetime.now(timezone.utc)
cycle = EvolutionCycle.build(
    project_id="my-product",
    cycle_id="cycle-0007",
    trigger="schedule",
    started_at=now,
    ended_at=now,
    status=CycleStatus.COMPLETED,
    counts=counts,
    attainment_before=Decimal("0.70"),
    attainment_after=Decimal("0.78"),
)
print(cycle.cycle_id, cycle.outcome, cycle.attainment_delta)
```

> `attainment_before` / `attainment_after` 是 **`Decimal`**。

**`build()` 没有 `outcome` 参数**——结论由 `classify_cycle` 推导，你**不能手填 "improved"**。

分类顺序：状态非 `COMPLETED` → `STALLED`；`completed_tasks == 0` → `STALLED`；缺前后达成度 → `NO_CHANGE`；增量 > 0 → `IMPROVED`；< 0 → `REGRESSED`；== 0 → `NO_CHANGE`。

### 6.19 任务级授权与工具调用审计

两个模块分工明确：**授权回答"准不准"**，**审计回答"做了什么"**。

- `harness/security/authorization.py` —— 判定一次请求是否被授权；
- `harness/security/audit.py` —— 把已经发生的事实原样记下来。

两者通过一个接口接在一起：授权模块只管"要不要留痕"，审计模块只管"留成什么样"。

#### 6.19.1 授权判定

授权绑定的是**任务**，不是 Agent。判定的 key 是五元组
`(agent_id, task_id, project_id, workspace_id, expiry)` —— 少了 `task_id`，
"这个 Agent 有权限"就变成了对整个仓库的授权。

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.policy.autonomy import Operation
from harness.security.audit import AuditLog, JsonlAuditStore
from harness.security.authorization import (
    AuthorizationRegistry,
    AuthorizationRequest,
    AuthorizationService,
    TaskAuthorization,
    path_scope,
)

workspace = Path("/tmp/ases-ws/TASK-0001").resolve()
workspace.mkdir(parents=True, exist_ok=True)

# 判定留痕的去处就是审计表
log = AuditLog(JsonlAuditStore(Path("/tmp/ases-audit.jsonl")))

registry = AuthorizationRegistry()
registry.register(
    TaskAuthorization(
        agent_id="agent-coding",
        task_id="TASK-0001",
        project_id="my-product",
        workspace_id="ws-0001",
        expiry=datetime.now(UTC) + timedelta(hours=1),
        allowed_operations=frozenset({Operation.CODE_CHANGES}),
        granted_scopes=frozenset({"repo:write", path_scope(workspace)}),
    )
)

service = AuthorizationService(
    registry,
    sink=log,
    workspace_roots={"ws-0001": workspace},
)

ok = service.authorize(
    AuthorizationRequest(
        agent_id="agent-coding",
        task_id="TASK-0001",
        project_id="my-product",
        workspace_id="ws-0001",
        operation=Operation.CODE_CHANGES,
        resource=str(workspace / "src/main.py"),
    )
)
print(ok.allowed, "|", ok.reason)
# True | 授权有效

denied = service.authorize(
    AuthorizationRequest(
        agent_id="agent-coding",
        task_id="TASK-0002",  # 换一个任务 —— 授权借不过来
        project_id="my-product",
        workspace_id="ws-0001",
        operation=Operation.CODE_CHANGES,
        resource=str(workspace / "src/main.py"),
    )
)
print(denied.allowed, "|", denied.reason)
# False | 没有针对 agent=agent-coding task=TASK-0002 project=my-product workspace=ws-0001 的授权记录
```

要点：

- **两道闸一起过**：`allowed_operations`（做什么）与 `granted_scopes`（能碰什么）都要过。只写一半的授权语义不完整。
- **空范围 = 什么都没授权**。这与 §6.4 里 `TaskContext.permits` 的"空 = 不收窄"**刻意不同** —— 那个方法是在策略**之上**做加法，而这里是授权**本身**，按"不收窄"解释等于把"忘了写范围"变成"全部允许"。
- **有效期是 key 的一部分**。同一身份下"10:00 到期"与"12:00 到期"是两条不同的授权；同身份多条记录时**后发接管**而不是取并集 —— 取并集会让"收回授权"根本做不到。
- **到期时刻本身就算过期**（含端点）。`expiry is None` 表示长期有效。
- **工作空间边界按路径关系判，不按字符串前缀**：`/ws` 与 `/ws-other` 是两个目录，`..` 爬出去与符号链接指向外部都被拒。
- **高风险能力要一份独立授权记录**（删除 / 发布 / 合并 / 部署 / 改治理规则），且该记录**必须引用一条审批编号** —— 自己签发自己的许可不构成独立授权。**没有独立册子时高风险全部拒绝**。
- **留痕失败即阻断**（fail-closed）：判定记录写不下去时，判定结果就是**拒绝**，操作压根不开始。
- `authorize()` 返回判定结果、不抛异常；`require()` 在被拒时抛 `AuthorizationDeniedError`（给工具调用拦截点用 —— 判定与"停在这里"是同一个动作）。

#### 6.19.2 审计记录

```python
from harness.security.audit import AuditResult

with log.audited_call(
    agent_id="agent-coding",
    task_id="TASK-0001",
    project_id="my-product",
    workspace_id="ws-0001",
    resource=str(workspace / "src/main.py"),
    operation="write_file",
    capability="repo:write",
) as outcome:
    outcome.result = AuditResult.SUCCESS
    outcome.reason = "已写入 src/main.py"
```

退出上下文时，块内的结果与说明被写进审计表：

```python
from harness.security.audit import AuditQuery

for event in log.query(AuditQuery(task_id="TASK-0001")):
    print(event.operation, "|", event.result, "|", event.decision, "|", event.capability)
    print("   理由：", event.reason)
# code_changes | success | allow | repo:write
#    理由： 授权有效
# write_file | success | allow | repo:write
#    理由： 已写入 src/main.py
```

要点：

- **四要素缺一不可**：主体（`agent_id`）、任务（`task_id`）、资源（`resource`）、结果（`result`）—— 缺一条就**构造不出来**（空白字符串也算空）。记录本身 frozen，写下来就改不了。
- **只追加是构造出来的**：存储协议 `AuditStore` 里只有 `append` 与 `query`，**没有 `update`、没有 `delete`**；业务代码拿到的 `AuditRecorder` 协议**只有一个成员** `record_tool_call` —— 既没有查询，也拿不到存储句柄。
- **两道闸位置不同是有意的**：判定留痕在动作**之前**（写失败 → 授权被拒，操作不开始）；调用记录在动作**之后**（写失败 → 抛 `AuditWriteError`，这次调用**不算做成了**）。前者能真正地"不让他做"，后者保证"做过的事不会没有记录"。
- **异常不会被审计吞掉**：块内抛异常时先记成 `failure` 再原样抛出；连这条失败记录都写不下去时抛 `AuditWriteError`，**原始异常挂在 `__cause__` 上**。
- **本体是文件**（`JsonlAuditStore`），一行一条 JSON，`flush` + `fsync` —— "写成功"不会因为之后的事务回滚而消失。**读到坏行就报错，不跳过** —— 一份会跳过坏行的审计表等于给了篡改者一个后门。
- **查询一律精确匹配**（前缀匹配会让"查 TASK-04"挖出半个仓库），时间范围是**闭区间**，naive 时间一律按 UTC 解释。

字段字典见 [`docs/observability/audit-schema.md`](observability/audit-schema.md)。

---

## 7. API 参考

### 7.1 当前 HTTP 面

系统**只有两个端点**，都无认证、都公开：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/healthz` | 存活 + 依赖探针 |
| `GET` | `/readyz` | 就绪 + 依赖探针（阶段一与 `/healthz` 探同一批依赖） |

另有 FastAPI 自动挂载的 `/docs`、`/redoc`、`/openapi.json`。

**成功（200）**：

```json
{
  "status": "ok",
  "checks": {
    "database": { "ok": true },
    "object_store": { "ok": true }
  }
}
```

**降级（503）**：

```json
{
  "status": "degraded",
  "checks": {
    "database": { "ok": false, "error": "OperationalError" },
    "object_store": { "ok": true }
  }
}
```

字段语义：

- `ok` 恒存在（bool）。
- `error` **只在失败时出现**（成功时该键完全不存在，不是 `null`）。值是**异常类型名**，**绝不含异常消息**——连接异常消息里常带含口令的 DSN。完整错误请查日志。
- `object_store` 的 `ping()` 返回 `False`（不抛异常）时，`error` 为字符串 `"Unreachable"`。

**503 而不是 500**：依赖挂了是"暂时不可用"，不是"服务有 bug"，负载均衡器要区别对待。

探针内容：`database` → `SELECT 1`；`object_store` → 只读 `ping()`（**不建桶**，建桶是 `ensure_ready()` 的职责）。

> **部署提醒**：当前 API 面完全公开且无认证。请**不要**把 8000 端口直接暴露到公网。

### 7.2 命令行

仓库里**没有** `console_scripts`、没有 `argparse`/`click`/`typer`、没有 `__main__.py`。可用的命令只有：

| 命令 | 作用 |
|---|---|
| `uvicorn apps.api.main:app --host 0.0.0.0 --port 8000` | 启动 API |
| `python -m apps.worker.main` | 启动 worker |
| `alembic upgrade head` | 数据库迁移 |
| `python -m harness.security.gate ...` | 安全门 CLI（见 §6.9） |

`make seed`（载入示例项目配置）**当前会失败**——`scripts/seed.py` 不存在，`scripts/` 下只有空的 `__init__.py`。

### 7.3 Worker 心跳

worker 当前**没有任务循环**，是一个"依赖探针 + 30 秒心跳 + 优雅退出"的待命进程。

- 心跳文件：`ASES_WORKER_HEARTBEAT_FILE`（默认 `/tmp/ases-worker/heartbeat`）
- 心跳间隔：30 秒；**过期判定：90 秒**（连续两个周期没刷新就不再算健康）
- 文件内容是 ISO-8601 时间戳（不是 `touch`，因为 mtime 时区不可靠）
- compose 的健康检查查的是**心跳文件新鲜度**，不是进程存活——"进程活着" ≠ "还在正常干活"
- `SIGTERM` / `SIGINT` → 优雅退出

---

## 8. 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| `make seed` 报 `ModuleNotFoundError: scripts.seed` | 脚本未实现 | 手动用 `load_project()` 载入（§6.1） |
| `make up` 卡住 | 依赖未就绪 | `make ps` 看健康检查；调 `COMPOSE_WAIT_TIMEOUT`；`make logs` |
| API 返回 503 `database` | Postgres 不可达 | 检查 `ASES_DATABASE_URL`；`make psql` 验证 |
| API 返回 503 `object_store` | MinIO 不可达 | 容器内地址是 `http://minio:9000`，宿主机是 `http://localhost:9100` |
| `ObjectStoreUnavailableError` | 缺 `aioboto3` | `uv sync --extra s3` |
| 启动即 `ValidationError`（非 local） | 用了 `minioadmin/minioadmin` | 换真实凭证 |
| 启动即 `ValidationError`（production） | 缺 Langfuse key 或 S3 secret | 补齐后重启 |
| `AcceptanceContractRequiredError` | 未绑验收契约就进 `RUNNING` | 先 `bind_acceptance_contract()` |
| `RepairBudgetExhaustedError` | 修复次数超限（默认 3） | 升级人工处理 |
| `IllegalTransitionError` | 非法状态迁移 | 用 `legal_actions(current)` 查合法动作 |
| Codex 任务全部 `FAILED` | 事件表为空（**设计如此**） | 暂用 Claude Code，或补 `executors/codex/events.py` |
| Claude Code 退出码 2 | 缺 `--verbose` | 适配器已自动带上；手工调用时补上 |
| `SessionMismatchError` | `--session-id` 非法 UUID | 用 `uuid.uuid4()` |
| Agent 拿到了宿主机的密钥 | 未传 `env_provider` | 传 `build_sandbox_env(...)`（§6.8） |
| 安全门退出码 0 但有"未产出结论" | 未配置 `protected_packages` | 用 `--protected-package` 补齐 |
| 安全门输出 `RuntimeWarning: 'harness.security.gate' found in sys.modules` | `harness/security/__init__.py` 已导入 `gate`，`-m` 又执行了一次 | 无害，可忽略；或直接 `python -c "from harness.security.gate import main; main()"` |
| `ArtifactHashMismatchError` | 产物被篡改 | 证据链出问题了；查 `artifact` 表与对象存储 |
| `AuditWriteError` | 审计记录写不下去 | 这是**阻断**不是告警 —— 查审计文件是否可写、磁盘是否满；原始异常在 `__cause__` 上 |
| `AuthorizationDeniedError` | 授权被拒 | 看 `decision.reason`：身份不匹配 / 过期 / 越界 / 高风险缺独立授权 |
| worker 容器不健康 | 心跳文件超过 90 秒未更新 | 看 `docker compose logs worker`；检查心跳路径可写 |

---

## 9. 已知缺口

按对使用者的影响排序：

| # | 缺口 | 影响 | 计划 |
|---|---|---|---|
| 1 | 无 HTTP 业务接口 | 无法通过 API 驱动任务生命周期 | TASK-P1-060/061/062 |
| 2 | `orchestration/` 全空 | 两个循环都没有实现，系统不会自主演进 | TASK-P1-050/060/061/064 |
| 3 | `agents/` 全空 | 没有 Director / Analyst / Critic | TASK-P1-051~057 |
| 4 | `knowledge/` 全空 | 每轮都要重新理解仓库，无跨轮认知 | TASK-P2-016 |
| 5 | 无人工审批门 | `approval_required` 只能靠外部流程兑现 | TASK-P1-046 |
| 6 | 集成引擎空 | 多任务并行后无合并/回归验证 | TASK-P1-047 |
| 7 | `make seed` 目标缺失 | 快速开始流程断了一步 | — |
| 8 | Codex 执行器不可用 | 只有 Claude Code 可用 | — |
| 9 | 授权与审计未接入执行链 | `AuthorizationService` / `AuditLog` 有实现有测试，但执行器还没有调用它们 | TASK-P1-046 |
| 10 | `config/governance/` 目录不存在 | `ASES_GOVERNANCE_DIR` 默认指向不存在的路径 | — |
| 11 | `ASES_ACCEPTANCE_RULES_FILE` / `ASES_BUDGET_FILE` 无消费方 | 两个配置项是摆设 | — |
| 12 | `knowledge` 与 `agents` 的设计文档缺失 | `docs/agents/prompt-contracts.md` 未写 | TASK-P1-057 |

---

## 10. 开发者附录

### 10.1 目录结构

| 目录 | 职责 |
|---|---|
| `apps/` | 可部署单元：`api`、`worker`（`director`、`scheduler` 为空占位） |
| `core/` | 领域模型与纯业务逻辑（不依赖 IO） |
| `agents/` | 角色化 Agent（**当前全空**） |
| `orchestration/` | 循环编排（**当前全空**） |
| `executors/` | 执行器适配器：Claude Code / Codex / OpenCode / OpenHands / Gemini CLI |
| `harness/` | 工程护栏：workspace / sandbox / security / verification / integration |
| `knowledge/` | 项目知识与状态（**当前全空**） |
| `evaluation/` | 评估：任务级、代码级（空）、项目级、演进级 |
| `infrastructure/` | 数据库、观测、存储、配置 |
| `docs/` | 设计文档与任务清单 |
| `tests/` | 单元 / 集成 / Agent / 系统级测试 |

### 10.2 开发命令

```bash
make lint         # ruff check
make format       # ruff format
make typecheck    # mypy --strict
make test         # pytest -m "not integration"
make test-all     # 全部测试（需先 make up）
make test-cov     # 覆盖率
make check        # lint + typecheck + test
make precommit    # pre-commit 全量钩子
```

当前规模：**211 个 Python 文件，1343 个测试**。

工具链约束：`ruff` line-length 100、target py312；`mypy --strict`（`disallow_any_explicit = true`，Pydantic 模型模块除外）；`pytest` asyncio auto、`--strict-markers`、`filterwarnings = ["error"]`。

### 10.3 分层依赖规则（由测试强制）

- `core/` 不依赖 IO，不 import `executors/` / `harness/` / `orchestration/`。
- `executors/` **只能** import `harness.workspace.naming`——不得 import `harness.sandbox`、`harness.workspace.{manager,leases,gc,git,models}`、`orchestration.*`、`agents.*`。
- `harness.workspace` 与 `harness.sandbox` **互不 import**——沙箱接受 `Path` 而非 `Workspace` 对象。
- `harness/` 不得 import `orchestration.*` / `agents.*` / `executors.*`。
- 验收状态按**字符串**比较（而不是导入 `executors` 的枚举），以维持分层。

违反会直接被 `tests/unit/executors/test_layering.py` 和 `tests/unit/harness/test_layering.py` 拦下。

### 10.4 命名与导入注意

部分包**没有**在 `__init__.py` 里 re-export，需要从子模块路径导入：

- `harness.verification` 不导出 `ContractEvidence`、`assert_verification_env_is_safe`
- `harness.sandbox` 不导出 `ChainResourceReclaimer`、`ContainerLauncher`、`workspace_container_name`
- `harness.workspace` 不导出 `InvalidTaskIdError`
- `infrastructure.config.__init__` 与 `infrastructure.observability.__init__` **完全为空**——要 import `infrastructure.config.settings`、`infrastructure.observability.logging` 等具体模块

### 10.5 数据模型速查

14 张表（12 张设计表 + 2 张预算表），全部外键 `ondelete="RESTRICT"`，**唯一例外**是 `execution_event.session_id → agent_session.id`（`CASCADE`）。

```
project ──< project_snapshot, evolution_cycle, evolution_candidate,
              evolution_decision, task, agent_session, execution_event,
              architecture_decision, task_dependency, artifact
evolution_cycle ──< evolution_candidate, evolution_decision, verification_run
evolution_candidate ──< evolution_decision ──< task
task ──< agent_session, execution_event, verification_run, artifact, task_dependency
verification_run ──< artifact
architecture_decision ──< architecture_decision (supersedes)
budget_account ──< budget_entry
```

ER 清单由 `tests/unit/database/test_data_model_doc.py` 双向断言，与 `docs/architecture/data-model.md` 保持同步。

### 10.6 日志查询示例

```jsonql
task_id = "TASK-042"
cycle_id = "CYCLE-007"
level in (error, critical)
degraded = true
```

字段字典见 [`docs/observability/log-schema.md`](observability/log-schema.md)，由 `tests/unit/infrastructure/test_log_schema.py` 固定。

---

## 附：相关文档

| 文档 | 内容 |
|---|---|
| [`design.md`](design.md) | 技术选型与总体设计（含两个循环的原始图） |
| [`tasks.md`](tasks.md) | 任务清单与待裁决项 |
| [`architecture/data-model.md`](architecture/data-model.md) | 14 张表的 ER 模型 |
| [`adr/0001-config-and-secret-handling.md`](adr/0001-config-and-secret-handling.md) | 配置与密钥处理决策 |
| [`observability/log-schema.md`](observability/log-schema.md) | 日志字段字典 |
| [`observability/audit-schema.md`](observability/audit-schema.md) | 审计表字段字典 |
| [`examples/project.yaml`](examples/project.yaml) | 完整项目定义样例 |
