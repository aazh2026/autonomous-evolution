"""沙箱执行环境 —— 策略、启动与取消。

设计依据：docs/design.md §3.5A（R-045~R-048）、§3.5D（默认禁止访问生产
环境；网络白名单；凭证使用短期令牌，不向 Prompt 暴露长期密钥）、
docs/tasks.md TASK-P1-032（沙箱容器启动 + 挂载策略 + 只读挂载凭据目录）、
TASK-P1-033（取消传播链）。

- :mod:`harness.sandbox.policy` —— 挂什么、给什么环境变量、怎么起容器；
  全部是"否定命题"——沙箱里不该有的东西，一样都不许有。
- :mod:`harness.sandbox.launcher` —— 把描述变成真的容器。
- :mod:`harness.sandbox.cancellation` —— 取消传播的四层链（任务 → Adapter
  → 进程 → 容器）。
"""

from __future__ import annotations

from harness.sandbox.cancellation import (
    AdapterLayer,
    CancellationChain,
    CancellationLayer,
    LauncherContainerStopper,
    NoContainerStopper,
    NoProcessStopper,
    ProcessLayer,
    SandboxLayer,
    TaskLayer,
    build_cancellation_chain,
)
from harness.sandbox.launcher import SandboxLauncher, SandboxLaunchError
from harness.sandbox.policy import (
    MountSpec,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxResult,
    SandboxSpec,
    assert_mounts_are_safe,
    assert_sandbox_env_is_safe,
    build_sandbox_command,
    build_sandbox_env,
    build_workspace_sandbox_spec,
    default_sandbox_policy,
    forbidden_mount_sources,
    validate_sandbox_spec,
)

__all__ = [
    "AdapterLayer",
    "CancellationChain",
    "CancellationLayer",
    "LauncherContainerStopper",
    "MountSpec",
    "NoContainerStopper",
    "NoProcessStopper",
    "ProcessLayer",
    "SandboxLaunchError",
    "SandboxLauncher",
    "SandboxLayer",
    "SandboxPolicy",
    "SandboxPolicyError",
    "SandboxResult",
    "SandboxSpec",
    "TaskLayer",
    "assert_mounts_are_safe",
    "assert_sandbox_env_is_safe",
    "build_cancellation_chain",
    "build_sandbox_command",
    "build_sandbox_env",
    "build_workspace_sandbox_spec",
    "default_sandbox_policy",
    "forbidden_mount_sources",
    "validate_sandbox_spec",
]
