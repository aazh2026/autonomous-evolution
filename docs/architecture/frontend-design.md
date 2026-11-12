# ASES 前端架构与交互设计（ASES Cockpit · 自主演进控制舱）

设计依据：
- `docs/design.md` §1.2 态势感知与自主权分级控制、§3.2 任务拆解与 DAG 依赖图、§3.4 多执行器监控与实时事件流、§3.5B 独立六层验证结果矩阵、§7 治理审批门与防篡改审计、§8 工程目录结构（`apps/dashboard/`）；
- `docs/architecture/trend-board.md` 跨周期趋势看板（四层指标曲线、诚实缺失态、R-117 虚荣指标结构性拒绝）；
- `docs/architecture/high-risk-review.md` 高风险变更治理（操作 × 种类双轴风险、名册资格核验、SHA-256 内容指纹锁定）；
- `apps/api/control.py` 控制面 REST API、`apps/api/static_frontend.py` 前端托管与降级、`apps/dashboard/` 独立前端工程。

---

## 一、 设计理念与核心不变量的前端映射

ASES 前端不是传统的「聊天对话框」或「CI 任务列表」，而是人类指挥官对 AI 研发组织进行**目标设定、态势感知、证据溯源、决策干预与高风险审批**的控制舱。

| 系统核心不变量 / 架构约束 | 前端视觉与交互映射 | 关联后端实现 |
| :--- | :--- | :--- |
| **1. 双环严格解耦**（循环 B 宏观演进 ≠ 循环 A 微观执行） | 主工作台按「宏观认知与决策沙盘（Loop B）」与「微观 DAG 调度与六层验证（Loop A）」分区呈现，严禁混成单一聊天流 | `apps/director/m2_cycle.py` |
| **2. 候选 ≠ 决策 ≠ 任务**（三表外键分离） | 采用三阶漏斗视图：`EvolutionCandidate`（机会卡片）$\rightarrow$ `EvolutionDecision`（Critic 质询与沙盘裁决）$\rightarrow$ `Task`（DAG 节点） | `core/entities/evolution.py`、`core/entities/task.py` |
| **3. 验证独立于执行**（`self_assessment` 不参与判定） | 将 Agent 的自我报告折叠为灰色次要参考，主视觉突出展示独立进程的**六层验证矩阵** | `core/task/outcome.py`、`harness/verification/engine.py` |
| **4. 算不出来 ≠ 0 & 拒绝虚荣指标** | 趋势看板在前端层面**结构性拒绝**渲染任务数、Token 数、提交量；对 `no_samples` 和 `baseline_missing` 渲染专用 `—` 缺失态徽标与原因说明，绝不渲染为 `0%` | `docs/architecture/trend-board.md`、`evaluation/trend.py` |
| **5. 高风险双轴治理与防篡改签名** | 审批台强制展示 `content_digest`（SHA-256）指纹锁与名册资格（`architecture` / `security` / `governance`），内容发生 1 字节变动即置灰失效 | `docs/architecture/high-risk-review.md`、`core/policy/high_risk.py` |

---

## 二、 整体布局与可拖拽分屏架构（Resizable Split Panes）

控制舱采用全视口无页面级滚动（`100vh × 100vw, overflow-hidden`）的 **四区可拖拽分屏架构**，基于自研的 `useResizableSplit`（原生拖拽分割条，见 §5.2）实现面板比例自由伸缩与布局持久化：

```mermaid
flowchart TD
    subgraph TopBar["顶部全局安全与自主权中枢 (Top Control Bar · 56px 固定)"]
        T1["项目切换器 & 健康探针 (/healthz)"]
        T2["自主权分级拨杆 (L0 手动 | L1 监督 | L2 半自主 | L3 全自主)"]
        T3["日预算水位仪表盘 ($Spent / $Cap)"]
        T4["闭环暂停 & 物理级急停熔断开关 (Emergency Kill Switch)"]
    end

    subgraph MainStage["中部主工作舞台 (Resizable Split Panes · 自由拖拽分屏)"]
        direction LR
        LeftPane["左侧栏: 演进周期时间轴 & 导航<br/>(默认 260px, 可折叠)"]
        CenterPane["中左工作窗: 决策沙盘 / DAG 拓扑 / 验证矩阵<br/>(最小 30%, 默认 50%)"]
        RightPane["中右溯源窗: PDF.js 规范渲染器 & 代码 Diff<br/>(最小 25%, 默认 50%)"]
        LeftPane --- CenterPane
        CenterPane <==> |"SVG 动态贝塞尔视觉溯源连线"| RightPane
    end

    subgraph BottomDrawer["底部实时遥测与审计流水抽屉 (Resizable Telemetry Drawer · 默认 180px)"]
        B1["NDJSON 实时事件流 (session.start / thought / action / verify)"]
        B2["工具调用防篡改审计日志流 (Fail-Closed Audit Stream)"]
    end

    TopBar --> MainStage
    MainStage --> BottomDrawer
```

---

## 三、 六大核心 UX 优化机制详细设计

> **本节写的是设计目标，不是当前实现。** 已落地的部分与现状见 §五
> （当前实现里 PDF.js 证据渲染已完成；Monaco Diff 视图、跨分屏 SVG 溯源连线、
> 双击行内编辑尚未接入 —— 它们的验收口径与数据结构已经定好，但代码还没写）。
> 分清这两者很重要：把设计目标写成"已经做完了"，下一次读这份文档的人
> 会去找一个不存在的功能。

### 1. 高保真文档与证据渲染（PDF.js + Monaco Dual Viewer）
在右侧「证据与源码溯源窗」内置双模渲染引擎：
- **PDF.js 高保真文档渲染器**：针对项目 PRD、架构规范 PDF、安全扫描报告 PDF（存储在 MinIO `Artifact` 库中），通过 `pdfjs-dist` 实现 Canvas + TextLayer 双层渲染，支持页码跳转、关键词高亮、缩放自适应及矩形坐标框（Bounding Box）标注。
- **Monaco Diff / AST 源码查看器**：针对代码级证据（如 `src/tinylib/cli.py:61`）和 Worktree 变更 Diff，提供语法高亮、行内批注与变更前后并排对比。

### 2. 可拖拽分屏面板（Resizable Split Panes）
- **三向可调分割条（Split Gutter）**：左栏与中栏、中左决策区与中右文档/代码区、主舞台与底部遥测抽屉之间均配置 `4px` 感应拖拽条（悬停高亮为 `brand-500` 蓝色，支持双击复位 50:50 比例）。
- **焦点最大化快捷键**：支持按 `Cmd/Ctrl + \` 快速收起左侧周期栏与底部遥测抽屉，将「验证矩阵 vs. PDF/代码溯源」扩展为全屏双栏审查模式。

### 3. 视觉溯源连线（Visual Traceability Overlay）
消除「AI 结论」与「原始事实证据」之间的认知断层：
- **跨面板 SVG 贝塞尔连线层**：在分屏容器顶层挂载一个 `pointer-events-none` 的全屏 `<svg id="traceability-overlay">`。
- **双向联动激活**：
  - 当用户在左侧点击或悬停某个演进候选（如 `CAND-001`）的证据条目 `evidence_ref`、或某项 Critic 质询结论时，右侧面板自动滚动至对应的 **PDF 规范段落 Bounding Box** 或 **源码行号**。
  - 同时，系统在左侧「结论锚点」与右侧「PDF 文本框 / 代码行锚点」之间绘制一条带脉冲动画的 SVG 三次贝塞尔曲线（绿色代表正证据支撑，红色代表冲突或失败断言）。

### 4. 置信度分级视觉系统（Confidence-Based Grading）
对系统提取的所有演进要素（`EvolutionCandidate`、`GoalGap`、优先级评分 $Priority = \frac{V \times I \times C \times U}{E \times (1+R)}$）实行严格的三级视觉编码：

| 置信度区间 ($C$) | 等级标签 | 视觉色彩规范（Tailwind） | 系统默认行为与 UI 标识 |
| :--- | :--- | :--- | :--- |
| $C \ge 0.80$ | **确凿级 (High / Verified)** | 翠绿 `emerald-400` / 边框 `emerald-700/50` | 附带复现单测或明确 AST 证据，默认推荐进入 `record` 立项通道 |
| $0.30 \le C < 0.80$ | **待核级 (Medium / Review)** | 琥珀 `amber-400` / 边框 `amber-600/50` | 存在局部证据但需人工校准假设或补充分析，高亮提示指挥官介入 |
| $C < 0.30$ | **投机级 (Low / Speculative)** | 绯红 `rose-400` / 边框 `rose-800/50`（半透明降噪） | 触发「不行动也是合法决策」保护机制，自动标记为 `deferred`（推迟） |

### 5. 提取结果行内编辑（Inline Editing for Extraction & Contracts）
AI 提取的演进机会或生成的验收契约往往需要人类架构师微调，而无需推翻重来：
- **双击即改（Click-to-Edit）**：在候选卡片与任务准入面板中，`title`（标题）、`hypothesis`（演进假设）、`expected_outcome`（预期收益）以及 `AcceptanceContract` 的阈值（如将覆盖率要求从 `>= 0.85` 改为 `>= 0.90`）均支持行内编辑。
- **修订留痕（Diff Pill & Audit）**：保存行内修改后，字段旁自动显示 `Edited by @reviewer` 徽标，Hover 可查看 AI 原始提取值与人工修改值的行内 Diff，并同步写入后端审计表。

### 6. 可执行风险工作流（Actionable Risk Workflows: Accept / Fix / Ignore）
每一处风险提示、演进候选、Critic 质询失败项或高风险审批卡片，均配备标准化的 **三态动作工作流按钮组**（告别只读报表）：
- **Accept（采纳 / 签名放行）**：
  - 在决策沙盘：确认候选立项（调用 `POST /api/v1/decisions/{decision_id}/override` 设为 `record`）。
  - 在高风险法庭：核验 SHA-256 指纹与名册资格后签署放行票据（调用 `POST /api/v1/governance/approvals/{request_id}/review`）。
- **Fix（行内修正 / 派发定向修复）**：
  - 在决策沙盘：打开行内编辑器微调变更边界或验收条件后再立项。
  - 在六层验证矩阵：针对某层未通过项（如架构越界或单测失败），一键生成定向修复任务（对接 `core/task/repair.py`），携带失败证据进入下一轮修复预算。
- **Ignore / Defer（忽略 / 推迟 / 驳回）**：
  - 将低置信度或非目标候选标记为 `defer`（暂缓）或 `reject`（永久忽略并加入反例库），防止下一轮周期重复发现（降低 `duplicate_discovery_rate`）。

---

## 四、 五大核心业务视图与交互设计

> **本节同样写的是设计目标。** 五个视图的骨架、交互与数据来源都已落地
> （见 §5.3 的目录与 §六 的接口矩阵），但视图里点名的一些**交互细节**
> —— 跨分屏 SVG 溯源连线、双击行内编辑、Monaco 级别的源码 Diff ——
> 代码还没写。与 §三 一样，这些是验收口径，不是现状。

### 视图 1：双环态势与跨周期趋势看板（Dual-Loop & Trend Board）
- **顶部 4+2 指标卡与 7 天累计趋势图**：
  - 严格对齐 `docs/architecture/trend-board.md`，展示 **4 层 8 项核心指标**（自主发现层：有效发现率/重复发现率；决策质量层：采纳率/误报率；工程交付层：任务成功率/返工率/回归缺陷率；项目演进层：目标达成度差值 $\Delta$）+ **2 项诊断量**（验收通过率、累计花费）。
  - **诚实空值渲染**：当点状态为 `baseline_missing` 或 `no_samples` 时，图表节点渲染为空心虚线圆点，数值显示为 `—` 并附带 Tooltip 说明（如「尚未建立人工标注基线，算不出来 ≠ 0」）。
- **双环流转实时状态机**：左环展示循环 B 五阶段（认知 $\rightarrow$ 诊断 $\rightarrow$ 探索 $\rightarrow$ 决策 $\rightarrow$ 规划），右环展示循环 A 四步闭环，实时高亮当前活跃节点。

### 视图 2：演进决策沙盘（Decision Sandbox）
- **中左面板（候选与 Critic 9 问矩阵）**：
  - 展示当前周期发现的 `EvolutionCandidate` 列表，带置信度红黄绿分级标签与 $Priority$ 评分拆解雷达图（$V, I, C, U, E, R$ 六维）。
  - 展开独立 **Critic 审查清单**（如「问题是否真实存在且有复现证据」「是否触碰 Non-goals」），每项质询均附带通过/拦截图标。
  - 底部提供 **[Accept 强制立项]** / **[Fix 行内修订契约]** / **[Ignore/Defer 推迟]** 快捷操作栏。
- **中右面板（PDF 规范 / 源码证据溯源）**：
  - 经由 SVG 视觉溯源连线，直接定位到触发该候选的 PDF 需求文档页码区域或测试失败文件行号。

### 视图 3：DAG 多任务调度与执行器舰队（DAG Topology & Executor Fleet）
- **分层波次 DAG 画布（手写 SVG）**：
  - 按 `Execution Wave 0 -> Wave 1 -> Wave 2` 从左至右渲染有向无环图（对接 `GET /api/v1/tasks/dag`）。
  - **级联阻断因果高亮**：当上游节点失败导致下游进入 `BLOCKED` 状态时，连线变为红色虚线并显示阻断根因徽标（`blocked_by: TASK-xxx`）。
- **多执行器动态选择器透视**：
  - 在任务节点上展示由 `executors/` 路由分配的执行器徽标（`claude-code`、`codex`、`gemini-cli`、`opencode`），Hover 显示选择依据（亲和度 `affinity`、成功率、当前并发槽位负载与成本档位）。

### 视图 4：独立六层验证矩阵与工作区 Diff（Six-Layer Verification）
- **左半屏（Worktree Diff & PDF 报告）**：展示隔离沙箱 Git Worktree 产生的代码变更 Diff 及构建产物。
- **右半屏（独立六层验证矩阵）**：
  - 对接 `GET /api/v1/verifications`，独立展示 6 个验证层卡片：**① 功能层 (Pytest)**、**② 代码质量 (Ruff/Mypy)**、**③ 架构守约 (AST)**、**④ 安全扫描 (Bandit/Secret Gate)**、**⑤ 性能基线 (Bench)**、**⑥ 产品契约 (Contract)**。
  - 任一层失败时，展示该层对应的 Actionable Risk 操作：**[Fix 派发定向修复]**、**[查看详细日志 Artifact]**，并绘制连线指向左半屏触发报错的具体代码行。

### 视图 5：高风险变更审批法庭（Governance Court）
- **双轴风险与资格核验台**：
  - 展示变更操作（如 `merge_to_main`、`modify_governance`）与变更种类推导出的**最少独立评审份数**（如 `1/2 已签`）和**必需资格槽位**（如 `[✓ architecture] [⏳ governance]`）。
- **SHA-256 防篡改内容锁**：
  - 醒目显示 `content_digest` 指纹；评审人点击 **[Accept 签名放行]** 或 **[Reject 粘性驳回]** 时，前端将该指纹连同评审人 ID 一并提交核验，杜绝「批后偷改内容」。

---

## 五、 前端技术栈与工程目录

### 1. 运行形态：三种，同一份代码

前后端分离之后，**前端是一份代码、三种跑法**。区别只在"谁来发那几百 KB 的静态文件"：

| 形态 | 静态文件由谁托管 | 浏览器看到的 API 地址 | 什么时候用 |
| :--- | :--- | :--- | :--- |
| **本地开发** | vite dev server（`localhost:3000`） | `/api/v1/...`（vite 代理到 `localhost:8000`） | 改前端代码时 |
| **本地一体化** | 后端自己（`apps/api/static_frontend.py` 托管 `apps/dashboard/dist`） | 同源 `/api/v1/...` | `make ui` —— 一条命令起全栈 |
| **全栈容器** | nginx 容器（compose 的 `dashboard` 服务） | 同源 `/api/v1/...`（nginx 反代到 `api:8000`） | `make up` / 部署 |

**三种形态下浏览器看到的请求都是同源的 `/api/v1/...`，于是完全不需要 CORS。**
这不是巧合，是刻意的：跨域白名单配错时，浏览器会拦掉响应而 `curl` 完全正常，
于是"后端没问题"这句话会被反复重复，直到有人去翻浏览器控制台。
真要把前端放到独立域名时，再打开后端的 CORS 白名单（`ASES_CORS_ALLOWED_ORIGINS`）。

### 2. 技术选型清单（**实际在用的**）

| 领域 | 选型 | 核心用途 |
| :--- | :--- | :--- |
| **构建** | Vite 6 | 产物是纯静态文件，dev server 自带 `/api` 代理 |
| **框架** | React 19 + TypeScript 5.7（`strict` + `noUncheckedIndexedAccess`） | 类型安全的组件化架构，映射后端 Pydantic Schema |
| **样式** | Tailwind CSS 3 | 高密度多栏布局、暗色工程主题（`slate-950`） |
| **图标** | `lucide-react` | 顶栏与导航图标 |
| **文档渲染** | `pdfjs-dist` (PDF.js) | 决策沙盘的规范 PDF 渲染（单独成块，不拖累首屏） |
| **趋势图表** | **手写 SVG** | 4 层 8 指标累计趋势曲线 —— 见下方「为什么手写」 |
| **分屏** | **手写 `useResizableSplit`** | 三向可拖拽分割条 —— 见下方「为什么手写」 |
| **状态** | React 内置（`useCockpitData` + `useAsyncAction`） | 服务端状态拉取与写操作 |
| **测试** | vitest 4 + @testing-library/react + jsdom | 前端单测（46 个） |
| **契约** | `src/types/control-plane.ts` + `tests/unit/api/test_frontend_contract.py` | 两端不漂移的**机制**，见下方 §5.4 |

#### 为什么不用 Next.js（设计稿原先写的是 Next.js 15 App Router）

1. **控制舱是内网工具，没有 SEO 需求** —— SSR 带来的收益是零，代价是一整个 Node 运行时。
   前端分离的产物应该是**纯静态文件**：nginx 一把梭就能发，运行镜像里没有 Node、
   没有 `node_modules`，攻击面小得多，冷启动也快得多。
2. **视图切换走应用状态，不是 URL** —— 六个工作视图是 Tab，不是六个路由。
   Next.js 的 App Router 在这里没有对应物，硬套只会多出一层没用的路由树。
3. **前后端分离的重点是"后端不再是前端的宿主"** —— 一个 Node 运行时把这件事又拧回去了：
   前端镜像要自己起服务、要自己监听、要处理优雅退出。

#### 为什么趋势图表与分屏是手写的

- **趋势图**：库（recharts / visx）按「每个点都有值」设计，而本项目的硬约束恰恰相反 ——
  **算不出来 ≠ 0**。缺失点必须是「空心虚线圆点 + `—`」，而且**折线要在缺失处断开**
  （连过去等于把"没测到"说成"没变化"）。这套语义在图表库里要么没有、要么得靠 hack 绕。
  手写 SVG 反倒更短也更直白：`scalePoints` 保持与输入**逐位对齐**，
  `splitSegments` 在缺失处断开。
  （这条约束有专门的回归测试：`TrendBoard.test.tsx` 断言 polyline 段数。）
- **分屏**：`react-resizable-panels` 能用，但控制舱只需要三个分割条、
  一套「双击复位 50:50」的交互 —— 三十行 hook 与一个第三方依赖的取舍，
  在这里不值得多背一个依赖。

### 3. `apps/dashboard/` 目录结构（实际）

```text
apps/dashboard/
├── Dockerfile                    # 多阶段：node 构建 → nginx 运行
├── nginx.conf.template           # 发静态文件 + 反代 /api + SPA 回退（${API_UPSTREAM} 部署时注入）
├── security-headers.conf         # 安全响应头片段（被各 location 显式 include）
├── src/
│   ├── main.tsx                  # 挂载点
│   ├── App.tsx                   # 顶栏 + 主舞台 + 底部遥测抽屉的骨架与视图切换
│   ├── api/
│   │   ├── client.ts             # 唯一的 HTTP 出口：类型化 fetch + ApiError
│   │   └── client.test.ts        # 46 个前端测试的一部分
│   ├── components/
│   │   ├── TopBar.tsx            # L0-L3 自主权切换、预算水位、急停熔断开关
│   │   ├── Sidebar.tsx           # 左侧 Cycle 历史与导航
│   │   ├── TelemetryDrawer.tsx   # 底部事件流与审计日志
│   │   ├── shared/
│   │   │   ├── TrendBoard.tsx    # 4 层 8 指标累计趋势图（诚实缺失态）
│   │   │   └── Primitives.tsx    # 徽标 / 卡片 / 空态等基础件
│   │   └── views/
│   │       ├── DualLoopDashboard.tsx   # 双环态势
│   │       ├── DecisionSandbox.tsx     # 决策沙盘（Accept / Fix / Ignore）
│   │       ├── DagTopology.tsx         # 多波次 DAG 拓扑与级联阻断高亮
│   │       ├── VerificationMatrix.tsx   # 独立六层验证矩阵
│   │       ├── GovernanceCourt.tsx      # 双轴风险审批与 SHA-256 签名放行
│   │       └── EvidenceViewer.tsx       # PDF.js 规范渲染
│   ├── hooks/
│   │   ├── useCockpitData.ts     # 拉取各视图数据
│   │   ├── useAsyncAction.ts     # 写操作（立项 / 审批 / 派发修复）
│   │   └── useResizableSplit.ts  # 三向分屏
│   ├── lib/
│   │   ├── confidence.ts         # 置信度分级映射
│   │   └── format.ts             # 金额 / 百分比 / 时间的诚实格式化
│   └── types/
│       └── control-plane.ts      # 与 apps/api/control.py 对齐的 TypeScript 接口
└── tests（后端侧）：tests/unit/api/test_frontend_contract.py
```

### 4. 两端怎么保证不漂移

**类型契约是一份，两头各自检查**：

- `apps/dashboard/src/types/control-plane.ts` —— 前端侧的类型声明；
- `tests/unit/api/test_frontend_contract.py` —— 后端侧的**门禁**。它解析上面那个 TS 文件，
  拿**活的 API 响应**逐字段对账，四种漂移都会被抓到：

  | 漂移 | 后果 | 门禁怎么抓 |
  | :--- | :--- | :--- |
  | 后端多给了字段 | 前端静默丢掉这份数据 | 断言响应键 ⊆ 契约字段 |
  | TS 声明必填、后端没给 | 前端渲染成「缺失」 | 断言必填字段都在响应里 |
  | 类型对不上（number vs string） | 前端拿到 `undefined` | 按契约逐字段断言类型 |
  | 嵌套结构对不上 | 深层字段取不到 | 递归对账嵌套对象 |

  外加两条专项断言：`Decimal` 字段（Pydantic v2 序列化成**字符串**）
  与决策 id **不可推导**（`DEC-001`/`DEC-002` 这种猜法在有第三个候选时必然覆盖错对象）。

  这条门禁的价值在于：**它不需要前端跑起来**。改后端响应结构而忘了改前端，
  CI 会直接红。

- `apps/dashboard/src/api/client.test.ts` —— 前端侧的回归测试。其中一条专门锁死
  「决策覆盖用的就是调用方给的 `decision_id`，不会自己去猜」——
  早期版本按候选 id 硬编码推导决策 id，第三个候选永远覆盖到错的决策上。

### 5. 降级路径：内嵌单页

`apps/api/control.html` 是分离之前的单文件控制舱（Tailwind CDN + 原生 Fetch）。
它**保留**下来了，而且是**显式的降级路径**而不是死代码：

- 后端在 `apps/dashboard/dist` 不存在时，自动回落到托管它（`/ui` 与 `/` 都可用）；
- 原因很实际：控制舱是一个**值班用的界面** —— 操作员半夜被叫醒时，
  不该因为「前端没构建」而什么都看不到。

降级路径同样有测试（`tests/unit/api/test_static_frontend.py`），
两个分支都被覆盖，避免「本地构建过一次之后，测试就再也测不到降级分支了」。

---

## 六、 前后端 API 对接矩阵

前端所有视图与交互动作均与 `apps/api/control.py` 中的 REST 端点一一对应。

**所有请求都从 `apps/dashboard/src/api/client.ts` 出去** —— 组件里不出现裸 `fetch`。
这不只是"整洁"：错误处理、金额格式化、`Decimal` 的字符串序列化都在那一处处理，
散开写就会漏掉一处，而漏掉一处的症状是「某个数字莫名其妙变成 —」。

| 前端模块 / 交互动作 | HTTP 方法与端点 | 后端处理函数 |
| :--- | :--- | :--- |
| 顶部状态栏初始化与轮询 | `GET /api/v1/status` | `get_system_status` |
| 切换 L0~L3 自主权 / 暂停 / 急停熔断 | `POST /api/v1/autonomy` | `update_autonomy` |
| 左侧演进周期列表与达成度趋势 | `GET /api/v1/cycles` | `list_cycles` |
| 决策沙盘候选列表与置信度分级 | `GET /api/v1/candidates` & `GET /api/v1/decisions` | `list_candidates`、`list_decisions` |
| 风险工作流（Accept 立项 / Fix 修订 / Ignore 推迟） | `POST /api/v1/decisions/{decision_id}/override` | `override_decision` |
| 多波次任务 DAG 拓扑与阻断状态 | `GET /api/v1/tasks/dag` | `get_task_dag` |
| 独立六层验证矩阵与 Diff 摘要 | `GET /api/v1/verifications` | `list_verifications` |
| 高风险审批列表与双签放行/驳回 | `GET / POST /api/v1/governance/approvals...` | `list_approvals`、`review_approval` |
| 执行器集群画像与底部实时遥测流 | `GET /api/v1/executors` & `GET /api/v1/telemetry/events` | `list_executors`、`list_telemetry_events` |

### API 基址怎么定

默认**相对路径** `/api/v1`（`apps/dashboard/src/api/client.ts` 里的 `API_BASE`）。
三种运行形态（本地开发 / 本地一体化 / 全栈容器）下浏览器看到的都是同源，
所以**同一个构建产物**在三种形态里都能跑，不需要为部署改代码。

只有前端要放到**独立域名**时，才设 `VITE_API_BASE_URL` 指到后端绝对地址，
并给后端配 `ASES_CORS_ALLOWED_ORIGINS`：

```bash
# .env
ASES_CORS_ALLOWED_ORIGINS=https://cockpit.example.com
```

写死绝对地址的坏处很具体：镜像会绑死在某个域名上，换域名就得重新构建镜像 ——
而镜像发版通常比改一行环境变量慢得多。
