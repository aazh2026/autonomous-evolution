# ASES · Autonomous Software Evolution System

自主软件演进系统。把"持续改进一个软件项目"这件事拆成**两个独立循环**，
每一步改进都必须是**可验证的**，且验证环节独立于执行环节。

- **循环 A · 工程执行循环**（分钟~小时级）：Coding Agent + QA 在隔离沙箱中执行任务
- **循环 B · 项目演进循环**（小时~天级）：Director 决策"下一步该改什么"

两个循环**不得合并成一次无限循环的 Prompt** —— 那是本项目要解决的核心问题，
而不是它要采用的架构。

## 当前状态

阶段一（Thin Slice）实现中。任务清单见 [`docs/tasks.md`](docs/tasks.md)，
设计文档见 [`docs/design.md`](docs/design.md)，
**使用手册见 [`docs/user-guide.md`](docs/user-guide.md)**（含能力边界说明与可运行示例）。

## 快速开始

```bash
uv sync --extra dev      # 安装依赖
make check               # 静态检查 + 单元测试
make up                  # 启动本地依赖（PostgreSQL + MinIO）
make migrate             # 执行数据库迁移
```

## 目录结构

| 目录 | 职责 |
| --- | --- |
| `apps/` | 可部署单元：API、Director、Scheduler |
| `core/` | 领域模型与纯业务逻辑（不依赖 IO） |
| `agents/` | 角色化 Agent：analyst / strategist / architect / quality / critic / director |
| `orchestration/` | 循环编排：LangGraph 决策图、Temporal 长期工作流、状态机 |
| `executors/` | 执行器适配器：Claude Code / Codex / OpenCode / OpenHands / Gemini CLI |
| `harness/` | 工程护栏：工作空间、沙箱、验证、集成、安全 |
| `knowledge/` | 项目知识与状态：仓库索引、架构认知、项目记忆、产物 |
| `evaluation/` | 评估：任务级、代码级、项目级、演进级 |
| `infrastructure/` | 数据库、观测、存储、配置 |
| `docs/` | 设计文档与任务清单 |
| `tests/` | 单元 / 集成 / Agent / 系统级测试 |

## 核心不变量

实现过程中必须始终成立的约束（每条都对应 `docs/tasks.md` 中的验证项）：

1. **候选 ≠ 任务**。`EvolutionCandidate` → `EvolutionDecision` → `Task` 三表分离，
   由数据库外键强制：`Task.decision_id NOT NULL`。
2. **验收契约先于执行**。`Acceptance Contract` 必须在 `READY → RUNNING` 之前存在并绑定
   `task_id`，是准入闸门而不是事后报告。
3. **验证独立于执行**。`self_assessment` 只是自我报告，不参与验收判定。
4. **身份不可混用**。授权键 = `(agent_id, task_id, project_id, workspace_id, expiry)`。
5. **治理外置**。权限策略、验收规则、预算上限来自外部配置且变更需审批；
   系统不得自主修改控制自身的策略。
6. **证据不可丢失**。`VerificationRun` / `Artifact` 为 append-only，跨演进周期保留。
7. **审计写失败即阻断**（fail-closed），不允许"审计失败就放行"。
8. **密钥不入代码、不入库**。仅从环境注入，长效密钥不进入 Prompt。

## 开发

```bash
make lint        # ruff check + ruff format
make typecheck   # mypy --strict
make test        # 单元测试
make test-cov    # 覆盖率
```

## 许可

见 [`LICENSE`](LICENSE)。
