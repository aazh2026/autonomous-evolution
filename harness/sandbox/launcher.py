"""沙箱启动器 —— 把描述变成真的容器。

设计依据：docs/design.md §3.5A（R-046：Docker 为 POC 首选执行环境）；
docs/tasks.md TASK-P1-032（沙箱容器启动 + 挂载策略）、TASK-P1-033（取消
传播要能停掉容器）。

**启动器只做两件事：把命令跑起来，把结果收回来。** 策略判断全在
:mod:`harness.sandbox.policy` 里 —— 启动器在起容器**之前**调用一次
:func:`~harness.sandbox.policy.validate_sandbox_spec`，不通过就压根不跑。

这条边界很重要：如果策略检查散落在各个调用点，那么"某条路径忘了检查"
就会变成一个只有出事那天才会发现的洞。集中在一处，就只需要守一处。

**容器名是确定的**（``ases-sandbox-<task_id>``）。取消传播要能准确地停掉
**这一个**容器；随机的容器名做不到这件事 —— 停错了容器比停不掉更糟。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from harness.sandbox.policy import (
    SandboxPolicy,
    SandboxResult,
    SandboxSpec,
    build_sandbox_command,
    default_sandbox_policy,
    validate_sandbox_spec,
)

#: ``docker ps`` 里算"还在跑"的容器状态。
#:
#: ``paused`` 与 ``restarting`` 也算：这两种容器都还占着资源，"它没在跑"
#: 是个假结论。
_RUNNING_CONTAINER_STATES: frozenset[str] = frozenset({"running", "paused", "restarting"})


class SandboxLaunchError(RuntimeError):
    """沙箱容器起不来。

    与 :class:`~harness.sandbox.policy.SandboxPolicyError` 分开：前者是
    "策略不允许"（配置错误，必须在启动前暴露），后者是"docker 说不行"
    （环境问题，可以重试）。
    """


@dataclass(frozen=True, slots=True)
class SandboxLauncher:
    """启动沙箱容器。

    :param docker_binary: docker 可执行文件。
    :param policy: 沙箱策略。默认用 :func:`~harness.sandbox.policy.default_sandbox_policy`。
    :param timeout_seconds: 同步运行的超时。
    """

    docker_binary: str = "docker"
    policy: SandboxPolicy | None = None
    timeout_seconds: int = 1800

    def _active_policy(self) -> SandboxPolicy:
        return self.policy if self.policy is not None else default_sandbox_policy()

    def command_for(self, spec: SandboxSpec) -> tuple[str, ...]:
        """这条容器要跑的命令 —— 检查通过之后才算数。"""
        validate_sandbox_spec(spec, policy=self._active_policy())
        return (self.docker_binary, *build_sandbox_command(spec))

    def run(self, spec: SandboxSpec) -> SandboxResult:
        """同步跑一次，跑完拿结果。"""
        command = self.command_for(spec)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxLaunchError(
                f"沙箱容器运行超时（{self.timeout_seconds}s）：{spec.container_name}"
            ) from exc
        return SandboxResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def start(self, spec: SandboxSpec) -> str:
        """后台起一个容器，返回容器 id。

        用 ``docker run -d`` 而不是 ``docker create`` + ``docker start``：
        前者一次就把"创建并运行"做完，少一次状态转换，也就少一类"创建了
        但没起来"的中间态。
        """
        command = self.command_for(spec)
        detached = (command[0], command[1], "-d", *command[2:])
        try:
            completed = subprocess.run(
                detached,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxLaunchError("启动沙箱容器超时") from exc
        if completed.returncode != 0:
            raise SandboxLaunchError(
                f"启动沙箱容器失败（退出码 {completed.returncode}）：{completed.stderr.strip()}"
            )
        return completed.stdout.strip()

    def stop(self, container_id: str, *, grace_seconds: int = 30) -> None:
        """停掉容器。

        先 ``stop``（给容器一点时间自己收尾），再 ``rm -f``（收尾超时也
        一定要删掉）。两个都**吞掉错误** —— 容器可能早就自己没了（比如
        ``--rm`` 在它退出时就删了），那种情况下"停下来"这个目标**已经达成**
        了，不该报失败。
        """
        self._ignore_failure(("stop", "-t", str(grace_seconds), container_id))
        self._ignore_failure(("rm", "-f", container_id))

    def is_running(self, container_id: str) -> bool:
        """容器还在不在跑。

        **"问不话"算作"没在跑"。** 只关心"在不在跑"时够用；要区分"docker
        说它没在跑"与"docker 根本没说上话"，用 :meth:`container_running`。
        """
        return self.container_running(container_id) is True

    def container_running(self, container_id: str) -> bool | None:
        """容器还在不在跑。**docker 不可用时返回 ``None``**。

        三态而不是两态，因为"容器没在跑"与"我们没问上话"必须分开：取消传
        播链要靠这个区别判断"停掉了"还是"停不掉"（TASK-P1-033）。问不话
        却报"没在跑"，会把"我们没停掉它"与"它本来就没在跑"混成一句话。

        **用 ``docker ps -a`` 而不是 ``docker inspect``。** 区别在"容器已经
        被删了"这一种情况：``inspect`` 对着不存在的容器会报错退出，于是
        "容器没了"与"docker 坏了"长得一模一样；而 ``ps -a`` 对着不存在的
        容器只是**列不出来**（输出为空），对 docker 本身不可用才是**报错**。
        取消传播恰恰要在这两种情况里分辨：容器没了正是我们要的，docker 坏
        了则意味着"我们没停掉它"。
        """
        completed = self._ignore_failure(
            ("ps", "-a", "--filter", f"id={container_id}", "--format", "{{.State}}")
        )
        if completed is None or completed.returncode != 0:
            # docker 不可用 —— 不是"没在跑"，是"没问上话"。
            return None
        state = completed.stdout.strip()
        if not state:
            # 容器已经不在了（多半是我们刚删的）。它当然没在跑。
            return False
        return state in _RUNNING_CONTAINER_STATES

    def _ignore_failure(self, args: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                (self.docker_binary, *args),
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None


def workspace_container_name(task_id: str) -> str:
    """这个任务的沙箱容器名。"""
    return f"ases-sandbox-{task_id}"


__all__ = [
    "SandboxLaunchError",
    "SandboxLauncher",
    "workspace_container_name",
]
