"""执行治理 —— 安全门、任务级授权与审计。

设计依据：docs/design.md §3.5D（"每个 Agent 有独立执行身份"、"每个任务有独立
Task ID 和授权范围"、"Agent 的权限绑定到任务、项目、工作空间和有效期"、
"默认禁止访问生产环境"、"网络访问采用白名单"、"删除、发布、合并、部署等
高风险操作有独立授权"、"每一次工具调用均记录主体、任务、资源和执行结果"、
"Agent 身份不能替代任务身份"）；docs/tasks.md TASK-P1-043、TASK-P1-044、
TASK-P1-045、R-048、R-054、R-059~R-063。

- :mod:`harness.security.gate` —— 三项 ``forbidden`` 检查器（密钥、依赖、
  公共接口）；
- :mod:`harness.security.network` —— 网络出站白名单；
- :mod:`harness.security.authorization` —— 任务级授权上下文（TASK-P1-044）；
- :mod:`harness.security.audit` —— 工具调用审计（TASK-P1-045）。

**这四件事是一条链上的四道关。** 授权决定"能不能做"，审计记录"做了什么"，
安全门检查"做出来的东西合不合规"，出站白名单限制"能连到哪儿"。它们共用
同一组身份（Agent / 任务 / 项目 / 工作空间）—— 所以身份的定义只在这一层
出现一次，不在四个模块里各写一遍。

**分工的边界**：授权上下文只回答"准不准"，安全门只回答"合不合规" ——
两者都不回答"做了没有"，那是审计的事。授权不该去检查 diff，安全门也不该
去查授权记录；一旦一个模块开始做另一个模块的活，它们的边界就再也说不清了。
"""

from __future__ import annotations

from harness.security.authorization import (
    HIGH_RISK_CAPABILITIES,
    AuthorizationDecision,
    AuthorizationDeniedError,
    AuthorizationIdentity,
    AuthorizationKey,
    AuthorizationRegistry,
    AuthorizationRequest,
    AuthorizationService,
    Capability,
    DecisionSink,
    TaskAuthorization,
    path_scope,
)
from harness.security.gate import (
    DEFAULT_PROTECTED_PACKAGES,
    DEFAULT_SECRET_PATTERNS,
    ChangedLine,
    FileChange,
    GateReport,
    SecurityGate,
    SecurityPolicy,
    Violation,
    normalize_dependency,
    parse_unified_diff,
    requirement_name,
)
from harness.security.network import (
    ALLOWED_SCHEMES,
    NetworkPolicy,
    NetworkPolicyError,
    check_url,
    default_network_policy,
    fetch_url,
)

__all__ = [
    "ALLOWED_SCHEMES",
    "DEFAULT_PROTECTED_PACKAGES",
    "DEFAULT_SECRET_PATTERNS",
    "HIGH_RISK_CAPABILITIES",
    "AuthorizationDecision",
    "AuthorizationDeniedError",
    "AuthorizationIdentity",
    "AuthorizationKey",
    "AuthorizationRegistry",
    "AuthorizationRequest",
    "AuthorizationService",
    "Capability",
    "ChangedLine",
    "DecisionSink",
    "FileChange",
    "GateReport",
    "NetworkPolicy",
    "NetworkPolicyError",
    "SecurityGate",
    "SecurityPolicy",
    "TaskAuthorization",
    "Violation",
    "check_url",
    "default_network_policy",
    "fetch_url",
    "normalize_dependency",
    "parse_unified_diff",
    "path_scope",
    "requirement_name",
]
