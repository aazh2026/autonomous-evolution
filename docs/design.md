# 持续自演进型 AI 自主研发系统：技术选型与总体设计

我建议把这个系统定义为一个 Autonomous Software Evolution System（自主软件演进系统，ASES）。

它不是一个让 Coding Agent 不断执行任务的自动化工具，而是一个能够长期承担软件项目演进责任的 AI 研发组织。

它需要具备一种不同于传统软件研发自动化的能力：

> 人类定义项目的目标、边界和约束。系统持续观察项目，理解项目现状，识别目标差距，自主决策演进方向，组织不同的 Coding Agent 实施变更，并通过独立验证决定是否接受变更。系统根据每轮结果更新对项目的认知，进入下一轮演进。

这里我先提出一个关键判断：

不要把系统设计成一个超级 Agent，而要设计成一个由自主决策系统、可替换执行器、工程验证体系和项目状态系统组成的闭环。

尤其是你不限定 Coding Agent，这意味着架构必须从一开始就做到执行器解耦，不能把 Claude Code SDK、OpenCode SDK 或某个特定 Agent 的会话模型变成系统核心。

还有一个需要纠正的前提：持续自演进不等于持续修改代码。系统有时最正确的行动是继续开发，有时是重构，有时是补测试，有时是停止修改并收集证据。自主判断不做什么，是比自主执行什么更重要的能力。

## 一、首先确定总体技术路线

经过对现有开源框架和 Agent Runtime 的比较，我建议采用下面的组合，而不是选择某一个完整平台直接套用。

## 推荐技术选型

自主决策引擎

LangGraph

负责项目理解、差距分析、演进决策、任务规划和动态决策。

长期运行编排

Temporal

负责持续运行、定时唤醒、任务调度、超时、重试、故障恢复和人工审批等待。

Coding Agent 执行层

Agent Adapter

统一接入 Claude Code、Codex、OpenCode、Gemini CLI、OpenHands 等。

执行环境

Docker / Daytona

隔离执行、独立工作空间、Git Worktree、资源限制。

项目状态与知识

PostgreSQL + Git + Object Storage

项目状态、任务图、架构知识、执行记录、评测结果和交付物。

观测与评估

OpenTelemetry + Langfuse

Agent 执行轨迹、模型成本、质量指标、失败归因和演进效果。

为什么选择这套组合？

| 技术             | 解决的问题     | 不应该承担的职责    |
| -------------- | --------- | ----------- |
| LangGraph      | 智能决策、动态规划 | 项目数据的唯一存储   |
| Temporal       | 长期流程生命周期  | 代替 Agent 思考 |
| Agent Adapter  | 执行器统一接入   | 项目级任务决策     |
| Docker/Daytona | 安全执行环境    | 研发任务调度      |
| PostgreSQL     | 项目状态和事实记录 | 代码版本管理      |
| Langfuse       | 过程观测与分析   | 代码质量最终验收    |

LangGraph 本身是长程、有状态 Agent 工作流运行时；Temporal 擅长具有持久状态、定时器、信号、子工作流和故障恢复的长期业务流程。二者解决的问题不同。

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub+3



一个重要的取舍： POC 阶段可以只用 LangGraph + PostgreSQL，不必立即引入 Temporal。等到需要 7×24 小时运行、多项目并行、跨天恢复和复杂事件调度时，再引入 Temporal。

## 二、总体架构设计

我把系统分成六个核心子系统。

\#chatgpt-mermaid-\_r_15a\_{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;fill:rgb(13, 13, 13);}@keyframes edge-animation-frame{from{stroke-dashoffset:0;}}@keyframes dash{to{stroke-dashoffset:0;}}#chatgpt-mermaid-\_r_15a\_ .edge-animation-slow{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 50s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_15a\_ .edge-animation-fast{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 20s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_15a\_ .error-icon{fill:rgb(243, 243, 243);}#chatgpt-mermaid-\_r_15a\_ .error-text{fill:rgb(13, 13, 13);stroke:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_15a\_ .edge-thickness-normal{stroke-width:1px;}#chatgpt-mermaid-\_r_15a\_ .edge-thickness-thick{stroke-width:3.5px;}#chatgpt-mermaid-\_r_15a\_ .edge-pattern-solid{stroke-dasharray:0;}#chatgpt-mermaid-\_r_15a\_ .edge-thickness-invisible{stroke-width:0;fill:none;}#chatgpt-mermaid-\_r_15a\_ .edge-pattern-dashed{stroke-dasharray:3;}#chatgpt-mermaid-\_r_15a\_ .edge-pattern-dotted{stroke-dasharray:2;}#chatgpt-mermaid-\_r_15a\_ .marker{fill:rgb(143, 143, 143);stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_15a\_ .marker.cross{stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_15a\_ svg{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;}#chatgpt-mermaid-\_r_15a\_ p{margin:0;}#chatgpt-mermaid-\_r_15a\_ .label{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_15a\_ .cluster-label text{fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_15a\_ .cluster-label span{color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_15a\_ .cluster-label span p{background-color:transparent;}#chatgpt-mermaid-\_r_15a\_ .label text,#chatgpt-mermaid-\_r_15a\_ span{fill:rgb(13, 13, 13);color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_15a\_ .node rect,#chatgpt-mermaid-\_r_15a\_ .node circle,#chatgpt-mermaid-\_r_15a\_ .node ellipse,#chatgpt-mermaid-\_r_15a\_ .node polygon,#chatgpt-mermaid-\_r_15a\_ .node path{fill:rgb(222, 234, 251);stroke:rgb(83, 154, 248);stroke-width:1px;}#chatgpt-mermaid-\_r_15a\_ .rough-node .label text,#chatgpt-mermaid-\_r_15a\_ .node .label text,#chatgpt-mermaid-\_r_15a\_ .image-shape .label,#chatgpt-mermaid-\_r_15a\_ .icon-shape .label{text-anchor:middle;}#chatgpt-mermaid-\_r_15a\_ .node .katex path{fill:#000;stroke:#000;stroke-width:1px;}#chatgpt-mermaid-\_r_15a\_ .rough-node .label,#chatgpt-mermaid-\_r_15a\_ .node .label,#chatgpt-mermaid-\_r_15a\_ .image-shape .label,#chatgpt-mermaid-\_r_15a\_ .icon-shape .label{text-align:center;}#chatgpt-mermaid-\_r_15a\_ .node.clickable{cursor:pointer;}#chatgpt-mermaid-\_r_15a\_ .root .anchor path{fill:rgb(143, 143, 143)!important;stroke-width:0;stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_15a\_ .arrowheadPath{fill:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_15a\_ .edgePath .path{stroke:rgb(143, 143, 143);stroke-width:1px;}#chatgpt-mermaid-\_r_15a\_ .flowchart-link{stroke:rgb(143, 143, 143);fill:none;}#chatgpt-mermaid-\_r_15a\_ .edgeLabel{background-color:rgb(252, 252, 252);text-align:center;}#chatgpt-mermaid-\_r_15a\_ .edgeLabel p{background-color:rgb(252, 252, 252);}#chatgpt-mermaid-\_r_15a\_ .edgeLabel rect{opacity:0.5;background-color:rgb(252, 252, 252);fill:rgb(252, 252, 252);}#chatgpt-mermaid-\_r_15a\_ .labelBkg{background-color:rgba(252, 252, 252, 0.5);}#chatgpt-mermaid-\_r_15a\_ .cluster rect{fill:rgb(243, 243, 243);stroke:rgba(0, 0, 0, 0.1);stroke-width:1px;}#chatgpt-mermaid-\_r_15a\_ .cluster text{fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_15a\_ .cluster span{color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_15a\_ div.mermaidTooltip{position:absolute;text-align:center;max-width:200px;padding:2px;font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:12px;background:rgb(243, 243, 243);border:1px solid rgba(0, 0, 0, 0.1);border-radius:2px;pointer-events:none;z-index:100;}#chatgpt-mermaid-\_r_15a\_ .flowchartTitleText{text-anchor:middle;font-size:18px;fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_15a\_ rect.text{fill:none;stroke-width:0;}#chatgpt-mermaid-\_r_15a\_ .icon-shape,#chatgpt-mermaid-\_r_15a\_ .image-shape{background-color:rgb(252, 252, 252);text-align:center;}#chatgpt-mermaid-\_r_15a\_ .icon-shape p,#chatgpt-mermaid-\_r_15a\_ .image-shape p{background-color:rgb(252, 252, 252);padding:2px;}#chatgpt-mermaid-\_r_15a\_ .icon-shape .label rect,#chatgpt-mermaid-\_r_15a\_ .image-shape .label rect{opacity:0.5;background-color:rgb(252, 252, 252);fill:rgb(252, 252, 252);}#chatgpt-mermaid-\_r_15a\_ .label-icon{display:inline-block;height:1em;overflow:visible;vertical-align:-0.125em;}#chatgpt-mermaid-\_r_15a\_ .node .label-icon path{fill:currentColor;stroke:revert;stroke-width:revert;}#chatgpt-mermaid-\_r_15a\_ .node .neo-node{stroke:rgb(83, 154, 248);}#chatgpt-mermaid-\_r_15a\_ [data-look="neo"].node rect,#chatgpt-mermaid-\_r_15a\_ [data-look="neo"].cluster rect,#chatgpt-mermaid-\_r_15a\_ [data-look="neo"].node polygon{stroke:url(#chatgpt-mermaid-\_r_15a\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_15a\_ [data-look="neo"].swimlane.cluster rect{filter:none;}#chatgpt-mermaid-\_r_15a\_ [data-look="neo"].node path{stroke:url(#chatgpt-mermaid-\_r_15a\_-gradient);stroke-width:1px;}#chatgpt-mermaid-\_r_15a\_ [data-look="neo"].node .outer-path{filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_15a\_ [data-look="neo"].node .neo-line path{stroke:rgb(83, 154, 248);filter:none;}#chatgpt-mermaid-\_r_15a\_ [data-look="neo"].node circle{stroke:url(#chatgpt-mermaid-\_r_15a\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_15a\_ [data-look="neo"].node circle .state-start{fill:#000000;}#chatgpt-mermaid-\_r_15a\_ [data-look="neo"].icon-shape .icon{fill:url(#chatgpt-mermaid-\_r_15a\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_15a\_ [data-look="neo"].icon-shape .icon-neo path{stroke:url(#chatgpt-mermaid-\_r_15a\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_15a\_ .node text{font-size:14px;font-weight:600;letter-spacing:normal;fill:rgb(0, 79, 153);}#chatgpt-mermaid-\_r_15a\_ .edgeLabels text{font-size:13px;font-weight:600;letter-spacing:-0.08px;fill:rgb(0, 79, 153);}#chatgpt-mermaid-\_r_15a\_ .node tspan[font-weight="normal"],#chatgpt-mermaid-\_r_15a\_ .edgeLabels tspan[font-weight="normal"]{font-weight:600;}#chatgpt-mermaid-\_r_15a\_ .edgeLabel .label rect{opacity:1;rx:13px;ry:13px;fill:rgb(245, 250, 255);stroke:rgb(206, 219, 229);stroke-width:1px;}#chatgpt-mermaid-\_r_15a\_ .node rect,#chatgpt-mermaid-\_r_15a\_ .node circle,#chatgpt-mermaid-\_r_15a\_ .node ellipse,#chatgpt-mermaid-\_r_15a\_ .node polygon,#chatgpt-mermaid-\_r_15a\_ .node path{fill:rgb(229, 243, 255);stroke:rgba(0, 0, 0, 0.1);stroke-width:1px;}#chatgpt-mermaid-\_r_15a\_ .node rect{rx:16px;ry:16px;}#chatgpt-mermaid-\_r_15a\_ .mermaid-decision .label-container{fill:rgb(245, 250, 255);stroke:rgb(206, 219, 229);stroke-dasharray:2,2;}#chatgpt-mermaid-\_r_15a\_ .edgePaths .flowchart-link{stroke:rgb(143, 143, 143);stroke-width:1px;stroke-linecap:round;stroke-linejoin:round;}#chatgpt-mermaid-\_r_15a\_ .marker{fill:rgb(143, 143, 143);stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_15a\_ :root{--mermaid-font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";}6. Project Knowledge & State5. Engineering Harness4. Agent Execution Plane3. Workflow Orchestrator2. Autonomous Director1. Project Control PlanePostgreSQLGit RepositoryArtifacts / Object StorageTrace & EvaluationIsolated Sandbox / WorktreeTest & CICode / Architecture ReviewSecurity & Policy GateIntegration & MergeAgent Adapter InterfaceClaude CodeCodexOpenCodeOpenHandsGemini CLITemporal / LangGraphTask Graph & SchedulerExecution State MachineProject ObserverGap & Opportunity AnalyzerEvolution Decision EngineStrategic PlannerPriority & Budget ManagerGoal & Policy ManagerEvent CollectorProject Dashboard人类：项目目标 / 约束 / 授权审批 / 调整目标

这里有一个关键设计：Director 和 Executor 必须分离。

Director 是整个研发系统的责任主体，负责项目演进决策；Executor 是可替换的执行资源，负责完成具体工程工作。

即使未来 Claude Code 被更好的 Coding Agent 替代，Director 的项目认知、任务历史、架构知识、质量基线都不需要重建。

## 三、六个子系统的具体设计

### 1. Project Control Plane：项目控制面

它管理的是项目的长期目标和自主权，而不是代码执行。

主要模块：

| 模块               | 核心职责                    |
| ---------------- | ----------------------- |
| Goal Manager     | 管理项目愿景、阶段目标和可量化结果       |
| Policy Manager   | 管理不可违反的约束和权限            |
| Project Registry | 管理多个项目及其仓库、环境和技术栈       |
| Event Collector  | 收集代码、测试、缺陷、依赖、运行时等事件    |
| Autonomy Manager | 控制 Agent 自主程度和审批级别      |
| Budget Manager   | 管理 Token、模型调用、计算资源和时间预算 |

项目目标不能只是一段自然语言。

应该把目标拆成：

- Objective：希望实现什么。
- Key Results：通过哪些指标判断实现。
- Constraints：哪些条件不能违反。
- Non-goals：明确不做什么。
- Autonomy Policy：允许系统自主做哪些事情。
- Stop Conditions：满足什么条件后停止演进。

例如：

```
project:
  id: my-product
  objective:
    - 建成完整的在线PPT制作平台
    - 支持AI生成和编辑SVG页面
    - 支持模板提取和复用

  key_results:
    - id: KR-001
      metric: e2e_success_rate
      target: ">= 0.95"
    - id: KR-002
      metric: svg_render_success_rate
      target: ">= 0.99"

  constraints:
    - 不允许未经授权修改生产环境
    - 所有代码变更必须通过自动化测试
    - 不允许绕过权限和安全策略

  autonomy:
    code_changes: autonomous
    test_execution: autonomous
    branch_creation: autonomous
    merge_to_main: approval_required
    production_deployment: approval_required

  budget:
    max_daily_cost: 50
    max_parallel_agents: 4
```

以上是结构示意，实际应支持不同指标类型、单位、评测方法和预算币种。

关键原则：目标由人确定，差距由 Agent 识别，演进策略由 Agent 生成，权限边界由系统强制执行。

### 2. Autonomous Director：自主研发指挥官

这是整个系统最核心的部分。

我建议不要只使用一个通用 Director Prompt，而是构建四个逻辑角色。

Project Director

负责最终演进决策，综合项目目标、风险、成本与收益。

Project Analyst

理解项目现状，识别实现差距、缺陷和机会。

Product Strategist

判断产品价值、需求优先级和功能演进方向。

Chief Architect

判断架构质量、技术债、技术选型和重构必要性。

Quality Strategist

评估质量、可靠性、安全性和测试充分性。

四个角色不一定是四个常驻 Agent。建议初期作为四种独立的决策任务，由同一个 Director 调用不同模型或 Prompt 完成。

Director 每轮需要回答五个问题：

1. 项目当前真实状态是什么？
2. 距离最终目标还有哪些差距？
3. 当前最值得解决的问题是什么？
4. 应该生成哪些具体任务？
5. 哪些任务值得现在执行，哪些应该延后或放弃？

这里最重要的是第二、第三个问题。

不能简单地让 Agent 遍历 TODO，然后挑一个执行。因为 TODO 只是已知任务，不代表项目真正需要的下一步。

我建议建立五类演进机会：

| 演进类别 | 发现机制      | 任务示例      |
| ---- | --------- | --------- |
| 产品演进 | 目标差距、用户反馈 | 缺失的核心功能   |
| 架构演进 | 依赖分析、架构规则 | 模块耦合、架构重构 |
| 工程演进 | CI、研发流程分析 | 构建速度、开发体验 |
| 质量演进 | 测试、线上反馈   | 缺陷修复、测试补齐 |
| 技术演进 | 依赖版本、技术趋势 | 依赖升级、技术替换 |

注意：技术趋势不应直接转化为升级任务，必须先证明它对当前项目有实际收益。

### 3. Evolution Engine：自主演进决策引擎

这是我认为最值得单独设计的模块。

它解决的是：系统如何主动发现自己应该做什么，而不是被动接收任务。

我建议将其分成三个层次。

第一层：项目状态观察（Observe）

建立持续观察机制，数据来源包括：

- Git Commit、PR、代码变更。
- 单元测试、集成测试、E2E 测试。
- 静态代码分析、依赖漏洞、代码复杂度。
- 运行时性能、日志、异常和业务指标。
- 产品需求、Issue、用户反馈。
- 技术文档、架构决策和设计规范。

第二层：演进机会发现（Discover）

将发现的问题转化成统一的 Evolution Candidate。

例如：

```
candidate:
  id: EVO-1024
  category: architecture
  title: 拆分用户模块中的权限逻辑

  evidence:
    - 文件耦合度过高
    - 多个模块直接访问内部实现
    - 单元测试隔离困难

  hypothesis:
    拆分权限领域服务可以降低模块耦合，
    提高独立测试能力。

  expected_outcome:
    - 降低模块依赖数量
    - 提高测试覆盖率

  risk: medium
  confidence: 0.82
  estimated_effort: 2d
```

Candidate 不等于任务。它只是一个待评估的改进机会。

第三层：演进价值评估（Prioritize）

建议采用多维评分，而不是完全交给 LLM 主观排序。

可以先使用下面的启发式模型：

\\[ Priority = \frac{V \times I \times C \times U} {E \times (1+R)} \\]

其中：

- \\(V\\)：预期价值。
- \\(I\\)：影响范围。
- \\(C\\)：证据置信度。
- \\(U\\)：紧迫程度。
- \\(E\\)：实施成本。
- \\(R\\)：风险系数。

所有指标归一化到 0～1，成本可以设定合理下限。

这不是成熟的价值模型，后续应该用实际任务收益、失败率和返工成本校准。

特别建议增加一个机制：不行动也是一个合法决策。

例如：

- 缺乏足够证据，先补充分析。
- 改进收益太低，暂不执行。
- 存在重大架构风险，要求专家审查。
- 项目当前处于稳定期，不主动引入高风险变更。

### 4. Agent Execution Plane：多执行器调度层

这是你选择不限定 Coding Agent 后最重要的技术设计。

我不建议直接把多个 Coding Agent 集成在 Director 的业务逻辑里面，而是建立一个统一的执行器协议。

目前值得考虑的执行器：

[Claude Code in Production: 40% Productivity Increase on a Large Project - DEV Community](https://images.openai.com/static-rsc-4/2_fVAyjh2A7rIE9SxJrra2xCv4ZlpBBEPC9M4stzaB1bUAOTIGECnWZxuPRa23fPYhIVPWUS3do-g52ox0TQAMr429CjCkeQUgX_zMLlw-F0X8q0uJLyMxkzWozWi99IGc4f_p3ld6pg-HHL2mUqMtDP3nKHvvj6W4e0CiCoJTk?purpose=inline)

Claude Code

擅长复杂代码仓库理解、长任务执行和工具协作。

接入：Agent SDK / CLI

[官方 Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview?utm_source=chatgpt.com)

[🚨 AI News | TestingCatalog (@testingcatalog) on X](https://images.openai.com/static-rsc-4/c2zFI940OGngXaTnP1M7m6IHV93bVtdY17cAdejdSNVKKbzcrDy5_bmfQ2hOXi9wp43Oct70G6OHP85-a7OOl7zS1YkaD8ngKTccu1E52NYeZqiEewTXKHh6vl4NwwfW9JHOkRFYlvKN9N1lR5rHdZsOSuFRPwez6B7yJX1pAnI?purpose=inline)

Codex

作为另一种独立执行器，适合代码任务执行、测试和并行开发。

接入：Codex CLI / SDK 能力或受支持的执行接口

[官方 Codex 文档](https://developers.openai.com/codex/?utm_source=chatgpt.com)

[What Is Oh My OpenAgent (OMO)? Complete 2026 Guide to OpenCode Multi-Agent Orchestration Harness | Blog | a2a mcp](https://images.openai.com/static-rsc-4/N_twF1GO9SW6FueQ52YRjAHcCGWkTwWpXZH9o_jsnCLMDvXOVVma-gwYTbBAWgPgHCXzG6s9UeTIFy_ZikYcHkUHtTwoQ_vlJQc0fo1wK2ez0wXrhdhTQHXoAMA15Cla8UGPZ736R63XoJkNYDuNcDNbtZTUbAiAqSbipWjvQXA?purpose=inline)

OpenCode

适合自定义 Agent、模型路由和可编程接入。

接入：Server API / TypeScript SDK

[官方文档](https://opencode.ai/docs/?utm_source=chatgpt.com)

[OpenHands — Coding Agents | AI Landscape](https://images.openai.com/static-rsc-4/PAYKgXPwTL_-02RxjiwhQLywd-NsHf57Wk6-pdCYvqNKkfoy-0F094e2tJww4JDBGtFmg6C1XqOLcBuvhXF2g4Oa71tQPT1gKO7xStitMLkY0VMwyIupz4c8P5GbfygZlINzx_EnnFNlCaTCsNDt2ev06-X3BqeTvHhmchMRWw8?purpose=inline)

OpenHands

适合模型无关的 Agent Runtime、隔离工作空间和复杂开发任务。

接入：SDK / Agent Server

[GitHub SDK](https://github.com/OpenHands/software-agent-sdk?utm_source=chatgpt.com)

[Brand Kit | Gemini CLI](https://images.openai.com/static-rsc-4/9ENF2VurCGrxjWWOBvxpEULkDyJp9OIWKQgT1W1vPz2jXq7nwEE8Tt0yPBo4E4RLaO6W34AYlvEQAYw6JL2JnezVsdXsQEwa8S9BhnbMCZd_QOIFfFwg_A3_7pEN7VUZyqZHLYm969vWgwKbk3XlVfwCmUy9hAcaBxVYQdIb58E?purpose=inline)

Gemini CLI

作为额外的执行器和代码分析能力来源。

接入：CLI

[GitHub 项目](https://github.com/google-gemini/gemini-cli?utm_source=chatgpt.com)

这里有一个值得特别关注的开源方案：OpenHands 现在的生态已经提供 SDK、Agent Server 和 Automation，并且其平台介绍支持 Claude Code、Codex、Gemini CLI、OpenHands 等不同执行后端。它值得作为执行层的候选，而不是必须自己从零开发所有运行时。

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub+2



我的建议：优先研究 OpenHands Automation / SDK 能否满足执行调度需求；不满足的部分再自己实现 Adapter。

#### Agent Adapter 接口设计

统一执行器的核心接口应该是：

```
class CodingAgentAdapter:    async def create_session(self, config):        """创建执行会话"""    async def execute(self, task):        """提交开发任务"""    async def get_status(self, session_id):        """查询执行状态"""    async def stream_events(self, session_id):        """获取实时执行事件"""    async def interrupt(self, session_id):        """中断执行"""    async def collect_result(self, session_id):        """收集代码变更和执行结果"""    async def terminate(self, session_id):        """结束会话"""
```

实际工程中还需要补充认证、超时、能力声明、取消确认、幂等键和错误分类。

执行器必须返回统一结果：

```
{
  "task_id": "TASK-1024",
  "executor": "claude-code",
  "status": "completed",
  "artifacts": {
    "branch": "agent/TASK-1024",
    "commit": "abc123",
    "changed_files": 12
  },
  "execution": {
    "duration": 360,
    "tool_calls": 35,
    "cost": 2.4
  },
  "self_assessment": {
    "completed": true,
    "limitations": []
  }
}
```

注意 `self_assessment` 只是 Agent 的自我报告，不是验收结果。

#### 执行器协议：ACP、CLI 和 SDK 如何选择？

这里容易走错方向。

- CLI：通用性最高，几乎任何 Coding Agent 都能接入。
- SDK：能力丰富，但容易和具体供应商绑定。
- ACP（Agent Client Protocol）：可以作为支持该协议的 Agent 的标准化交互接口。
- MCP：用于连接工具和数据，不是 Coding Agent 执行协议。

ACP 的目标是标准化编辑器与 Coding Agent 之间的通信，并不是所有 Agent 都必须实现它。

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub



因此建议采用：

```
Agent Adapter
    |
    +-- ACP Adapter
    |
    +-- SDK Adapter
    |
    +-- CLI Adapter
    |
    +-- OpenHands Server Adapter
```

不要强制所有执行器实现同一种底层协议，而是强制它们实现同一种系统级任务契约。

### 5. Engineering Harness：工程执行与质量保障

这是整个系统能否真正交付软件，而不只是生成代码的决定性部分。

我建议把 Harness 分成四个子系统。

A. Workspace Manager：工作空间管理

每个开发任务都应该有独立的工作环境：

```
Project
  |
  +-- Main Repository
  |
  +-- Task Workspace A
  |     +-- Git Worktree A
  |     +-- Coding Agent A
  |     +-- Test Environment A
  |
  +-- Task Workspace B
        +-- Git Worktree B
        +-- Coding Agent B
        +-- Test Environment B
```

推荐技术：

| 技术           | 用途               | 选择建议      |
| ------------ | ---------------- | --------- |
| Git Worktree | 同一仓库的并行分支工作空间    | 必选        |
| Docker       | 隔离代码执行           | POC 首选    |
| Daytona      | 远程隔离 Sandbox     | 多项目生产环境候选 |
| Kubernetes   | 大规模 Agent 执行资源管理 | 后期按需引入    |

Daytona 提供隔离的执行环境、独立文件系统、网络、进程和 Git 操作能力，适合运行不可信的 AI 生成代码。

[image](https://www.google.com/s2/favicons?domain=https://www.daytona.com\&sz=32)

daytona.com+2



我的建议是：

- 单机 POC：Docker + Git Worktree。
- 多机、多项目：Daytona 或自建 Kubernetes + 微虚拟机。
- 不要让 Coding Agent 直接访问宿主机的 SSH 凭据、云平台管理员密钥和生产数据库。

B. Verification Engine：独立验证引擎

不要让 Coding Agent 自己决定是否验收通过。

验证引擎应当独立于执行器，并具备以下能力：

| 验证层  | 验证内容     | 验证方式           |
| ---- | -------- | -------------- |
| 功能验证 | 需求是否实现   | 单元、集成、E2E      |
| 代码验证 | 代码是否符合规范 | Lint、静态分析      |
| 架构验证 | 是否违反架构约束 | 依赖规则、架构测试      |
| 安全验证 | 是否存在安全问题 | SAST、依赖漏洞、密钥扫描 |
| 性能验证 | 是否出现性能退化 | Benchmark、负载测试 |
| 产品验证 | 是否达到预期体验 | 场景测试、人工评估      |

尤其需要增加一个概念：

Acceptance Contract（验收契约）

每个任务在开始执行前，必须先定义怎样才算完成。

例如：

```
acceptance:
  task_id: TASK-1024

  required:
    - unit_tests_pass
    - integration_tests_pass
    - lint_pass
    - architecture_rules_pass

  thresholds:
    test_coverage:
      operator: ">="
      value: 0.85

  forbidden:
    - secrets_in_diff
    - unauthorized_dependency_change
    - public_api_breaking_change

  artifacts:
    - git_diff
    - test_report
    - execution_trace
```

这里的覆盖率阈值只是示意，实际应根据模块基线确定，不能通过降低测试质量来达到指标。

C. Integration Engine：代码集成

多个 Agent 并行开发时，不能直接共享主分支。

建议采用：

```
Task Branch
    |
    v
Local Validation
    |
    v
Integration Branch
    |
    v
Cross-task Integration Test
    |
    v
Architecture & Regression Test
    |
    v
Merge Approval
    |
    v
Main Branch
```

特别注意：单任务通过测试，不代表多个任务集成后也通过测试。

应该建立集成任务，由独立 Integrator Agent 或确定性集成程序解决冲突、执行回归验证。

D. Security & Governance Engine：执行治理

持续自演进意味着系统可能长期运行并且拥有大量工具权限。

至少需要：

- 每个 Agent 有独立执行身份。
- 每个任务有独立 Task ID 和授权范围。
- Agent 的权限绑定到任务、项目、工作空间和有效期。
- 默认禁止访问生产环境。
- 网络访问采用白名单。
- 凭证使用短期令牌，不向 Prompt 暴露长期密钥。
- 删除、发布、合并、部署等高风险操作有独立授权。
- 每一次工具调用均记录主体、任务、资源和执行结果。

特别重要的是：Agent 身份不能替代任务身份。

同一个 Coding Agent 可以执行多个不同任务，但每个任务应有独立的授权上下文，不能因为某个 Agent 获得了仓库访问权限，就自动拥有该仓库所有操作的权限。

### 6. Project Knowledge & State：项目认知与状态系统

这是长期自主演进的基础。

如果每一轮 Agent 都重新阅读整个仓库、重新理解项目、重新推断架构，那么不仅成本高，而且容易出现认知不一致。

我建议采用四类存储。

| 存储    | 推荐技术                      | 存放内容            |
| ----- | ------------------------- | --------------- |
| 项目状态库 | PostgreSQL                | 目标、任务、状态、指标、决策  |
| 项目事实库 | Git + Markdown/YAML       | SPEC、ADR、架构图、规范 |
| 语义检索  | PostgreSQL + pgvector（按需） | 文档、代码语义索引       |
| 执行产物库 | S3 / MinIO                | 测试报告、日志、截图、评估结果 |

这里有一个重要区别：

- Git 保存项目的真实工程资产。
- PostgreSQL 保存项目的结构化状态。
- 语义检索提供知识发现。
- 执行产物库保存不可丢失的证据。

不要把所有项目知识都存进向量数据库，也不要把 Agent 的对话历史当作项目事实。

#### 核心数据模型

建议从下面这些实体开始：

| 实体                   | 主要属性                      | 作用     |
| -------------------- | ------------------------- | ------ |
| Project              | id、goal、policy            | 项目定义   |
| ProjectSnapshot      | version、commit、metrics    | 项目状态快照 |
| EvolutionCandidate   | evidence、value、risk       | 演进机会   |
| EvolutionDecision    | decision、rationale        | 演进决策   |
| Task                 | type、priority、status      | 研发任务   |
| TaskDependency       | predecessor、successor     | 任务依赖   |
| AgentSession         | executor、session_id       | 执行会话   |
| ExecutionEvent       | event_type、payload        | 执行轨迹   |
| VerificationRun      | checks、result             | 验收记录   |
| Artifact             | uri、hash、type             | 交付证据   |
| ArchitectureDecision | context、decision、tradeoff | 架构决策   |
| EvolutionCycle       | cycle_id、outcome          | 演进周期   |

其中最关键的三个实体是：

1. EvolutionCandidate：系统发现了什么值得改进。
2. EvolutionDecision：系统为什么决定做或不做。
3. Task：系统最终决定执行什么。

这样可以区分「发现问题」「作出决策」「执行任务」，避免 Agent 把发现的问题直接转化成代码修改。

## 四、最核心的运行机制：持续自演进循环

这部分决定系统最终是自动化开发工具，还是自主研发系统。

我建议设计为两个相互独立、但可以相互触发的循环。

### 循环 A：工程执行循环

负责将已经确定的任务完成。

\#chatgpt-mermaid-\_r_19n\_{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;fill:rgb(13, 13, 13);}@keyframes edge-animation-frame{from{stroke-dashoffset:0;}}@keyframes dash{to{stroke-dashoffset:0;}}#chatgpt-mermaid-\_r_19n\_ .edge-animation-slow{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 50s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_19n\_ .edge-animation-fast{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 20s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_19n\_ .error-icon{fill:rgb(243, 243, 243);}#chatgpt-mermaid-\_r_19n\_ .error-text{fill:rgb(13, 13, 13);stroke:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19n\_ .edge-thickness-normal{stroke-width:1px;}#chatgpt-mermaid-\_r_19n\_ .edge-thickness-thick{stroke-width:3.5px;}#chatgpt-mermaid-\_r_19n\_ .edge-pattern-solid{stroke-dasharray:0;}#chatgpt-mermaid-\_r_19n\_ .edge-thickness-invisible{stroke-width:0;fill:none;}#chatgpt-mermaid-\_r_19n\_ .edge-pattern-dashed{stroke-dasharray:3;}#chatgpt-mermaid-\_r_19n\_ .edge-pattern-dotted{stroke-dasharray:2;}#chatgpt-mermaid-\_r_19n\_ .marker{fill:rgb(143, 143, 143);stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_19n\_ .marker.cross{stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_19n\_ svg{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;}#chatgpt-mermaid-\_r_19n\_ p{margin:0;}#chatgpt-mermaid-\_r_19n\_ .label{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19n\_ .cluster-label text{fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19n\_ .cluster-label span{color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19n\_ .cluster-label span p{background-color:transparent;}#chatgpt-mermaid-\_r_19n\_ .label text,#chatgpt-mermaid-\_r_19n\_ span{fill:rgb(13, 13, 13);color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19n\_ .node rect,#chatgpt-mermaid-\_r_19n\_ .node circle,#chatgpt-mermaid-\_r_19n\_ .node ellipse,#chatgpt-mermaid-\_r_19n\_ .node polygon,#chatgpt-mermaid-\_r_19n\_ .node path{fill:rgb(222, 234, 251);stroke:rgb(83, 154, 248);stroke-width:1px;}#chatgpt-mermaid-\_r_19n\_ .rough-node .label text,#chatgpt-mermaid-\_r_19n\_ .node .label text,#chatgpt-mermaid-\_r_19n\_ .image-shape .label,#chatgpt-mermaid-\_r_19n\_ .icon-shape .label{text-anchor:middle;}#chatgpt-mermaid-\_r_19n\_ .node .katex path{fill:#000;stroke:#000;stroke-width:1px;}#chatgpt-mermaid-\_r_19n\_ .rough-node .label,#chatgpt-mermaid-\_r_19n\_ .node .label,#chatgpt-mermaid-\_r_19n\_ .image-shape .label,#chatgpt-mermaid-\_r_19n\_ .icon-shape .label{text-align:center;}#chatgpt-mermaid-\_r_19n\_ .node.clickable{cursor:pointer;}#chatgpt-mermaid-\_r_19n\_ .root .anchor path{fill:rgb(143, 143, 143)!important;stroke-width:0;stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_19n\_ .arrowheadPath{fill:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_19n\_ .edgePath .path{stroke:rgb(143, 143, 143);stroke-width:1px;}#chatgpt-mermaid-\_r_19n\_ .flowchart-link{stroke:rgb(143, 143, 143);fill:none;}#chatgpt-mermaid-\_r_19n\_ .edgeLabel{background-color:rgb(252, 252, 252);text-align:center;}#chatgpt-mermaid-\_r_19n\_ .edgeLabel p{background-color:rgb(252, 252, 252);}#chatgpt-mermaid-\_r_19n\_ .edgeLabel rect{opacity:0.5;background-color:rgb(252, 252, 252);fill:rgb(252, 252, 252);}#chatgpt-mermaid-\_r_19n\_ .labelBkg{background-color:rgba(252, 252, 252, 0.5);}#chatgpt-mermaid-\_r_19n\_ .cluster rect{fill:rgb(243, 243, 243);stroke:rgba(0, 0, 0, 0.1);stroke-width:1px;}#chatgpt-mermaid-\_r_19n\_ .cluster text{fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19n\_ .cluster span{color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19n\_ div.mermaidTooltip{position:absolute;text-align:center;max-width:200px;padding:2px;font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:12px;background:rgb(243, 243, 243);border:1px solid rgba(0, 0, 0, 0.1);border-radius:2px;pointer-events:none;z-index:100;}#chatgpt-mermaid-\_r_19n\_ .flowchartTitleText{text-anchor:middle;font-size:18px;fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19n\_ rect.text{fill:none;stroke-width:0;}#chatgpt-mermaid-\_r_19n\_ .icon-shape,#chatgpt-mermaid-\_r_19n\_ .image-shape{background-color:rgb(252, 252, 252);text-align:center;}#chatgpt-mermaid-\_r_19n\_ .icon-shape p,#chatgpt-mermaid-\_r_19n\_ .image-shape p{background-color:rgb(252, 252, 252);padding:2px;}#chatgpt-mermaid-\_r_19n\_ .icon-shape .label rect,#chatgpt-mermaid-\_r_19n\_ .image-shape .label rect{opacity:0.5;background-color:rgb(252, 252, 252);fill:rgb(252, 252, 252);}#chatgpt-mermaid-\_r_19n\_ .label-icon{display:inline-block;height:1em;overflow:visible;vertical-align:-0.125em;}#chatgpt-mermaid-\_r_19n\_ .node .label-icon path{fill:currentColor;stroke:revert;stroke-width:revert;}#chatgpt-mermaid-\_r_19n\_ .node .neo-node{stroke:rgb(83, 154, 248);}#chatgpt-mermaid-\_r_19n\_ [data-look="neo"].node rect,#chatgpt-mermaid-\_r_19n\_ [data-look="neo"].cluster rect,#chatgpt-mermaid-\_r_19n\_ [data-look="neo"].node polygon{stroke:url(#chatgpt-mermaid-\_r_19n\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_19n\_ [data-look="neo"].swimlane.cluster rect{filter:none;}#chatgpt-mermaid-\_r_19n\_ [data-look="neo"].node path{stroke:url(#chatgpt-mermaid-\_r_19n\_-gradient);stroke-width:1px;}#chatgpt-mermaid-\_r_19n\_ [data-look="neo"].node .outer-path{filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_19n\_ [data-look="neo"].node .neo-line path{stroke:rgb(83, 154, 248);filter:none;}#chatgpt-mermaid-\_r_19n\_ [data-look="neo"].node circle{stroke:url(#chatgpt-mermaid-\_r_19n\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_19n\_ [data-look="neo"].node circle .state-start{fill:#000000;}#chatgpt-mermaid-\_r_19n\_ [data-look="neo"].icon-shape .icon{fill:url(#chatgpt-mermaid-\_r_19n\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_19n\_ [data-look="neo"].icon-shape .icon-neo path{stroke:url(#chatgpt-mermaid-\_r_19n\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_19n\_ .node text{font-size:14px;font-weight:600;letter-spacing:normal;fill:rgb(0, 79, 153);}#chatgpt-mermaid-\_r_19n\_ .edgeLabels text{font-size:13px;font-weight:600;letter-spacing:-0.08px;fill:rgb(0, 79, 153);}#chatgpt-mermaid-\_r_19n\_ .node tspan[font-weight="normal"],#chatgpt-mermaid-\_r_19n\_ .edgeLabels tspan[font-weight="normal"]{font-weight:600;}#chatgpt-mermaid-\_r_19n\_ .edgeLabel .label rect{opacity:1;rx:13px;ry:13px;fill:rgb(245, 250, 255);stroke:rgb(206, 219, 229);stroke-width:1px;}#chatgpt-mermaid-\_r_19n\_ .node rect,#chatgpt-mermaid-\_r_19n\_ .node circle,#chatgpt-mermaid-\_r_19n\_ .node ellipse,#chatgpt-mermaid-\_r_19n\_ .node polygon,#chatgpt-mermaid-\_r_19n\_ .node path{fill:rgb(229, 243, 255);stroke:rgba(0, 0, 0, 0.1);stroke-width:1px;}#chatgpt-mermaid-\_r_19n\_ .node rect{rx:16px;ry:16px;}#chatgpt-mermaid-\_r_19n\_ .mermaid-decision .label-container{fill:rgb(245, 250, 255);stroke:rgb(206, 219, 229);stroke-dasharray:2,2;}#chatgpt-mermaid-\_r_19n\_ .edgePaths .flowchart-link{stroke:rgb(143, 143, 143);stroke-width:1px;stroke-linecap:round;stroke-linejoin:round;}#chatgpt-mermaid-\_r_19n\_ .marker{fill:rgb(143, 143, 143);stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_19n\_ :root{--mermaid-font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";}Task ReadyTask PlannerSelect Coding AgentCreate Isolated WorkspaceAgent ExecutesCollect Git Diff & ArtifactsIndependent VerificationAcceptance Passed?Integration & Merge GateFailure DiagnosisCan Auto Repair?Create Repair TaskEscalate / BlockUpdate Project StateExecution CompleteYesNoYesNo

注意，这个循环可以有多种任务类型：

- Feature Development
- Bug Fix
- Refactoring
- Test Improvement
- Performance Optimization
- Dependency Upgrade
- Documentation Update

不能所有任务都走相同的验收流程。

例如，重构任务需要强调行为保持和回归测试，性能优化任务需要比较优化前后的基准，安全修复任务需要漏洞复测。

### 循环 B：项目自主演进循环

这是整个系统的核心竞争力。

\#chatgpt-mermaid-\_r_19u\_{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;fill:rgb(13, 13, 13);}@keyframes edge-animation-frame{from{stroke-dashoffset:0;}}@keyframes dash{to{stroke-dashoffset:0;}}#chatgpt-mermaid-\_r_19u\_ .edge-animation-slow{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 50s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_19u\_ .edge-animation-fast{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 20s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_19u\_ .error-icon{fill:rgb(243, 243, 243);}#chatgpt-mermaid-\_r_19u\_ .error-text{fill:rgb(13, 13, 13);stroke:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19u\_ .edge-thickness-normal{stroke-width:1px;}#chatgpt-mermaid-\_r_19u\_ .edge-thickness-thick{stroke-width:3.5px;}#chatgpt-mermaid-\_r_19u\_ .edge-pattern-solid{stroke-dasharray:0;}#chatgpt-mermaid-\_r_19u\_ .edge-thickness-invisible{stroke-width:0;fill:none;}#chatgpt-mermaid-\_r_19u\_ .edge-pattern-dashed{stroke-dasharray:3;}#chatgpt-mermaid-\_r_19u\_ .edge-pattern-dotted{stroke-dasharray:2;}#chatgpt-mermaid-\_r_19u\_ .marker{fill:rgb(143, 143, 143);stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_19u\_ .marker.cross{stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_19u\_ svg{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;}#chatgpt-mermaid-\_r_19u\_ p{margin:0;}#chatgpt-mermaid-\_r_19u\_ .label{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19u\_ .cluster-label text{fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19u\_ .cluster-label span{color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19u\_ .cluster-label span p{background-color:transparent;}#chatgpt-mermaid-\_r_19u\_ .label text,#chatgpt-mermaid-\_r_19u\_ span{fill:rgb(13, 13, 13);color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19u\_ .node rect,#chatgpt-mermaid-\_r_19u\_ .node circle,#chatgpt-mermaid-\_r_19u\_ .node ellipse,#chatgpt-mermaid-\_r_19u\_ .node polygon,#chatgpt-mermaid-\_r_19u\_ .node path{fill:rgb(222, 234, 251);stroke:rgb(83, 154, 248);stroke-width:1px;}#chatgpt-mermaid-\_r_19u\_ .rough-node .label text,#chatgpt-mermaid-\_r_19u\_ .node .label text,#chatgpt-mermaid-\_r_19u\_ .image-shape .label,#chatgpt-mermaid-\_r_19u\_ .icon-shape .label{text-anchor:middle;}#chatgpt-mermaid-\_r_19u\_ .node .katex path{fill:#000;stroke:#000;stroke-width:1px;}#chatgpt-mermaid-\_r_19u\_ .rough-node .label,#chatgpt-mermaid-\_r_19u\_ .node .label,#chatgpt-mermaid-\_r_19u\_ .image-shape .label,#chatgpt-mermaid-\_r_19u\_ .icon-shape .label{text-align:center;}#chatgpt-mermaid-\_r_19u\_ .node.clickable{cursor:pointer;}#chatgpt-mermaid-\_r_19u\_ .root .anchor path{fill:rgb(143, 143, 143)!important;stroke-width:0;stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_19u\_ .arrowheadPath{fill:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_19u\_ .edgePath .path{stroke:rgb(143, 143, 143);stroke-width:1px;}#chatgpt-mermaid-\_r_19u\_ .flowchart-link{stroke:rgb(143, 143, 143);fill:none;}#chatgpt-mermaid-\_r_19u\_ .edgeLabel{background-color:rgb(252, 252, 252);text-align:center;}#chatgpt-mermaid-\_r_19u\_ .edgeLabel p{background-color:rgb(252, 252, 252);}#chatgpt-mermaid-\_r_19u\_ .edgeLabel rect{opacity:0.5;background-color:rgb(252, 252, 252);fill:rgb(252, 252, 252);}#chatgpt-mermaid-\_r_19u\_ .labelBkg{background-color:rgba(252, 252, 252, 0.5);}#chatgpt-mermaid-\_r_19u\_ .cluster rect{fill:rgb(243, 243, 243);stroke:rgba(0, 0, 0, 0.1);stroke-width:1px;}#chatgpt-mermaid-\_r_19u\_ .cluster text{fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19u\_ .cluster span{color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19u\_ div.mermaidTooltip{position:absolute;text-align:center;max-width:200px;padding:2px;font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:12px;background:rgb(243, 243, 243);border:1px solid rgba(0, 0, 0, 0.1);border-radius:2px;pointer-events:none;z-index:100;}#chatgpt-mermaid-\_r_19u\_ .flowchartTitleText{text-anchor:middle;font-size:18px;fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_19u\_ rect.text{fill:none;stroke-width:0;}#chatgpt-mermaid-\_r_19u\_ .icon-shape,#chatgpt-mermaid-\_r_19u\_ .image-shape{background-color:rgb(252, 252, 252);text-align:center;}#chatgpt-mermaid-\_r_19u\_ .icon-shape p,#chatgpt-mermaid-\_r_19u\_ .image-shape p{background-color:rgb(252, 252, 252);padding:2px;}#chatgpt-mermaid-\_r_19u\_ .icon-shape .label rect,#chatgpt-mermaid-\_r_19u\_ .image-shape .label rect{opacity:0.5;background-color:rgb(252, 252, 252);fill:rgb(252, 252, 252);}#chatgpt-mermaid-\_r_19u\_ .label-icon{display:inline-block;height:1em;overflow:visible;vertical-align:-0.125em;}#chatgpt-mermaid-\_r_19u\_ .node .label-icon path{fill:currentColor;stroke:revert;stroke-width:revert;}#chatgpt-mermaid-\_r_19u\_ .node .neo-node{stroke:rgb(83, 154, 248);}#chatgpt-mermaid-\_r_19u\_ [data-look="neo"].node rect,#chatgpt-mermaid-\_r_19u\_ [data-look="neo"].cluster rect,#chatgpt-mermaid-\_r_19u\_ [data-look="neo"].node polygon{stroke:url(#chatgpt-mermaid-\_r_19u\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_19u\_ [data-look="neo"].swimlane.cluster rect{filter:none;}#chatgpt-mermaid-\_r_19u\_ [data-look="neo"].node path{stroke:url(#chatgpt-mermaid-\_r_19u\_-gradient);stroke-width:1px;}#chatgpt-mermaid-\_r_19u\_ [data-look="neo"].node .outer-path{filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_19u\_ [data-look="neo"].node .neo-line path{stroke:rgb(83, 154, 248);filter:none;}#chatgpt-mermaid-\_r_19u\_ [data-look="neo"].node circle{stroke:url(#chatgpt-mermaid-\_r_19u\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_19u\_ [data-look="neo"].node circle .state-start{fill:#000000;}#chatgpt-mermaid-\_r_19u\_ [data-look="neo"].icon-shape .icon{fill:url(#chatgpt-mermaid-\_r_19u\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_19u\_ [data-look="neo"].icon-shape .icon-neo path{stroke:url(#chatgpt-mermaid-\_r_19u\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_19u\_ .node text{font-size:14px;font-weight:600;letter-spacing:normal;fill:rgb(0, 79, 153);}#chatgpt-mermaid-\_r_19u\_ .edgeLabels text{font-size:13px;font-weight:600;letter-spacing:-0.08px;fill:rgb(0, 79, 153);}#chatgpt-mermaid-\_r_19u\_ .node tspan[font-weight="normal"],#chatgpt-mermaid-\_r_19u\_ .edgeLabels tspan[font-weight="normal"]{font-weight:600;}#chatgpt-mermaid-\_r_19u\_ .edgeLabel .label rect{opacity:1;rx:13px;ry:13px;fill:rgb(245, 250, 255);stroke:rgb(206, 219, 229);stroke-width:1px;}#chatgpt-mermaid-\_r_19u\_ .node rect,#chatgpt-mermaid-\_r_19u\_ .node circle,#chatgpt-mermaid-\_r_19u\_ .node ellipse,#chatgpt-mermaid-\_r_19u\_ .node polygon,#chatgpt-mermaid-\_r_19u\_ .node path{fill:rgb(229, 243, 255);stroke:rgba(0, 0, 0, 0.1);stroke-width:1px;}#chatgpt-mermaid-\_r_19u\_ .node rect{rx:16px;ry:16px;}#chatgpt-mermaid-\_r_19u\_ .mermaid-decision .label-container{fill:rgb(245, 250, 255);stroke:rgb(206, 219, 229);stroke-dasharray:2,2;}#chatgpt-mermaid-\_r_19u\_ .edgePaths .flowchart-link{stroke:rgb(143, 143, 143);stroke-width:1px;stroke-linecap:round;stroke-linejoin:round;}#chatgpt-mermaid-\_r_19u\_ .marker{fill:rgb(143, 143, 143);stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_19u\_ :root{--mermaid-font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";}Observe ProjectBuild Project SnapshotAnalyze Goal GapDiscover EvolutionCandidatesGather EvidenceEvaluate Value / Risk / CostWorth Doing?Record / Defer / RejectGenerate Evolution PlanDecompose into TasksDependency & ResourceSchedulingExecute Engineering CycleEvaluate Actual OutcomeUpdate Project KnowledgeUpdate Goal GapNoYes

两个循环的区别：

| 维度   | 工程执行循环            | 项目演进循环    |
| ---- | ----------------- | --------- |
| 输入   | 已确定的任务            | 项目当前状态    |
| 目标   | 完成任务              | 找到值得完成的任务 |
| 核心判断 | 怎么实现              | 应该实现什么    |
| 输出   | 代码和验收结果           | 下一轮演进计划   |
| 运行周期 | 分钟到小时             | 小时到天      |
| 责任主体 | Coding Agent + QA | Director  |

这两个循环不能合并成一个无限执行 Prompt。

否则很容易出现两个问题：

- Coding Agent 不断创造新的开发任务，让项目陷入无意义的自我修改。
- Agent 不断修复自己制造的问题，形成无限的修复循环。

### 持续演进应该由事件驱动，而不是死循环

建议设置六种触发源：

| 触发源  | 典型事件        | 响应       |
| ---- | ----------- | -------- |
| 定时触发 | 每日、每周       | 全项目扫描    |
| 代码事件 | Commit、PR   | 重新分析变更影响 |
| 质量事件 | CI 失败、覆盖率下降 | 自动分析和修复  |
| 运行事件 | 性能退化、异常增加   | 创建诊断任务   |
| 外部事件 | 用户反馈、需求变化   | 重新规划     |
| 知识事件 | 依赖漏洞、技术升级   | 评估是否需要演进 |

我特别建议将定期扫描和事件触发结合起来。

事件触发适合及时处理局部变化，定期扫描适合发现跨模块、长期积累的问题。

## 五、如何真正让 Director 具备自主规划能力

这是我建议重点投入研发的部分。

不要只是给 Director 一个 Prompt：

> 你是一个优秀的软件架构师，请不断优化这个项目。

这类 Prompt 缺少明确的证据链、决策规则和约束。

我建议把 Director 的决策拆成五个阶段。

| 阶段 | 输入               | 输出                    |
| -- | ---------------- | --------------------- |
| 认知 | 项目快照、目标、历史       | Project Understanding |
| 诊断 | 目标差距、质量指标        | Gap Analysis          |
| 探索 | Gap Analysis     | Evolution Candidates  |
| 决策 | Candidates、预算、约束 | Evolution Decisions   |
| 规划 | 已批准的 Decisions   | Evolution Roadmap     |

每一阶段都应该有结构化输出，并且允许下游独立验证。

例如，Director 每次完成项目分析后，输出：

```
project_assessment:
  project_id: my-product
  snapshot: commit-abc123

  overall_status: evolving

  goal_gap:
    - id: GAP-001
      description: SVG页面渲染质量未达到目标
      evidence:
        - test_report: artifact://report-001
      severity: high

    - id: GAP-002
      description: 模板提取缺少复杂布局支持
      evidence:
        - issue: ISSUE-102
      severity: medium

  opportunities:
    - id: EVO-001
      category: product
      priority: high
      action: 补充复杂布局解析能力

    - id: EVO-002
      category: quality
      priority: high
      action: 建立SVG渲染回归评测集

  recommendations:
    - EVO-002
    - EVO-001

  deferred:
    - reason: 当前证据不足
      candidate: EVO-003
```

这个例子中，系统可能会发现：先建立 SVG 渲染回归评测集，比直接开发更多 SVG 功能更有价值。

这就是项目级自主决策与普通任务自动化的区别。

### 建议增加一个独立的 Critic

Director 给出演进计划后，不要立即执行。

让独立 Critic 检查：

1. 这个问题是否真实存在？
2. 是否有充分证据？
3. 是否已经有其他任务解决？
4. 是否违反项目目标或架构约束？
5. 有没有更低成本的解决方案？
6. 是否引入不必要的复杂性？
7. 是否值得现在执行？

Critic 不能只给出一个 LLM 评分，还应该调用代码仓库、Git 历史、测试报告和项目状态进行事实核验。

## 六、Temporal + LangGraph 的具体编排设计

这部分给出实际的程序结构。

我建议采用双层编排：

- Temporal：管理项目的长期生命周期。
- LangGraph：负责每一次需要智能判断的决策过程。

例如：

```
ProjectEvolutionWorkflow
    |
    +-- WaitForEventActivity
    |
    +-- AnalyzeProjectActivity
    |       |
    |       +-- LangGraph: Project Analysis
    |
    +-- DiscoverOpportunitiesActivity
    |       |
    |       +-- LangGraph: Candidate Discovery
    |
    +-- EvaluateCandidatesActivity
    |       |
    |       +-- LangGraph: Decision & Critic
    |
    +-- GeneratePlanActivity
    |       |
    |       +-- LangGraph: Planning
    |
    +-- ExecuteTasksWorkflow
    |       |
    |       +-- TaskWorkflow A
    |       |       +-- Agent Adapter
    |       |       +-- Verification
    |       |
    |       +-- TaskWorkflow B
    |               +-- Agent Adapter
    |               +-- Verification
    |
    +-- EvaluateEvolutionActivity
    |
    +-- UpdateProjectKnowledgeActivity
    |
    +-- Continue / Wait / Stop
```

Temporal 的 Workflow 负责确定性状态流转，LLM 调用、外部 API、数据库操作等放在 Activity 中执行，利用其重试、超时和恢复机制。

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub+2



### 建议的核心状态机

每个任务具有以下状态：

\#chatgpt-mermaid-\_r_1b4\_{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;fill:rgb(13, 13, 13);}@keyframes edge-animation-frame{from{stroke-dashoffset:0;}}@keyframes dash{to{stroke-dashoffset:0;}}#chatgpt-mermaid-\_r_1b4\_ .edge-animation-slow{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 50s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_1b4\_ .edge-animation-fast{stroke-dasharray:9,5!important;stroke-dashoffset:900;animation:dash 20s linear infinite;stroke-linecap:round;}#chatgpt-mermaid-\_r_1b4\_ .error-icon{fill:rgb(243, 243, 243);}#chatgpt-mermaid-\_r_1b4\_ .error-text{fill:rgb(13, 13, 13);stroke:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_1b4\_ .edge-thickness-normal{stroke-width:1px;}#chatgpt-mermaid-\_r_1b4\_ .edge-thickness-thick{stroke-width:3.5px;}#chatgpt-mermaid-\_r_1b4\_ .edge-pattern-solid{stroke-dasharray:0;}#chatgpt-mermaid-\_r_1b4\_ .edge-thickness-invisible{stroke-width:0;fill:none;}#chatgpt-mermaid-\_r_1b4\_ .edge-pattern-dashed{stroke-dasharray:3;}#chatgpt-mermaid-\_r_1b4\_ .edge-pattern-dotted{stroke-dasharray:2;}#chatgpt-mermaid-\_r_1b4\_ .marker{fill:rgb(143, 143, 143);stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_1b4\_ .marker.cross{stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_1b4\_ svg{font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";font-size:16px;}#chatgpt-mermaid-\_r_1b4\_ p{margin:0;}#chatgpt-mermaid-\_r_1b4\_ defs [id$="-barbEnd"]{fill:rgb(143, 143, 143);stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_1b4\_ g.stateGroup text{fill:rgb(83, 154, 248);stroke:none;font-size:10px;}#chatgpt-mermaid-\_r_1b4\_ g.stateGroup text{fill:rgb(13, 13, 13);stroke:none;font-size:10px;}#chatgpt-mermaid-\_r_1b4\_ g.stateGroup .state-title{font-weight:bolder;fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_1b4\_ g.stateGroup rect{fill:rgb(222, 234, 251);stroke:rgb(83, 154, 248);}#chatgpt-mermaid-\_r_1b4\_ g.stateGroup line{stroke:rgb(143, 143, 143);stroke-width:1;}#chatgpt-mermaid-\_r_1b4\_ .transition{stroke:rgb(143, 143, 143);stroke-width:1;fill:none;}#chatgpt-mermaid-\_r_1b4\_ .stateGroup .composit{fill:transparent;border-bottom:1px;}#chatgpt-mermaid-\_r_1b4\_ .stateGroup .alt-composit{fill:#e0e0e0;border-bottom:1px;}#chatgpt-mermaid-\_r_1b4\_ .state-note{stroke:rgb(107, 198, 127);fill:rgb(243, 243, 243);}#chatgpt-mermaid-\_r_1b4\_ .state-note text{fill:rgb(13, 13, 13);stroke:none;font-size:10px;}#chatgpt-mermaid-\_r_1b4\_ .stateLabel .box{stroke:none;stroke-width:0;fill:rgb(222, 234, 251);opacity:0.5;}#chatgpt-mermaid-\_r_1b4\_ .edgeLabel .label rect{fill:rgb(222, 234, 251);opacity:0.5;}#chatgpt-mermaid-\_r_1b4\_ .edgeLabel{background-color:rgb(252, 252, 252);text-align:center;}#chatgpt-mermaid-\_r_1b4\_ .edgeLabel p{background-color:rgb(252, 252, 252);}#chatgpt-mermaid-\_r_1b4\_ .edgeLabel rect{opacity:0.5;background-color:rgb(252, 252, 252);fill:rgb(252, 252, 252);}#chatgpt-mermaid-\_r_1b4\_ .edgeLabel .label text{fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_1b4\_ .label div .edgeLabel{color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_1b4\_ .stateLabel text{fill:rgb(13, 13, 13);font-size:10px;font-weight:bold;}#chatgpt-mermaid-\_r_1b4\_ .node circle.state-start{fill:rgb(143, 143, 143);stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_1b4\_ .node .fork-join{fill:rgb(143, 143, 143);stroke:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_1b4\_ .node circle.state-end{fill:rgb(83, 154, 248);stroke:transparent;stroke-width:1.5;}#chatgpt-mermaid-\_r_1b4\_ .end-state-inner{fill:transparent;stroke-width:1.5;}#chatgpt-mermaid-\_r_1b4\_ .node rect{fill:rgb(222, 234, 251);stroke:rgb(83, 154, 248);stroke-width:1px;}#chatgpt-mermaid-\_r_1b4\_ .node polygon{fill:rgb(222, 234, 251);stroke:rgb(83, 154, 248);stroke-width:1px;}#chatgpt-mermaid-\_r_1b4\_ [id$="-barbEnd"]{fill:rgb(143, 143, 143);}#chatgpt-mermaid-\_r_1b4\_ .statediagram-cluster rect{fill:rgb(222, 234, 251);stroke:rgb(83, 154, 248);stroke-width:1px;}#chatgpt-mermaid-\_r_1b4\_ .cluster-label,#chatgpt-mermaid-\_r_1b4\_ .nodeLabel{color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_1b4\_ .statediagram-cluster rect.outer{rx:5px;ry:5px;}#chatgpt-mermaid-\_r_1b4\_ .statediagram-state .divider{stroke:rgb(83, 154, 248);}#chatgpt-mermaid-\_r_1b4\_ .statediagram-state .title-state{rx:5px;ry:5px;}#chatgpt-mermaid-\_r_1b4\_ .statediagram-cluster.statediagram-cluster .inner{fill:transparent;}#chatgpt-mermaid-\_r_1b4\_ .statediagram-cluster.statediagram-cluster-alt .inner{fill:rgb(243, 243, 243);}#chatgpt-mermaid-\_r_1b4\_ .statediagram-cluster .inner{rx:0;ry:0;}#chatgpt-mermaid-\_r_1b4\_ .statediagram-state rect.basic{rx:5px;ry:5px;}#chatgpt-mermaid-\_r_1b4\_ .statediagram-state rect.divider{stroke-dasharray:10,10;fill:rgb(243, 243, 243);}#chatgpt-mermaid-\_r_1b4\_ .note-edge{stroke-dasharray:5;}#chatgpt-mermaid-\_r_1b4\_ .statediagram-note rect{fill:rgb(243, 243, 243);stroke:rgb(107, 198, 127);stroke-width:1px;rx:0;ry:0;}#chatgpt-mermaid-\_r_1b4\_ .statediagram-note rect{fill:rgb(243, 243, 243);stroke:rgb(107, 198, 127);stroke-width:1px;rx:0;ry:0;}#chatgpt-mermaid-\_r_1b4\_ .statediagram-note text{fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_1b4\_ .statediagram-note .nodeLabel{color:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_1b4\_ .statediagram .edgeLabel{color:red;}#chatgpt-mermaid-\_r_1b4\_ [id$="-dependencyStart"],#chatgpt-mermaid-\_r_1b4\_ [id$="-dependencyEnd"]{fill:rgb(143, 143, 143);stroke:rgb(143, 143, 143);stroke-width:1;}#chatgpt-mermaid-\_r_1b4\_ .statediagramTitleText{text-anchor:middle;font-size:18px;fill:rgb(13, 13, 13);}#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].statediagram-cluster rect{fill:rgb(222, 234, 251);stroke:url(#chatgpt-mermaid-\_r_1b4\_-gradient);stroke-width:1;}#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].statediagram-cluster rect.outer{rx:5px;ry:5px;filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_1b4\_ .node .neo-node{stroke:rgb(83, 154, 248);}#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].node rect,#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].cluster rect,#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].node polygon{stroke:url(#chatgpt-mermaid-\_r_1b4\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].swimlane.cluster rect{filter:none;}#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].node path{stroke:url(#chatgpt-mermaid-\_r_1b4\_-gradient);stroke-width:1px;}#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].node .outer-path{filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].node .neo-line path{stroke:rgb(83, 154, 248);filter:none;}#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].node circle{stroke:url(#chatgpt-mermaid-\_r_1b4\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].node circle .state-start{fill:#000000;}#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].icon-shape .icon{fill:url(#chatgpt-mermaid-\_r_1b4\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_1b4\_ [data-look="neo"].icon-shape .icon-neo path{stroke:url(#chatgpt-mermaid-\_r_1b4\_-gradient);filter:drop-shadow( 1px 2px 2px rgba(185,185,185,1));}#chatgpt-mermaid-\_r_1b4\_ :root{--mermaid-font-family:-apple-system-body,ui-sans-serif,-apple-system,system-ui,"Segoe UI",Helvetica,"Apple Color Emoji",Arial,sans-serif,"Segoe UI Emoji","Segoe UI Symbol";}DISCOVEREDEVALUATINGDEFERREDAPPROVEDPLANNEDREADYRUNNINGVERIFYINGPASSEDREPAIRINGBLOCKEDINTEGRATINGMERGEDCOMPLETEDRe-planRe-evaluate

几个容易忽略的实现细节：

- 同一任务重复提交必须幂等。
- Agent 执行中断后可以恢复或重新执行。
- 任务取消必须能传递到执行器和 Sandbox。
- Agent 超时不等于任务失败，需要判断是否可以恢复。
- 修复失败必须有次数、成本和时间上限。
- 已完成任务的验收证据不能因为下一轮执行而丢失。

## 七、项目持续演进需要怎样的自主权限？

这是一个很重要的设计维度。

如果完全自主，Agent 可能不断重构代码、替换依赖、修改架构，甚至破坏原本稳定的功能。

我建议定义四级自主权。

| 级别      | 自主能力           | 人工介入   |
| ------- | -------------- | ------ |
| L1 分析自主 | 分析、诊断、生成建议     | 决定是否执行 |
| L2 开发自主 | 创建分支、修改代码、测试   | 审核合并   |
| L3 集成自主 | 自动修复、集成、合并指定范围 | 关键变更审批 |
| L4 运营自主 | 自动发布、监控、回滚     | 重大操作授权 |

我建议初始状态：

## 建议的初始自主权限

项目分析和机会发现

允许持续自主运行

L4

任务拆解和计划生成

允许自主决策

L4

代码开发和自动测试

允许在隔离环境自主执行

L4

代码合并

需要人工审批

L2

生产部署

仅提供建议

L1

修改自身治理策略

只允许提交变更提案

L1

特别强调一个边界：

允许系统自主改进被管理的软件项目，不等于允许它自主修改控制自己的系统。

如果 Director 想修改自己的决策逻辑、权限策略、验收规则、预算限制或安全机制，应当生成一个独立的 System Evolution Proposal，经过独立测试和审批后再发布。

否则系统可能逐步修改验收标准，让自己更容易通过验收。

## 八、推荐的技术栈与项目代码结构

我倾向于使用 Python 作为 Director 和 Orchestrator 的主要开发语言，Agent Adapter 可以根据执行器需要使用 Python、TypeScript 或 Go。

## 推荐技术栈清单

| 层次         | 技术选型                            | 优先级  |
| ---------- | ------------------------------- | ---- |
| API        | FastAPI + Pydantic              | 必选   |
| 决策编排       | LangGraph                       | 必选   |
| 长期工作流      | Temporal                        | 第二阶段 |
| Agent 执行   | OpenHands SDK / CLI / SDK       | 必选   |
| 执行器适配      | 自研 Adapter                      | 必选   |
| Sandbox    | Docker                          | 必选   |
| 远程 Sandbox | Daytona                         | 后续   |
| 任务与状态库     | PostgreSQL                      | 必选   |
| 向量检索       | pgvector                        | 按需   |
| 对象存储       | MinIO / S3                      | 必选   |
| 事件消息       | PostgreSQL Outbox，后期 Kafka/NATS | 按需   |
| 可观测性       | OpenTelemetry + Langfuse        | 必选   |
| 代码管理       | Git + GitHub/GitLab             | 必选   |
| 前端         | Next.js                         | 可选   |

相关项目：

- LangGraph：[GitHub](https://github.com/langchain-ai/langgraph?utm_source=chatgpt.com)
- Temporal：[GitHub](https://github.com/temporalio/temporal?utm_source=chatgpt.com)
- OpenHands：[SDK](https://github.com/OpenHands/software-agent-sdk?utm_source=chatgpt.com)
- Daytona：[GitHub](https://github.com/daytonaio/daytona?utm_source=chatgpt.com)
- Langfuse：[GitHub](https://github.com/langfuse/langfuse?utm_source=chatgpt.com)

### 建议的工程目录结构

```
autonomous-engineering/
│
├── apps/
│   ├── api/                    # 管理 API
│   ├── director/               # 项目总指挥
│   ├── dashboard/              # 项目管理界面
│   └── scheduler/              # 事件和周期调度
│
├── core/
│   ├── project/                # 项目模型
│   ├── evolution/               # 演进模型
│   ├── planning/                # 规划模型
│   ├── task/                    # 任务模型
│   └── policy/                  # 权限和约束
│
├── agents/
│   ├── analyst/
│   ├── strategist/
│   ├── architect/
│   ├── critic/
│   └── evaluator/
│
├── orchestration/
│   ├── langgraph/
│   ├── temporal/
│   └── state_machine/
│
├── executors/
│   ├── base.py
│   ├── claude_code/
│   ├── codex/
│   ├── opencode/
│   ├── openhands/
│   └── gemini_cli/
│
├── harness/
│   ├── workspace/
│   ├── sandbox/
│   ├── verification/
│   ├── integration/
│   └── security/
│
├── knowledge/
│   ├── repository_index/
│   ├── architecture/
│   ├── project_memory/
│   └── artifacts/
│
├── evaluation/
│   ├── task_evaluation/
│   ├── code_evaluation/
│   ├── project_evaluation/
│   └── evolution_evaluation/
│
├── infrastructure/
│   ├── database/
│   ├── observability/
│   └── storage/
│
└── tests/
    ├── unit/
    ├── integration/
    ├── agent/
    └── system/
```

## 九、分阶段实施：不要一次性构建完整平台

我建议分为四个阶段，先验证自主演进能力，再扩大执行规模。

1. 阶段一：最小自主演进闭环

   第一优先级

   建议 1～2 周，按单人快速开发估算
   - 一个项目 Director
   - 一个 Project Analyzer
   - 一个 Critic
   - 一个 Coding Agent Adapter（先接 Claude Code 或 Codex）
   - Git Worktree
   - 自动化测试
   - PostgreSQL 项目状态记录
   验收：不给具体开发任务，只给项目目标，系统能自主发现问题、生成任务、开发、测试并产生下一轮计划。
2. 阶段二：自主研发能力增强

   建议 2～3 周
   - 多种 Coding Agent 接入
   - 动态执行器选择
   - 多任务依赖管理
   - 多 Agent 并行执行
   - 独立验证引擎
   - 自动失败诊断和修复
   - 项目知识库和架构认知
   验收：不同执行器能够完成同一种任务契约，执行结果可以比较和评估。
3. 阶段三：持续运行和自演进

   建议 3～4 周
   - Temporal 长期工作流
   - 事件驱动
   - 定时项目扫描
   - 演进机会发现
   - 自动优先级决策
   - 预算和资源控制
   - 断点恢复
   - 长期执行观测
   验收：系统能够连续运行多个演进周期，不依赖人工反复输入任务。
4. 阶段四：研发控制平面

   持续演进
   - 多项目管理
   - 多模型路由
   - Agent 能力评测
   - 执行器动态调度
   - 自主演进策略优化
   - 高风险变更治理
   - 系统自身改进提案机制
   验收：成为可以长期管理多个软件项目的自主研发基础设施。

这里的工期是 MVP 研发的粗略规划，不是框架本身的安装时间；执行器兼容、权限和验收体系往往比 Agent 调用本身更耗时。

## 十、如何衡量系统是否真的具备自主研发能力？

我建议不要使用 Agent 执行任务数量、Token 数量、代码提交量作为核心指标。

它们很容易被优化，却不代表项目真的变好了。

建议建立四级指标：

| 指标层  | 核心指标              | 评价目的     |
| ---- | ----------------- | -------- |
| 自主发现 | 有效问题发现率、重复发现率     | 能否找到真实问题 |
| 决策质量 | 决策采纳率、误报率、价值预测偏差  | 能否选对问题   |
| 工程交付 | 任务成功率、返工率、回归缺陷率   | 能否正确交付   |
| 项目演进 | 目标达成度、质量趋势、单位成本收益 | 项目是否真正变好 |

尤其需要做一个对照实验：

同一个代码仓库、同一组目标：

- A 组：人工指定任务，由 Coding Agent 开发。
- B 组：Director 自主发现任务，由 Coding Agent 开发。

比较一段时间内：

1. 项目目标达成度。
2. 真实缺陷减少量。
3. 工程质量变化。
4. 单位成本交付价值。
5. 人工干预次数。
6. 引入的新问题和回归缺陷。

只有 B 组在这些指标上持续取得更好的结果，才能证明自主演进系统有真实价值。

## 十一、我认为最值得重点研究的三个开源方向

在正式开发之前，我会优先研究这三个项目的架构和代码，而不是广泛收集各种 Agent 框架。

[OpenHandsとは？詳細情報を解説 | OSSサポートのOpenStandia™【NRI】](https://images.openai.com/static-rsc-4/xN-9z0KmSvbX3SxuLZh24P9y37fUpjMB94WLRqSgKe5xZGH6jIG5NfXfrtIeMfNI1-66rK-m6OcCpKX_4rWJPA2SiY2ohD7hvU7RN4Tb7kTguhqdNy8yvY0WrFC7AA3Eqwu_uGq2XPAl7ZQEW7kgQY0zfIRgiryWSsLZzB3Lb1g?purpose=inline)

1\. OpenHands

研究重点：多执行器、Agent Server、工作空间管理、自动化调度。

它最值得借鉴的是 Coding Agent 执行基础设施，而不是直接把它当成你的项目 Director。

[项目组织](https://github.com/OpenHands?utm_source=chatgpt.com)

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub+1

[Demystifying Agentic AI: Why LangGraph Outshines LangChain for Complex AI Workflows](https://images.openai.com/static-rsc-4/UPZLuqWIm8bqWpfJTgafB-uoJEZ1ebyX_Fr3cLr6v-R-Y3AWqpdPW-7iXUBn6hdXzev2nnRi0BTZDC21D6_N3yt6NH5WR9CIrhWYmRO88oX5LpZ3Q8smNIS69N5iJaFvCRwOHHDKC5_R9F3Xim6WK0Psnd3J2TXHU6QBK7S-5pM?purpose=inline)

2\. LangGraph

研究重点：复杂决策图、状态持久化、动态路由、Agent 协作和恢复机制。

它适合成为自主决策引擎。

[GitHub](https://github.com/langchain-ai/langgraph?utm_source=chatgpt.com)

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub+1

[Report: Temporal Technologies Business Breakdown & Founding Story | Contrary Research](https://images.openai.com/static-rsc-4/CDs1pYcajwL9fdYHIGknYzO0vWHkIxCtjIoeJS73eRr4URVRc4g96ESExCqApK9fPQB5Z_ECi54NUxLKEZ28XsESWinqPKS6uXH6I0L0D957a74ix1VBi_5eAuFAXsBH2FaYTOnQ1qWQjyoYMT7dyuXV4MLcuy1SlwU2ORD70Fs?purpose=inline)

3\. Temporal

研究重点：长期运行、子工作流、故障恢复、事件触发、定时任务和资源调度。

它适合成为自主研发系统的生命周期管理引擎。

[GitHub](https://github.com/temporalio/temporal?utm_source=chatgpt.com)

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub+1

## 十二、最后一个关键判断：系统的核心壁垒到底是什么？

我认为不是模型，也不是 Coding Agent，更不是多 Agent 调度。

真正的壁垒是三个能力：

1\. 项目认知能力

系统能否持续、准确地理解一个复杂代码仓库的真实状态，包括架构、功能、技术债、质量、运行情况，而不只是理解当前代码文件。

2\. 演进决策能力

系统能否判断什么问题最值得解决，能够基于证据分析价值、成本、风险和机会，并且知道什么时候不应该改代码。

3\. 工程验证能力

系统能否客观证明每一次变更真正改善了项目，而不是仅仅完成了 Agent 自己定义的任务。

这三者构成一个闭环：

项目认知

Understand

演进决策

Decide

工程验证

Verify

# Autonomous Evolution

每一轮演进都应该让项目状态、目标达成度或工程质量得到可验证的改善。

这与我建议采用的五层 Harness 思路也能很好地对应：领域工程提供项目知识，能力工程提供决策与分析能力，执行工程负责 Agent 调度，可靠性工程负责独立验证，进化工程负责从结果中持续学习。

我的最终建议：先不要做一个完整的 AI 研发平台，而是先做一个能够在没有人工派发任务的情况下，自主管理一个真实代码仓库并连续完成 10 个有效演进周期的系统。

这是最有价值的第一个里程碑。

如果这个闭环跑通了，再扩展成多项目、多执行器、多 Agent 的自主研发平台，技术路线就比较清晰了。