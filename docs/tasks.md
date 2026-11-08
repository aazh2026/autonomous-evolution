# ASES 开发任务清单

> 本文把 [`design.md`](./design.md) 的技术设计拆解为可执行、可验证的开发任务。
> 每条任务都绑定**具体的完成判据**，并可回溯到设计文档的章节与要求编号。

---

## 0. 阅读指引

### 0.1 这份清单解决什么问题

`design.md` 是一份完整的散文式设计，回答了"系统应该长成什么样"，但没有回答三个工程问题：

| 工程问题 | design.md 的回答 | 本清单的回答 |
| --- | --- | --- |
| **要做什么** | 六个子系统、12 个核心实体、14 个任务状态 | 55 条阶段一任务 + 阶段二~四里程碑，每条有交付物 |
| **先做什么** | 四阶段路线（§9） | 阶段一按 9 个工作流分组 + M0/M1/M2 三道验收门 |
| **怎样算做完了** | 散落在各章的验收描述（"所有代码变更必须通过自动化测试"） | 每条任务的 `验证（T1）` ~ `验证（T4）` 字段，写明断言什么、测什么数、演示什么 |

**核心主张**：如果一条任务无法被客观验证，它就没有被拆解完成。本清单强制每条任务至少声明一个验证形态 —— 包括那些"我知道现在验不了"的降级声明（见 [第 5 节](#5-验证盲区当前阶段无法客观验证的要求)），而不是用弱证据冒充强证据。

### 0.2 编号体系

| 格式 | 含义 | 示例 |
| --- | --- | --- |
| `TASK-P1-0xx` | 阶段一任务 | `TASK-P1-042` |
| `TASK-P2-0xx` | 阶段二任务 | `TASK-P2-010` |
| `TASK-P3-0xx` | 阶段三任务 | `TASK-P3-020` |
| `TASK-P4-0xx` | 阶段四任务 | `TASK-P4-010` |
| `MS-P1-M0` | 验收门（milestone） | `MS-P1-M1` |
| `R-0xx` | 设计要求编号，指向 design.md 对应章节 | `R-063` |
| `I-0x` | 隐含工程约束编号，design.md 未明说但必须覆盖 | `I-09` |
| `T1`/`T2`/`T3`/`T4` | 验证形态标签 | `T1` |

**设计要求编号 `R-0xx` 的来源**：对 design.md 十二章逐节穷举出的 130 条可交付要求，按章节顺序编号。第 [4 节](#4-验证矩阵) 给出完整对照。

### 0.3 验证等级（分层验证法）

| 标签 | 含义 | 典型断言方式 | 证据强度 |
| --- | --- | --- | --- |
| **T1** | 自动化测试可验证 | pytest 单元/契约测试、集成测试 | 强（可重复、CI 可跑） |
| **T2** | 可量化指标可验证 | 明确的数字阈值：成功率、覆盖率、成本、次数、时延 | 强（但需基线，否则无意义） |
| **T3** | 人工演示/评审可验证 | demo 脚本、代码评审、架构评审纪要 | 中（依赖评审者） |
| **T4** | 文档/设计产物可验证 | 写下来即可检查完备性：ADR、状态机定义、目录骨架 | 弱（只证明"想清楚了"） |

**规则**：一条任务的完成判据中，**必须至少有一个 T1 或 T2**。只有 T3/T4 的任务必须在条目里显式写明"为什么本阶段只能弱验证"，并指向第 5 节的对应盲区编号。

**T2 的前提**：任何 T2 阈值必须有来源。design.md 给出的是示意值（如 `test_coverage >= 0.85`、`max_daily_cost: 50`），不是可用的基线。阶段一需要先采集基线（见 `TASK-P1-072`），否则所有 T2 指标都是空转。

### 0.4 任务条目字段

````markdown
### [ ] TASK-P1-042 覆盖率模块基线机制
- **工作流**：Harness / Verification
- **设计要求**：R-055（§3.5B 脚注）、I-09
- **依赖**：TASK-P1-020, TASK-P1-023
- **交付物**：`core/task/coverage_baseline.py` — 每模块基线存储 + 阈值计算
- **验证（T1）**：pytest 断言 — 修改模块基线后阈值同步变化；尝试下调基线被拒绝并留审计记录
- **验证（T2）**：阈值 = max(模块基线, 全局地板)，下限 0.85；基线下调次数 = 0
- **自主权**：L2（自动执行）
- **预估**：1d
````

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| 工作流 | ✅ | 归属的子系统（见 §1.2） |
| 设计要求 | ✅ | 至少一个 `R-0xx`；没有 `R-` 的说明是本文新增的设计补强（会标注） |
| 依赖 | ✅ | 前置任务 ID；留空表示无依赖 |
| 交付物 | ✅ | 具体到文件 / 模块 / 接口；不写"实现 X 功能"这种无边界表述 |
| 验证 | ✅ | 至少一条 T1 或 T2 |
| 自主权 | ✅ | L1~L4，见 design.md §7；标明该任务在哪个级别下可自动执行 |
| 预估 | ✅ | 单人快速开发估算；阶段一全量合计 **31.75 人日**。design.md §9 给的"1~2 周"对应的是约 13~16 人日的 **Thin Slice** 子集（见 §2 前言与附录 B） |

### 0.5 勾选与推进规则

- 任务状态用 `[ ]` / `[x]`，由执行者手动勾选。
- **一条任务的 `[x]` 意味着它的全部验证字段都已通过**，而不只是代码写完了。验证没过的任务保持 `[ ]`。
- 任务完成后，把实际验证产出（测试报告、指标快照、评审纪要）登记到对应 `EvolutionCycle` 的产物库，而不是留在本地。

---

## 1. 任务清单总览

### 1.1 阶段与验收门

| 阶段 | 名称 | 出口门 | 出口条件 |
| --- | --- | --- | --- |
| **阶段一** | 最小自主演进闭环 | `MS-P1-M2` | 只给项目目标（不给具体任务），系统能自主发现问题 → 生成任务 → 开发 → 独立验证 → 产出下一轮计划 |
| 阶段二 | 自主研发能力增强 | `MS-P2-M1` | 不同执行器能完成同一任务契约，执行结果可比较、可评估 |
| 阶段三 | 持续运行和自演进 | `MS-P3-M1` | 连续运行多个演进周期，不依赖人工反复输入任务 |
| 阶段四 | 研发控制平面 | `MS-P4-M1` | 可长期管理多个软件项目的自主研发基础设施 |

阶段一内部三道门：

| 门 | 名称 | 通过条件 |
| --- | --- | --- |
| `MS-P1-M0` | 骨架可运行 | `docker compose up` 起全栈；迁移可跑；健康检查通过 |
| `MS-P1-M1` | 单轮闭环 | 手工注入一个 Candidate，系统能走完 决策→任务→执行→独立验证→记录 全链 |
| `MS-P1-M2` | 自主闭环 | 只给目标不给任务，系统自己发现并完成一轮；产出可验证的改善证据 |

### 1.2 九个工作流

| 前缀 | 工作流 | 对应 design.md | 阶段一任务数 |
| --- | --- | --- | --- |
| `INF` | 基础设施与工程骨架 | §8 技术栈与目录结构 | 7 |
| `CP` | Project Control Plane | §3.1 | 5 |
| `KS` | Project Knowledge & State | §3.6 | 6 |
| `EP` | Agent Execution Plane | §3.4 | 6 |
| `HN` | Engineering Harness | §3.5 | 9 |
| `DR` | Autonomous Director / Evolution Engine | §3.2 + §3.3 | 8 |
| `OR` | 编排与演进闭环 | §4 + §6 | 6 |
| `EV` | 观测与评估 | §10 | 4 |
| `MS` | 里程碑与验收 | §9 | 4 |
| | | **合计** | **55** |

### 1.3 依赖关系主干

```text
INF (骨架) ──┬─> CP (目标/权限/预算) ──┐
             ├─> KS (实体/状态机/事件) ─┼─> DR (Director 五阶段) ─> OR (循环 B)
             └─> EP (执行器/沙箱) ──────┼─> HN (验收/安全/审批) ─> OR (循环 A)
                                       └─> EV (观测/指标)
```

三条不可绕过的顺序约束：

1. **`KS` 的三表分离约束必须先于 `DR` 的任务生成** —— 否则 Director 会把 Candidate 直接变成 Task，整个设计失效（`TASK-P1-022`）。
2. **`HN` 的验收契约必须先于 `OR` 的执行启动** —— Acceptance Contract 是 READY→RUNNING 的准入 gate，不是事后报告（`TASK-P1-040` → `TASK-P1-062`）。
3. **`EP` 的沙箱隔离必须先于 `EP` 的真实执行器接入** —— 不能让 Coding Agent 先在宿主机跑起来再补隔离（`TASK-P1-032` → `TASK-P1-031`）。

---

## 2. 阶段一任务清单：最小自主演进闭环

> **阶段一目标**：只给项目目标（不给具体开发任务），系统能自主发现问题 → 生成任务 → 开发 → 独立验证 → 产出下一轮计划。
>
> **工期校正**：design.md §9 给阶段一的估算是"1~2 周，单人快速开发"。按本文拆解，全量阶段一 **31.75 人日**。二者不矛盾 —— design.md 的估算对应的是约 **13~16 人日的 Thin Slice** 子集（见附录 B）；从 Thin Slice 到 `MS-P1-M2`（自主闭环验收）另需约 **16~18 人日**。建议把阶段一排期按 5~6 周单人计，或把 Thin Slice 作为阶段一的实际交付边界。

### 2.1 基础设施与工程骨架（INF）

### [x] TASK-P1-001 仓库骨架与工具链

- **工作流**：INF
- **设计要求**：R-105, R-107（§8 目录结构）
- **依赖**：无
- **交付物**：`pyproject.toml`（Python 3.12）、`ruff`/`mypy --strict`/`pytest` 配置、`Makefile`（`make test` `make lint` `make typecheck` `make migrate`）、`pre-commit` 钩子、`tests/{unit,integration,agent,system}/` 目录骨架、CI workflow（推 PR 跑 lint+typecheck+unit）
- **验证（T1）**：`make lint && make typecheck && make test` 全绿；断言 `mypy` 以 `--strict` 运行且 `core/` 下无 `Any` 逃逸（`disallow_any_explicit = true`）
- **验证（T2）**：CI 在 PR 上从推送到结果 < 5 分钟
- **自主权**：L2（自动执行）
- **预估**：0.5d
- **验证结果**：T1 —— `make check` 全绿：ruff "All checks passed!"、mypy "Success: no issues found in 119 source files"、pytest "342 passed, 73 deselected"；`core/` 无 `Any` 逃逸由 `tests/unit/core/test_no_any_escape.py` 的 AST 门禁断言（不用 mypy 的报错位置判定 —— Pydantic v2 的 `BaseModel.__init__(**data: Any)` 继承会把 `disallow_any_explicit` 的违规归到每个子类定义行，改用按模块覆写 + AST 门禁）。T2 —— PR #1 的 CI run 42078392146 从推送（17:49:39Z）到四个 job 全部完成（17:51:10Z）耗时 **91 秒**（预算 300 秒）；静态检查 / 单元测试 / 集成测试 / 本地全栈四个 job 全绿。

### [x] TASK-P1-002 配置与密钥管理

- **工作流**：INF
- **设计要求**：R-060（§3.5D 短期令牌）、I-08
- **依赖**：TASK-P1-001
- **交付物**：`infrastructure/config/settings.py` —— Pydantic Settings；配置项分三类：**外部配置**（policy、验收规则、预算 —— 必须可外部修改，见 `TASK-P1-011`）、**运行时配置**（模型名、超时）、**密钥**（仅从环境/secret 注入，禁止进代码库）；`.env.example` 不含任何真实值
- **验证（T1）**：pytest 断言 —— ① 缺少必需密钥时进程启动即失败并给出明确报错；② 扫描代码库无硬编码 `api_key`/`token`/`password` 字面量（用 `detect-secrets` 或正则 + CI 门禁）；③ 所有密钥字段在 `Settings` 中标记为 `SecretStr`，`repr()` 不泄露
- **验证（T4）**：`docs/adr/0001-config-and-secret-handling.md` 记录三类配置的边界
- **自主权**：L2
- **预估**：0.25d
- **验证结果**：T1 ① `tests/unit/infrastructure/test_settings.py::TestMissingSecretFailsFast` —— production 缺 `ASES_S3_SECRET_KEY` / Langfuse 密钥时构造 `Settings` 即抛 `ValidationError`，错误信息点名具体环境变量；T1 ② `tests/unit/infrastructure/test_no_hardcoded_secrets.py` —— 门禁本体：形状规则（`sk-`/`AKIA`/`ghp_`/PEM/JWT 等已知格式，扫全部文件含文档与 `.env.example`）+ 命名规则（字面量赋给 `*_api_key`/`*_token`/`*_password` 等名字，扫生产代码）。该扫描器**自身被测试**（`TestScannerItselfWorks` 喂入确实含密钥的片段，必须报出来），并经变异验证：往 `core/policy/budget.py` 插入 `API_KEY = "sk-live-..."` 后门禁转红。T1 ③ `::TestSecretFieldsAreSecretStr` —— 六个密钥字段逐个断言类型为 `SecretStr` 且 `repr()`/`str()` 不含明文。连接串按同一标准处理（`database_url` 亦为 `SecretStr`，明文走 `Settings.database_dsn`）。T4 —— `docs/adr/0001-config-and-secret-handling.md`。

### [x] TASK-P1-003 PostgreSQL 接入与迁移工具链

- **工作流**：INF
- **设计要求**：R-065, R-068（§3.6 PostgreSQL 为项目状态库）
- **依赖**：TASK-P1-001, TASK-P1-002
- **交付物**：`infrastructure/database/` —— async SQLAlchemy 2.x engine/session、Alembic 配置、`alembic upgrade head` 可重复执行、`make migrate` 封装
- **验证（T1）**：pytest —— ① `alembic upgrade head` 后 `downgrade base` 再 `upgrade head` 成功（迁移可逆）；② 空库从零迁移耗时 < 30s
- **自主权**：L2
- **预估**：0.5d
- **验证结果**：T1 ① `tests/integration/test_migrations.py::test_upgrade_downgrade_upgrade_succeeds`、`::test_downgrade_leaves_no_business_tables`、`::test_upgrade_is_idempotent`；T1 ② `::test_empty_database_migration_is_under_30_seconds`。另附 `::connection_string_is_not_hardcoded`（断言 `alembic.ini` 的 `sqlalchemy.url` 为空，连接串只从 `ASES_DATABASE_URL` 注入）。

### [x] TASK-P1-004 对象存储与产物哈希校验

- **工作流**：INF
- **设计要求**：R-065, R-071（§3.6 产物库 S3/MinIO；Artifact 带 hash）
- **依赖**：TASK-P1-001, TASK-P1-002
- **交付物**：`infrastructure/storage/object_store.py` —— S3/MinIO 客户端封装、`put`/`get`/`exists`、内容寻址写入（key = `sha256:<hex>`）；`hashlib` 流式计算，不整文件读入内存
- **验证（T1）**：pytest —— ① 上传后下载字节完全一致；② 篡改存储中的对象后 `get` 返回 hash 不匹配并抛错；③ 同一内容重复上传不产生第二个对象（幂等）
- **自主权**：L2
- **预估**：0.25d
- **验证结果**：T1 ① `tests/unit/storage/test_object_store.py::TestRoundTrip`（上传后下载字节完全一致，`LocalObjectStore` 与 `S3ObjectStore` 两个后端各来一遍）+ `tests/integration/test_object_store_s3.py::TestRoundTrip::test_bytes_round_trip` / `::test_file_round_trip` / `::test_download_to_file`（对真实 MinIO 走同样的往返）。T1 ② `tests/unit/storage/test_object_store.py::TestTamperDetection`（篡改后 `get` 抛 `ArtifactHashMismatchError`）+ `tests/integration/test_object_store_s3.py::TestTamperDetection::test_overwritten_object_is_rejected`（把 MinIO 里的对象覆写掉，读取必须报错）。T1 ③ `tests/unit/storage/test_object_store.py::TestUploadIdempotence` + `tests/integration/test_object_store_s3.py::TestIdempotence::test_only_one_object_is_created` / `::test_second_upload_is_reported_as_skipped`（同一内容重复上传只产生**一个**对象，第二次报"已存在"而不是再写一个 —— 幂等是内容寻址的**构造性**结果，不是额外加的检查）。内容寻址本身由 `tests/unit/storage/test_object_store.py::TestContentAddressing`（key = `sha256:<hex>`）与 `::TestKeyMapping`（`:` 转成目录层级、可逆、非法 key 拒绝）钉住。另有三条容易被忽略的语义单独钉住：`::TestTamperIsNotSilentlyRepaired` + `tests/integration/test_object_store_s3.py::TestTamperIsNotSilentlyRepaired::test_reupload_does_not_hide_the_tampering` —— **篡改必须保持可见**，`put` 发现已有同名对象时**不修复**它（否则一次重传就能把被篡改的证据"洗白"）；`::TestStoredObject::test_digest_from_key_round_trips` / `::test_digest_from_key_rejects_non_content_addressed_keys`（只能从合法的内容寻址 key 反解出摘要）。健康语义由 `tests/unit/storage/test_object_store_health.py`（9 条）补齐：`ping()` 只回答"后端可达吗"（`head_bucket` 返回 404 恰恰证明服务应答了 → 可达），`ensure_ready()` 才回答"能写了吗"（建桶，且幂等）—— 两个词混用会让"服务挂了就绪"或"服务活着但桶不存在"这两种故障被误判。合计单元 48 条 + 集成 9 条全绿。

### [x] TASK-P1-005 结构化日志与追踪骨架

- **工作流**：INF
- **设计要求**：R-004, R-106（§8 OTel + Langfuse 必选）
- **依赖**：TASK-P1-001, TASK-P1-002
- **交付物**：`infrastructure/observability/` —— JSON 结构化日志（每条带 `task_id`/`cycle_id`/`project_id`）、OTel tracer/exporter 配置、Langfuse 客户端封装（失败时降级为 no-op，不阻塞主流程）
- **验证（T1）**：pytest —— ① 任意一条日志都能按 `task_id` 检索；② Langfuse 不可达时主流程不中断且日志降级标记可见
- **验证（T4）**：日志字段字典文档化（`docs/observability/log-schema.md`）
- **自主权**：L2
- **预估**：0.25d
- **验证结果**：T1 ① `tests/unit/infrastructure/test_logging.py::TestTaskIdRetrieval` —— 绑定上下文后连续两条日志都能按 `task_id` 检索；保留键固定为 `task_id`/`cycle_id`/`project_id`（`logging.RESERVED_CONTEXT_KEYS`），未设的键不绑定（避免 `"task_id": null` 与"没写"混淆）。T1 ② `tests/unit/infrastructure/test_langfuse.py::TestUnreachableLangfuseDoesNotBreakTheCaller` —— 传输层失败时 `record_trace` 不抛异常、客户端转入 `DEGRADED`、日志里留下 `degraded=true` / `langfuse_state=degraded` 标记；持续故障只告警一次（不刷屏），一次成功即恢复并记 `langfuse_recovered`。OTel 侧同构：`tests/unit/infrastructure/test_tracing.py` 断言 exporter 构造失败时降级并留标记。T4 —— `docs/observability/log-schema.md`，由 `tests/unit/infrastructure/test_log_schema.py` 钉在实现上（保留键、脱敏键名单、标准字段、降级标记、检索示例所用字段全部双向比对）。

### [x] TASK-P1-006 沙箱镜像

- **工作流**：INF
- **设计要求**：R-046, R-047（§3.5A Docker 为 POC 首选）
- **依赖**：TASK-P1-001
- **交付物**：`docker/sandbox/Dockerfile` —— 通用 Linux 沙箱基础镜像：非 root 用户、无 SSH 密钥挂载、出站网络默认关闭（`--network none` 可用）、预装 `git`/`python3`/`node`；以及目标项目测试镜像模板 `docker/target/Dockerfile.tmpl`
- **验证（T1）**：pytest 启动容器断言 —— ① 容器内 `id -u != 0`；② 容器内不存在 `~/.ssh`、`~/.aws`、`.docker/config.json`；③ 默认网络下容器无法访问外网
- **自主权**：L2
- **预估**：0.5d
- **验证结果**：T1 ① `tests/integration/test_sandbox_image.py::TestSandboxImage::test_container_runs_as_non_root`（容器内 `id -u != 0`）。T1 ② `::test_no_ssh_directory` / `::test_no_aws_directory` / `::test_no_docker_config`（`~/.ssh`、`~/.aws`、`.docker/config.json` 三者均不存在 —— 沙箱拿不到宿主机的 SSH 凭据、云平台管理员密钥，这是 design.md §3.5A 的硬约束）。T1 ③ `::TestNetworkIsolation::test_configured_default_network_is_none`（默认值本身就是 `none`，断的是**配置与行为一致**，不是"我们记得加 `--network none`"）+ `::test_container_cannot_reach_the_internet`（`--network none` 下连 `1.1.1.1:443` 必须失败；探测目标用公共 DNS 而非内网地址 —— 换成内网地址的话，"连不上"可能只是地址不对，断言就失去意义了）。目标项目模板另有 `::TestTargetImageTemplate::test_template_builds_and_runs_as_non_root` / `::test_template_has_no_credentials` / `::test_template_runs_the_test_command`（模板能构建、同样非 root 且无凭据、能跑测试命令）。沙箱镜像另有 `::TestSandboxImage::test_toolchain_is_installed`（`git`/`python3`/`node` 齐备）。10 条全绿。附带说明：`docker/sandbox/Dockerfile` 的基础镜像做成了 `ARG BASE_IMAGE`（默认 `python:3.12-slim-bookworm`），公共仓库不可达时可指向内部镜像仓库。

### [x] TASK-P1-007 本地一键启动

- **工作流**：INF
- **设计要求**：R-007, R-065
- **依赖**：TASK-P1-003, TASK-P1-004, TASK-P1-005, TASK-P1-006
- **交付物**：`docker-compose.yml` —— PG + MinIO + Langfuse + ASES API + worker；`make up` / `make down` / `make logs`；健康检查端点
- **验证（T1）**：`make up` 后 `/healthz` 返回 200 且 DB/MinIO 均连通；`make down && make up` 后数据仍在（卷持久化）
- **验证（T2）**：从零到全栈就绪 < 90s
- **自主权**：L2
- **预估**：0.25d
- **验证结果**：T1 ① `tests/integration/test_local_stack.py::test_healthz_reports_dependencies_connected` —— `/healthz` 返回 200，且 `checks.database.ok` 与 `checks.object_store.ok` **逐项**为真。只断言状态码是不够的：永远返回 200 的健康检查比没有健康检查更危险 —— 它会在依赖挂了的时候给出放行信号。另有 `::test_healthz_does_not_leak_credentials`（响应里不出现口令/连接串 —— 连接异常的消息里带着带口令的 DSN，健康端点对谁开放谁就能看到）与 `::test_readyz_is_also_available`（`/readyz` 探同一批依赖）。T1 ② `::test_data_survives_down_and_up` —— 插入一行带随机 id 的 project，`make down && make up` 之后**仍在**（卷持久化；用随机 id 是为了不靠上一轮遗留的数据假通过）。T2 `::test_stack_reaches_ready_within_budget` —— 断言从零到全栈就绪 < 90s，本机实测 **12.1s**（另一次冷启动 7.3s）。另有 `::test_worker_is_running`（worker 容器在跑；心跳的新鲜度由 compose 的 healthcheck 负责）。6 条全绿。

  **与任务书字面表述的两处偏离（有意为之）**：
  1. **Langfuse 放在 `observability` profile 里，不在 `make up` 的默认栈中。** 交付物字段写的是"PG + MinIO + Langfuse + ASES API + worker"，但 Langfuse v2 自带 ClickHouse + Redis，首次启动要初始化 ClickHouse 的数据卷，这一段本身就要几十秒 —— 把它算进 90s 预算，预算就变成了"ClickHouse 有多快"而不是"ASES 有多重"。而且 Langfuse 是**观测**设施不是运行依赖（它挂了主流程照跑，见 TASK-P1-005 的降级实现），让观测设施拖累主流程的启动预算是方向反了。因此拆成两个入口：`make up`（核心栈：PG + MinIO + API + worker）与 `make up-observability`（核心栈 + Langfuse）。langfuse-web / langfuse-worker / langfuse-clickhouse / langfuse-redis 四个服务**完整存在**于 compose 中，只是带 `profiles: ["observability"]` —— 不是删掉了，是不默认启动。
  2. **T2 量的是"编排耗时"，不含镜像构建。** "从零"= 容器全停（数据卷保留）到所有服务 healthy；镜像构建单独做（夹具里先构建再计时）。理由同上：拉镜像是网络操作，测的是网络有多快而不是 ASES 有多重。这条口径同时写在 `docker-compose.yml` 头部注释、`tests/integration/test_local_stack.py` 模块文档与夹具里，三处一致。

  **其他实现取舍**：
  - `migrate` / `api` / `worker` **共用同一个镜像** `ases-app:latest`，`build:` 只挂在 `migrate` 上。三个服务都带 `build` 时 compose 会并行构建同一个 tag 互相踩（报 "image already exists"）；而三个镜像分叉时，"只重建了其中一个"的失败方式极难发现 —— worker 跑着旧代码崩溃重启，compose 还会报"已就绪"。api/worker 都 `depends_on migrate: service_completed_successfully`，所以镜像一定先于它们就位。
  - `pull_policy: never` —— `ases-app` 是**本地**镜像，仓库里根本没有这个 tag，让 compose 去拉只会得到一句 "manifest unknown"。
  - worker 的健康检查看**心跳文件的新鲜度**（`/tmp/ases-worker/heartbeat`，mtime < 90s），不看进程是否在跑 —— 进程活着不等于还在干活，卡死循环的 worker 也是 running。阈值与 `apps/worker/main.py` 的 `HEARTBEAT_STALE_SECONDS` 保持一致。
  - MinIO 的**宿主机**端口用 9100/9101（容器内仍是 9000/9001）：9000/9001 在很多机器上已经被别的 S3 兼容服务占着。集成测试从同一个 `MINIO_PORT` 变量读，两边不会漂移。
  - API 健康检查用 Python 自带的 `urllib` 而不是 `curl` —— slim 镜像里没有 curl，为一个健康检查装一个包不值得。
  - 迁移是一次性任务，跑完就退出；api/worker 用 `service_completed_successfully` 等它 —— 不是等它"启动"，是等它"做完"，这样"表还没建好就接请求"在编排层面就不可能发生。

---

### 2.2 Project Control Plane（CP）

### [x] TASK-P1-010 项目目标结构化六要素模型

- **工作流**：CP
- **设计要求**：R-008, R-009, R-010, R-011, R-014（§3.1 目标不能只是自然语言；实际须支持不同指标类型/单位/评测方法/币种）
- **依赖**：TASK-P1-003
- **交付物**：`core/project/models.py` —— `Objective` / `KeyResult(id, metric, target, unit, eval_method)` / `Constraint`（带 `machine_checkable` 谓词表达式） / `NonGoal` / `AutonomyPolicy`（按操作维度） / `StopCondition`（带机器可判定谓词）；YAML 载入 + Pydantic 校验
- **验证（T1）**：pytest —— ① 非法目标（KR 缺 metric / target 非可比较表达式 / 缺任一六要素）载入报错并指出缺哪一项；② 同一 metric 可配置不同 `unit` 与 `eval_method` 且都被接受（不硬编码 `e2e_success_rate`）
- **验证（T4）**：`docs/examples/project.yaml` 给出完整示例
- **自主权**：L2
- **预估**：0.5d
- **验证结果**：T1 ① `tests/unit/project/test_models.py::test_missing_element_is_reported`（参数化六要素，断言报错点名缺哪一项）、`::test_missing_metric_is_rejected`、`::test_non_comparable_target_is_rejected`、`::test_metric_type_must_match_target_type`；T1 ② `::test_same_metric_different_unit_and_eval_method`、`::test_arbitrary_metric_names_are_accepted`。T4 —— `docs/examples/project.yaml`，由 `::test_example_project_file_is_valid` 断言示例文件真能被模型载入（示例不是装饰）。

### [x] TASK-P1-011 Policy Manager 与四级自主权判定器

- **工作流**：CP
- **设计要求**：R-012, R-015, R-019, R-100, R-101, R-102, R-104（§3.1 Autonomy Policy；§7 四级自主权与初始基线；防自改验收标准）
- **依赖**：TASK-P1-010, TASK-P1-002
- **交付物**：`core/policy/` —— `AutonomyLevel` 枚举（L1 分析 / L2 开发 / L3 集成 / L4 运营）、`Operation` 枚举（code_changes / test_execution / branch_creation / merge_to_main / production_deployment / modify_governance）、`PolicyManager.check(operation, task_ctx) -> Decision(allow/deny/require_approval)`；**policy 从外部配置读取，不硬编码在代码中**
- **验证（T1）**：pytest 参数化 —— 初始基线断言：`code_changes`/`test_execution`/`branch_creation`/项目分析/计划生成 → allow；`merge_to_main` → require_approval；`production_deployment` → deny（仅建议，L1）；`modify_governance` → deny（仅提案，L1）
- **验证（T1）**：修改外部 policy 文件为更高权限后，判定器读到的等级随之改变（断言非硬编码）
- **验证（T3）**：架构评审确认"policy 修改需走审批"路径存在且不可由 Director 绕过
- **自主权**：L2
- **预估**：0.5d
- **验证结果**：T1 ① —— `tests/unit/policy/test_autonomy.py::TestInitialBaseline`：`test_autonomous_operations_are_allowed`（参数化 5 个操作，断言 ALLOW 且授予 L4）、`test_merge_to_main_requires_approval`（REQUIRE_APPROVAL + 授予 L2）、`test_dangerous_operations_are_denied`（参数化 `production_deployment` / `modify_governance`，断言 DENY + 授予 L1）、`test_modify_governance_is_advisory_only`（理由含"建议"）；T1 ② —— `::TestPolicyIsExternal`：`test_changing_external_file_changes_decision`（改外部 YAML 后 `merge_to_main` 由 REQUIRE_APPROVAL 变 ALLOW）、`test_baseline_and_external_policy_differ`（内置基线与外部配置对同一操作给出不同结论）、`test_missing_file_falls_back_to_conservative_baseline`（文件缺失回退基线且回退结果仍保守）、`test_incomplete_policy_is_rejected`（缺一条操作规则即拒绝加载 —— 缺规则等于把决定权交给隐式默认值）。T3 —— 实现期代码评审（自审，尚未经第二人复核）：审查结论是本任务原先的 T3 表述"policy 修改需走审批路径存在"**在实现前是假的** —— `AutonomyPolicy.rules` 声明为 `Mapping` 但运行时仍是可写的 `dict`，进程内改一条规则就能放宽权限且不留痕迹。已补上不可改写机制（`core/immutable.py::FrozenDict`，覆盖 `__setitem__` / `__delitem__` / `clear` / `pop` / `popitem` / `setdefault` / `update` 全部 mutator —— `dict.update` 是 C 实现的，不会走 `__setitem__`，只堵一个是不够的），并由 `::TestPolicyCannotBeMutatedInProcess` 断言：`test_rules_mapping_rejects_item_assignment`、参数化 `test_rules_mapping_rejects_every_mutator`（5 个 mutator）、`test_mutating_a_policy_does_not_affect_a_manager`（改写被拒后判定结果原封不动）、`test_policy_manager_exposes_no_mutating_api`（判定器上没有 `set_`/`update`/`grant`/`revoke`/`override`/`patch`/`mutate`/`relax` 开头的公开方法）、`test_policy_still_serializes`（只读化不破坏持久化）。另按 R-063 补 `TaskContext`（`agent_id`/`task_id`/`project_id`/`workspace_id`/`expiry`/`allowed_operations`）与 `check(operation, task_ctx)`：判定顺序为 过期 → 任务范围收窄 → 策略，任务上下文**只能收紧不能放宽**（`test_task_scope_cannot_widen_a_stricter_policy`），同一 Agent 在不同 task 下各有范围（`test_same_agent_in_another_task_has_its_own_scope`）。

### [x] TASK-P1-012 Budget Manager

- **工作流**：CP
- **设计要求**：R-013, R-018（§3.1 Budget；管 Token/模型调用/计算资源/时间预算；币种可配置）
- **依赖**：TASK-P1-010, TASK-P1-035
- **交付物**：`core/policy/budget.py` —— `Budget`（`max_daily_cost` + `currency` 可配置、`max_parallel_agents`、`max_model_calls`、`max_wall_clock_minutes`）；`BudgetManager.consume()` 原子扣减；返回 `BudgetExhausted` 而非静默截断
- **验证（T1）**：pytest —— ① 扣减超过 `max_daily_cost` 时抛 `BudgetExhausted`；② 并发 10 次扣减总和不超限（无竞态，用真实并发写库验证）；③ 不同 `currency` 配置都能工作（不硬编码 USD）
- **自主权**：L2
- **预估**：0.5d
- **验证结果**：T1 ① —— `tests/unit/policy/test_budget.py::TestConsume`：`test_exceeding_daily_cost_raises`（抛 `BudgetExhausted` 且带上 metric/limit/requested）、`test_rejected_consume_is_not_recorded`（被拒的调用不计入累计额）、`test_rejected_attempt_is_still_auditable`（被拒尝试留痕，`status = blocked`）、`test_exact_limit_is_allowed`（刚好用满上限合法）、`test_negative_amount_is_rejected`；T1 ② —— **真实并发写库**：`tests/integration/test_budget_concurrency.py::TestConcurrentConsume::test_ten_concurrent_consumes_never_exceed_the_limit`，10 个协程经 `asyncio.gather` 同时发起、各自持有独立数据库连接，断言两条不变量："总额 ≤ 上限"与"总额 = 成功次数 × 单次金额"，且成功次数恰为 6（100 / 15 向下取整）。并发正确性靠 PostgreSQL 行级锁（`SELECT … FOR UPDATE`，见 `infrastructure/database/budget.py`）—— 已做反向验证：临时去掉 `.with_for_update()` 后该用例与另外两个并发用例**立即失败**（出现丢失更新，50 记成 20），证明断言有牙而非恒真。数据库层另有两道独立拒绝：累计额只增不减（`ases_budget_account_guard` 触发器，UPDATE 把 `spent` 改小与 DELETE 均被拒）与流水 append-only（UPDATE / DELETE / TRUNCATE 被拒），由 `::TestDatabaseGuards` 五个用例覆盖；T1 ③ —— `::TestCurrencyIsNotHardcoded`：单元层 `test_consume_uses_the_configured_currency` + `Budget(currency="CNY")` 归一化；集成层 `test_non_usd_currency_works` / `test_currency_is_stored_on_the_entry`（CNY / JPY 落库为对应币种）、`test_mixed_currency_on_one_account_is_rejected`（同一账户混算两种币种直接报错 —— 那个数字不再代表任何东西）。另：`InMemoryBudgetLedger`（同进程 `asyncio.Lock`）与 `PostgresBudgetLedger`（跨进程行级锁）共用 `BudgetLedger` 端口，领域逻辑（上限/币种/周期）留在 `core/`；**移除了 `reset_daily()`** —— 日切靠周期键（`day:YYYY-MM-DD`）自动生效，不需要删除任何记录，账本因此保持 append-only（`test_daily_rolls_over_without_deletion` 断言日切后前一天的流水一条不少）。依赖项 TASK-P1-035（成本换算器）负责"Adapter / 账本 / OTel 三处同源"的换算统一，本任务交付它将要接入的账本端口与持久化实现。

### [x] TASK-P1-013 Project Registry

- **工作流**：CP
- **设计要求**：R-008, R-016（§3.1 管理多项目及其仓库、环境和技术栈）
- **依赖**：TASK-P1-003, TASK-P1-010
- **交付物**：`core/project/registry.py` —— `Project`（id / 目标 / policy / 仓库地址 / 技术栈 / 环境配置）；注册、查询、列举接口；阶段一只注册 1 个项目，但接口按多项目设计
- **验证（T1）**：pytest —— 注册 2 个项目后按 id 查询各自返回独立的目标与 policy，互不污染
- **自主权**：L2
- **预估**：0.25d
- **验证结果**：T1 —— `tests/unit/project/test_registry.py`：`::TestRegistration`（注册 / 按 id 查询 / 列举按 id 排序 / `__contains__` / `__len__` / 按仓库查）、`::TestIsolation`（两个项目的目标、policy、预算、**环境配置**互不污染 —— `test_each_project_keeps_its_own_environment` 断言 alpha 的环境是 `{"target": "staging"}` 而 beta 是 `{"target": "prod", "region": "cn-north-1"}`；`test_registered_project_is_immutable` 断言领域模型深度不可变；`test_environment_mapping_is_immutable` 断言环境映射在进程内不可改写）、`::TestPolicyChangeNeedsApproval`（`replace` 无 `approval_ref` 抛 `MissingApprovalError`；有审批则记录 `RegistryChange` 且保留**替换前**的定义；改一个项目不影响另一个；对未注册项目替换抛 `ProjectNotFoundError`）。补上环境配置的原因：design.md §3.1 说 Project Registry 管"仓库、环境和技术栈"，而 `core/project/models.py::Project.environment` 此前在落库时被静默丢弃 —— 已补 ORM 列与迁移 `0002_project_environment`，并在 `tests/integration/test_entity_crud.py` 的往返断言中覆盖；**领域模型 ↔ 持久化实体的转换层此前完全没有测试覆盖**（`grep -rn "from_domain\|to_domain" tests/` 无命中），而转换层正是"字段搬运"这类静默丢数据发生的地方 —— 已补 `tests/unit/entities/test_project_conversion.py::TestEnvironmentSurvivesTheConversion`（`from_domain` 搬、`to_domain` 搬、往返不丢、未配置环境时是空字典而非 `None`），并做反向验证：临时从 `from_domain` 删掉 `environment=` 后三个用例转红，从 `to_domain` 删掉后同样三个用例转红。


### [x] TASK-P1-014 Stop Conditions 求值与目标达成度计算

- **工作流**：CP
- **设计要求**：R-009, R-011, R-128（§3.1 Stop Conditions；§12 可验证的改善）
- **依赖**：TASK-P1-010, TASK-P1-071
- **交付物**：`core/project/stop_conditions.py` —— 谓词求值器（`metric >= 0.95`、`no_new_critical_defects`、`cost_per_kr <= X`）；`GoalAttainmentCalculator` 按 KR 逐项计算达成度
- **验证（T1）**：pytest —— ① KR 达成度计算正确（构造已知指标值，断言达成/未达成）；② 不可判定的 Stop Condition 在启动时报错而非恒为 false（防止条件永不生效）
- **验证（T2）**：目标达成度 ∈ [0, 1]，且随指标单调变化
- **自主权**：L2
- **预估**：0.5d
- **验证结果**：T1 ① —— `tests/unit/project/test_stop_conditions.py::TestGoalAttainment`：`test_satisfied_key_result_has_attainment_one`（达标即 1.0）、`test_unsatisfied_key_result_has_partial_attainment`（未达标给**接近程度** 0.5/0.95，而非一律记 0 —— 否则"差 1%"和"差 100%"看起来一样）、`test_overall_is_the_mean_of_key_results`、`test_lower_is_better_direction`（越小越好的指标：目标/实际 = 0.5）、`test_boolean_key_result`、`test_unmeasured_key_result_counts_as_zero_and_lowers_coverage`（没测不给通行券，但 `coverage` 与 `unmeasured` 把"测得不全"显式摆出来）；T1 ② —— `::TestValidationAtStartup`：`test_unregistered_metric_is_rejected`（引用没人会提供的指标 → **启动时**报错，不是运行期恒 false）、`test_unparseable_expression_is_rejected`、`test_custom_expression_needing_threshold_without_one_is_rejected`、`test_missing_threshold_on_builtin_is_rejected`（纵深防御，用 `model_copy(update=...)` 绕过模型校验造出非法状态）、`test_evaluator_validates_on_construction`（校验发生在构造时 —— 那就是"启动"）；另 `::TestPredicateEvaluation::test_missing_fact_raises_instead_of_returning_false` 与 `::TestStopEvaluation::test_missing_actuals_make_the_condition_undecidable_not_false` —— **不可判定 ≠ 未触发**：缺事实与缺实测值分别落在 `StopEvaluation.undecidable`，与 `triggered` 分开记。T2 —— `::TestAttainmentIsMonotonic`：`test_attainment_stays_within_zero_and_one`（7 个取值遍历，断言 ∈ [0,1]）、`test_attainment_is_monotonic_in_a_higher_is_better_metric` / `..._lower_is_better_metric`（`itertools.pairwise` 逐对断言单调不减）。**不用 eval()**：表达式来自项目配置是外部输入，只支持三种明确形态（布尔事实 / 数值比较 / 阈值比较 `X`），解析不了就在启动时报错 —— `::TestPredicateParsing::test_code_injection_attempt_is_rejected` 用 `__import__("os").system(...)` 断言它被拒。**反向验证**（证明断言有牙而非恒真）：对生产代码做 8 处定点变异，每次都让对应用例转红 —— ① 未达标一律记 0（5 个用例红）、② 去掉上下夹取（`test_attainment_stays_within_zero_and_one` 红）、③ 启动校验不查未登记指标（2 个用例红）、④ 启动校验不查缺失 threshold（1 个用例红）、⑤ 缺事实返回 false 而非报错（1 个用例红）、⑥ 无法解析的表达式当成事实放行（5 个用例红）、⑦ 缺实测值记为"未达成"而非"不可判定"（1 个用例红）、⑧ `delta` 符号反转（1 个用例红）；8 处变异全部被抓住，文件还原后与原文逐字节一致。

---

### 2.3 Project Knowledge & State（KS）

### [x] TASK-P1-020 十二个核心实体的 schema 与迁移

- **工作流**：KS
- **设计要求**：R-065, R-066, R-068, R-069（§3.6 核心数据模型；四类存储职责分离）
- **依赖**：TASK-P1-003
- **交付物**：`core/` 下各模型 + Alembic 迁移 —— `Project` / `ProjectSnapshot` / `EvolutionCandidate` / `EvolutionDecision` / `Task` / `TaskDependency` / `AgentSession` / `ExecutionEvent` / `VerificationRun` / `Artifact` / `ArchitectureDecision` / `EvolutionCycle`，字段与 design.md 表一致
- **验证（T1）**：pytest —— 对每个实体做一轮 CRUD 往返；断言关键字段存在（`Candidate.evidence`/`value`/`risk`、`Decision.decision`/`rationale`、`Artifact.uri`/`hash`/`type`、`Snapshot.version`/`commit`/`metrics`、`TaskDependency.predecessor`/`successor`）
- **验证（T4）**：ER 关系图写入 `docs/architecture/data-model.md`
- **自主权**：L2
- **预估**：1d
- **验证结果**：T1 —— `tests/integration/test_entity_crud.py` 覆盖十二个实体各一轮 CRUD 往返与全部点名关键字段；`tests/unit/database/test_schema.py` 在元数据层断言表集合、关键列、外键指向与约束具名。T4 —— `docs/architecture/data-model.md`（含 ER 图），并由 `tests/unit/database/test_data_model_doc.py` 把文档钉在 ORM 元数据上：十二张表、24 条外键、design.md 点名字段逐条比对，正向与反向都断言（文档多出的"幽灵表"同样会失败）。该测试经变异验证（删外键行 / 插幽灵表 / 删字段明细行，各自对应断言转红）。另补 `tests/unit/entities/test_project_conversion.py` —— **领域模型 ↔ 持久化实体的转换层此前无任何测试覆盖**（`grep -rn "from_domain\|to_domain" tests/` 无命中），而这一层正是静默丢字段发生的地方：环境配置 / tech_stack / 预算 / 六要素逐项断言往返不丢；`policy` 列与 `goal.autonomy` 必须一致（治理策略要以可读形式存在、可被审计 —— design.md §7 / R-103）；**往返两次必须幂等** —— 否则"读出来再写回去"这个看似无害的动作就是在篡改治理策略；库里被改坏的残缺目标必须在 `to_domain` 当场抛 `GoalValidationError` 并点名缺哪一项，而不是带着残缺继续跑。

### [x] TASK-P1-021 Task 状态机（14 态）

- **工作流**：KS
- **设计要求**：R-092, R-093, R-094（§6 状态机；同一任务重复提交必须幂等）
- **依赖**：TASK-P1-020
- **交付物**：`core/task/state_machine.py` —— 14 个状态（DISCOVERED / EVALUATING / DEFERRED / APPROVED / PLANNED / READY / RUNNING / VERIFYING / PASSED / REPAIRING / BLOCKED / INTEGRATING / MERGED / COMPLETED）+ 合法迁移表 + `Re-plan`（→PLANNED）/ `Re-evaluate`（→EVALUATING）动作；非法迁移抛错
- **验证（T1）**：pytest —— ① 遍历所有状态对，非法迁移全部抛错；② 合法路径 `DISCOVERED→…→COMPLETED` 可走通；③ 重复执行同一迁移**幂等**（第二次调用不报错、不重复执行副作用）
- **自主权**：L2
- **预估**：0.75d
- **验证结果**：T1 ① `tests/unit/task/test_state_machine.py::test_every_illegal_state_pair_raises`（穷举 14×14 状态对）、`::test_every_illegal_state_action_pair_raises`；T1 ② `::test_full_path_is_walkable`、`::test_every_state_is_reachable_from_discovered`；T1 ③ `::test_repeated_action_is_a_noop`、`::test_repeated_action_keeps_previous_state`、`::test_running_task_receiving_duplicate_start_stays_running`。另含 `::test_cannot_start_without_acceptance_contract`（R-051 准入在状态机层同样成立）与 `::test_state_names_match_design_doc`（状态名与 design.md 逐字比对）。

### [x] TASK-P1-022 Candidate / Decision / Task 三表分离的强制约束

- **工作流**：KS
- **设计要求**：R-029, R-030, R-070（§3.3 §3.6 —— 区分"发现问题""作出决策""执行任务"，避免 Agent 把发现的问题直接转化成代码修改）
- **依赖**：TASK-P1-020, TASK-P1-021
- **交付物**：DB 层约束 —— `Task.decision_id NOT NULL` + 外键指向 `EvolutionDecision`；`EvolutionDecision.candidate_id NOT NULL` + 外键；**Task 无旁路写入路径**（仓储层不提供不经 Decision 直接插入 Task 的方法）
- **验证（T1）**：pytest —— ① 直接 `INSERT INTO task` 省略 `decision_id` 被数据库拒绝（不是被应用层校验拦下 —— 用原生 SQL 绕过应用层验证）；② 插入 Decision 时引用不存在的 candidate_id 被拒绝；③ 静态检查：代码库中不存在不经 `EvolutionDecision` 构造 Task 的调用点
- **验证（T3）**：代码评审确认"新增 Task 的唯一入口是 Decision 落地路径"
- **自主权**：L2
- **预估**：0.5d
- **验证结果**：T1 ① `tests/integration/test_three_table_separation.py::test_raw_insert_without_decision_id_is_rejected`（原生 SQL，错误来自 DB 而非应用层）、`::test_raw_insert_with_unknown_decision_id_is_rejected`；T1 ② `::test_decision_with_unknown_candidate_is_rejected`、`::test_decision_without_candidate_id_is_rejected`；T1 ③ `tests/unit/database/test_bypass_paths.py` —— AST 静态扫描全库，断言 `Task(...)` 只在 `infrastructure/database/repositories.py` 出现、`TaskRepository` 不暴露通用 insert 方法、仓储层之外无 `session.add(task)`。另覆盖 defer/reject 语义：`::test_defer_decision_cannot_produce_a_task`、`::test_reject_decision_cannot_produce_a_task`、`::test_task_table_is_empty_when_only_defer_happened`。T3 —— 实现期代码评审（自审，尚未经第二人复核）确认唯一入口为 `TaskRepository.create_from_decision()`。

### [ ] TASK-P1-023 ProjectSnapshot 可复现性

- **工作流**：KS
- **设计要求**：R-068, R-069, I-20（§3.6 Snapshot = version + commit + metrics）
- **依赖**：TASK-P1-020, TASK-P1-072
- **交付物**：`core/project/snapshot.py` —— 快照构建器：采集时点的 commit SHA、代码统计、测试结果、覆盖率；**指标采集结果落盘**（不实时查），使"当时的快照"可被事后重建
- **验证（T1）**：pytest —— ① 对同一 commit 连续构建两次快照，元数据一致；② 快照记录可重现：仅凭快照记录 + 产物 URI 即可取回当时的指标数值
- **自主权**：L2
- **预估**：0.5d

### [x] TASK-P1-024 验收证据的 append-only 与不可丢失

- **工作流**：KS
- **设计要求**：R-071, R-099, I-05, I-21（§6 已完成任务的验收证据不能因下一轮执行而丢失；Artifact 带 hash）
- **依赖**：TASK-P1-004, TASK-P1-020
- **交付物**：`core/evidence/` —— `VerificationRun` 与 `Artifact` 只允许 INSERT；DB 层禁 UPDATE/DELETE（触发器或权限分离）；带 `sha256` 校验与保留期字段；产物存对象存储并以 hash 寻址
- **验证（T1）**：pytest —— ① 对已写入的 VerificationRun 执行 UPDATE / DELETE 被数据库拒绝；② 下一轮演进周期执行后，历史的 VerificationRun 与 Artifact 仍完整可读（跨周期不丢失）；③ 产物被篡改时 hash 校验失败并报错
- **自主权**：L2
- **预估**：0.5d
- **验证结果**：T1 ① `tests/integration/test_evidence_append_only.py::TestAppendOnly::test_update_verification_run_is_rejected` / `::test_delete_verification_run_is_rejected` / `::test_update_artifact_is_rejected` / `::test_delete_artifact_is_rejected` / `::test_truncate_verification_run_is_rejected` —— UPDATE / DELETE / TRUNCATE 在**数据库层**被触发器拒绝（`ases_append_only_guard()`，错误码 `restrict_violation`）。拦在 DB 层而不是应用层：应用层的自律可以被绕开，触发器不行。另有 `::test_verification_run_is_still_readable_after_rejected_update` —— 拒绝之后**原记录仍完整可读**，要证明的是"拒绝操作"没有顺带把行删掉。T1 ② `::TestCrossCycleDurability::test_history_survives_a_new_evolution_cycle` —— 下一轮演进周期执行后，历史的 VerificationRun 与 Artifact 仍完整可读（跨周期不丢失）。T1 ③ `::TestArtifactHashVerification::test_tampered_content_is_rejected`（内容被改 → hash 校验失败并抛 `ArtifactHashMismatchError`）+ `::test_hash_is_stable` + `::test_artifact_stored_hash_matches_content`（入库的 hash 与实际内容一致）。哈希寻址与**读时**校验由 TASK-P1-004 的交付物承担：`core/evidence/artifact_hash.py`（`compute_hash` / `compute_file_hash` / `verify_hash` / `is_valid_hash`）与 `infrastructure/storage/object_store.py` 的内容寻址 store；对象存储侧的篡改/幂等验证见 TASK-P1-004 的验证结果。保留期字段 `retention_until` 落在 `artifact` 表上（`core/entities/evidence.py`），语义是**到期不自动删除** —— append-only 与"保留期"不冲突的前提是保留期只影响"读不读"、不影响"删不删"；自动清理会毁掉"上一轮到底改了什么"的唯一凭据。10 条全绿。

### [ ] TASK-P1-025 PostgreSQL Outbox 事件表与 relay

- **工作流**：KS
- **设计要求**：R-079, I-06（§8 PostgreSQL Outbox；§4 事件驱动）
- **依赖**：TASK-P1-020
- **交付物**：`infrastructure/database/outbox.py` —— 事件表（`id`/`aggregate_type`/`aggregate_id`/`event_type`/`payload`/`created_at`/`published_at`）；**状态写入与事件发布同事务**；relay 轮询发布、失败重试、已发布标记
- **验证（T1）**：pytest —— ① 状态变更与事件行在同一事务：事务回滚后两者都不存在；② 事务提交后事件必被 relay 取出（不丢）；③ relay 重复发布时消费者侧按 `event_id` 去重（至少一次语义 + 幂等消费）
- **自主权**：L2
- **预估**：0.75d

---

### 2.4 Agent Execution Plane（EP）

### [x] TASK-P1-030 Adapter 抽象基类与统一执行结果契约

- **工作流**：EP
- **设计要求**：R-002, R-003, R-005, R-036, R-037, R-038, R-039, R-040, R-044（§3.4 七方法签名；统一结果 schema；self_assessment 只是自我报告；执行器解耦与 ACP/MCP 边界）
- **依赖**：TASK-P1-001
- **交付物**：`executors/base.py` —— `CodingAgentAdapter` 抽象基类，七个方法：`create_session(config)` / `execute(task)` / `get_status(session_id)` / `stream_events(session_id)` / `interrupt(session_id)` / `collect_result(session_id)` / `terminate(session_id)`；补充**能力声明**（`capabilities`）、**超时**、**取消确认**、**幂等键**、**错误分类**（可重试 / 不可重试 / 需人工）
- **验证（T1）**：pytest —— ① 抽象基类无法直接实例化；② 两个不同实现的 Adapter 通过同一套契约测试（conftest 参数化）全部通过 —— **契约测试是执行器可替换性的唯一客观证据**；③ 统一结果 schema 校验：`task_id`/`executor`/`status`/`artifacts{branch,commit,changed_files}`/`execution{duration,tool_calls,cost}`/`self_assessment{completed,limitations}` 字段齐全且类型正确
- **验证（T1）**：`self_assessment.completed = true` 但验证引擎判定失败时，**任务整体状态仍为失败** —— 断言 self_assessment 不参与验收判定
- **自主权**：L2
- **预估**：0.75d
- **验证结果**：新增两个模块 —— `executors/base.py`（`CodingAgentAdapter` 抽象基类 + 统一结果 schema）与 `core/task/outcome.py`（判定规则），121 个单元测试（`tests/unit/executors/test_adapter_contract.py` 51 个 / `test_result_schema.py` 31 个 / `test_records.py` 19 个 + `tests/unit/task/test_outcome.py` 20 个）。**判定规则住在 `core/`，结果 schema 住在 `executors/`** —— 这是刻意的分层：schema 是执行器的**产出物**（属于 `executors/`），而"任务算不算成功"是**领域规则**（属于 `core/`），方向必须是"执行器 → 领域"；反过来就等于让执行器给自己打分。`core/` 侧因此定义了一个只含 `status` 一个成员的 `StatusSource` 协议，判定函数 `resolve_outcome(result, report)` **只接受它** —— 于是"在判定里读 `self_assessment`"不是靠约定挡住，而是 mypy 直接报错（`StatusSource` 里没有这个成员）。这是**跨层**的测试：判定规则在 `core`、执行结果在 `executors`，要证明的正是这两者接上时自我报告没漏进判定。T1 ① —— `::TestTheAbstractBaseCannotBeInstantiated`：抽象基类无法实例化，且 `__abstractmethods__` **正好**是九个成员（七方法 + `name` + `capabilities`）；只要有一个是具体实现，它就退化成"可选的约定"，而约定是会被绕过的。T1 ② —— **两个结构不同的实现跑同一套断言**：`InlineAdapter`（同步跑完就返回，能力只有 `STRUCTURED_OUTPUT`，事件流明确拒绝、中断回 `REFUSED`）与 `SessionAdapter`（后台 `asyncio` 任务推进、四项能力齐全、中断要等后台任务**真的**停下才回 `CONFIRMED`）。两者不是同一个类的参数化实例 —— 那样只能证明"一个实现符合自己"；真正的证据是**两种不同的做法都能满足同一份契约**。契约测试挑行为不挑实现，所以同一个问题在两个实现上分支不同，断言写成"两条分支各自对"而不是"只测支持的那条"。`::TestTheIdempotencyKey::test_two_different_implementations_derive_the_same_key` 断言两个实现算出的幂等键相同 —— 键由基类派生，所以换了执行器，重放去重仍然认得同一件事。**能力声明必须兑现**：没声明的能力要**明确拒绝**（`UnsupportedCapabilityError`）而不是安静地返回空流 —— 安静地返回空流，调用方会以为"这次没有事件"，而真相是"这个执行器根本没有事件流"；且能力检查**在前**、会话查找在后，因为能力是**适配器**的属性，与参数无关（两个实现都得是这个顺序，否则契约不一致）。T1 ③ —— schema 用 `strict=True` / `frozen=True` / `extra="forbid"`：字段名不许漂移（多出来的字段说明执行器与契约已分叉，而分叉在这里不会被发现），类型不许将就（`changed_files="12"`、`cost=0.1` 当浮点数、`completed="yes"` 当布尔值、未知 `status`、不可解析的十进制、负数、空币种全部拒绝），**十进制字面量要能进**（跨 JSON 边界时金额与时长就是字符串）且浮点数按**十进制字面量**转换而非二进制精确值（`Decimal(0.1)` 是 0.1000000000000000055511151231257827，作者写的是 0.1 —— 断言指数为 `-1` 而不是 `-55`）。相对 §3.4 的示例**多了两个字段**：`execution.currency`（成本没有币种就没法和预算比，R-039 要求三处成本同源同币种）与 `idempotency_key`（没有它就无从核对"同一幂等键返回同一结果"，I-01）。T1 ④ —— `::TestTheExecutorCannotGradeItself`：Agent 说"我做完了"而验证引擎说"没过" → **失败**；Agent 说"我做得不完美"而验证引擎说"过了" → **完成**（两个方向都不行，只堵住"自我报告把失败抬成成功"是不够的）；把自我报告**翻个面**，结论必须一模一样；判定函数的入参**只有** `result` 与 `report` 两个 —— 自我报告连进门的机会都没有。**fail-closed**：执行器说完成了却**没有独立验证结论** → `VERIFICATION_ERROR`（"没验过"不等于"验过了"），且**验证引擎自己出错 ≠ 验证判定失败**（前者要人来看，后者可走有界修复）；**超时 ≠ 失败**（R-097 / I-03），判定结果保留原始验证结论 —— 超时的处置是"先探测会话能不能恢复"，而恢复与否要靠那份已经拿到的结论。**取消是三态而非布尔值**（`REQUESTED` / `CONFIRMED` / `REFUSED`）："请求发出去了"与"确认停下来了"是两件事，取消传播链（I-02）要靠后者才推进下一层。**错误分类是必填的**（`RETRYABLE` / `NON_RETRYABLE` / `NEEDS_HUMAN`）：没有分类，编排层只剩"一律重试"（撞上不可重试的错就一直撞）或"一律不重试"（网络抖动就报废一次执行）两种选择。**反向验证**（证明断言有牙而非恒真）：对生产代码做 **33 处**定点变异，每次都让对应用例转红 —— ① 把"已取消"移出终态、② 把"超时"移出终态、③ 取消"非终态不可判定"的守卫、④ 没有验证结论时改判为完成、⑤ 验证通过时改判为结论不可得、⑥ 验证未通过时改判为完成、⑦ 验证引擎出错时改判为失败、⑧ 丢掉验证结论的理由、⑨ 执行失败时改判为完成、⑩ 已取消时改判为完成、⑪ 超时改判为失败、⑫ 判定结果里丢掉原始验证结论（**这一条最初没被抓住** —— 补 `::TestTheExecutorStatusDecides::test_the_verdict_is_carried_through[3 个参数]` 后才变红，说明"只断言终态判定"是不够的，结论还得**带得住证据**）、⑬ 给 `StatusSource` 协议加上 `self_assessment`（捷径就此有落脚点）、⑭ 幂等键丢掉 `attempt`、⑮ 幂等键构造不守 `attempt` 守卫、⑯ 任务丢掉 `attempt` 守卫、⑰ 任务丢掉 `timeout` 守卫、⑱ `timeout` 守卫放宽成 `< 0`、⑲ schema 允许多余字段、⑳ schema 关掉严格模式、㉑ 结果模型不再冻结、㉒ `changed_files` 允许负数、㉓ `currency` 允许空串、㉔ 浮点数按二进制精确值转、㉕ 布尔值被当作金额接受、㉖ 整数不再被接受为十进制、㉗ `limitations` 列表不再转成元组、㉘ `status` 不再接受文本、㉙ `supports` 一律为真、㉚ `require_capability` 不再抛错、㉛ 幂等键不再跟着 `attempt` 走、㉜ 未知会话被归为可重试、㉝ 能力不被支持时被归为可重试；**33 处全部被抓住**，生产文件还原后与原文逐字节一致。

### [x] TASK-P1-031 CLI Adapter（Claude Code 优先）

- **工作流**：EP
- **设计要求**：R-041, R-042, R-043, R-122, R-123（§3.4 CLI 接入通用性最高；§11 优先研究 OpenHands Automation/SDK）
- **依赖**：TASK-P1-030, TASK-P1-032
- **交付物**：`executors/claude_code/` —— 通过 CLI 实现的 Adapter：进程管理、NDJSON 事件流解析、会话 ID 映射、退出码 → 统一状态映射；另留 `executors/codex/` 同契约的骨架（骨架只承诺"同契约"，**不承诺"协议正确"** —— 见**待裁决 15**；分支由谁切见**待裁决 14**）
- **验证（T1）**：pytest（打桩 CLI，不调用真实 Agent）—— ① 合法 NDJSON 事件流被解析为统一事件序列；② 非零退出码映射为正确状态；③ 畸形输出归类为"不可重试"错误；④ 打桩 CLI 跑通 `TASK-P1-030` 的全套契约测试
- **验证（T3）**：在真实 CLI 上跑一次冒烟：给一个改一行 README 的任务，确认返回 `artifacts.commit` 非空
- **自主权**：L2
- **预估**：1d
- **验证结果**：新增两个包与一个骨架 —— `executors/cli/`（共同骨架：`adapter.py` 进程与事件流 / `events.py` NDJSON 解析 / `exit_codes.py` 退出码对照表 / `artifacts.py` 工作空间事实 / `errors.py` 错误分类）、`executors/claude_code/`（协议表：`command.py` 命令行 / `events.py` 事件词汇表 / `adapter.py` 适配器）、`executors/codex/`（同契约骨架），**81 个新测试**（`test_cli_events.py` 14 / `test_cli_exit_codes.py` 19 / `test_claude_code_events.py` 17 / `test_cli_adapter.py` 25 / `test_layering.py` 6），契约套件从 **2 个适配器扩到 4 个**（`inline` / `session` / `claude-code` / `codex`，`test_adapter_contract.py` 97 项），仓库全量 **1200 通过 / 1 跳过**。**共同骨架 + 各写协议表，是 R-043 的直接后果**："不要强制所有执行器实现同一种底层协议，而是强制它们实现同一种系统级任务契约" —— 进程管理、事件排队、结果装配在两个适配器里**完全一样**，不一样的只有三件事：怎么拼命令行、一行输出怎么变成事件、结果事件怎么读出结论。于是共同部分放一处（`executors/cli/adapter.py`），两个适配器各写一张表；底层协议各写各的，系统级契约（七方法 + 统一结果）由 `CodingAgentAdapter` 统一。**Claude Code 的协议表是对真实 CLI 2.1.158 的实测结果，不是照文档抄的**，三条不试就会踩的事实写进了 `command.py` 的模块文档：① `--print` 配 `--output-format stream-json` **必须同时给 `--verbose`**，少了它 CLI 直接以退出码 2 退出 —— 它不是"多打印点东西"，是这条输出模式的**开关**；② `--session-id` 给非法值时 CLI **不报错**，它悄悄换成自己生成的那个；③ `--session-id` 会被**原样回显**在每条事件的 `session_id` 字段里。②③ 合起来就是**核对会话 ID**的理由：我们自己生成 UUID 交给 CLI，再核对它报回来的那个是不是同一个（`::TestTheSessionId::test_a_mismatched_session_id_is_rejected`）—— 不核对的话，别的事件会被记到这个任务头上，而那**看起来是成功的**，比直接失败坏得多。T1 ① —— `::TestTheEventStream`：`system/init` → `session.started`、assistant 的内容块**按块拆开**（文字一条、每个 `tool_use` 一条 —— 合成一条就丢了"调用了几次工具"这个事实，而工具调用次数要进成本指标）、`user` 的 `tool_result` → `tool.completed`、`result` → `result`、不认识的事件**原样记成 `unhandled`**（信息不丢，将来核对协议时用得上）。T1 ② —— `::TestTheExitCode`：那张对照表是**跨执行器可比**的前提，写下来了而不是各自揣摩一套。判错两种后果都很贵：把"不可重试"判成"可重试" → 自动重试一次授权被拒的任务，预算白烧；把"可重试"判成"不可重试" → 一次网络抖动报废掉一整次执行。**"没有证据就不重试"是默认**：CLI 说"我失败了"但没给 API 状态码时一律不可重试（权限被拒、预算超了、工具报错都不带状态码，重试一次还是同样的结果 —— 多一次人工介入比多烧一次预算便宜）。**超时单列成 `TIMED_OUT`**（R-097 / I-03）—— 并进 `FAILED` 会让编排层直接判死，那正是僵尸会话的来源。**进程正常退出但没有结果事件判失败**：拿"没出错"当"做成了"是两回事。**CLI 说"我成功了"但退出码非零也判失败** —— 退出阶段炸了的工作空间未必干净。T1 ③ —— `::TestBrokenOutput`：只认三种**结构性**的坏法（不是合法 JSON / 不是 JSON 对象 / 没有字符串类型的 `type`），其余一律不算坏 —— CLI 会加新事件类型，把不认识的当坏输出会让系统在 CLI 升级后直接瘫掉。坏输出**不可重试**：同一份输入重跑一次还是那份坏输出，重试不是修复，是再付一次钱。T1 ④ —— **打桩 CLI 是一个真的可执行文件**（`tests/unit/executors/stub_cli.py`）：真的被 `exec`、真的往 stdout 写 NDJSON、真的按配置的退出码退出、真的能被 SIGKILL —— 进程边界上的那些坑（stdout 没人读、stderr 写满缓冲区把进程卡死、SIGKILL 之后还要等它真的退出）假对象一个都测不出来，但它**不调真实 Agent**：不联网、不花钱、不碰工作空间里的任何文件。契约套件因此从两个实现扩到四个，`claude-code` 与 `codex` 用**同一份断言**跑过。**结果在收集之前不定案**：进程退出了不等于这次执行有结论，结论在 `collect_result` 时才根据退出码与结果事件算出来 —— 这条规矩让"取消请求在结论之前到达"有确定的含义（按取消收场，而不是看谁先到）。**取消必须确认**：`interrupt` 只有在**进程真的死了**之后才回 `CONFIRMED`（I-02 四层链，每层确认收到才推进）；`execute` 用 `asyncio.create_task` 起的 worker 不 `yield`，所以取消标记一定在 worker 首次运行前就位，进程**根本不会被起**（起了再杀是浪费，而且会给"这次执行到底跑了什么"留下一条含糊的记录）。**凭证只走环境变量**：长期密钥不进 argv（argv 会出现在 `ps` 的输出里）、不进提示词（提示词会被回传给模型）—— `::TestTheCredential` 直接断言打桩 CLI 记下的 `argv` 里没有那个令牌。**产出物只从事实里来**：执行器不产出代码，代码是 Agent 改出来的，所以"这次改了什么"只能从工作空间的 git 状态里读（`rev-parse` / `symbolic-ref` / `diff` / `ls-files`，只读探针，不建分支不切分支不提交）；读不出分支时退回**约定** `agent/<task_id>`（引用 `harness.workspace.naming.branch_for_task` 而不是复述它 —— 复述一遍就会漂移，而漂移的分支名会让两个执行器报出不同的产出位置），但 **commit 仍然是 `None`**：没有提交就是没有提交，不拿别的东西顶替。**分层规则钉死了**：执行器**不管理工作空间**（不 import `harness.workspace.manager` / `leases` / `gc` / `git`，只准引用那张纯约定表 `harness.workspace.naming`）—— 让执行器也能建工作空间，"一任务一目录一分支"这条不变量就有两个执行点，而两次建出来的分支可能不是同一条，那种不一致要到并行两个任务、产出串到一条分支上时才发现（`tests/unit/executors/test_layering.py`，用 AST 查而不是 grep）。**反向验证**（证明断言有牙而非恒真）：对生产代码做 **13 处**定点变异，每次都让对应用例转红 —— ① 超时不再单列状态、② 没有结果事件也算成功、③ 畸形输出被判成可重试、④ 不再核对 CLI 报回的会话 ID、⑤ 凭证不再进子进程环境、⑥ 工具调用不再拆成独立事件、⑦ 幂等键不再由 `task_id + attempt` 派生、⑧ 取消之后不是 `CANCELLED`、⑨ 缺 `type` 字段的事件不再算坏输出、⑩ 非对象输出不再算坏输出、⑪ 执行器开始依赖沙箱、⑫ 执行器开始管理工作空间、⑬ 执行器不再只依赖约定表；13 处全部被抓住，生产文件还原后与原文逐字节一致（SHA256 逐个核对）。**两处自己踩的坑也记在这里**：其一，变异验证最初把 pytest 的**退出码 5**（没收集到任何用例）当成"测试变红"—— 那是**根本没跑**，当成咬住了就是自欺；其二，Python 按 `(mtime, size)` 判断字节码还能不能用，而两处**长度相同**的变异又落在同一秒里，于是上一轮"改坏"的字节码替这一轮上场，测试看起来全绿 —— 改坏之后必须清掉 `__pycache__`，否则验的不是同一份代码。T3 冒烟 —— 在**真实** Claude Code CLI 2.1.158 上跑通：临时 git 仓库里给它一个"改一行 README 并提交"的任务，适配器把事件流映射成 `session.started → message.assistant → tool.started(Read) → tool.completed → tool.started(Edit) → tool.completed → tool.started(Bash) → tool.completed → message.assistant → result` 共 10 条统一事件，状态 `COMPLETED`，**提交从基线 `e01b830` 变成 `19ff67b`**，改动文件数 1，工具调用 3 次，成本 0.1999 USD。判据不止"commit 非空" —— 只判非空的话，一个只有初始提交的空仓库也能蒙混过关，所以同时断言**与基线不同**。**codex 骨架是诚实的**：本机没装 codex，它的协议表**刻意留空**（所有事件记成 `unhandled`、摘要为空），因此一次"成功"的 codex 运行会报 `FAILED`（"没有产出结果事件"）—— **没有证据就没有结论**，把"没结论"当成功，正是"进程正常退出但什么都没产出"这种假成功的来源。接入 codex 需要三步（实测协议 → 填事件表 → 填摘要表），写在 `executors/codex/events.py` 的模块文档里。

### [x] TASK-P1-032 沙箱执行器与 Git Worktree 生命周期

- **工作流**：EP
- **设计要求**：R-045, R-046, R-047, R-048, I-14, I-15（§3.5A 每任务独立工作空间；禁访问宿主 SSH/云密钥/生产库）
- **依赖**：TASK-P1-006
- **交付物**：`harness/workspace/` —— Worktree 创建（分支 `agent/<task_id>`）+ **租约与回收**（`WorkspaceLease`，带 TTL 与 GC）；`harness/sandbox/` —— 沙箱容器启动 + 挂载策略（只读挂载凭据目录，无长期密钥）。交付物分两个包而非一个，见**待裁决 13**
- **验证（T1）**：pytest —— ① 两个任务并行创建 worktree，目录互不干扰；② 沙箱内不存在宿主 `~/.ssh`、云 CLI 凭据、生产库连接串；③ 租约过期后 workspace 被 GC 回收；④ 崩溃遗留的 workspace 可被 GC 识别并清理
- **验证（T1）**：重试同一任务时产生独立分支或复用分支的行为符合约定（不产生分支冲突）
- **自主权**：L2
- **预估**：1d
- **验证结果**：新增两个包 —— `harness/workspace/`（`models.py` 记录与租约 / `naming.py` 命名约定 / `git.py` git 操作面协议与真实现 / `leases.py` 租约的磁盘存储 / `manager.py` 分配与回收 / `gc.py` 回收器）与 `harness/sandbox/`（`policy.py` 策略 / `launcher.py` 启动器），**211 个测试**（`tests/unit/harness/workspace/test_naming.py` 27 + `test_manager.py` 38 + `test_leases_and_gc.py` 24 + `tests/unit/harness/sandbox/test_policy.py` 73 + `tests/unit/harness/test_layering.py` 4 + `tests/integration/test_workspace_worktree.py` 24 + `tests/integration/test_sandbox_mounts.py` 21）。**分支冲突不是靠约定避免的，是结构上不可能** —— 一任务一目录一分支（`agent/<task_id>`）三条同时成立，加上 git 自己的硬约束"一条分支不能同时被两个 worktree 检出"，冲突就没有成立的余地。分支跟着**任务**走、不跟 attempt 走（I-15）：重试复用同一条分支，产物位置因此稳定（`ExecutionResult.artifacts.branch` 报的是哪一条，不用去猜"最后一次尝试是哪一条"）。T1 ① —— `::TestTwoTasksInParallel`（真 git）：两个 worktree 都在、git 认得两个（`worktree list --porcelain` 计数 = 主仓库 + 2）、各在自己分支、一个里写的文件另一个看不见、`--porcelain` 解析不错分支名；`::TestTheRetryDoesNotConflict`：重试复用同一条分支与同一个目录、不新增第二条分支、不新增第二个 worktree、**别的任务偷不走这条分支**（真 git 自己拒绝）、以及**释放之后再拿**仍复用原分支（"目录没了、分支还在"是常态 —— 释放刻意留着分支，因为那是产出）。T1 ② —— 分两层验：**策略层**（`test_policy.py`）环境变量**不是继承而是按白名单重造**（宿主 `AWS_*` / `GOOGLE_APPLICATION_CREDENTIALS` / `KUBECONFIG` / `*_PROXY` / `DATABASE_URL` 一个都进不来）、凭据目录**挂不进来**且只挡叶子不挡根（`~/.config` / `~` / `/` 一律拒绝 —— `~/.config` 里就有 gcloud 凭据）、凭据**走只读挂载不走环境变量**（环境变量会被日志与 Prompt 看到，那正是 design.md 说的"不向 Prompt 暴露长期密钥"）、连接串的**主机名**必须在允许清单里（允许清单才是主机制，"默认只放行本地与测试主机"）；**运行时**（`test_sandbox_mounts.py`，真容器）：容器里没有 `~/.ssh`、没有 `~/.aws` / `~/.docker` / `~/.kube` / `~/.gnupg`、`env` 里搜不到任何凭据名或代理变量、**宿主私钥的内容在整个文件系统里都搜不到**、凭据挂载**确实是只读的**（用 `/proc/self/mountinfo` 的挂载选项断言，而不是"写一下试试失败" —— 后者在 Linux CI 上会因为目录属主不对而失败，验的是别的东西）、网络默认 `none` 所以连不上 `1.1.1.1`。T1 ③ —— 租约到期后 GC 回收该工作空间、回收原因是 `LEASE_EXPIRED`、**分支活着**、git 的登记被摘掉，而**活着的租约不被回收**。T1 ④ —— 崩溃遗留的目录（没有租约记录）被判为 `ORPHANED` 并清掉，连"工作空间建好、租约记录被删"这种半路状态也认得出。**租约记两处**：内存里那份是活着的租约（进程活着时它说了算），磁盘上那份（`<workspace_root>/.ases/leases/<id>.json`）是给 GC 用的（进程重启后内存那份没了，磁盘那份还能告诉 GC 什么时候到期）。磁盘那份**刻意放在 worktree 之外** —— worktree 是 Agent 的工作区，往里塞未跟踪文件，Agent 可能顺手提交进去；而"有没有主人"必须是外面那份记录说了算，判断才不会因为工作区里多了个文件而变含糊。**先落租约再建 worktree**，建失败回滚租约 —— 顺序反过来，崩在两步中间会留下一个"目录在、租约不在"的工作空间，GC 认不出它有主人就当成崩溃遗留回收掉，而它其实刚被分配出去。**"读不出来"与"没有记录"是同一件事**：都没有主人；这份判定是安全的，因为回收只删**目录**与 git 的登记，**分支留着** —— 分支是任务的产出，删了就没了。**GC 只回收两样东西**：工作空间目录、git 的 worktree 登记。**fail-closed**：策略不过就抛 `SandboxPolicyError`，绝不"打个警告继续"；启动器在起容器**之前**调一次 `validate_sandbox_spec` —— 集中在一处就只需要守一处，散落在各调用点则"某条路径忘了检查"会变成只有出事那天才发现的洞。**重试与续跑由调用方显式区分**：重试 = `reset`（丢弃未提交、保留已提交 —— 那是上一次真正交付的东西），续跑什么也不做；管理器不替调用方猜，因为一个是"重来一次"、一个是"接着来"，后果完全不同。**两包互不 import**（`tests/unit/harness/test_layering.py`，用 AST 查而不是 grep —— grep 会漏掉动态 import，也会把注释里的字样算进去）：沙箱要挂载工作空间，但它拿到的是**路径**（`build_workspace_sandbox_spec(workspace_path=...)`）而不是 `Workspace` 对象，于是耦合是**数据**而不是**代码** —— 工作空间换成非 git 的实现，沙箱那侧一行不用改；反过来若让 `sandbox/` 直接 import `workspace/`，那正是"拆分"要避免的那种依赖长了出来。**反向验证**（证明断言有牙而非恒真）：对生产代码做 **28 处**定点变异，每次都让对应用例转红 —— ① 分支前缀不是 `agent/`、② 不再拒绝含 `..` 的 task_id（路径穿越）、③ task_id 长度上限与数据库列宽脱钩、④ workspace_id 不再等于 task_id、⑤ 建 worktree 失败时租约不回滚、⑥ reset 不再清理未跟踪文件、⑦ release 不删工作空间目录、⑧ 不再独占工作空间（别人也能拿）、⑨ 不再校验工作空间检出的分支、⑩ 租约记录写坏（读不出来）、⑪ 删租约记录时其实没删、⑫ 过期租约不再被回收（T1 ③）、⑬ 回收后不删租约记录、⑭ 回收时不删目录（T1 ④）、⑮ GC 把元数据目录也当工作空间、⑯ 租约永不过期、⑰ 建 worktree 时总是新建分支（重试会撞分支）、⑱ worktree 列表解析时丢掉了分支名、⑲ 环境变量整体继承宿主机、⑳ 凭据名字不再被拦、㉑ 连接串的主机名不再查允许清单、㉒ 凭据目录路径不再被拦、㉓ 凭据目录可以被挂载进沙箱、㉔ 凭据挂载可以不是只读、㉕ 代理变量进了白名单、㉖ 沙箱默认开着网络、㉗ 启动器不检查策略就起容器、㉘ 沙箱开始依赖工作空间（耦合从数据变成代码）；**其中 3 处最初没被抓住**，补断言后才变红 —— **分支前缀那条**的断言写成了 `f"{BRANCH_PREFIX}task-1"`，改的是常量而断言跟着常量一起变，等于自证（改成字面量 `"agent/task-1"`）；**"总是新建分支"那条**暴露的是"释放之后再拿"这条路径**根本没被测过** —— 释放留着分支、目录被删，再 acquire 时 git 客户端必须**复用**那条分支而不是新建（`worktree add -b` 会撞 "branch already exists"）；**"主机名不再查允许清单"那条**暴露的是六个生产 DSN 用例的主机名里**全都**含 `prod`，于是"生产环境关键词"那条先拦住了，允许清单那条根本没被走到 —— 补一条主机名不含 `prod` 但不在允许清单的用例（允许清单才是主机制，"生产"只是它的一个子集）；**28 处全部被抓住**，生产文件还原后与原文逐字节一致（SHA256 逐个核对）。

### [x] TASK-P1-033 取消传播链

- **工作流**：EP
- **设计要求**：R-096, I-02（§6 任务取消必须能传递到执行器和 Sandbox）
- **依赖**：TASK-P1-030, TASK-P1-032
- **交付物**：`harness/sandbox/cancellation.py` —— 四层链式传播：任务层 → Adapter（`interrupt`）→ 执行器进程 → 沙箱容器；每层确认收到后才向下一层推进
- **验证（T1）**：pytest —— ① 取消一个正在执行的沙箱任务，30s 内进程退出且容器停止；② 传播途中某层失败时，链路仍在有限时间内收敛（不挂死）；③ 取消后 `collect_result` 返回明确的 `cancelled` 状态而非 `completed`
- **自主权**：L2
- **预估**：0.5d
- **验证结果**：新增两个模块与两处生产代码改动 —— `core/task/cancellation.py`（取消链的**词汇表与端口**：`LayerName` / `LayerState` / `LayerOutcome` / `StopReport` / `CancellationTarget` / `CancellationReport`，以及 `Interrupter` / `ProcessStopper` / `ContainerStopper` 三个协议）、`harness/sandbox/cancellation.py`（四层链本体：`TaskLayer` → `AdapterLayer` → `ProcessLayer` → `SandboxLayer` 与 `build_cancellation_chain`），`executors/cli/adapter.py` 新增 `ensure_process_stopped`（进程层唯一的真实现）、`harness/sandbox/launcher.py` 新增三态的 `container_running`（`bool | None`），**46 个新测试**（`tests/unit/harness/sandbox/test_cancellation.py` 32 / `tests/unit/executors/test_cli_cancellation.py` 9 / `tests/integration/test_cancellation_chain.py` 5），仓库全量 **1099 通过 / 1 跳过**（单元）与 **142 通过**（集成）。**取消不是一个动作，是一条链**：一次执行留下的东西不止一样 —— 执行器的会话、它背后的进程、装它的容器。只停最里面那个（会话），容器还在跑；只停最外面那个（容器），执行器永远等不到确认，链只能报"取消失败"—— 而容器其实早就没了，**结论与事实相反**。四层各管一件事，从内到外依次收干净。**每层的后置条件是"这一类资源已经不在跑了"**，不是"收到请求了"，也不是"尝试过了" —— 只有这样"已取消"才是关于**事实**的结论，而不是关于**流程**的：流程走完了但东西还在跑，那不叫取消。**"确认"与"收到"不是一件事**，所以 `LayerState` 有四态而不是两态：`CONFIRMED`（后置条件成立）/ `REFUSED`（这一层明确停不下来）/ `FAILED`（试了但没成 —— 超时或报错）/ `UNCONFIRMED`（只回了"请求已收到"，没给出结论）；把后两种算成"确认"，就是拿"问过了"冒充"停住了"。**推进规则的两半缺一不可**：**先有结论再推进**（不许在上一层还悬着的时候就去动下一层 —— 容器先停，执行器就再也给不出确认，于是链报"取消失败"而容器早就没了）；**失败不挡路**（一层失败之后后面的层**仍然要试** —— "这一层没停下来"绝不等于"后面的层不用管"：容器还在跑，占着内存、占着端口、占着工作空间，而工作空间是**别的任务也要用**的，跳过它等于把一次取消变成一次泄漏）。所以失败记在**结论**里，不挡**动作**；链报"已取消"的条件是**每一层都确认**，少一层都不算（**fail-closed**：空的报告也不算取消 —— 没跑过链就不能说取消过）。**每层都有期限，所以链一定会收敛**：一层超时记 `FAILED` 然后继续往下走，整条链的时间上限是"层数 × 每层期限"（4 层 × 10 秒）；每层 10 秒是**刻意**小于任务书 T1 ① 那条 30 秒整链预算的 —— 四层各给 30 秒就等于整条链 120 秒，那条断言就永远过不了；30 秒是**正常路径**上的硬断言，最坏情况的上界另算。层内报错也**变成结论而不是异常** —— 取消失败是一个**结论**，不是一个崩溃，异常会把"哪一层没停下来"这条信息丢掉。**顺序固定且不可配置**：任务 → Adapter → 进程 → 容器，**先内后外**；可配置就会有人把容器排在前面，而那正是"结论与事实相反"的那种做法。**端口住在 `core/` 而不是任何一边**：链的一头是执行器（`executors/`），另一头是沙箱（`harness/`），而这两边**互不 import**（待裁决 13 与 `tests/unit/harness/test_layering.py` 守着这条边界）—— 于是"链怎么跟两边说话"这个约定必须住在**第三处**，这不是为了省事：放在任何一边，那一边就得依赖另一边；放在 `core/`，方向就仍然是"适配层 → 领域"，两边都只依赖领域。**端口只声明"怎么问"，不声明"怎么答"**：怎么停下来是各自的事 —— 执行器知道自己的进程怎么杀，沙箱知道容器怎么停。所以 `AdapterLayer` 把回执状态**按字符串比对**（`"confirmed"` / `"refused"` / `"requested"`）而不是 import 执行器的枚举，方向才不会反过来；**不认识的状态一律按"没确认"处理**（执行器要是换了一套状态取值，我们宁可说"没停干净"也不能说"停了"）。**进程层与 Adapter 层不是同一件事**：Adapter 层问的是"执行器说你停了吗"，进程层问的是"那个进程还在吗" —— 执行器可能只停了它管的**主**进程，而它 spawn 出来的子进程还在跑，那时执行器报的是"已停止"，而工作空间里还有东西在动；进程层因此是**执行器自己的实现**（`ensure_process_stopped`）：没有起过进程 → 后置条件**天然成立**（报失败才是撒谎 —— "没有进程"不是"进程没停掉"）、已经退出 → 报退出码、还在跑 → 真杀掉并等它**真的**退出（`wait()` 不只是发信号，还要**回收** —— 进程刚死但没被回收时它是个僵尸，僵尸占着 PID 表项，而"停干净了"必须包括"回收了"）；**问一个不存在的会话要报错而不是悄悄当成"已停止"** —— 报"已停止"会让调用方以为那个会话不用管了，而那个会话也许还在跑。**容器层把"问不话"与"没在跑"分开**：`container_running` 返回三态（`bool | None`），`None` 是"docker 根本没说上话"；问不话却报"没在跑"，会把"我们没停掉它"与"它本来就没在跑"混成一句话，而这两件事的后果完全不同 —— 所以它用 `docker ps -a --filter id=...` 而不是 `inspect`：**容器被删掉了**（输出为空）与 **docker 坏了**（命令报错）必须能分开，而 `inspect` 对已删容器也是报错，把两种情况混成一类；同理"停掉了但还没删掉"（`exited`）必须读成"不在运行"，否则取消链会对着一具尸体报"停不掉" —— 用户会看到一个永远取消不掉的取消。T1 ① —— `::TestCancellingARunningSandboxTask`（**真容器 + 真子进程**）：链跑完之后断言**外部事实** —— 进程在操作系统里查不到了（`os.kill(pid, 0)` 抛 `ProcessLookupError`）、容器在 `docker ps` 里查不到了、**独立于启动器再问一遍** docker（只问启动器的话，验的是"启动器与启动器自己的报告一致"—— 那正是最需要防的那种自证）、**30 秒预算**是硬断言（取消是**用户等着的**操作：30 秒内没停干净，这次取消就失败了，哪怕它最后停了）、四层**各有一条**结论（缺一条不是"少了个信息"，是"有一类资源没人管"）。T1 ② —— `::TestALayerThatCannotDoItsJob`：容器层**问不话**时（docker 二进制根本不存在），链必须**如实**说（`cancelled is False`，未完成的正是沙箱层）、**很快就报**（有限时间内收敛 —— 挂死的取消比撒谎更糟，因为用户会一直等在那里），同时前面几层**照样收了**（一层失败不挡住后面的动作），而且那个真容器**确实还在跑** —— 链说没停掉，它就确实还在，这两句话必须对上。T1 ③ —— `::TestTheCancelledResult`：取消之后 `collect_result` 给的是 `CANCELLED` 而不是 `COMPLETED`，会话状态也是"已取消"（状态给轮询的人看、结果给验收的人看，两者必须一致 —— 不一致时轮询的人会以为任务还在跑），且 `self_assessment.completed` 是 `False` —— 取消掉的执行不许自称做成了（自我报告不参与验收判定，但它会出现在报告里，说"完成了"就是在说假话）。**反向验证**（证明断言有牙而非恒真）：对生产代码做 **7 处**定点变异，每次都让对应用例转红 —— ① 空报告也算取消（破坏 fail-closed）、② 少一层确认也算取消、③ 一层失败就停下不走了、④ 四层顺序反过来、⑤ 执行器只回"请求已发出"就当确认、⑥ docker 不可用时报"没在跑"而不是"问不话"、⑦ 进程没退出也报"已停止"；**其中 ⑦ 最初没被抓住** —— 补 `::TestTheProcessLayerEdgeCases::test_a_process_that_survives_the_kill_is_not_reported_as_stopped` 后才变红（那一处是**超时分支**，此前完全没有用例走到：真造不出"杀不掉"的进程 —— SIGKILL 一定收得到，所以让"杀"这一步**失效**来模拟信号递不进去，链看到的现象完全一样：进程还活着，而等待超时了）；**7 处全部被抓住**，生产文件还原后与原文逐字节一致（SHA256 逐个核对）。**一处自己踩的坑也记在这里**：集成测试原本用**固定**的容器名，两次**并发**运行会撞名，docker 直接拒绝启动第二个（"Conflict. The container name ... is already in use"），报出来的失败是"启动沙箱容器失败" —— 与被测的取消链毫无关系，却会让整轮集成测试**随机**变红；名字带上 PID 之后，一次运行内仍然是确定的（取消要能准确地停掉**这一个**容器），跨运行不再撞名，顺带把"上次跑崩留下的同名残留"也清掉 —— 名字唯一之后清残留才是安全的，否则会抢走另一个正在跑的测试的容器。

### [x] TASK-P1-034 超时语义：超时 ≠ 失败

- **工作流**：EP
- **设计要求**：R-095, R-097, I-03（§6 Agent 超时不等于任务失败，需要判断是否可以恢复；中断后可恢复或重新执行）
- **依赖**：TASK-P1-030, TASK-P1-033
- **交付物**：`executors/recovery.py` —— 超时处理流程：先 `interrupt` → 等待确认 → 再探测会话是否可恢复 → 决定 `retry` / `resume` / `fail`；禁止直接置为失败
- **验证（T1）**：pytest —— ① 超时且会话可恢复 → 状态为 `interrupted_resumable`，可续跑；② 超时且会话已损坏 → 状态为 `failed`，且未留下僵尸会话（断言容器与进程均已回收）；③ 重试产生新的幂等键，不复用旧键
- **自主权**：L2
- **预估**：0.5d
- **验证结果**：新增 `executors/recovery.py`（超时处置流程与决策表）、`executors/cli/adapter.py` 两处生产代码改动（`interrupt` 不再把超时改写成"已取消"、新增 `probe_session`）、`harness/sandbox/cancellation.py` 一处（`ChainResourceReclaimer`），以及 `tests/integration/conftest.py` 的沙箱镜像回退，**29 个新测试**（`tests/unit/executors/test_recovery.py` 23 / `tests/integration/test_timeout_recovery.py` 6），仓库全量 **1121 通过 / 1 跳过**（单元）与 **148 通过**（集成）。**一次超时要处理两件事，而它们不是同一件** —— 一是**收干净**（跑飞的那个执行还在改工作空间，不把它停掉，后面无论续跑还是重做都会有**两个东西同时改同一个目录**，这正是 I-03 说的"僵尸会话"），二是**下结论**（超时就直接判失败的话，一个只是慢了的执行会被当成做砸的：任务被扔掉，而它其实只差一分钟 —— R-097）。**顺序固定为 interrupt → 回收 → 探测 → 决策**，每一步的结论都是下一步的前提：探一个还在跑的会话，探出来的"可恢复"没有意义（它随时可能在你探测的那一刻又改了一处文件），所以**必须先 interrupt 再探测**；而**探测之前还要先把资源收干净** —— 收不干净就探，探出来的是"会话还在"，而那个会话背后可能还有一个没停掉的进程在改工作空间。**回收无条件执行，哪怕中断没有确认** —— "不知道它停了"正是最该去收一收的时候；而且**"收不干净"比"没确认"更严重**：还有东西在跑的时候去续跑，等于让两个执行同时改一个工作空间，比晚点续跑坏得多，所以这一条**fail-closed**（判失败，不放行）。**决策拆成两个问题，两个答案**：**能不能续跑**问 `SessionProbe`（会话还在吗、工作还在吗），**该不该再试一次**问 `ErrorCategory`（这次超时是不是瞬时故障）—— 刻意**不揉成一个判断**，因为两者问的是两件事、证据也来自两处；系统里已经有一份分类（`executors/cli/exit_codes.py` 把超时归为可重试），重造一份只会让两份表漂移，所以 `error_category` 默认**可重试**、与生产分类一致。**三个结论与三个动作一一对应**，这张表写在 `OUTCOME_ACTIONS` 里并由测试钉死（`::TestTheOutcomeDecidesTheAction`）：多一个结论少一个动作、或者两者的对应关系变了（比如把 resume 与 retry 对调），测试都必须变红 —— 只断言"键值都在"对**映射错了**完全没感觉。**判失败的每一条路都自带理由**，且方案里带的是**依据**而不只是结论：`::TestARecoverableSession::test_the_reason_carries_the_evidence` 要求"可以续跑"必须说得出为什么，否则事后没人能判断该不该信它。**"探不出来"与"确认不能"不是一回事** —— 探测出错时结论按不可恢复处理，但**理由写的是"探不出来"**（`::test_a_probe_that_raises_is_not_taken_as_a_verdict`）；混成一句话，就会在"其实能续跑"的那一次上把任务判死，事后对账也分不清这两种情况。**需人工的分类不自己拍板**（`NEEDS_HUMAN` → `FAILED`）—— 续跑与重试都是拍板；本模块的词汇表里没有"挂起"，编排层看 `plan.error_category` 自己决定要不要转成阻塞态。**续跑不换幂等键，重试换** —— 续跑是**同一次尝试**的延续（键还是那个键，`TASK-1:3`），重试是**新的一次尝试**（`TASK-1:4`）；复用旧键的话，重放去重会把新的一次当成上一次的重投而丢掉（I-01）。**判失败之后没有下一次执行，也就没有下一次尝试的键** —— `plan.attempt` 与 `plan.idempotency_key` 都是 `None`，给一个"不会再跑"的方案发一把键，等于让调用方以为还有一次执行。**顺带修掉一个证据丢失的缺陷**：`CliAdapter.interrupt()` 原本会把一次超时的会话**改写成"已取消"**（`session.status = CANCELLED`）—— 而"超时"与"已取消"的区分**正是本任务的处置流程赖以工作的那条线**：两者被混成一句话之后，"该续跑还是该重试"就分不开了。现在 `interrupt()` 在 `session.timed_out` 时保留 `TIMED_OUT` 并如实回报"这次执行已经超时结束"（`::TestATimeoutThatLeavesARecoverableSession::test_the_timeout_is_not_rewritten_as_cancelled` 断言处置之后 `collect_result` 给的仍然是 `TIMED_OUT` 而不是 `CANCELLED`）。**探针只查两件真的知道的事** —— 这个会话我们还认不认（不认识 = 执行器重启过、租约被回收）、它的工作空间在不在（不在 = 工作已经丢了，接着跑没有意义，重做一次才是对的）；**不问 CLI 的对话上下文能不能恢复** —— 那是 CLI 的内部实现（Claude Code 把它写在会话记录里），猜它的格式就会得出一个假的"能续跑"，而假的"能续跑"会让编排层去续一个其实已经没了的会话；两种"续不上"的**理由不同**，事后对账要能分清是哪一种。**端口住在 `executors/` 而不是 `core/`** —— 取消链的端口住在 `core/task/cancellation.py` 是因为那条链的两头是 `executors/` 与 `harness/`、两边互不 import（待裁决 13）；而这次**只有 `executors/` 一头**，沙箱那一侧通过**实现**端口来适配（`harness/sandbox/cancellation.py` 的 `ChainResourceReclaimer` 把取消链包装成"回收器" —— 回收与取消**动机不同、后置条件完全一样**，都是"每一类资源都不在了"），不必让两边共享同一份声明，方向仍然是"适配层 → 领域"。T1 ① —— `::TestARecoverableSession`（单元，测**决策**）与 `::TestATimeoutThatLeavesARecoverableSession`（集成，测**真资源**）：结论为 `interrupted_resumable`、动作 `RESUME`、attempt 与幂等键**不变**（`TASK-1:3`）、理由带着探测给出的依据。T1 ② —— `::TestASessionThatCannotBeResumed` 与 `::TestATimeoutWhoseSessionIsBroken`：**破坏会话的方式是删掉工作空间**（工作没了，无从接着跑），结论 `failed`、动作 `FAIL`、attempt 与键都是 `None`；**僵尸断言查的是外部事实** —— 进程在**操作系统**里查不到了（`os.kill(pid, 0)` 抛 `ProcessLookupError`）、容器在 **`docker ps`** 里查不到了（**独立于启动器再问一遍** docker —— 只问启动器的话，验的是"启动器与启动器自己的报告一致"，那正是最需要防的那种自证）。T1 ③ —— `::TestTheRetryAfterATimeout`：结论 `interrupted_retryable`、动作 `RETRY`、attempt 加一、**新键 ≠ 旧键**（这条单独立一个用例：新键算错时"键等于 `TASK-1:3`"这种断言仍可能蒙对，而"不等于旧键"不会）。**决策表的形状也进了断言** —— `::TestTheOrderOfTheSteps` 里的替身**记下调用顺序**："先 interrupt 再探测"是一条**顺序**约束，只断言最终结论的话把这两步换过来结论可能一模一样，顺序错了就查不出来。**处置是用户等着的操作**：整条流程必须在有限时间内收敛（`::TestTheRecoveryConverges`，30 秒预算是硬断言）。**反向验证**（证明断言有牙而非恒真）：对生产代码做 **12 处**定点变异，每次都让对应用例转红 —— ① 结论与动作对调（resume ↔ retry）、② 重试复用旧幂等键（T1 ③）、③ 判失败也发一把键、④ **先探测再中断**（I-03）、⑤ 回收没收干净也放行、⑥ 中断回执不看状态就当确认、⑦ 需人工的分类自己拍板、⑧ 探不出来当成可续跑、⑨ 回收无视没收住的层、⑩ **超时被改写成"已取消"**、⑪ 探测一律说可续跑、⑫ 回收器不真的收（容器留着跑）；**其中 ④ 最初是靠一个 `NameError` 被抓到的** —— 那不算数（它证明的是"代码写坏了"，不是"断言在盯着顺序"），于是把"把探测结果换成第二次探测"合并进同一处变异，让失败重新变成一条**语义**上的失败（顺序断言真的红了）；**12 处全部被抓住**，生产文件还原后与原文逐字节一致（SHA256 逐个核对）。**一处环境问题也记在这里**：集成测试原先在镜像仓库不可达时整组跳过，而"本地明明有一个能跑的镜像"却整组跳过，等于把能验的事白白放过 —— **跳过的测试什么都证明不了**；改成"仓库不可达但本地已有该镜像就用本地那个"（本地镜像可能旧一点，内容变过时重建一次就行），并把"本地也没有"与"构建脚本写错了"分成两路（后者必须照常报错，不能被当成环境问题吞掉）。

### [ ] TASK-P1-035 成本上报的统一口径

- **工作流**：EP
- **设计要求**：R-039, R-118, I-13（§3.4 cost 字段；§10 单位成本收益；三处成本需同源同币种）
- **依赖**：TASK-P1-005
- **交付物**：`core/metrics/cost.py` —— 统一成本换算器：Adapter 上报 → 内部记账 → Langfuse 观测，三处走同一个换算函数；币种与单价表外部可配
- **验证（T1）**：pytest —— ① 同一次执行在 Adapter 报告、预算账本、OTel span 属性三处的成本数值相等（同一函数产出）；② 换算单价变更后，三处同步变化
- **验证（T2）**：成本记账与实际调用量偏差 < 2%
- **自主权**：L2
- **预估**：0.25d

---

### 2.5 Engineering Harness（HN）

### [x] TASK-P1-040 Acceptance Contract 模型与校验器

- **工作流**：HN
- **设计要求**：R-051, R-052, R-053, R-054（§3.5B 每个任务执行前必须先定义怎样才算完成）
- **依赖**：TASK-P1-020
- **交付物**：`harness/verification/contract.py` —— `AcceptanceContract`：`required[]`（unit_tests_pass / integration_tests_pass / lint_pass / architecture_rules_pass）/ `thresholds{}`（operator + value）/ `forbidden[]`（secrets_in_diff / unauthorized_dependency_change / public_api_breaking_change）/ `artifacts[]`（git_diff / test_report / execution_trace）；每任务生成一份并绑定 `task_id`
- **验证（T1）**：pytest —— ① 缺 `required` 中任一项的契约被拒绝；② 缺 `task_id` 绑定被拒绝；③ 阈值运算符与值类型校验（如 `>=` 配字符串值报错）；④ `forbidden` 命中任一条即整体判定失败（不因其他项通过而放行）
- **自主权**：L2
- **预估**：0.75d
- **验证结果**：T1 —— **32 个单元测试**（`tests/unit/harness/verification/test_contract.py`）覆盖四类断言：契约非空（`required` 不得为空、不得重复、未知检查名被拒）、任务绑定（空 `task_id` 被拒、拿 A 任务的契约放行 B 任务判失败）、阈值类型校验（`>=` 配字符串/布尔被拒、标量 `0.85` 被解析为数值型、`==` 配文本合法、类型不匹配的比较**报错**而不是悄悄返回 False）、判定规则（缺必检项算"没产出结果"而非"算过"、`ERROR` 与 `FAILED` 分开、缺实测值算失败、缺产物算失败、命中 `forbidden` 一票否决）。判定实现为**与**逻辑 —— `forbidden` 一票否决是规则的推论而非特例分支，因此"三项通过、一项命中密钥"必然整体判失败。阈值映射包进 `FrozenDict`，进程内改松验收口径的路被封死。`ruff check` / `mypy --strict`（3 个源文件）/ `pytest` 全绿；`harness.verification.contract` 已登记进 `tests/unit/core/test_no_any_escape.py` 的豁免清单（Pydantic 模型继承 `BaseModel.__init__` 的 `Any`，见 pyproject 同源说明）。**反向验证**：对生产代码做 **12 处**定点变异（把判定改成计分、把"缺结果"当通过、去掉 `forbidden` 的一票否决、把 `FrozenDict` 换回普通 dict、放宽运算符与值类型的配对校验、把"缺实测值"默认成通过、把 `ERROR` 当 `PASSED` 等），每次都让对应用例转红 —— **12 处全部被抓住**，生产文件还原后与原文逐字节一致。

### [x] TASK-P1-041 最小独立验证引擎

- **工作流**：HN
- **设计要求**：R-049, R-051, I-22（§3.5B 不要让 Coding Agent 自己决定是否验收通过；验证引擎独立于执行器）
- **依赖**：TASK-P1-040, TASK-P1-032
- **交付物**：`harness/verification/engine.py` —— 独立进程/容器执行目标项目测试套件 + 契约检查；产出 `VerificationRun`（checks + result）；**验证进程不接触 Agent 会话、不读取 `self_assessment`**；校验器与被测代码模型可分别配置
- **验证（T1）**：pytest —— ① 验证进程以独立容器/进程启动，断言其环境中不存在 Agent 会话句柄；② 故意让 Agent 报告 `self_assessment.completed = true` 而实际测试失败 → 验证结果为 **failed**，任务状态为 failed；③ 验证进程挂掉时结果为 `error` 而非 `passed`（fail-closed）
- **验证（T2）**：验证结论与 `self_assessment` 的一致率为 0（阶段一用构造样本验证二者完全解耦）
- **自主权**：L2
- **预估**：1d
- **验证结果**：T1 —— **33 个单元测试**（`tests/unit/harness/verification/test_engine.py`）。**T1 ①**：验证进程用 `asyncio.create_subprocess_exec` 以独立进程组（`start_new_session=True`）启动，环境由 `build_verification_env` 白名单构造 —— 只放行 15 个无害变量（`PATH`/`HOME`/`LANG`/`TZ`…），`HOME` 指向一次性临时目录（退出即删），凭据形状的名字（`SECRET`/`TOKEN`/`PASSWORD`/`KEY`/`SESSION`…）**直接拒绝**而不是静默丢弃；断言落在**运行器实际收到的环境**上，而不是"某个参数没传"。另有一条真起子进程把 `os.environ` 整个 dump 出来的用例，用它证明验证进程里既没有会话句柄也没有密钥。**T1 ②**：Agent 的 `self_assessment.completed = true` 与"测试实际失败"同时成立时，验证结论为 **failed**、任务状态为 **failed** —— 这条不是靠"引擎不读 `self_assessment`"的约定守住的，而是**结构上做不到**：`VerificationRequest` 的字段集合就是 `{task_id, workspace, contract, plan}`，压根没有能装下执行结果的地方；`harness/` 整个包不 import `executors/`（由 `tests/unit/harness/test_layering.py` 用 AST 守着），依赖方向被钉死为 `执行器 → 领域 ← 验证引擎`。**T1 ③**：检查起不来（`started=False`）、超时、被信号打死一律判 `error` 而非 `passed` —— fail-closed 双向都守："没跑成"与"跑了没过"分属 `ERROR` 与 `FAILED`，前者该重跑、后者该返工。**T2**：四个构造样本（单元测试失败 / lint 失败 / 进程起不来 / 密钥命中）里 Agent 全都说"做完了"，验证结论与 `self_assessment` 的**一致率为 0**。`ruff check` / `ruff format --check` / `mypy --strict`（209 个源文件）/ 全量单元测试全绿。**反向验证**：对生产代码做 **19 处**定点变异（把退出码非 0 当通过、把 `ERROR` 当 `FAILED`、让证据不齐也能给通过/不通过、指标读不到时返回 0、不乘 `scale`、继承宿主机整个环境、`HOME` 不指向一次性目录、放行凭据形状的环境变量、契约没要求的检查也照跑、扫描命中不记为违规、出错的检查也去读指标、结论永远 `passed` 等），每次都让对应用例转红 —— **19 处全部被抓住**。变异脚本跑在仓库外的独立沙箱副本里（`/tmp/ases-mut-p1041/tree`），主仓库全程不被改动；沙箱内每次写入后清掉 `__pycache__` —— Python 的 pyc 头部只记 mtime（秒级）与大小，而本轮变异全是**等长替换**（`FAILED`→`PASSED`），还原又常发生在同一秒里，于是缓存字节码与还原后的源文件"对得上"而被复用，跑出来的其实是含变异的旧字节码（实测表现为"还原后测试仍然转红"，看着像断言太严，其实是加载了脏字节码）。

### [ ] TASK-P1-042 覆盖率模块基线机制

- **工作流**：HN
- **设计要求**：R-055, I-09（§3.5B 脚注：阈值应根据模块基线确定，不能通过降低测试质量来达成指标）
- **依赖**：TASK-P1-020, TASK-P1-023
- **交付物**：`core/task/coverage_baseline.py` —— 每模块基线存储（落 `ProjectSnapshot.metrics` 或独立基线表）、阈值计算 `threshold = max(模块基线, 全局地板)`、基线更新流程（随代码演进）、**基线下调需审批**
- **验证（T1）**：pytest —— ① 修改某模块基线后，该模块的覆盖率阈值同步变化，其他模块不受影响；② 尝试下调基线被拒绝并留下审计记录；③ 阈值为 `max(模块基线, 全局地板)`，不小于全局地板
- **验证（T2）**：阶段一周期结束时，基线下调次数 = 0
- **自主权**：L2
- **预估**：0.75d

### [x] TASK-P1-043 安全门（forbidden 检查）

- **工作流**：HN
- **设计要求**：R-048, R-054, R-060（§3.5B forbidden；§3.5D 默认禁止访问生产环境、网络白名单）
- **依赖**：TASK-P1-040, TASK-P1-032
- **交付物**：`harness/security/gate.py` —— 三项 forbidden 检查器：① `secrets_in_diff`（diff 内密钥扫描）；② `unauthorized_dependency_change`（依赖清单变更需在授权范围）；③ `public_api_breaking_change`（公共接口签名 diff）；网络出站白名单配置
- **验证（T1）**：pytest —— ① diff 中注入测试密钥 → 检查命中；② 新增未授权依赖 → 检查命中；③ 删除/改签名公共接口 → 检查命中；④ 干净 diff → 三项均不命中
- **验证（T1）**：网络白名单外的域名不可达（真实请求断言）
- **自主权**：L2
- **预估**：0.75d
- **验证结果**：T1 —— **54 + 22 = 76 个单元测试**（`tests/unit/harness/security/test_gate.py` 54 条、`test_network.py` 22 条）。**T1 ①**：`TestTheSecretScan` —— diff 里埋一枚 AWS 形状的假凭据必须命中（`FAKE_AWS_KEY`（一枚 AWS 形状的假凭据，**不写在这里** —— 写在这里它就成了仓库里的一枚真凭据形状的字面量；按仓库既有惯例登记进 `tests/unit/infrastructure/test_no_hardcoded_secrets.py` 的 `ALLOWED_CANARIES`，并写明放行理由）），**删掉**一行凭据不算命中（只扫新增行），占位符（`changeme-…`）不算命中；"赋值形"兜底（`api_key = "…"`）必须命中 —— 否则换个厂商就漏了。**命中详情里不回显密钥原文**：详情会进日志、进审计表、进看板，把密钥抄一遍等于把它复制到第二个地方，所以详情只给"哪一行、什么形状、多长"（断言详情里既没有原文、又有 `AKIA` 与长度）。**T1 ②**：`TestTheDependencyScan` —— 依赖名按 PEP 503 归一化后比较（授权 `requests` 挡得住 `Requests`，但**挡不住** `re-quests` —— 那是另一个包）；给了改动前的清单就只查**新增**的依赖（授权清单里于是不必列全项目历史依赖）；清单**读不出来**（`setup.py` 这类格式、或者正文存在但 TOML 解析失败）按**未授权**处理 —— 宁可误报一次，也不要"清单看不懂所以放过"；锁文件**不作依赖声明**（它是声明的结果，数它一遍会把一次改动报成几十条）；`pyproject.toml` / `package.json` / `go.mod`（单行与块形式两种写法）都认。**T1 ③**：`TestThePublicApiScan` —— 受保护模块里的公共符号被**删掉**、**改签名**、所在文件被整个删除，都算破坏性变更；只改函数体不算（定义行在 diff 里是上下文行）；下划线开头的私有符号不算公共接口。**T1 ④**：`TestTheReport::test_a_clean_diff_triggers_nothing` —— 干净 diff 三项均不命中。**"没结论 ≠ 通过"**：`GateReport` 把三种状态分开 —— `violations`（命中）、`evaluated`（查过、没命中）、`unevaluated`（**没结论**）。公共接口面没配置时 `PUBLIC_API_BREAKING_CHANGE` 进 `unevaluated`；此时 `passed` 是 `True` 而 `complete` 是 `False` —— 两个布尔量**必须分开看**，合并了就一定会有人把"没查"当成"查过"。**网络白名单（真实请求断言）**：`test_network.py` 真起一台本地 HTTP 服务器并**数它收到了几个请求** —— 被拒的地址到达服务器的请求数必须是 **0**（不是"连上再断开"），放行的地址必须是 1 且响应内容要对；白名单**默认什么都不允许**（忘配置的后果是吵闹的失败，不是安静的放行）；只放行 http/https（`file://` 是读本地文件的另一条路，URL 里带 `user:password@` 是把凭据塞进日志的路）；**不跟随重定向**（白名单只对"你明确写出来的那个地址"生效）；**不读环境里的代理设置**（`trust_env=False` —— `HTTP_PROXY` 会让实际连接落到代理主机上，白名单判定的是这个主机、流量去的是另一个，白名单一旦能被环境变量改道就不再是白名单；这条由"设一个死代理地址、请求仍然直达本地服务器"钉住）。`ruff check` / `ruff format --check` / `mypy --strict`（**214** 个源文件）/ 全量单元测试（**1240 passed, 1 skipped**）全绿。**反向验证**：对生产代码做 **23 处**定点变异（新增行里的凭据不再算命中、占位符也被当成真凭据、详情里回显密钥原文、依赖名不归一化、清单读不出来当成"没声明依赖"、锁文件也当清单处理、不做基线减法、签名变化不再算破坏性变更、私有符号也算公共接口、没配置公共接口面也假装查过、"没结论"被算成"不通过"、粗判里把键值对也当依赖、`go.mod` 块形式取到版本号、白名单判定被放到连接之后、协议不再限制、URL 里的凭据不再被拒、端口不再检查、通配符也匹配域本身、前导点不再匹配域本身、跟随重定向、读环境里的代理设置），每次都让对应用例转红 —— **23 处全部被抓住**。首轮只抓住 19 处，漏掉的 4 处里有 1 处是**真 bug**：锁文件此前会被当成依赖清单（`kind` 已是 `"lock"` 却仍进了粗判），锁文件里一行 `    "httpx>=0.27",` 会被报成"引入了未授权依赖" —— 一次依赖变更在锁文件上再报一遍，一个依赖带来几十行哈希，于是"改了一个依赖"变成"报了三十条"。另 3 处是断言不到位（基线减法、键值对、协议判据），补了三条用例后转绿。变异脚本跑在仓库外的独立沙箱副本里（`/tmp/ases-mut-p1043/tree`），主仓库全程不被改动；沙箱内每次写入后清掉 `__pycache__`（Python 的 pyc 头部只记 mtime 秒级与大小，等长替换的变异在同一秒还原时会被复用 —— 详见 TASK-P1-041 的记录）。

### [ ] TASK-P1-044 任务级授权上下文（Agent 身份 ≠ 任务身份）

- **工作流**：HN
- **设计要求**：R-059, R-061, R-062, R-063, I-24（§3.5D Agent 权限绑定到任务、项目、工作空间和有效期；Agent 身份不能替代任务身份）
- **依赖**：TASK-P1-011, TASK-P1-020
- **交付物**：`harness/security/authorization.py` —— `TaskAuthorization`（agent_id / task_id / project_id / workspace_id / expiry / allowed_operations / granted_scopes）；授权判定 key 为上述五元组；高风险操作（删除/发布/合并/部署）需独立授权记录
- **验证（T1）**：pytest —— ① **同一 Agent 在 task A 中持有仓库读权限，无法在 task B 中执行合并操作**（断言拒绝且审计留痕）；② 授权过期后操作被拒；③ 授权 workspace 外的路径访问被拒；④ 高风险操作缺少独立授权记录时被拒
- **自主权**：L2
- **预估**：0.75d

### [ ] TASK-P1-045 工具调用审计日志

- **工作流**：HN
- **设计要求**：R-062（§3.5D 每一次工具调用均记录主体、任务、资源和执行结果）
- **依赖**：TASK-P1-005, TASK-P1-044
- **交付物**：`harness/security/audit.py` —— `AuditEvent`（subject/agent_id、task_id、resource、operation、result、timestamp、decision）；审计写入路径不可被业务代码绕过；提供查询接口
- **验证（T1）**：pytest —— ① 一次任务执行后，审计表中存在该任务全部工具调用的记录，且每条含主体/任务/资源/结果四要素；② 审计写失败时**阻断操作**（fail-closed，不允许"审计失败就放行"）
- **验证（T4）**：审计字段字典写入 `docs/observability/audit-schema.md`
- **自主权**：L2
- **预估**：0.5d

### [ ] TASK-P1-046 人工审批门

- **工作流**：HN
- **设计要求**：R-012, R-101, R-111, I-12（§7 merge_to_main = L2 需人工审批；§1 人工审批等待）
- **依赖**：TASK-P1-011, TASK-P1-020
- **交付物**：`harness/approval/` —— DB-backed 审批门：`ApprovalRequest`（task_id / operation / diff摘要 / 验证证据引用 / 状态 / 请求人 / 审批人 / 时间戳）；支持 `pending/approved/rejected/expired`；**阶段三将升级为 Temporal Signal，本任务不绑定 Temporal**
- **验证（T1）**：pytest —— ① `merge_to_main` 在无审批记录时被 `PolicyManager` 拒绝；② 审批通过后允许合并；③ 审批被拒后任务进入 `BLOCKED` 状态；④ 审批请求超时过期后不可再用于放行
- **验证（T3）**：demo —— 走一次"任务验证通过 → 停在审批门 → 人工批准 → 合并到 main"
- **自主权**：L2
- **预估**：0.5d

### [ ] TASK-P1-047 最小集成路径

- **工作流**：HN
- **设计要求**：R-056, R-057（§3.5C Task Branch → Local Validation → Integration Branch → … → Main；单任务通过 ≠ 集成后通过）
- **依赖**：TASK-P1-041, TASK-P1-046
- **交付物**：`harness/integration/pipeline.py` —— 单任务路径：Task Branch → 本地验证 → 审批门 → Main；**阶段一只做单任务集成路径**，跨任务集成与 Integration Branch 留阶段二（见 `TASK-P2-030`）
- **验证（T1）**：pytest —— ① 任务分支验证通过后可经审批合并进 main；② 未经验证或验证失败的分支无法进入合并流程；③ 合并后 main 的测试仍通过（合并动作本身不破坏基线）
- **自主权**：L2
- **预估**：0.5d

### [ ] TASK-P1-048 验收证据归档

- **工作流**：HN
- **设计要求**：R-065, R-071, R-099（§3.6 执行产物库保存不可丢失的证据）
- **依赖**：TASK-P1-004, TASK-P1-041
- **交付物**：`knowledge/artifacts/archiver.py` —— 验证结束后把 git diff / test report / execution trace 上传对象存储并登记 `Artifact`（uri + hash + type）；`Artifact` 与 `VerificationRun` 绑定
- **验证（T1）**：pytest —— ① 验证完成后三类产物均可按 URI 取回且 hash 与登记值一致；② 归档失败的验证**不计为通过**（fail-closed）
- **自主权**：L2
- **预估**：0.25d

---

### 2.6 Autonomous Director 与 Evolution Engine（DR）

### [ ] TASK-P1-050 Director 五阶段决策图（LangGraph）

- **工作流**：DR
- **设计要求**：R-001, R-005, R-020, R-021, R-081, R-082, R-124, R-126, R-127（§2 四要素闭环禁止超级 Agent；§3.2 四个逻辑角色；§5 五阶段：认知→诊断→探索→决策→规划）
- **依赖**：TASK-P1-051, TASK-P1-052, TASK-P1-054, TASK-P1-055, TASK-P1-056
- **交付物**：`orchestration/langgraph/decision_graph.py` —— 五节点有向图：认知 → 诊断 → 探索 → 决策 → 规划；每节点输入输出为 Pydantic 模型（结构化，不返回自由文本）；图状态可持久化（checkpoint）
- **验证（T1）**：pytest —— ① 五个节点各产出符合 schema 的结构化对象；② 用打桩节点跑通整图，端到端产出 `EvolutionRoadmap`；③ 中途异常后从 checkpoint 恢复不重复执行已完成的节点
- **验证（T4）**：节点输入输出契约表写入 `docs/architecture/decision-stages.md`
- **自主权**：L2
- **预估**：1d

### [ ] TASK-P1-051 Project Understanding 与 Gap Analysis 的结构化模型

- **工作流**：DR
- **设计要求**：R-022, R-082, R-083（§5 project_assessment 结构化输出）
- **依赖**：TASK-P1-010, TASK-P1-020
- **交付物**：`core/evolution/assessment.py` —— `ProjectAssessment`（project_id / snapshot / overall_status / goal_gap[]（id、description、evidence[]、severity）/ opportunities[] / recommendations[] / deferred[]（reason、candidate））
- **验证（T1）**：pytest —— ① schema 校验拒绝缺 `evidence` 的 gap；② `evidence` 至少一条且指向可解析的引用（`artifact://` 或 `issue:` 或文件路径）；③ `deferred` 条目必须带 `reason`
- **自主权**：L2
- **预估**：0.5d

### [ ] TASK-P1-052 Evolution Candidate 生成与五类机会分类法

- **工作流**：DR
- **设计要求**：R-024, R-029, R-030（§3.2 五类演进机会；§3.3 Candidate 结构）
- **依赖**：TASK-P1-051
- **交付物**：`core/evolution/candidate.py` —— `EvolutionCandidate`（id / category / title / evidence[] / hypothesis / expected_outcome[] / risk / confidence / estimated_effort）；`category ∈ {product, architecture, engineering, quality, technology}`；发现器接口（技术趋势发现器必须先证明对本项目有实际收益才产出候选，R-025）
- **验证（T1）**：pytest —— ① 缺 `evidence` 或 `hypothesis` 的候选被拒绝；② `confidence ∈ [0,1]`、`risk ∈ {low,medium,high}` 校验生效；③ 技术趋势类候选若无"对本项目的实际收益"论证则被丢弃（构造样本断言）
- **自主权**：L2
- **预估**：0.75d

### [ ] TASK-P1-053 Priority 评分模型

- **工作流**：DR
- **设计要求**：R-031, R-032, R-033, R-084（§3.3 Priority = V×I×C×U / (E×(1+R))；归一化 0~1；成本需合理下限）
- **依赖**：TASK-P1-052
- **交付物**：`core/evolution/priority.py` —— 六因子（V 价值 / I 影响范围 / C 证据置信度 / U 紧迫度 / E 实施成本 / R 风险系数）；归一化函数；成本下限常量；预留用实际收益校准的接口（阶段四）
- **验证（T1）**：pytest —— ① 各因子输入 ∈ [0,1]，越界报错；② `E` 取 0 时不除零（走下限）；③ 因子全相等时排序稳定（不因并列而随机重排）；④ 已知输入的评分与手算值一致
- **验证（T1）**：提高 `C`（置信度）使该候选排名上升；提高 `E`（成本）使排名下降（方向性断言）
- **自主权**：L2
- **预估**：0.5d

### [ ] TASK-P1-054 Decision 三态与"不行动"的强制落地

- **工作流**：DR
- **设计要求**：R-034, R-035（§3.3 不行动也是合法决策：Record / Defer / Reject；§4 循环 B）
- **依赖**：TASK-P1-022, TASK-P1-053
- **交付物**：`core/evolution/decision.py` —— `EvolutionDecision`（decision ∈ {record, defer, reject} / rationale / candidate_id / 触发的补充分析项）；**defer 必须产出补充分析项且该轮不创建 Task**
- **验证（T1）**：pytest —— ① 构造 `confidence = 0.1` 的候选 → 系统产出 defer 决策、写入 rationale、**且任务表中无对应 Task**；② defer 决策包含至少一条具体的补充分析项；③ reject 必须带 rationale
- **验证（T2）**：阶段一周期中 defer/reject 占决策总数的比例被记录（≥ 1 条不行动决策是闭环健康信号，若恒为 0 则说明"不行动"能力未真正实现）
- **自主权**：L2
- **预估**：0.5d

### [ ] TASK-P1-055 独立 Critic

- **工作流**：DR
- **设计要求**：R-085, R-086, R-087, I-18（§5 Critic 七问；必须调用代码仓库、Git 历史、测试报告和项目状态进行事实核验）
- **依赖**：TASK-P1-051, TASK-P1-052
- **交付物**：`agents/critic/` —— 九项检查（原设计七问 + **本清单补入的第八问"是否触碰项目 Non-goals"** + **第九问"是否触发 Stop Conditions"**）；每项检查可调事实核验工具（仓库搜索、Git 历史查询、测试报告读取、项目状态查询）；产出结构化 `CriticReport`（逐项结论 + 证据引用 + 是否否决）
- **验证（T1）**：pytest —— ① 九项检查全部被执行（断言调用记录，**不检查判定质量**）；② 构造"违反 Non-goals"的候选 → Critic 给出否决且引用具体 Non-goal 条目；③ 构造"该问题已有其他任务在解决"的候选 → Critic 检出（通过查询 Task 表实现事实核验）；④ Critic 否决后，该候选不得进入 `APPROVED`
- **验证（T3）**：评审 —— 抽 5 个历史候选，检查 Critic 的否决理由是否站得住
- **自主权**：L2
- **预估**：1d

### [ ] TASK-P1-056 Roadmap 生成与任务拆解

- **工作流**：DR
- **设计要求**：R-022, R-023, R-076, R-081（§3.2 应生成哪些具体任务、七种任务类型；§5 规划阶段）
- **依赖**：TASK-P1-054, TASK-P1-055
- **交付物**：`core/planning/roadmap.py` —— `EvolutionRoadmap`（cycle_id / 计划任务列表 / 依赖关系 / 排期建议）；任务拆解器：把已批准的 Decision 转为 `Task`（**必须经 Decision 落地路径**，受 `TASK-P1-022` 约束）+ `TaskDependency`；任务类型 ∈ 七类（feature / bugfix / refactoring / test / performance / dependency / docs）
- **验证（T1）**：pytest —— ① 拆解产生的每个 Task 都能追溯到一个 `EvolutionDecision`（断言外键）；② 依赖图无环；③ 任务类型七类均可构造；④ 未被 Critic 放行的 Decision 不产生 Task
- **自主权**：L2
- **预估**：0.75d

### [ ] TASK-P1-057 角色化 Prompt 集

- **工作流**：DR
- **设计要求**：R-020, R-021, R-026, I-11（§3.2 四个逻辑角色作为独立决策任务；§6 Critic 不能只给 LLM 评分）
- **依赖**：TASK-P1-051
- **交付物**：`agents/prompts/` —— 六套 Prompt（analyst / strategist / architect / quality / director / critic），每套输出绑定对应 Pydantic schema；每套含**证据引用要求**（结论必须附证据，无证据的结论被下游丢弃）；模型与温度可外部配置
- **验证（T1）**：pytest —— ① 每套 Prompt 的模型输出能被对应 schema 解析；② 输出中缺证据引用的结论在解析后被标记为 `unsupported` 并在下游流程中被排除
- **验证（T4）**：每套 Prompt 的职责边界与输入输出写入 `docs/agents/prompt-contracts.md`
- **自主权**：L2
- **预估**：0.5d

---

### 2.7 编排与演进闭环（OR）

### [ ] TASK-P1-060 循环 A：工程执行循环编排

- **工作流**：OR
- **设计要求**：R-006, R-073, R-075, R-078（§4 循环 A 节点链；两循环不能合并成一个无限执行 Prompt；POC 不引入 Temporal）
- **依赖**：TASK-P1-030, TASK-P1-041, TASK-P1-046, TASK-P1-056
- **交付物**：`orchestration/loop_a.py` —— 节点链：Task Ready → Task Planner → Select Coding Agent → Create Isolated Workspace → Agent Executes → Collect Diff & Artifacts → Independent Verification → Acceptance Passed? → Integration & Merge Gate → Failure Diagnosis → Can Auto Repair? → Create Repair Task → Escalate/Block → Update Project State → Execution Complete；**由 LangGraph 驱动**（Temporal 留阶段三）
- **验证（T1）**：pytest（打桩 Agent + 打桩验证引擎）—— ① 全链路状态迁移序列符合设计文档节点顺序；② 验证失败 → 进入 Failure Diagnosis → 产生修复任务；③ 修复次数超上限 → 进入 `BLOCKED`，不再自动重试；④ 每步都写入 `AgentSession` 与 `ExecutionEvent`
- **自主权**：L2
- **预估**：1d

### [ ] TASK-P1-061 循环 B：项目自主演进循环编排

- **工作流**：OR
- **设计要求**：R-006, R-027, R-074, R-075, R-078（§4 循环 B 节点链；两循环独立但可相互触发；POC 不引入 Temporal）
- **依赖**：TASK-P1-050, TASK-P1-060, TASK-P1-061
- **交付物**：`orchestration/loop_b.py` —— 节点链：Observe Project → Build Project Snapshot → Analyze Goal Gap → Discover Candidates → Gather Evidence → Evaluate Value/Risk/Cost → Worth Doing? → Record/Defer/Reject → Generate Plan → Decompose into Tasks → Dependency & Resource Scheduling → Execute Engineering Cycle → Evaluate Actual Outcome → Update Project Knowledge → Update Goal Gap；触发方式为**事件驱动**（阶段一的最小实现：手动/定时触发 + Outbox 事件）
- **验证（T1）**：pytest —— ① 全链路可跑通并产出 `EvolutionRoadmap`；② 循环 B **不直接执行代码**（断言其不调用 Adapter）；③ 循环 B 的执行由循环 A 承接（断言调用的是循环 A 入口）；④ 循环 B 结束后项目知识被更新（`EvolutionCycle` + `ProjectSnapshot` 落库）
- **自主权**：L2
- **预估**：1d

### [ ] TASK-P1-062 验收契约准入 gate

- **工作流**：OR
- **设计要求**：R-051, I-05（§3.5B 每个任务在开始执行前必须先定义怎样才算完成 —— 时序约束）
- **依赖**：TASK-P1-040, TASK-P1-021
- **交付物**：`orchestration/gates.py` —— READY→RUNNING 迁移的准入检查：目标任务的 Acceptance Contract 必须**已存在且已绑定 task_id**，否则拒绝迁移并报错
- **验证（T1）**：pytest —— ① 无契约的任务无法从 READY 迁移到 RUNNING（断言迁移被拒且状态仍为 READY）；② 补上契约后迁移成功；③ 契约在任务开始后被修改 → 下一次迁移用新版本，且修改留审计
- **自主权**：L2
- **预估**：0.5d

### [ ] TASK-P1-063 失败诊断与有界自动修复

- **工作流**：OR
- **设计要求**：R-098, I-04（§6 修复失败必须有次数、成本和时间上限）
- **依赖**：TASK-P1-060, TASK-P1-012
- **交付物**：`orchestration/repair.py` —— 失败归类（可自动修复 / 需人工 / 不可修复）+ 修复任务生成；**三重上限为全局共享**（次数 + 成本 + 时间），不是每任务独立
- **验证（T1）**：pytest —— ① 修复次数达上限后停止并置 `BLOCKED`；② 修复成本超预算后停止；③ 修复总时长超限后停止；④ **并行两个任务时，全局上限不会被各自突破**（两个任务各修 3 次但全局上限为 5 → 总次数不超过 5）
- **自主权**：L2
- **预估**：0.75d

### [ ] TASK-P1-064 事件去重、因果链与回环熔断

- **工作流**：OR
- **设计要求**：R-072, I-11（§4 两个循环相互触发；防止"执行结果回写→再次触发演进循环"的无限回环）
- **依赖**：TASK-P1-025, TASK-P1-061
- **交付物**：`orchestration/dedup.py` —— 事件去重窗口（按 `event_type` + `aggregate_id` + 时间窗）、**因果链 ID**（`causation_id` 贯穿演进→执行→回写）、熔断规则（同一 gap 连续 N 轮未改善则停止重规划并标记 `stalled`）
- **验证（T1）**：pytest —— ① 同一事件重复投递只被消费一次；② 演进循环触发的执行，其结果回写**不再触发新一轮演进循环**（因果链 ID 匹配，断言未产生新的 cycle）；③ 同一 gap 连续 2 轮未改善 → 熔断，产生 `stalled` 标记而非继续规划
- **自主权**：L2
- **预估**：0.5d

### [ ] TASK-P1-065 确定性演练 runner（无 LLM 回放）

- **工作流**：OR
- **设计要求**：R-108, R-109（§9 阶段一验收）；本文新增 —— 闭环编排需要可重复的回归测试手段
- **依赖**：TASK-P1-060, TASK-P1-061
- **交付物**：`tests/system/replay_runner.py` —— 打桩 Agent Adapter（按脚本返回预置 diff 与状态）+ 打桩验证引擎 + 打桩 LLM 节点；能从 YAML 脚本回放完整演进周期，用于回归与 CI
- **验证（T1）**：pytest —— ① 同一脚本回放两次，产生**完全相同**的状态迁移序列与 DB 记录（可重复）；② 打桩 Agent 报告成功但打桩验证判失败 → 系统进入修复路径（演练失败语义）；③ 打桩 Critic 否决 → 候选不进入 APPROVED
- **自主权**：L2
- **预估**：0.5d

---

### 2.8 观测与评估（EV）

### [x] TASK-P1-070 EvolutionCycle 记录

- **工作流**：EV
- **设计要求**：R-068, R-069, R-129（§3.6 EvolutionCycle；§12 每一轮演进应产生可验证的改善）
- **依赖**：TASK-P1-020
- **交付物**：`core/metrics/cycle.py` —— `EvolutionCycle`（cycle_id / 起止时间 / 触发原因 / 候选数 / 决策数 / 任务数 / 完成数 / outcome / 目标达成度变化 / 成本 / 证据引用）
- **验证（T1）**：pytest —— ① 一轮周期结束后 cycle 记录完整且各计数字段与实际 DB 计数一致；② `outcome` 取值 ∈ {improved, no_change, regressed, stalled}，能表达"无改善"；③ 周期记录包含前后两次达成度，可算出差值
- **自主权**：L2
- **预估**：0.5d
- **验证结果**：新增领域模块 `core/metrics/cycle.py`（45 个单元测试，`tests/unit/metrics/test_cycle.py`）+ `infrastructure/database/repositories.py::CycleRepository`（19 个集成测试，`tests/integration/test_cycle_recording.py`）。**结局拆成两个维度**：原 `evolution_cycle.outcome` 一个字段同时承担"周期**怎么**结束"与"这一轮**有没有变好**"，合成一个必然丢掉其中一个 —— 被预算掐断的周期，跑完了但没变好。故拆为 `status`（completed / aborted / budget_exhausted / blocked）与 `outcome`（improved / no_change / regressed / stalled），各自带 DB CHECK 约束（`ck_evolution_cycle_evolution_cycle_status_values` / `..._outcome_values`），迁移 `0004` 把旧行按对应表拆开（见待裁决 10）。T1 ② —— `::TestOutcomeVocabulary`：`test_improved_when_attainment_rises` / `test_regressed_when_attainment_falls` / `test_no_change_when_attainment_stays` / `test_stalled_when_nothing_was_completed` / `test_stalled_when_the_cycle_did_not_run_to_completion[3 个非 completed 状态]` / `test_all_four_outcomes_are_reachable`（断言四种结局**都真的可达** —— 纸面上的枚举不算）；`::TestBuildDerivesTheOutcome::test_build_derives_the_outcome_from_the_evidence[5 个参数]` 覆盖"结局由证据判定而非由人指定"：regressed / no_change / 无基线 / 无任务完成 / 周期未正常结束，五个用例**没有一个**是 improved —— 一个把结局写死成 improved 的实现必须在其中至少一条上露馅。`test_build_has_no_outcome_parameter` 断言构造函数签名里**根本没有** outcome 这个入参（传不进来）。T1 ③ —— `::TestAttainmentDelta`：`test_record_carries_both_snapshots` / `test_delta_is_after_minus_before` / `test_delta_is_zero_when_unchanged` / `test_delta_is_negative_when_it_regressed` / `test_delta_is_none_when_a_snapshot_is_missing`（**缺数据 ≠ 差值为 0**）/ `test_delta_is_computed_from_decimals_not_floats`（`Decimal("0.3") - Decimal("0.1")` 必须精确等于 `Decimal("0.2")`，浮点数会把它变成 0.30000000000000004 —— 达成度差值算错一点点就可能把"没变"判成"变好了"）。T1 ① —— `::TestCountsMatchTheDatabase::test_counts_match_the_rows_that_were_actually_created`（候选 3 / 决策 2 / 任务 2 / 已完成 1，逐项对上）、`test_counts_are_scoped_to_the_cycle_not_the_project` 与 `test_closing_one_cycle_does_not_change_the_other`（**同一个项目下的两个周期，计数不能串** —— "按周期数"与"按项目数"唯一分水岭，也是这类偏差最常见的藏身处：数字看上去仍然是个合理整数，只是算错了对象）、`test_tasks_are_counted_through_the_decision_chain`（任务无 `cycle_id` 列，计数必须穿过决策链；若误按 `task.project_id` 数就会把别的周期的任务算进来）。**计数由仓储层从库里数出，不接受调用方声明** —— "这一轮发现了 5 个候选"这句话写起来不需要任何依据。**结论也由判定器得出**：结局、理由一律来自 `classify_cycle`，调用方只能提供证据。**不可判定 ≠ 没变**：缺"之前"的达成度快照时算不出差值，落 `no_change` 而非 `improved`，且前值留空让读的人看得出这条缺基线（`::TestUndecidableIsNotNoImprovement`）。**转换层此前无覆盖**，补 `::TestEntityConversion`：领域模型 ↔ 持久化实体往返逐项相等（`test_roundtrip_preserves_everything`）、十进制往返不掉精度（`test_roundtrip_preserves_exact_decimals`）、JSON 里存字符串而非浮点数（`test_decimals_are_stored_as_strings_not_floats`）、进行中的周期拒绝还原（`test_open_cycle_cannot_be_restored`）、**缺计数时拒绝还原而不是默认补零**（`test_missing_counts_is_rejected_rather_than_defaulted_to_zero` —— 全 0 的计数是一个**断言**，而断言必须有依据）、证据引用必须是字符串列表（`test_malformed_evidence_refs_are_rejected` / `test_non_string_evidence_refs_are_rejected` —— 把字符串当序列拆开会得到 `('a','r','t')` 这种"看起来有证据"的结果）。另 `tests/integration/test_entity_crud.py` 增两条 DB 层断言：绕过应用层写一个自造的 `outcome` / `status` 字符串被 CHECK 约束拒绝。**反向验证**（证明断言有牙而非恒真）：对生产代码做 13 处定点变异，每次都让对应用例转红 —— ① 达成度相等也算改善、② 没完成任务也算有产出、③ 周期没跑完也算有结论、④ 缺基线时编成改善、⑤ 差值方向反转、⑥ 缺数据当成差值为零、⑦ 结局写死成 improved（**这一条最初没被抓住**，补 `test_build_derives_the_outcome_from_the_evidence` 后才变红 —— 说明"只断言能得出 improved"是不够的）、⑧ 达成度不再限制在 [0,1]、⑨ 结束早于开始也放过、⑩ 证据引用不校验类型、⑪ 缺计数时默认补零、⑫ 任务按项目数而不是按周期数、⑬ 候选计数改用项目口径；13 处全部被抓住，生产文件还原后与原文逐字节一致。

### [x] TASK-P1-071 Phase 1 指标三层子集

- **工作流**：EV
- **设计要求**：R-117, R-118（§10 四级指标；不得以任务数/Token 数/提交量为核心指标）
- **依赖**：TASK-P1-070
- **交付物**：`evaluation/` —— 阶段一实现"自主发现 / 决策质量 / 工程交付"三层的可算子集：有效问题发现率、重复发现率、决策采纳率、误报率（**需人工标注基线，见 TASK-P1-083**）、任务成功率、返工率、回归缺陷率；"项目演进"层只做达成度差值，不做单位成本收益（阶段四）
- **验证（T1）**：pytest —— ① 每个指标的计算函数有已知输入的期望值断言；② 任务数 / Token 数 / 提交量**不在**核心指标集合中（断言指标注册表中不含这三项）
- **自主权**：L2
- **预估**：0.75d
- **验证结果**：新增 `evaluation/` 包（`common.py` / `registry.py` + 四个子包），110 个单元测试（`tests/unit/evaluation/`）+ 11 个集成测试（`tests/integration/test_metric_inputs.py`）。**指标层不碰数据库** —— 计算是纯函数，输入是若干条记录，输出带上下文的结果对象；这样每个指标都能单独测（给它一组已知输入，断言它算出什么）。**三态而非两态**（`evaluation/common.py::MetricState`）：`computed` / `no_samples`（连样本都没有）/ `baseline_missing`（有样本但缺 ground truth）。最后两种状态下 `value` 为 `None` —— "算不出来"与"算出来是 0"在报表上长得一模一样，而含义相反，报 0 会让看板显示"误报率 0%"，读的人会以为系统很准（TASK-P1-014 的"不可判定 ≠ 未触发"在这里是同一条规矩）。`rate()` 是这条规矩的执行点：`::TestRateRefusesToInventANumber` 三条断言（空分母 → `value is None`、调用方可指定 `baseline_missing`、**分子分母仍要报出** —— 算不出来不等于信息全丢）。T1 ① —— 每个指标各有**已知输入 → 已知期望值**的断言：`valid_discovery_rate`（`::TestValidDiscoveryRate`，4 个已标注候选 3 有效 → 0.75；**未标注的不进分母** —— 它们既不能算对也不能算错，进分母会让指标随标注进度漂移；无标注 → `baseline_missing`；全无效 → **值是 0 但状态是 computed**，与"没标"必须区分）、`duplicate_discovery_rate`（`::TestDuplicateDiscoveryRate`，共享证据引用即重复；**一条候选引用多次证据不算自己跟自己重复**）、`decision_adoption_rate`（`::TestDecisionAdoptionRate`，**分母只算 `record` 决策** —— defer / reject 本来就不是"要执行它"的提议，把它们算进去，指标就变成了"系统动作的激进程度"，一个什么都 reject 的系统会拿满分）、`false_positive_rate`（`::TestFalsePositiveRate`，**分母只算已标注的 record 决策** —— 被 reject 的无效候选没有任何损失，把它算进误报率等于因为系统**没有**浪费资源而惩罚它）、`task_success_rate`（**分母是全部任务** —— 只拿"跑完的任务"当分母，会让一个跑不完就放弃的系统显示出很高的成功率）、`rework_rate`（返工按**任务**算，返工次数的**总和**另放进 `detail` —— "有多少任务返过工"是率，"返了多少次"是量）、`regression_defect_rate`（**分母是已完成任务** —— 一个没完成的任务不构成交付，谈不上"引入了回归"）、`attainment_delta`（**取平均而不是求和** —— 求和会随周期数线性增长，看不出"每一轮平均好了多少"；**缺基线的轮次不进平均** —— 把它们当成 0 参与平均等于宣称"那一轮没有任何改善"；负值**不截断成 0**，否则"系统在变差"会显示成"没变化"）。T1 ② —— `::TestTheActivityCountsAreNotCoreMetrics` 断言 16 个写法（`task_count` / `token_count` / `commit_count` / `total_task_count` / `daily_token_count` / `num_tasks` / `task_number` / `tasks_created` / `tasks_dispatched` / `token_usage` / `tokens_used` / `total_tokens` / `commit_frequency` / `commits_sent` …）**既不在注册表里，登记时还会被拒绝**（抛 `ForbiddenMetricError`）。**只断言"初始集合里没有"是不够的** —— 那只防住"一开始就没加"；R-117 要防的是"后来被加进去"：某个指标好算就被加进看板，看板上的数字被当成成绩，系统于是开始优化那个数字。**任务数是最容易优化、也最不该被优化的东西** —— 让 Agent 多派任务，多派任务不费什么力气。所以注册表**主动拒绝**，且拒绝后不留半条登记（`test_the_registry_rejection_does_not_leave_a_half_registered_metric`）。匹配规则按**主体词 + 量词**两段判定（`task`/`token`/`commit` 且 `count`/`usage`/`total`/`number`/…），而不是死记三个精确名字 —— 列不完的变体说明匹配方式本身选错了；而"同时"二字是关键：R-117 禁的是"任务**数**"，不是"任务"，"任务成功率"恰恰是核心指标（`::TestTheBoundaryOfTheRule` 同时守住两侧）。**反向验证**（证明断言有牙而非恒真）：对生产代码做 17 处定点变异，每次都让对应用例转红 —— ① 空分母返回 0 而不是 `None`、② 分子分母颠倒、③ 忽略调用方指定的空状态、④ 有效率分母用全部候选、⑤ 缺标注时不再区分 `baseline_missing`、⑥ 重复发现把自己与自己算重复、⑦ 采纳率分母用全部决策、⑧ 误报率分母用全部 record 决策、⑨ 把被 reject 的无效候选也算成误报、⑩ 任务成功率分母只算已完成任务、⑪ 返工率分子用返工总次数、⑫ **缺缺陷数据时折成 0**、⑬ 回归缺陷率分母用全部任务、⑭ 达成度差值求和而不求平均、⑮ 缺基线的轮次按 0 参与平均、⑯ **取消 R-117 拒绝**、⑰ 只看主体词丢掉量词那一半；17 处全部被抓住，生产文件还原后与原文逐字节一致。**集成测试证明的是"形状对得上"**（`tests/integration/test_metric_inputs.py`）：指标输入的每个字段都有确定的来源 —— 证据引用取自 `evolution_candidate.evidence` 的 `ref` 键、决策三态取自 `evolution_decision.decision`（指标层按 `record` 过滤，字符串对不上过滤就静默失效）、完成状态取自 `task.status`、达成度快照取自周期的 `metrics` JSON。**取不到的数据，指标必须说"取不到"**：没有人工标注 → 有效率与误报率报 `baseline_missing`；**阶段一的库里没有缺陷表 → 回归缺陷率报 `baseline_missing`，绝不报 0** —— "回归缺陷率 0%"与"没人查过缺陷"在报表上长得一模一样，而前者会让读的人以为这批交付是干净的，一个**假的安全信号**比"缺数据"危险得多。为此 `TaskOutcome.introduced_defects` 的类型是 `int | None`（`None` = 缺陷数据尚不可得，不是"没有缺陷"），分母只算**缺陷数据可得**的已完成任务 —— "查过了没查出缺陷"是知识，"没查过"不是。

### [ ] TASK-P1-072 成本、时延与覆盖率基线采集

- **工作流**：EV
- **设计要求**：R-018, R-039, I-06, I-13, 盲区 F/G（§10 单位成本收益需要基线）
- **依赖**：TASK-P1-035, TASK-P1-041
- **交付物**：`evaluation/baselines.py` —— 在试点仓库上跑 10 个代表性任务，记录单任务 Token 消耗、成本、时长、工具调用数、覆盖率分布，产出基线表 `docs/evaluation/baselines.md`
- **验证（T1）**：pytest —— ① 基线表可被程序读取并用于阈值比较；② 基线缺失时相关 T2 指标明确报"基线未建立"而非返回 0
- **验证（T2）**：基线表中位数为后续成本预算提供依据（阶段一用于 `max_daily_cost` 校准）
- **自主权**：L2
- **预估**：0.5d

### [ ] TASK-P1-073 执行轨迹与 task_id 关联

- **工作流**：EV
- **设计要求**：R-004, R-062（§8 Langfuse；轨迹与审计对齐）
- **依赖**：TASK-P1-005, TASK-P1-030
- **交付物**：`infrastructure/observability/tracing.py` —— 每个 Agent 会话、验证运行、决策节点都带 `task_id`/`cycle_id`/`agent_session_id` 写入 OTel/Langfuse
- **验证（T1）**：pytest —— ① 一次任务执行后可按 `task_id` 检索到完整轨迹；② 轨迹中的工具调用数与 `ExecutionEvent` 记录数一致
- **自主权**：L2
- **预估**：0.25d

---

### 2.9 里程碑与验收（MS）

### [ ] TASK-P1-080 M0：骨架可运行

- **工作流**：MS
- **设计要求**：R-007, R-106（§8 技术栈落地）
- **依赖**：TASK-P1-001 ~ TASK-P1-007
- **交付物**：`make up` 一键起全栈；迁移可跑；`/healthz` 通过；CI 绿
- **验证（T1）**：`make up && make test` 全新环境一次通过；`alembic upgrade head` 幂等
- **验证（T2）**：从零到全栈就绪 < 90s
- **自主权**：L2
- **预估**：0.25d

### [ ] TASK-P1-081 M1：单轮闭环

- **工作流**：MS
- **设计要求**：R-108（§9 阶段一交付物）
- **依赖**：TASK-P1-001 ~ TASK-P1-073（除 P1-082/083 外全部）
- **交付物**：手工注入一个 Candidate，走完 决策 → 任务 → 执行 → 独立验证 → 记录 全链；`docs/demo/m1-walkthrough.md` 记录逐步过程与截图
- **验证（T1）**：回放 runner（`TASK-P1-065`）跑通该链；每一步的 DB 记录可查
- **验证（T2）**：单轮闭环端到端耗时与成本落在 `TASK-P1-072` 基线的 2 倍以内
- **验证（T3）**：demo —— 演示"从候选到验证通过"的完整链路
- **自主权**：L2
- **预估**：0.5d

### [ ] TASK-P1-082 M2：自主闭环验收

- **工作流**：MS
- **设计要求**：R-109, R-128（§9 阶段一验收：不给具体开发任务，只给项目目标）
- **依赖**：TASK-P1-081
- **交付物**：在试点仓库上只提供项目目标 YAML，系统自主完成一轮；产出 `docs/demo/m2-autonomous-cycle.md`，含候选、决策、任务、验证证据、达成度差值
- **验证（T2）**：① 系统自主产出的候选数 ≥ 1 且全部有证据引用；② 至少 1 个候选走了 defer/reject 路径（证明"不行动"能力真实生效）；③ 完成的变更通过了独立验证（VerificationRun 存在且 result = passed）；④ 目标达成度差值可计算（> 0 或明确记录"无改善"）
- **验证（T3）**：评审 —— 人工确认候选是**真实问题**而非编造；这一步的结论记为标注样本，供 `TASK-P1-071` 的误报率使用
- **自主权**：L2
- **预估**：0.5d

### [ ] TASK-P1-083 试点仓库选取与误报率标注基线

- **工作流**：MS
- **设计要求**：R-109, R-118, 盲区 B（§10 误报率需 ground truth）
- **依赖**：无（应最先启动）
- **交付物**：`docs/evaluation/pilot-repo.md` —— 选定 1 个真实仓库作为试点（要求：有可运行的自动化测试、有明确目标可量化、规模适中、依赖已安装）；附初始质量基线（覆盖率、测试数量、已知缺陷清单）；并对 20 个历史问题/缺陷做"是否为有效发现"的人工标注，作为误报率的分母，存为 `evaluation/labels/pilot_labels.jsonl`
- **验证（T1）**：pytest —— ① 标注集可被程序读取，每条含 `issue_ref` / `is_valid_finding` / `category` / `annotated_by` / `annotated_at`；② 条数恰为 20 且 `is_valid_finding` 为布尔；③ 误报率指标的分母取自该标注集，且标注集缺失时指标报"基线未建立"而非返回 0
- **验证（T4）**：试点仓库文档含目标 YAML、基线指标、标注样本集
- **验证（T3）**：人工评审确认试点仓库的测试套件能在沙箱内稳定运行（无外网依赖或依赖已镜像）
- **自主权**：L2
- **预估**：0.25d

---

## 3. 阶段二~四骨架

阶段二~四保持**里程碑粒度**而非任务粒度。原因是这三阶段依赖的设计决策尚未确定（多执行器契约的最终形态、Temporal 迁移路径、模型路由策略），过早拆到任务级会产生大量返工。

### 3.1 阶段二：自主研发能力增强

**阶段目标**：从"一个执行器跑通闭环"扩展到"多个执行器、多任务并行、独立验证、失败自愈"，并让项目认知沉淀下来。

| ID | 里程碑 | 交付物 | 验收条件 | 验证 | 追溯 |
| --- | --- | --- | --- | --- | --- |
| `TASK-P2-010` | 多执行器接入 | Codex / OpenCode / Gemini CLI / OpenHands Adapter | 每个新 Adapter 通过 `TASK-P1-030` 的**同一套契约测试**，无需修改契约 | T1 | R-041, R-043 |
| `TASK-P2-011` | 动态执行器选择 | 按任务类型 / 成本 / 能力声明的选择器 | 同一任务类型在多次运行中可能选中不同执行器，选择依据可解释并落库 | T1 + T3 | R-038, R-111 |
| `TASK-P2-012` | 多任务依赖管理 | 依赖图调度、DAG 执行、依赖失败的传播 | 依赖未满足的任务不启动；上游失败时下游进入正确状态而非误跑 | T1 | R-056 |
| `TASK-P2-013` | 多 Agent 并行执行 | 并发调度、并发上限、资源隔离、事件采集器 | 并行任务的工作空间互不干扰；并发数不超过 `max_parallel_agents` | T1 + T2 | R-013, R-017, R-045 |
| `TASK-P2-014` | 独立验证引擎完整化 | 六层验证：功能 / 代码 / 架构 / 安全 / 性能 / 产品 | 每层验证有独立执行器与独立判定；六层结果独立记录而非合并为一个布尔值 | T1 + T2 | R-050, R-077 |
| `TASK-P2-015` | 自动失败诊断与修复 | 失败归类器 + 修复任务生成 | 阶段一已有的有界修复扩展到多失败类型；修复上限仍全局生效 | T1 | R-098, I-04 |
| `TASK-P2-016` | 项目知识库与架构认知 | 架构知识图谱、ADR 存储、模块依赖索引 | 系统能回答"某模块被哪些模块依赖"且答案来自已构建的索引而非每次重读仓库 | T1 + T2 | R-064, R-067, R-068 |
| `TASK-P2-017` | 跨任务集成引擎 | Integration Branch、冲突解决、跨任务回归测试 | 构造两个各自通过单任务测试但集成后失败的任务 → 集成阶段能检出 | T1 | R-056, R-057, R-058 |
| `TASK-P2-018` | 执行器能力评测 | 同一任务的跨执行器结果对比与打分 | 同一任务在不同执行器上跑 ≥3 次，结果可按统一口径比较（成功率 / 成本 / 耗时 / 质量） | T2 | R-110, R-111, I-17 |

**出口门 `MS-P2-M1`**：不同执行器能完成同一种任务契约，执行结果可以比较和评估（R-111）。
**关键判据**：`TASK-P2-018` 的评测数据显示不同执行器在至少一项指标上存在**统计上可辨的差异**——否则"可替换执行器"只是名义上的。

---

### 3.2 阶段三：持续运行和自演进

**阶段目标**：把闭环从"一次跑完"变成"长期自己转起来"——事件驱动、定时扫描、跨天恢复、资源可控。

| ID | 里程碑 | 交付物 | 验收条件 | 验证 | 追溯 |
| --- | --- | --- | --- | --- | --- |
| `TASK-P3-010` | Temporal 长期工作流 | `ProjectEvolutionWorkflow` + 子工作流；循环 A/B 的 Temporal 化 | 迁移后行为等价：回放 runner（`TASK-P1-065`）在 LangGraph 与 Temporal 两种编排下产出相同状态序列 | T1 | R-088, R-089, R-090, R-125 |
| `TASK-P3-011` | 事件驱动六触发源 | 定时 / 代码 / 质量 / 运行 / 外部 / 知识 六类触发器 | 六类事件各能触发一轮演进；同一事件去重后只触发一次 | T1 + T2 | R-079, R-080, I-11 |
| `TASK-P3-012` | 定时项目扫描 | 跨模块、长期积累问题的定期扫描 | 定期扫描能发现事件触发发现不了的问题（跨模块耦合类） | T1 + T2 | R-080 |
| `TASK-P3-013` | 演进机会发现流水线 | Observe 层六类数据源接入 + Discover 层 | 六类数据源各接入 ≥1 个真实来源；候选的 evidence 指向真实可解析的证据 | T1 | R-028, R-024 |
| `TASK-P3-014` | 自动优先级决策 | 价值模型 + 用实际收益校准 | 校准后模型的排序与"事后实际收益"有正相关（Spearman ρ > 0.3），否则退回启发式模型并记录 | T2 | R-031, R-033 |
| `TASK-P3-015` | 预算与资源控制 | 跨周期预算、成本归集、并发限流 | 连续运行期间每日成本不超过 `max_daily_cost`；超限时优雅停止而非崩溃 | T1 + T2 | R-013, R-018 |
| `TASK-P3-016` | 断点恢复 | 进程重启 / 机器重启后从检查点恢复 | 在执行中强杀进程，重启后任务状态可恢复且不重复执行已完成的副作用 | T1 | R-091, R-095, R-099 |
| `TASK-P3-017` | 长期执行观测 | 跨周期趋势看板（达成度、质量、成本） | 能导出 ≥7 天连续运行的四层指标趋势 | T2 | R-117, R-118 |
| `TASK-P3-018` | 连续 10 周期长跑 | 10 个有效演进周期的运行记录 | 连续 10 轮无人工派发任务；每轮产出可验证证据；达成度呈非负趋势 | T2 + T3 | **R-130**（首个里程碑）、R-112, R-113 |

**出口门 `MS-P3-M1`**：系统能够连续运行多个演进周期，不依赖人工反复输入任务（R-113）。
**关键判据**：`TASK-P3-018` 的长跑记录 —— 注意"有效演进周期"的判定标准需在开工前固定，否则会出现"跑了 10 轮但全是无效修改"的凑数情况。判定标准建议：每轮至少产生 ①一个有证据的候选 ②一个决策 ③一条可验证的改善记录。

---

### 3.3 阶段四：研发控制平面

**阶段目标**：从"管理一个项目"扩展为"管理多个项目的研发基础设施"，并让系统能安全地改进自己。

| ID | 里程碑 | 交付物 | 验收条件 | 验证 | 追溯 |
| --- | --- | --- | --- | --- | --- |
| `TASK-P4-010` | 多项目管理 | Project Registry 完整化、项目隔离 | 两个项目并行演进，任务 / 候选 / 决策互不串扰；预算分别核算 | T1 + T2 | R-016 |
| `TASK-P4-020` | 多模型路由 | 按任务 / 成本 / 能力路由模型 | 不同模型处理不同决策角色（如 Critic 用更强模型）；成本归集到项目维度 | T1 + T2 | R-018, I-13 |
| `TASK-P4-030` | Agent 能力评测 | 执行器 × 模型的二维能力矩阵 | 能力矩阵由实测数据得出，且能指导自动选型 | T2 | R-111, I-17 |
| `TASK-P4-040` | 执行器动态调度 | 基于历史表现的自适应调度 | 调度器在实测中优于静态分配的对照组 | T2 | R-038 |
| `TASK-P4-050` | 自主演进策略优化 | 决策策略的自学习与参数调优 | 调优后达成度或单位成本收益相对基线有统计显著改善 | T2 | R-033, R-121 |
| `TASK-P4-060` | 高风险变更治理 | 架构级变更的独立评审与放行流程 | 标记为高风险的变更 100% 经过独立评审后才可执行 | T1 + T2 | R-061, R-102 |
| `TASK-P4-070` | 系统自身改进提案机制 | `SystemEvolutionProposal` 实体 + 独立测试 + 审批后发布 | 系统提出的自我修改全部走独立测试与审批；**不存在绕过审批的发布路径** | T1 | R-103, R-104 |
| `TASK-P4-080` | A/B 对照实验 | 同仓库同目标的 A 组（人工指定任务）vs B 组（自主发现） | 六项对比指标（达成度 / 缺陷减少 / 工程质量 / 单位成本价值 / 人工干预次数 / 新引入问题）完成统计 | T2 + T3 | R-114, R-115, R-119, R-120, R-121 |

**出口门 `MS-P4-M1`**：成为可以长期管理多个软件项目的自主研发基础设施（R-115）。
**关键判据**：`TASK-P4-080` —— B 组需在六项指标上**持续**更优，而非某一项偶然领先。这是整个系统的证伪标准：若 B 组不优于 A 组，前面三阶段投入的只是"更贵的任务自动化"，不是"自主研发"。

---

### 3.4 阶段间追溯关系

| 阶段 | 依赖的关键阶段一成果 | 若阶段一缺失会怎样 |
| --- | --- | --- |
| 阶段二 | 契约测试基类（`TASK-P1-030`）、三表分离（`TASK-P1-022`）、可复现快照（`TASK-P1-023`） | 多执行器接入无法验证"同一契约"；结果不可比 |
| 阶段三 | 事件去重与因果链（`TASK-P1-064`）、有界修复（`TASK-P1-063`）、审批门（`TASK-P1-046`） | 长期运行会陷入事件回环与无限修复 |
| 阶段四 | policy 外置（`TASK-P1-011`）、append-only 证据（`TASK-P1-024`）、自主权判定器（`TASK-P1-011`） | 系统自我修改无审批基线可挂靠，治理机制无处落地 |

---

## 4. 验证矩阵

对 design.md 逐节穷举出的 **130 条可交付要求**，逐条给出落地任务与验证形态。
覆盖规则：**每条 R-0xx 至少映射到一个任务；每个任务至少声明一个 T1 或 T2**。

**形态分布**（要求侧，一个要求可同时落多个形态）：T1 77 条 · T2 16 条 · T3 16 条 · T4 21 条。
其中 **93/130 条要求有至少一条 T1 或 T2 的验证路径**；其余 37 条只能弱验证（多为设计哲学、职责边界、治理约束），已在 [第 5 节](#5-验证盲区当前阶段无法客观验证的要求) 逐条说明降级方式。
**未覆盖项：0**（自检口径见 §4.9）。

### 4.1 全局架构与技术选型（§1–§2、§8、§11）

| R | 要求 | 任务 | 形态 | 阶段 |
| --- | --- | --- | --- | --- |
| R-001 | 四要素闭环，禁止"超级 Agent" | P1-050, P1-030 | T3 | 1 |
| R-002 | 执行器解耦，不绑定特定 Agent 会话模型 | P1-030, P1-031 | T1 | 1 |
| R-003 | Director 与 Executor 分离 | P1-030, P1-061 | T1 | 1 |
| R-004 | 技术选型组合落地 | P1-003, P1-004, P1-005 | T4 | 1 |
| R-005 | 各技术职责边界不被越界使用 | P1-050, P1-030 | T3 | 1 |
| R-006 | POC 阶段不引入 Temporal | P1-060, P1-061 | T4 | 1 |
| R-007 | 六个子系统的边界 | P1-001, P1-007 | T4 | 1 |
| R-105 | Python 为主语言 | P1-001 | T4 | 1 |
| R-106 | 技术栈清单与优先级 | P1-001 ~ P1-007 | T4 | 1 |
| R-107 | 目录结构骨架 | P1-001 | T4 | 1 |
| R-122 | 优先研究 OpenHands / LangGraph / Temporal | P1-031, P1-050, P3-010 | T4 | 1,3 |
| R-123 | OpenHands 借鉴点（执行基础设施而非 Director） | P1-031, P2-010 | T4 | 1,2 |
| R-124 | LangGraph 借鉴点（决策图 / 状态持久化） | P1-050 | T4 | 1 |
| R-125 | Temporal 借鉴点（长期运行 / 恢复） | P3-010 | T4 | 3 |

### 4.2 Project Control Plane（§3.1）

| R | 要求 | 任务 | 形态 | 阶段 |
| --- | --- | --- | --- | --- |
| R-008 | 六个控制面模块 | P1-010 ~ P1-014 | T1 | 1 |
| R-009 | 目标结构化为六要素 | P1-010 | T1 | 1 |
| R-010 | Key Result 含 id / metric / target | P1-010 | T1 | 1 |
| R-011 | Constraints 须机器可判定 | P1-010, P1-043 | T1 | 1 |
| R-012 | Autonomy 按操作维度设定 | P1-011 | T1 | 1 |
| R-013 | Budget 含日成本与并发上限，可配 | P1-012 | T1 | 1 |
| R-014 | 指标类型 / 单位 / 评测方法 / 币种可配 | P1-010, P1-012 | T1 | 1 |
| R-015 | 目标由人定、差距由 Agent 识、边界由系统强制 | P1-011 | T3 | 1 |
| R-016 | Project Registry 管多项目 | P1-013 | T1 | 1,4 |
| R-017 | Event Collector 采五类事件 | P2-013, P3-013 | T1 | 2,3 |
| R-018 | Budget 管 Token / 调用 / 资源 / 时间 | P1-012, P3-015 | T2 | 1,3 |
| R-019 | Autonomy Manager 管自主程度与审批级别 | P1-011 | T1 | 1 |

### 4.3 Autonomous Director 与 Evolution Engine（§3.2、§3.3、§5）

| R | 要求 | 任务 | 形态 | 阶段 |
| --- | --- | --- | --- | --- |
| R-020 | 多个逻辑角色，而非单一 Director Prompt | P1-057 | T1 | 1 |
| R-021 | 角色作为独立决策任务 | P1-050, P1-057 | T3 | 1 |
| R-022 | Director 每轮回答五问 | P1-051 | T4 + 盲区 A | 1 |
| R-023 | 能识别 TODO 之外的真实下一步 | P1-052 | T3 + 盲区 A | 1 |
| R-024 | 五类演进机会分类法 | P1-052 | T1 | 1 |
| R-025 | 技术趋势须先证明对本项目有收益 | P1-052 | T3 | 1 |
| R-026 | 综合目标 / 风险 / 成本 / 收益决策 | P1-053, P1-054 | T3 + 盲区 A | 1 |
| R-027 | Observe / Discover / Prioritize 三层 | P1-061, P1-052, P1-053 | T1 | 1 |
| R-028 | Observe 覆盖六类数据源 | P3-013 | T1 | 3 |
| R-029 | Candidate 统一结构 | P1-052 | T1 | 1 |
| R-030 | Candidate ≠ Task | P1-022, P1-054 | T1 | 1 |
| R-031 | 六因子优先级公式 | P1-053 | T1 | 1 |
| R-032 | 归一化 0~1，成本设下限 | P1-053 | T1 | 1 |
| R-033 | 价值模型须用实际收益校准 | P3-014, P4-050 | T2 | 3,4 |
| R-034 | "不行动"是合法决策 | P1-054 | T1 | 1 |
| R-035 | Record / Defer / Reject 三态 | P1-054 | T1 | 1 |
| R-081 | 五阶段决策链 | P1-050 | T1 | 1 |
| R-082 | 每阶段结构化输出且下游可独立验证 | P1-050, P1-051 | T1 | 1 |
| R-083 | project_assessment 结构 | P1-051 | T1 | 1 |
| R-084 | 跨方案优先级判断能力 | P1-053, P1-082 | T3 + 盲区 A | 1 |
| R-085 | 独立 Critic | P1-055 | T1 | 1 |
| R-086 | Critic 七项检查 | P1-055 | T1 | 1 |
| R-087 | Critic 须做事实核验 | P1-055 | T1 | 1 |

### 4.4 Agent Execution Plane（§3.4）

| R | 要求 | 任务 | 形态 | 阶段 |
| --- | --- | --- | --- | --- |
| R-036 | 统一执行器协议，不内嵌进 Director | P1-030 | T1 | 1 |
| R-037 | Adapter 七方法签名 | P1-030 | T1 | 1 |
| R-038 | 补齐认证 / 超时 / 能力 / 取消 / 幂等 / 错误分类 | P1-030 | T1 | 1 |
| R-039 | 统一执行结果 schema | P1-030, P1-035 | T1 | 1 |
| R-040 | self_assessment 不是验收结果 | P1-030, P1-041 | T1 | 1 |
| R-041 | 候选执行器清单与接入方式 | P1-031, P2-010 | T4 | 1,2 |
| R-042 | 优先评估 OpenHands Automation / SDK | P1-031 | T4 | 1 |
| R-043 | Adapter 分层，不强制统一底层协议 | P1-030, P1-031 | T1 | 1 |
| R-044 | ACP / MCP 边界澄清 | P1-030 | T4 | 1 |

### 4.5 Engineering Harness（§3.5）

| R | 要求 | 任务 | 形态 | 阶段 |
| --- | --- | --- | --- | --- |
| R-045 | 每任务独立工作空间 | P1-032 | T1 | 1 |
| R-046 | Worktree 必选、Docker 首选、Daytona/K8s 后续 | P1-006, P1-032 | T4 | 1,4 |
| R-047 | 单机 POC 与多机生产方案分层 | P1-006 | T4 | 1,4 |
| R-048 | 禁访问宿主 SSH / 云密钥 / 生产库 | P1-006, P1-032, P1-043 | T1 | 1 |
| R-049 | 验证引擎独立于执行器 | P1-041 | T1 | 1 |
| R-050 | 六层验证能力 | P2-014 | T1 | 2 |
| R-051 | Acceptance Contract 执行前定义 | P1-040, P1-062 | T1 | 1 |
| R-052 | 契约结构（required/thresholds/forbidden/artifacts） | P1-040 | T1 | 1 |
| R-053 | required 至少含四项检查 | P1-040 | T1 | 1 |
| R-054 | forbidden 至少含三项检查 | P1-040, P1-043 | T1 | 1 |
| R-055 | 覆盖率阈值按模块基线，不得靠降质量达成 | P1-042 | T2 + 盲区 C | 1 |
| R-056 | 集成流水线顺序 | P1-047, P2-017 | T1 | 1,2 |
| R-057 | 必须做跨任务集成测试 | P2-017 | T1 | 2 |
| R-058 | 独立 Integrator 解决冲突并回归 | P2-017 | T1 | 2 |
| R-059 | 独立执行身份与任务授权范围 | P1-044 | T1 | 1 |
| R-060 | 禁生产 / 网络白名单 / 短期令牌 | P1-002, P1-043 | T1 | 1 |
| R-061 | 高风险操作独立授权 | P1-044, P4-060 | T1 | 1,4 |
| R-062 | 每次工具调用记录主体 / 任务 / 资源 / 结果 | P1-045, P1-073 | T1 | 1 |
| R-063 | Agent 身份不能替代任务身份 | P1-044 | T1 | 1 |

### 4.6 Project Knowledge & State（§3.6）

| R | 要求 | 任务 | 形态 | 阶段 |
| --- | --- | --- | --- | --- |
| R-064 | 不每轮重读仓库重建认知 | P2-016 | T3 | 2 |
| R-065 | 四类存储 | P1-003, P1-004, P1-020 | T1 | 1 |
| R-066 | 四类存储职责分离 | P1-020, P1-024 | T3 | 1 |
| R-067 | 不全塞向量库，不用对话历史当事实 | P2-016 | T1 | 2 |
| R-068 | 十二个核心实体 | P1-020 | T1 | 1 |
| R-069 | 各实体关键属性 | P1-020 | T1 | 1 |
| R-070 | 三个关键实体分离 | P1-022 | T1 | 1 |
| R-071 | Artifact 带 hash | P1-004, P1-024 | T1 | 1 |

### 4.7 两个演进循环与编排（§4、§6）

| R | 要求 | 任务 | 形态 | 阶段 |
| --- | --- | --- | --- | --- |
| R-072 | 两个相互独立但可相互触发的循环 | P1-060, P1-061, P1-064 | T1 | 1 |
| R-073 | 循环 A 节点链 | P1-060 | T1 | 1 |
| R-074 | 循环 B 节点链 | P1-061 | T1 | 1 |
| R-075 | 两循环不得合并为一个无限 Prompt | P1-060, P1-061 | T3 | 1 |
| R-076 | 七种任务类型 | P1-056 | T1 | 1 |
| R-077 | 不同任务类型走不同验收流程 | P2-014 | T2 | 2 |
| R-078 | 两循环六维差异 | P1-060, P1-061 | T4 | 1 |
| R-079 | 六种事件触发源 | P3-011 | T1 | 3 |
| R-080 | 定期扫描与事件触发结合 | P3-011, P3-012 | T4 | 3 |
| R-088 | Temporal + LangGraph 双层编排 | P3-010 | T4 | 3 |
| R-089 | Workflow 骨架 | P3-010 | T1 | 3 |
| R-090 | Workflow 确定性、副作用入 Activity | P3-010 | T1 | 3 |
| R-091 | 重试 / 超时 / 恢复机制 | P3-010, P3-016 | T2 | 3 |
| R-092 | 任务状态机 14 态 | P1-021 | T1 | 1 |
| R-093 | Re-plan / Re-evaluate 迁移动作 | P1-021 | T1 | 1 |
| R-094 | 同一任务重复提交幂等 | P1-021, P1-025 | T1 | 1 |
| R-095 | 中断后可恢复或重新执行 | P1-034, P3-016 | T1 | 1,3 |
| R-096 | 取消传播到执行器与 Sandbox | P1-033 | T1 | 1 |
| R-097 | 超时不等于失败 | P1-034 | T1 | 1 |
| R-098 | 修复有次数 / 成本 / 时间上限 | P1-063 | T2 | 1 |
| R-099 | 验收证据不因下一轮丢失 | P1-024, P1-048 | T1 | 1 |

### 4.8 自主权限、指标与实施（§7、§9、§10、§12）

| R | 要求 | 任务 | 形态 | 阶段 |
| --- | --- | --- | --- | --- |
| R-100 | 四级自主权定义 | P1-011 | T1 | 1 |
| R-101 | 初始权限基线 | P1-011 | T1 | 1 |
| R-102 | 不得自主修改控制自身的系统 | P1-011, P4-070 | T3 | 1,4 |
| R-103 | System Evolution Proposal 机制 | P4-070 | T1 | 4 |
| R-104 | 防止系统自改验收标准 | P1-011, P4-070 | T3 | 1,4 |
| R-108 | 阶段一交付物 | P1-080 ~ P1-083 | T1 | 1 |
| R-109 | 阶段一验收（只给目标不给任务） | P1-082 | T2 | 1 |
| R-110 | 阶段二交付物 | P2-010 ~ P2-018 | T1 | 2 |
| R-111 | 阶段二验收（结果可比较可评估） | P2-011, P2-018 | T2 | 2 |
| R-112 | 阶段三交付物 | P3-010 ~ P3-018 | T1 | 3 |
| R-113 | 阶段三验收（连续多周期） | P3-018 | T2 | 3 |
| R-114 | 阶段四交付物 | P4-010 ~ P4-080 | T1 | 4 |
| R-115 | 阶段四验收 | P4-080 | T3 | 4 |
| R-116 | 工期估算性质说明 | §2 工期校正 | T4 | 1 |
| R-117 | 不得用任务数 / Token 数 / 提交量作核心指标 | P1-071 | T1 | 1 |
| R-118 | 四级指标体系 | P1-071, P2-018, P3-017, P4-080 | T2 | 1–4 |
| R-119 | A/B 对照实验 | P4-080 | T2 | 4 |
| R-120 | 六项对比指标 | P4-080 | T2 | 4 |
| R-121 | B 组持续更优（证伪标准） | P4-080 | T2 | 4 |
| R-126 | 三项核心壁垒 | 贯穿 P1-050 / P1-060 / P1-061 | T3 | 1–4 |
| R-127 | Understand → Decide → Verify 闭环 | P1-050, P1-060, P1-061 | T3 | 1 |
| R-128 | 每轮产生可验证的改善 | P1-070, P1-082 | T2 | 1 |
| R-129 | 五层 Harness 视角 | §3.4 映射说明 | T4 | 2–4 |
| R-130 | 首个里程碑：连续 10 个有效演进周期 | P3-018 | T2 | 3 |

### 4.9 覆盖自检口径

| 检查项 | 期望 | 实测 |
| --- | --- | --- |
| R-0xx 总数 | 130 | 130 ✓ |
| 至少映射一个任务的 R-0xx | 130（缺口 0） | 130 ✓ |
| 至少一个 T1/T2 验证的任务（阶段一） | 55/55（缺口 0） | 55 ✓ |
| 至少一个 T1/T2 验证的阶段二~四里程碑 | 26/26（缺口 0） | 26 ✓ |
| 至少一个 T1/T2 验证路径的要求 | — （其余走弱验证，见第 5 节） | 93/130 |
| 依赖中引用了不存在的任务 ID | 0 | 0 ✓ |
| 重复的任务 ID | 0 | 0 ✓ |
| 同一 R-0xx 映射到 >1 个任务 | 逐条复核（允许，需理由） | 已复核 |

---

## 5. 验证盲区：当前阶段无法客观验证的要求

design.md 的核心主张是"每一轮演进都应产生**可验证的改善**"。但其中一部分要求**在给定阶段无法被客观验证**。把它们伪装成可测试需求，会让验收用弱证据冒充强证据 —— 这恰恰违背系统自身的主张。

以下每条给出：真实要求 → 可做到的验证 → 做不到的验证 → 降级处理。

### A. 决策质量（最大盲区）

**涉及**：R-022（五问）、R-023（不只是遍历 TODO）、R-026（综合成本收益决策）、R-031/R-032（价值模型）、R-084（跨方案优先级判断）、R-086/R-087（Critic 七问）

- **能做到**：断言五阶段结构存在且各阶段产出可解析；断言 Critic 的九项检查**被执行**（调用记录）。
- **做不到**：断言 Critic **有效拦截了坏决策**、断言 Director 选的是"最值得做"的问题。这是 LLM 判断质量，没有 ground truth。
- **降级**：阶段一标注为 `T1（结构与流程）+ T3（评审有效性）+ 盲区 A`；效果验证推迟到阶段四 A/B 对照（`TASK-P4-080`）。**阶段一验收结论中必须显式写明"本阶段不验证决策质量"**。

### B. 发现的问题是否真实

**涉及**：R-030、R-070、R-087

- **能做到**：断言 candidate / decision / task 是三张独立的表，且 Task 必须由 Decision 产生（DB 层强制）。
- **做不到**：断言"这个 candidate 真的对应一个真实问题"。结构的分离不等于内容的真实。
- **降级**：唯一代理是"误报率"，其 ground truth 需人工标注 —— `TASK-P1-083` 建立 20 个标注样本，`TASK-P1-082` 的 T3 评审同时产出新标注。**在标注基线建立前，误报率指标不可用。**

### C. 覆盖率不得靠降低测试质量达成

**涉及**：R-055

- **能做到**：断言阈值按模块基线计算（`TASK-P1-042`）；断言基线不可被下调。
- **做不到**：自动判定"覆盖率上升的同时测试断言被削弱"（删断言、测试文件行数下降、变异存活率上升）。**design.md 没有指定任何检测手段。**
- **降级**：默认走 T3 代码评审；变异测试（mutation testing）作为可选加码项，标注为 design.md 范围外的自选增强。

### D. "不行动"与自我治理的正确性

**涉及**：R-034、R-035、R-104

- **能做到**：断言 `confidence = 0.1` 时系统产出 defer 记录、写入 rationale、**且不创建 Task**（`TASK-P1-054`）。
- **做不到**：断言"这次 defer 是对的"—— 只有事后才知道。同样，无法自动判定"系统自改的验收标准是否真的更宽松"。
- **降级**：结构断言 + 人工评审；R-104 的防护依赖 `TASK-P1-011` 的"治理外置 + 变更需审批"这一**前置机制**（阶段一即生效），而非依赖事后检测。

### E. 长期行为

**涉及**：R-099、R-113、R-130

- **能做到**：断言跨周期后证据未被删除（`TASK-P1-024`）；断言断点恢复（`TASK-P3-016`）。
- **做不到**：断言"连续 10 轮都有效" —— 这需要实际长跑。
- **降级**：写成**可执行的 harness 脚本 + 一次实际长跑记录**（`TASK-P3-018`），而不是 CI 断言。长跑记录需人工确认"有效"的判定标准。

### F. 成本基线缺失

**涉及**：R-018、R-039、R-091、R-120

- design.md 给的 `max_daily_cost: 50` 是示意值，**没有"一个典型任务消耗多少"的基线**。
- **降级**：`TASK-P1-072` 采集基线；在基线建立前，所有成本类 T2 指标报"基线未建立"而非返回 0。

### G. 性能退化判定缺基线

**涉及**：R-050、R-077

- 性能验证与性能优化任务都隐含"不退化"的阈值，但 design.md 未给回归判定规则，也未保证基准环境稳定。
- **降级**：阶段一不做性能层（已在阶段二 `TASK-P2-014`）；进入阶段二前需先补基准环境与判定规则。

### H. 安全隔离的负向证明

**涉及**：R-048、R-060、R-062

- **能做到**：断言容器内无 `~/.ssh` / 云凭据；断言审计记录完整；断言网络白名单外的域名不可达。
- **做不到**：断言"Agent 实际上没有拿到生产凭据" —— 本质是负向证明，只能靠配置断言 + 白盒审计。
- **降级**：标注为**半客观**。审计记录的**覆盖率**（是否漏掉了某些调用路径）需白盒审计确认。

### I. 越权检测

**涉及**：R-101

- **能做到**：断言权限为 L1 时 merge 调用被拒（`TASK-P1-011`）。
- **做不到**：断言"系统没有在 L4 任务中悄悄做了需要 L2 的事" —— 需要运行时拦截审计。
- **降级**：`TASK-P1-044` 的授权判定落到了工具调用拦截点，使越权在**发生时**即被阻断，而非事后审计发现。

### J. A/B 实验的统计显著性

**涉及**：R-119 ~ R-121

- design.md 要求"B 组持续更优"，但未给样本量、周期数、显著性要求。属研究级要求。
- **降级**：阶段一~三只交付"实验框架就绪"（可配置、可复现、可导出六项指标）；统计显著性判定在阶段四补齐。

### K. 目录结构与角色命名的内部矛盾

见 [第 6 节](#6-设计文档待裁决项) 待裁决项 1。

---

## 6. 设计文档待裁决项

design.md 有 **8 处**需要裁决的问题：前 6 处是**内部不一致**（同一件事两处说法互相矛盾），后 2 处是**缺口**（该说的没说，导致实现期无处落笔）。本文每处给出推荐默认，可直接采用或推翻。

另有 **7 处**是实现期才暴露出来的歧义（待裁决 9~15）—— 它们不是 design.md 自相矛盾，而是"设计说得清、但落到具体取值或边界时仍有多解"，或拆解时的依赖关系与实际数据流不符。裁决结论已回写进相应任务的实现与文档。

### 待裁决 1 — Director 角色数量与目录命名不一致

- **矛盾**：§3.2 写"构建**四个**逻辑角色"，随后列出**五个**名字（Project Director / Project Analyst / Product Strategist / Chief Architect / Quality Strategist）；§8 目录 `agents/` 只有五个子目录（analyst / strategist / architect / critic / evaluator），缺 `director/`，且 `architect/` 与 Quality Strategist 语义不符。
- **推荐默认**：**以 §8 目录为准**，补 `agents/director/` 与 `agents/quality/`；阶段一只实现 `analyst` + `director` + `critic` 三个角色（对应 §9 阶段一交付物的 "1 个 Project Director + 1 个 Project Analyzer + 1 个 Critic"），其余三个角色在阶段二补齐。**理由**：§9 阶段一交付物清单是最具体的约束，且三角色已足够跑通"发现→决策→复核"链路；过早实现五角色会引入无验证收益的 Prompt 调优工作。
- **影响任务**：TASK-P1-057, TASK-P1-050

### 待裁决 2 — Harness 分层两套说法不一致

- **矛盾**：§12 末段讲"**五层** Harness 思路"（领域工程 / 能力工程 / 执行工程 / 可靠性工程 / 进化工程），§3.5 讲的是"**四个**子系统"（Workspace / Verification / Integration / Security & Governance），两套分层无映射关系。
- **推荐默认**：**§3.5 四子系统作为实现依据**（它有明确的模块与职责边界），§12 五层作为**目标分类视图**并列映射：领域工程→Knowledge、可靠性工程→Verification、进化工程→Evaluation。**理由**：五层是设计哲学的表述，没有可执行的模块边界；用它做实现依据会导致职责重复。
- **影响任务**：TASK-P1-001（目录结构）

### 待裁决 3 — Critic 七问未覆盖目标六要素中的两项

- **矛盾**：§3.1 定义目标含 `Non-goals` 与 `Stop Conditions`，但 §5 Critic 的七项检查中没有任何一项直接对应它们 —— **字段定义了但检查项没接上**。
- **推荐默认**：**补为第九、十问**（本文已按"九问"实现，见 `TASK-P1-055`）：⑧ 该候选是否触碰项目 Non-goals？⑨ 该候选是否触发 Stop Conditions（应当停止而非继续）？**理由**：这是设计文档的实质漏洞，不是表述差异；且这两项恰好是"人类定义边界、Agent 不越界"的技术保障。
- **影响任务**：TASK-P1-055, TASK-P1-057

### 待裁决 4 — 人工审批机制在编排骨架中缺失

- **矛盾**：§1 表格称 Temporal 负责"人工审批等待"，§7 初始权限把 `merge_to_main` 设为 L2（需人工审批），但 §6 的 Workflow 骨架中**没有任何审批节点**，且阶段一~二无此机制。
- **推荐默认**：**阶段一用 DB-backed 审批门**（`TASK-P1-046`），阶段三升级为 Temporal Signal。**理由**：审批是"合并到 main"的准入条件，不实现则阶段一无法端到端完成闭环；用 Temporal 会把阶段一强绑定到 Temporal，与 §1 "POC 不必引入 Temporal" 的取舍冲突。
- **影响任务**：TASK-P1-046, TASK-P3-010

### 待裁决 5 — 阶段一是否包含独立验证引擎

- **矛盾**：§9 阶段一交付物列"自动化测试"，把"独立验证引擎"列在阶段二；但 §3.5B 明确要求"不要让 Coding Agent 自己决定是否验收通过"。
- **推荐默认**：**阶段一纳入最小独立验证引擎**（`TASK-P1-041`：独立进程运行目标项目测试套件 + 契约检查），完整六层验证留阶段二。**理由**：系统的核心主张是"可验证的改善"，若阶段一由 Coding Agent 自评，闭环验证的是一个未经证伪的主张 —— 这正是本系统声称要避免的问题。
- **影响任务**：TASK-P1-041, TASK-P2-014

### 待裁决 6 — 首个里程碑与阶段划分不对齐

- **矛盾**：§12 最终建议的首个里程碑是"自主管理一个真实代码仓库并**连续完成 10 个有效演进周期**"，但 §9 阶段一只要求"产生下一轮计划"（单轮）。10 周期需要事件驱动与长跑能力，属阶段三特征。
- **推荐默认**：**引入 M0/M1/M2 三级门**（见 §1.1）：M1（单轮闭环）作为阶段一的实际交付边界；**10 周期归入阶段三出口门** `TASK-P3-018`。**理由**：把 10 周期塞进阶段一会诱导为凑数而重复执行低价值改动 —— 这恰好是设计文档警告的"Coding Agent 不断创造新的开发任务，让项目陷入无意义的自我修改"。
- **影响任务**：TASK-P1-081, TASK-P1-082, TASK-P3-018

### 待裁决 7 — §6 状态机的迁移边在文档导出时全部丢失

- **矛盾**：§6「建议的核心状态机」以 mermaid 状态图给出 14 个状态与 `Re-plan` / `Re-evaluate` 两个动作，但**图中的边（迁移）在导出为 Markdown 时全部丢失**（同 §1 架构图、§5 Critic 图一样，只剩下 mermaid 的 CSS 转义残留与散落的文字）。当前 design.md 中实际只剩状态名列表，**没有任何一条可读的迁移定义**。
- **推荐默认**：以 `core/task/state_machine.py` 的 `_TRANSITION_TABLE` 为准，并**把该表回写进 design.md §6**（用文字表格而非 mermaid，避免再次丢失）。回写时的迁移表为：

  | 源状态 | 允许动作 → 目标状态 |
  | --- | --- |
  | DISCOVERED | evaluate → EVALUATING |
  | EVALUATING | defer → DEFERRED；approve → APPROVED |
  | DEFERRED | re_evaluate → EVALUATING |
  | APPROVED | plan → PLANNED；re_evaluate → EVALUATING |
  | PLANNED | ready → READY |
  | READY | start → RUNNING；re_plan → PLANNED；block → BLOCKED |
  | RUNNING | verify → VERIFYING；block → BLOCKED |
  | VERIFYING | pass → PASSED；repair → REPAIRING；block → BLOCKED；re_plan → PLANNED |
  | PASSED | integrate → INTEGRATING；block → BLOCKED；re_plan → PLANNED |
  | REPAIRING | verify → VERIFYING；block → BLOCKED |
  | BLOCKED | re_evaluate → EVALUATING；re_plan → PLANNED |
  | INTEGRATING | merge → MERGED；repair → REPAIRING；block → BLOCKED |
  | MERGED | complete → COMPLETED |
  | COMPLETED | （终态，无出边） |

- **重建依据**：状态名本身的语义；§6 的四条实现细节（幂等、恢复、取消传播、修复三重上限）；R-051（验收契约必须在 READY→RUNNING 之前）；R-034/R-035（defer 后必须能重新评估）；阶段三的重规划需求。**两条实现约定**：① 目标态等于当前态时是幂等空操作（`changed=False`），且该判定**先于**允许集合判定 —— 否则消息重投会直接抛错；② 进入 RUNNING 前必须已有绑定 `task_id` 的验收契约，这是引擎级闸门，不依赖调用方自觉。
- **影响任务**：TASK-P1-021, TASK-P1-054, TASK-P1-058

### 待裁决 8 — §3.6 的十二个实体里没有能承载预算累计额的表

- **矛盾**：§3.6 列出十二个核心实体，覆盖项目、演进、执行、证据、知识五类，但**没有任何一张表能承载"已消耗多少预算"**。§3.1 要求 Budget Manager 管 Token / 模型调用 / 计算资源 / 时间预算，R-013 / R-018 要求币种与指标可配置，而 TASK-P1-012 的 T1 ② 明确要求"用**真实并发写库**验证" —— 这要求一张能承载累计额、且能对其加行级锁的表。
- **推荐默认**：**新增两张表**，作为十二个实体之外的补充：`budget_account`（累计额，主键 `(scope, metric, period)`，靠行级锁串行化扣减）与 `budget_entry`（append-only 流水，每次扣减一条，含被拒绝的尝试）。`scope` 用自由文本列而非项目外键 —— TASK-P1-012 不依赖项目注册，预算口径不该被项目表的生命周期绑住；阶段四多项目时再取 project id。**理由**：把这张表硬塞进现有十二个实体里（例如让 `project.budget` 的 JSONB 兼任账本）会同时失去三样东西 —— 行级锁的粒度（`FOR UPDATE` 锁的是整行，JSONB 里的数字无法单独加锁）、append-only 的可执行性（JSONB 里的数字可以被原地改写，且没有"只增不减"这个概念）、以及账本与项目定义解耦的能力。周期（`day:YYYY-MM-DD` / `lifetime`）做进主键还有一个附带好处：日切不需要删除任何记录，新的一天自然是一行从零开始的累计额 —— 这与 append-only 账本天然相容。
- **影响任务**：TASK-P1-012, TASK-P1-020（实体数由 12 变 14，`docs/architecture/data-model.md` 与 `tests/unit/database/test_schema.py` 已同步）

### 待裁决 9 — TASK-P1-014 ↔ TASK-P1-070 ↔ TASK-P1-071 构成依赖环

- **矛盾**：按原文的依赖字段，三条任务首尾相接 —— TASK-P1-014（Stop Conditions 求值与目标达成度计算）**依赖** TASK-P1-071（Phase 1 指标三层子集），TASK-P1-071 **依赖** TASK-P1-070（EvolutionCycle 记录），而 TASK-P1-070 的交付物里写着"目标达成度变化" —— 那个数字只有 TASK-P1-014 的 `GoalAttainmentCalculator` 算得出来。三条任务构成一个环，按依赖顺序一条都开不了工。这不是实现时的疏漏，是拆解时把"依赖"写成了"数据流上下游"造成的。
- **推荐默认**：**按 014 → 070 → 071 的顺序实现**，并把环上那条"目标达成度变化"降级为 TASK-P1-070 的**可选字段**（有则填，无则留空），使 070 不再硬依赖 014。**理由**：三条任务之间真正的语义边界是 —— 014 提供**计算达成度**的能力（纯函数，无副作用，不依赖指标注册表）；070 负责**记录**一轮演进的元数据与前后达成度差值；071 负责**把达成度差值纳入指标注册表**并与其他指标一起算。达成度的*计算*是 071 用到的**上游**能力，而不是它的**前置条件**：071 完全可以先用"达成度差值"这个占位输入跑通其余六个指标，达成度计算晚一步接上。硬依赖会让一个本可并行的能力被另一个更小的任务卡住 —— 而两个任务加起来不到 1.5d，这个环的代价不值。
- **影响任务**：TASK-P1-014, TASK-P1-070, TASK-P1-071（已按推荐默认实施：TASK-P1-014 已完成；TASK-P1-070 的"目标达成度变化"字段设计为可选；TASK-P1-071 的指标注册表不因缺达成度计算而无法开工）

### 待裁决 10 — `EvolutionCycle.outcome` 一个字段承担了两个维度

- **矛盾**：§3.6 的 `EvolutionCycle` 只点了两个属性 —— `cycle_id` 和 `outcome`，没有说 `outcome` 取什么值、也没有说它有几个维度。但 §12 要求"每一轮演进都应该让项目状态、目标达成度或工程质量得到**可验证的改善**" —— 这要求周期记录能回答"这一轮**有没有变好**"。而 §6 的状态机里，周期本身还有"**怎么**结束"这回事（正常跑完 / 被人工中止 / 被预算掐断 / 被阻塞）。两件事都叫"结局"，但它们是两个维度：一轮被预算掐断的周期，跑完了（虽然是掐断的），但没变好。合成一个字段，就必然丢掉其中一个 —— 要么预算掐断的周期被记成"完成"，要么它被记成"没改善"，而这两个说法都不准确。
- **推荐默认**：**拆成两个字段** —— `status`（周期**怎么**结束：completed / aborted / budget_exhausted / blocked）与 `outcome`（这一轮**有没有变好**：improved / no_change / regressed / stalled），各自带 DB CHECK 约束。**理由**：① 这两个维度回答的是不同的问题，审计时要分别查 —— "上个月有多少轮没跑完"（查 status）与"上个月有多少轮白跑了"（查 outcome）是两个不同的管理问题；② `outcome` 的四个取值里必须有 `no_change` 与 `stalled` 两个"没变好" —— 只留 improved / regressed 时，"这轮啥也没干成"就只能被硬塞进 improved 或 regressed，记录于是开始撒谎，而撒谎的方向恰好是"看起来系统很能干"；③ `stalled` 与 `no_change` 的区别是"什么也没落地"与"落地了但没变好"，前者要复盘的是流程，后者要复盘的是质量 —— 混在一起就都复盘不出来。**代价**：多一列、多一次迁移，且旧数据要按对应表拆开（`migrations/versions/0004_cycle_outcome.py`）。这是本次实现中**唯一一处超出 §3.6 点名属性清单**的 schema 变更，已在 `docs/architecture/data-model.md` §6.3 同步。
- **影响任务**：TASK-P1-070, TASK-P1-071, TASK-P1-072（已按推荐默认实施：`core/metrics/cycle.py` 定义 `CycleStatus` / `CycleOutcome` 两个枚举；`core/entities/evolution.py` 增 `status` 列与两条 CHECK 约束；迁移 0004 把旧行按 completed→completed/no_change、aborted|budget_exhausted|blocked→同 status/stalled 拆开 —— 旧记录从来没有"变好了没有"这个信息，所以除 `completed` 外一律落 `stalled`（而非 `no_change`）：那几轮根本没跑完，"没有可验证的改善"与"什么也没落地"里只有后者是当时的事实）。TASK-P1-071 的指标注册表把 `outcome` 当成一个**取值集合**消费，不依赖它与 `status` 的具体拆分方式）

### 待裁决 11 — §10 指标清单与阶段一"可算子集"的差额：哪些推迟、哪些缺数据源

- **矛盾**：§10 列了四层共十二项指标。TASK-P1-071 的交付物只点名七个指标 + 达成度差值 = 八项，差额里的三项各有各的麻烦：**价值预测偏差**（决策质量层）需要"预测的价值"与"实际兑现的价值"可比，而一个候选被 reject 时无法判定这是"选错了"还是"当时信息不足" —— 阶段一没有额外标注，硬算出来的是噪声；**质量趋势**（项目演进层）需要跨周期的质量基线，而覆盖率基线机制（TASK-P1-042）与基线采集（TASK-P1-072）都还没做；**单位成本收益**（项目演进层）需要每轮的成本与收益配对，收益归因在阶段一没有可辩护的做法。**还有一项不是推迟而是缺源**：**回归缺陷率**实现了，但阶段一的库里**没有缺陷表** —— 缺陷个数没有任何来源，这个指标在阶段一永远算不出数。
- **推荐默认**：三项**显式推迟**（在代码里以文档字符串写明"为什么现在做不了"，而不是留一个占位实现 —— 留占位实现会让看板以为它已经就绪），质量趋势另有一条独立的顾虑：覆盖率作为**核心**指标还能被"降质量达成"（见盲区 C），所以即使基线就位，它是否进入核心指标集也要单独裁决。**回归缺陷率保留实现，但如实报"算不出来"** —— 缺陷数据不可得时报 `baseline_missing` 而非 0，缺陷数据可得的任务才进分母。**理由**：一个指标"算不出来"是可接受的（TASK-P1-072 的成本基线同理，docs/tasks.md §5 已写明"基线建立前，相关 T2 指标报'基线未建立'而非返回 0"）；一个指标"假装算得出来"不可接受 —— 那是在制造假的安全信号。**代价**：回归缺陷率的 T2 阈值在缺陷来源落地之前无法评估（TASK-P1-083 之后），阶段一的相关看板项只能显示"基线未建立"。
- **影响任务**：TASK-P1-071, TASK-P1-042, TASK-P1-072, TASK-P1-083，阶段二~四骨架（已按推荐默认实施：三个子包 `evaluation/{evolution,task,project}_evaluation/` 只实现阶段一的可算子集，`evaluation/code_evaluation/` **有意留空**并在模块文档里写明为什么 —— 覆盖率基线未就绪之前往那里塞一个"覆盖率"指标，只会让它看起来已经可以当核心指标用了；价值预测偏差与单位成本收益的推迟理由写在 `evaluation/evolution_evaluation/decision.py` 与 `evaluation/project_evaluation/attainment.py` 的模块文档里；`TaskOutcome.introduced_defects` 定为 `int | None`，`None` 表示缺陷数据尚不可得）

### 待裁决 12 — §3.4 的 `execute(task)` 不带会话参数，"任务落在哪个会话上"无处可说

- **矛盾**：§3.4 把统一执行器的核心接口定为七个方法，其中 `execute(task)` 只收一个 `task` 参数 —— 但同一个接口里还有 `get_status(session_id)` / `stream_events(session_id)` / `interrupt(session_id)` / `collect_result(session_id)` / `terminate(session_id)` 五个**按会话**寻址的方法。于是"任务"与"会话"的关系在设计里是空的：适配器若只记"最近一个会话"，并发跑两个任务时必然张冠李戴（任务 A 的结果被记到任务 B 的会话上），而"任务"这个参数里又没有地方能说出它属于哪个会话。这个缺口在**单执行器、单任务**的演示里完全看不出来 —— 一旦并行，它就变成一个静默的、随机归属的错误。
- **推荐默认**：**会话由任务自己带过来** —— `ExecutionTask` 增加 `session_id` 字段，`execute(task)` 的签名**一个不动**。**理由**：① 这是七个方法签名里唯一不动的补法 —— 改签名就等于改设计，而设计明确说"这七个方法"，改动会波及阶段二"多个 Adapter 通过同一套契约测试"的验收口径（`TASK-P2-010`）；② 补在**任务**上而不是补在**方法签名**上，是因为任务本来就是"这次要做什么"的载体，会话归属是任务的一个属性，不是调用现场的一个参数；③ 让适配器自己维护"当前会话"是一次隐含的全局状态 —— 而多会话并行（TASK-P1-032 的 worktree 生命周期、TASK-P2-010 的多执行器接入）要求这份状态**不依赖调用顺序**。**代价**：任务对象多一个字段，且调用方必须显式填 —— 但这个"必须显式"恰恰是想要的：填错会话会在契约测试里立刻暴露（`test_the_session_id_is_part_of_the_task`），而隐式记法填错只会静默串台。
- **影响任务**：TASK-P1-030（已按推荐默认实施：`executors/base.py::ExecutionTask` 增加 `session_id` 字段，并在模块文档与类文档里写明这是对 §3.4 的补全；`tests/unit/executors/test_records.py::TestTheTaskCarriesItsSession` 固化这条约定）、TASK-P1-031、TASK-P1-032、TASK-P2-010（真实 Adapter 实现时按同一约定填 `session_id`；契约测试本身不需要改，因为七个方法签名未变）

### 待裁决 13 — 沙箱与工作空间该合成一个包还是拆成两个

- **矛盾**：TASK-P1-032 的交付物写的是 `harness/workspace/` 一个包，但这一条里装了两件**生命周期完全不同**的东西 —— 工作空间（worktree 创建 / 租约 / GC，生命周期以任务为单位）和沙箱容器（镜像 / 网络 / 挂载 / 环境变量，生命周期以**容器运行**为单位）。把启动容器与挂载策略塞进 `harness/workspace/`，这个包名就不再描述它的内容；而 design.md §3.5 把 Workspace 与 Security & Governance 列为**两个**子系统，方向上也是分开的两件事。
- **推荐默认**：**拆成 `harness/workspace/` 与 `harness/sandbox/` 两个包**，本任务按此实施。**理由**：① 两者的**失效模式不同** —— 工作空间出问题会丢产出（分支、目录），沙箱出问题会**越过隔离**（凭据进容器、生产库被连上）。把两种失效放在一个包里，"这里出问题会怎样"就没有唯一答案；② 两者的**耦合方式是数据而不是代码** —— 沙箱要挂载工作空间，但它拿到的是一个**路径**（`build_workspace_sandbox_spec(workspace_path=...)`），不是 `Workspace` 对象；于是两个包**互不 import**，各自能单独测、单独替换（工作空间换成非 git 的实现，沙箱那侧一行不用改）。合成一个包反而会诱导出"沙箱去问管理器要路径"的依赖 —— 那种依赖一旦长出来，工作空间就被绑死在沙箱上；③ 沙箱的策略（`policy.py`）是**安全边界**，它要被 `TASK-P1-043`（安全门）与 `TASK-P1-033`（取消传播要能停掉容器）复用 —— 放在 `sandbox/` 里，这两个任务引用的就是同一个包，而不是"从 workspace 里 import 一个 sandbox 模块"。**代价**：`docs/tasks.md` 这条交付物描述被本次实现修正过（原文只写 `harness/workspace/`），后续 `TASK-P1-033` 的 `harness/sandbox/cancellation.py` 也落在同一个包里 —— 这是**修正**而非**偏离**：任务书的交付物栏是拆解时的初步估计，与 `design.md` 冲突时以设计为准并在此登记。
- **影响任务**：TASK-P1-032（已按推荐默认实施：`harness/workspace/` = 工作空间生命周期，`harness/sandbox/` = 沙箱容器与策略；**两包互不 import**，耦合只经由路径 —— `build_workspace_sandbox_spec` 收的是 `Path` 而不是 `Workspace` 对象）、TASK-P1-033（取消传播落在 `harness/sandbox/cancellation.py`）、TASK-P1-043（安全门复用 `harness/sandbox/policy.py` 的策略原语）

### 待裁决 14 — 谁负责把工作空间切到任务分支上：执行器不建分支，harness 建

- **矛盾**：契约测试（TASK-P1-030）要求**并行两个任务的产出分支必须可区分**，而 TASK-P1-031 的 CLI 适配器报的是**工作空间当前检出的分支**（事实优先，读不出时才退回约定 `agent/<task_id>`）。这两件事放在一起就有一个说不清的缝：契约里那条断言在 CLI 适配器上**只有在工作空间不是 git 仓库时才由约定兜底成立**；若两个并行任务共用一个真实 git 工作空间，它们报出的分支会**一样**。而任务书里没有任何一条说"交给执行器的工作空间必须已经切在任务分支上"—— TASK-P1-032 负责**建** worktree，TASK-P1-031 负责在**给定**的工作空间里跑进程，两者之间的这条衔接**没有明文**。真实 CLI 冒烟把这一点照得很清楚：工作空间在 `main` 上，Agent 就在 `main` 上提交，适配器如实报 `main`。
- **推荐默认**：**执行器不建分支、不切分支，只报告事实；由 harness 在把工作空间交给执行器之前切好分支**（`agent/<task_id>`，TASK-P1-032 的 worktree 生命周期已经这么做）。**理由**：① 产出分支是**事实**，不是执行器的选择 —— 执行器报"仓库现在什么样"才可信；让它顺手把分支切了，它报的就不再是事实而是"我让它做的"；② "一任务一目录一分支"这条不变量只该有**一个**执行点 —— 执行器也能建工作空间，就有了第二个，而两次建出来的分支可能不是同一条，那种不一致要到并行两个任务、产出串到一条分支上时才发现；③ 方向也是反的：工作空间是执行器的**输入**，不是它的产物。**代价**：单独跑执行器（不经 harness）时，工作空间不在任务分支上，产出就会落在别的分支上 —— 这是**调用方的责任**，执行器如实报告，不掩盖。为了让这条边界不靠口头约定，`tests/unit/executors/test_layering.py` 用 AST 把它钉死：执行器**不得** import `harness.workspace.manager` / `leases` / `gc` / `git`，只准引用那张纯约定表 `harness.workspace.naming`（无 IO、无依赖，约定必须只有一份 —— 复述一遍就会漂移）。
- **影响任务**：TASK-P1-031（已按推荐默认实施：`executors/cli/artifacts.py` 只用只读探针读工作空间的 git 状态，分支读不出时退回 `harness.workspace.naming.branch_for_task` 的约定；不 import 任何工作空间管理面）、TASK-P1-030（契约里"并行分支可区分"那条断言在 CLI 适配器上依赖上面这条衔接）、TASK-P1-032（worktree 生命周期是这条衔接的**执行方** —— 每个任务一个目录、一条 `agent/<task_id>` 分支）、TASK-P2-010（多执行器接入时，同一条衔接对所有适配器一致成立）

### 待裁决 15 — codex 骨架"满足契约"与"协议正确"是两件事

- **矛盾**：TASK-P1-031 的交付物写的是"另留 `executors/codex/` 同契约的骨架"。但"同契约"有两种含义：一是**实现同一个 `CodingAgentAdapter` 契约**（七个方法 + 统一结果 + 能力声明 + 幂等键 + 错误分类），二是**说得对 Codex CLI 的真实协议**（命令行怎么拼、事件词汇表是什么、结果事件怎么读）。本机没装 codex，第一种能验证，第二种**验证不了**。若不把这件事说清楚，骨架就会被当成"codex 已经支持了"——而它现在的行为恰恰相反：协议表**刻意留空**，于是任何 codex 运行都会报 `FAILED`（"没有产出结果事件"）。
- **推荐默认**：**骨架只承诺第一种，明确不承诺第二种**。**理由**：把"没结论"报成"失败"而不是"成功"，是**诚实**的失败 —— 拿"进程正常退出"当"这次执行做成了"，正是"什么都没产出却看着像成功"这种假成功的来源；一个诚实的 `FAILED` 会把问题指回"协议表还没填"，而一个虚假的 `COMPLETED` 会一路走到验收环节才炸。**代价**：codex 在填表之前**不可用** —— 这不是缺陷，是"未接入"的准确表述。接入要三步（实测协议 → 填 `executors/codex/events.py` 的事件表 → 填摘要表），写在那个模块的文档里；`executors/codex/command.py` 的每个字段（子命令名、JSON 开关、会话 ID 开关、模型开关）都**标着"未验证"**，因为它们是从"codex 大概长这样"推出来的，不是测出来的。
- **影响任务**：TASK-P1-031（已按推荐默认实施：`executors/codex/` 的事件表与摘要表留空、命令行字段逐个标注未验证；`::TestTheCodexSkeleton` 断言"没有结果事件 ⇒ 判失败"与"事件全部原样记成 `unhandled`"）、TASK-P2-010（多执行器接入时，codex 的真实协议要补测并补填 —— 那时才允许它报 `COMPLETED`）

---

## 7. 隐含工程约束清单

以下 24 条约束在 design.md 正文中**未明说**，但拆解时若不覆盖，实现阶段必然返工。

| # | 约束 | 为什么容易漏 | 覆盖任务 | 验证方式 |
| --- | --- | --- | --- | --- |
| I-01 | 幂等键的构造规则（task_id + attempt）与语义（重放返回原结果） | 分处 §3.4 与 §6 两节，容易只做一处 | P1-021, P1-030 | T1：同一幂等键提交两次返回同一结果 |
| I-02 | 取消传播是**四层链**（任务→Adapter→进程→容器），不是两层 | 原文只写"传递到执行器和 Sandbox" | P1-033 | T1：逐层确认收到才推进下一层 |
| I-03 | 超时≠失败：须先 interrupt 再探测可恢复性，避免僵尸会话 | 原文一句话，语义关键 | P1-034 | T1：超时后无僵尸容器残留 |
| I-04 | 修复三重上限是**全局共享**，非每任务独立 | 原文未说明作用域 | P1-063 | T1：并行任务不各自突破全局上限 |
| I-05 | VerificationRun / Artifact **append-only**（禁 UPDATE/DELETE） | 只写"保存证据"，未写不可变 | P1-024 | T1：DB 层拒绝 UPDATE/DELETE |
| I-06 | 状态写入与事件发布**同事务**（Outbox） | 只写"PostgreSQL Outbox"，未强调同事务 | P1-025 | T1：事务回滚后两者都不存在 |
| I-07 | pgvector **不得**在阶段一成为主知识路径 | 三处提到"按需"，易被当成必选 | P2-016 | T1：阶段一知识检索不依赖向量库 |
| I-08 | 预算币种与指标类型必须**可配置**，不可硬编码 USD / 示例 metric | 原文是脚注，常被忽略 | P1-010, P1-012 | T1：换币种 / 换 metric 均正常工作 |
| I-09 | 覆盖率基线**存哪、谁更新、如何防下调** | 原文一句"根据模块基线确定"，机制未定义 | P1-042 | T1：基线变化→阈值同步；下调被拒 |
| I-10 | 被撤回的 Decision 需有**终态**（superseded），否则重规划后旧决策仍"生效" | 状态机图只画了 Re-plan 指向 PLANNED | P1-021, P1-054 | T1：重规划后旧 Decision 标记为 superseded |
| I-11 | 两个循环"可相互触发"会带来**事件回环** | 原文只警告了"合并成一个 Prompt"的坑 | P1-064 | T1：回写不触发新一轮；同 gap 未改善则熔断 |
| I-12 | 人工审批应是**信号 / 事件**而非轮询，且有超时与撤回语义 | §1 提到审批等待，§6 无审批节点 | P1-046, P3-010 | T1：审批过期后不可放行 |
| I-13 | 成本核算三处（Adapter / 记账 / 观测）必须**同源同币种** | 分散在 §3.4、§8 | P1-035 | T1：三处数值相等 |
| I-14 | 工作空间需要**租约与 GC**，否则并发耗尽磁盘 / 容器 | 原文只讲创建不讲回收 | P1-032 | T1：租约过期后回收；崩溃遗留可清理 |
| I-15 | 分支命名与重试的**冲突约定**（每任务一分支 vs 每次尝试一分支） | 隐含在 `artifacts.branch` 字段里 | P1-032 | T1：重试不产生分支冲突 |
| I-16 | Integration Branch 需要**生命周期**（合并后回收） | 流程图只给顺序 | P2-017 | T1：全部合并后分支被回收 |
| I-17 | 多执行器结果**可比性口径**需前置设计，否则阶段二验收无法判定 | 阶段二验收依赖阶段一未做的设计 | P2-018 | T2：统一口径下的成功率 / 成本 / 耗时对比 |
| I-18 | Critic 未检查 **Non-goals**（目标六要素定义了但没接上） | 字段与检查项分离 | P1-055 | T1：触碰 Non-goals 的候选被否决 |
| I-19 | **Stop Conditions 须机器可判定**，否则永不生效 | 只列了字段，没列执行位置 | P1-014 | T1：不可判定条件启动时报错而非恒 false |
| I-20 | 快照指标必须**落盘**，使"当时的快照"可重建 | 只写了 version+commit+metrics 三元组 | P1-023 | T1：仅凭快照记录即可取回当时指标 |
| I-21 | 产物需**保留期**与不可变策略，第三方可复现 | 原文未定义保留期 | P1-024, P1-048 | T1：按 URI 取回且 hash 一致 |
| I-22 | 验证所用的模型须与**被测代码生成模型解耦** | 只写了 self_assessment 不算验收，没写验收用什么 | P1-041 | T1：验证端与执行端模型可分别配置 |
| I-23 | **测试环境本身**也需在沙箱内，不可跑在 Agent 的宿主 shell | 出现在结构图里，正文未强调 | P1-032, P1-041 | T1：验证进程在独立容器内运行 |
| I-24 | 四级权限必须落到**工具调用拦截点**，不只是文档标签 | §7 只定义等级，未定义执行点 | P1-011, P1-044 | T1：越权操作在发生时即被阻断 |

---

## 附录 A：一页速查

```text
阶段一 55 条任务 · 9 个工作流 · 约 32 人日（单人）
├── INF  基础设施     001-007   7 条   2.50d
├── CP   控制面       010-014   5 条   2.25d
├── KS   状态与知识   020-025   6 条   4.00d
├── EP   执行器       030-035   6 条   4.00d
├── HN   工程护栏     040-048   9 条   5.75d
├── DR   决策引擎     050-057   8 条   5.50d
├── OR   编排闭环     060-065   6 条   4.25d
├── EV   观测评估     070-073   4 条   2.00d
└── MS   里程碑       080-083   4 条   1.50d
                              合计  55 条  31.75d

验收门   M0 骨架可运行 → M1 单轮闭环 → M2 自主闭环
核心壁垒 ① 项目认知 ② 演进决策 ③ 工程验证
首个真里程碑  TASK-P3-018 —— 连续 10 个有效演进周期
```

## 附录 B：开始动手前先确认的三件事

1. **试点仓库**（`TASK-P1-083`）—— 有可运行测试套件、目标可量化、依赖可离线安装。没有它，M2 无法验收。
2. **第一批"人类标注"** —— 20 个历史问题用于建立误报率分母。这是自主发现能力唯一可量化的验证依据。
3. **Thin Slice 的取舍** —— 若要按 design.md 的"1~2 周"推进，需明确砍掉哪些任务（推荐先砍 `TASK-P1-048`、`TASK-P1-072`、`TASK-P1-073`，保留全部安全与验收类任务）。**安全与验收类任务不建议为了赶工期而砍** —— 它们的缺失会让后续所有"验证通过"失去意义。
