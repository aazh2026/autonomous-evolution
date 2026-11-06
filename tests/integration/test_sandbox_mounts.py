"""TASK-P1-032 —— 沙箱挂载策略的真容器验证。

覆盖任务书的 T1 ②（沙箱内不存在宿主 ``~/.ssh``、云 CLI 凭据、生产库连接
串）。

**这一组跑的是真容器，不是"拼出来的 docker 命令"。** 命令由
:func:`~harness.sandbox.policy.build_workspace_sandbox_spec` 生成、由
:class:`~harness.sandbox.launcher.SandboxLauncher` 执行 —— 走的是生产代码
那条路。拼一条手写的 ``docker run`` 来断言，验的就只是"docker 能跑"，跟
"我们的策略对不对"没关系。

断言分两类：

- **策略层**（不需要 Docker）：宿主机的 ``~/.ssh`` 之类的目录**被拒绝
  挂载** —— 策略在容器起来之前就把这条路堵死了；
- **运行时**（需要 Docker）：真的容器里没有 ``~/.ssh``、没有云凭据、
  没有生产库连接串；凭据挂载**确实是只读**的。

"只读"用 ``/proc/self/mountinfo`` 断言，而不是"写一下试试失败" —— 后者在
Linux CI 上会因为目录属主不对而失败，验的是别的东西。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harness.sandbox.launcher import SandboxLauncher
from harness.sandbox.policy import (
    CREDENTIAL_DIRS_IN_HOME,
    MountSpec,
    SandboxPolicyError,
    SandboxSpec,
    assert_mounts_are_safe,
    build_workspace_sandbox_spec,
    default_sandbox_policy,
    forbidden_mount_sources,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: 探针：把容器里某个路径的挂载点与挂载选项打出来。
#:
#: ``/proc/self/mountinfo`` 的第 5 个字段是挂载点，第 6 个是挂载选项
#: （``ro`` / ``rw``）。只看这两个字段就够了 —— 后面那些是文件系统的
#: 超级块选项（``rw,errors=remount-ro`` 之类），会把只读挂载也显示成含
#: ``rw``，拿它们判断会得出相反的结论。
_MOUNT_PROBE = (
    "import sys\n"
    "targets = set(sys.argv[1:])\n"
    "for line in open('/proc/self/mountinfo'):\n"
    "    parts = line.split()\n"
    "    if len(parts) > 5 and parts[4] in targets:\n"
    "        print(parts[4], parts[5].split(',')[0])\n"
)


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """一个假想的宿主机家目录，里面摆着各种凭据目录。"""
    base = (tmp_path / "home").resolve()
    base.mkdir()
    for relative in CREDENTIAL_DIRS_IN_HOME:
        (base / relative).mkdir(parents=True, exist_ok=True)
    (base / ".ssh" / "id_rsa").write_text("宿主私钥标记", encoding="utf-8")
    return base


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = (tmp_path / "workspace").resolve()
    path.mkdir()
    (path / "项目文件.txt").write_text("工作区里的东西", encoding="utf-8")
    return path


@pytest.fixture
def launcher() -> SandboxLauncher:
    return SandboxLauncher(timeout_seconds=120)


class TestTheHostCredentialDirectoriesCannotBeMounted:
    """策略层：宿主机的凭据目录**被拒绝挂载**。"""

    @pytest.mark.parametrize("relative", CREDENTIAL_DIRS_IN_HOME)
    def test_the_credential_directory_is_refused(self, fake_home: Path, relative: str) -> None:
        policy = default_sandbox_policy(fake_home)
        with pytest.raises(SandboxPolicyError):
            assert_mounts_are_safe(
                [MountSpec(source=fake_home / relative, target="/creds")], policy=policy
            )

    def test_the_real_host_home_is_covered(self) -> None:
        """真实家目录下的凭据目录，在策略里是**算出来的**，不是写死的。"""
        sources = forbidden_mount_sources()
        for relative in CREDENTIAL_DIRS_IN_HOME:
            assert (Path.home() / relative).resolve() in sources

    def test_the_real_host_home_itself_is_covered(self) -> None:
        assert Path.home().resolve() in forbidden_mount_sources()


class TestTheContainerHasNoCredentials:
    """运行时：真的容器里没有那些东西。"""

    def test_the_ssh_directory_is_absent(
        self, launcher: SandboxLauncher, sandbox_image: str, workspace: Path
    ) -> None:
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image=sandbox_image,
            policy=default_sandbox_policy(),
        )
        result = launcher.run(
            _with_command(spec, ("sh", "-c", "test -e $HOME/.ssh && echo EXISTS || echo ABSENT"))
        )
        assert result.exit_code == 0, result.stderr
        assert result.stdout.strip() == "ABSENT"

    def test_the_cloud_credential_directories_are_absent(
        self, launcher: SandboxLauncher, sandbox_image: str, workspace: Path
    ) -> None:
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image=sandbox_image,
            policy=default_sandbox_policy(),
        )
        result = launcher.run(
            _with_command(
                spec,
                (
                    "sh",
                    "-c",
                    "for p in $HOME/.aws $HOME/.docker $HOME/.kube $HOME/.gnupg; "
                    'do test -e "$p" && echo "EXISTS:$p"; done; echo DONE',
                ),
            )
        )
        assert result.exit_code == 0, result.stderr
        assert "EXISTS" not in result.stdout

    def test_the_environment_carries_no_credentials(
        self, launcher: SandboxLauncher, sandbox_image: str, workspace: Path
    ) -> None:
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image=sandbox_image,
            policy=default_sandbox_policy(),
        )
        result = launcher.run(_with_command(spec, ("env",)))
        assert result.exit_code == 0, result.stderr
        for forbidden in (
            "AWS_",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "KUBECONFIG",
            "PROXY",
            "DATABASE_URL",
            "SECRET",
            "TOKEN",
            "PASSWORD",
        ):
            assert forbidden not in result.stdout, f"环境变量里出现了 {forbidden}"

    def test_the_environment_has_no_production_connection_string(
        self, launcher: SandboxLauncher, sandbox_image: str, workspace: Path
    ) -> None:
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image=sandbox_image,
            policy=default_sandbox_policy(),
        )
        result = launcher.run(_with_command(spec, ("env",)))
        assert "prod" not in result.stdout.lower()

    def test_the_host_credential_file_is_nowhere_in_the_container(
        self, launcher: SandboxLauncher, sandbox_image: str, workspace: Path
    ) -> None:
        """宿主私钥的内容，在容器里**整个文件系统**都搜不到。"""
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image=sandbox_image,
            policy=default_sandbox_policy(),
        )
        result = launcher.run(
            _with_command(
                spec,
                (
                    "sh",
                    "-c",
                    "grep -rl --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev "
                    "'宿主私钥标记' / 2>/dev/null; echo SEARCH_DONE",
                ),
            )
        )
        assert result.exit_code == 0, result.stderr
        assert "SEARCH_DONE" in result.stdout
        assert "宿主私钥标记" not in result.stdout.replace("SEARCH_DONE", "")


class TestTheWorkspaceIsMounted:
    def test_the_workspace_content_is_visible(
        self, launcher: SandboxLauncher, sandbox_image: str, workspace: Path
    ) -> None:
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image=sandbox_image,
            policy=default_sandbox_policy(),
        )
        result = launcher.run(_with_command(spec, ("cat", "/workspace/项目文件.txt")))
        assert result.exit_code == 0, result.stderr
        assert "工作区里的东西" in result.stdout

    def test_the_workspace_is_mounted_read_write(
        self, launcher: SandboxLauncher, sandbox_image: str, workspace: Path
    ) -> None:
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image=sandbox_image,
            policy=default_sandbox_policy(),
        )
        result = launcher.run(_with_command(spec, ("python3", "-c", _MOUNT_PROBE, "/workspace")))
        assert result.exit_code == 0, result.stderr
        assert "/workspace rw" in result.stdout


class TestTheCredentialMountIsReadOnly:
    def test_the_credential_mount_is_read_only(
        self,
        launcher: SandboxLauncher,
        sandbox_image: str,
        workspace: Path,
        tmp_path: Path,
    ) -> None:
        """凭据挂载必须是只读的 —— 沙箱能改令牌，就等于令牌没被锁住。"""
        creds = tmp_path / "credentials"
        creds.mkdir()
        (creds / "token.txt").write_text("短期令牌", encoding="utf-8")
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image=sandbox_image,
            policy=default_sandbox_policy(),
            credential_dir=creds,
        )
        result = launcher.run(
            _with_command(
                spec,
                (
                    "python3",
                    "-c",
                    _MOUNT_PROBE,
                    "/workspace",
                    "/etc/ases/credentials",
                ),
            )
        )
        assert result.exit_code == 0, result.stderr
        assert "/etc/ases/credentials ro" in result.stdout

    def test_the_credential_is_readable(
        self,
        launcher: SandboxLauncher,
        sandbox_image: str,
        workspace: Path,
        tmp_path: Path,
    ) -> None:
        """只读不等于不可读 —— 短期令牌是要给 Agent 用的。"""
        creds = tmp_path / "credentials"
        creds.mkdir()
        (creds / "token.txt").write_text("短期令牌", encoding="utf-8")
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image=sandbox_image,
            policy=default_sandbox_policy(),
            credential_dir=creds,
        )
        result = launcher.run(_with_command(spec, ("cat", "/etc/ases/credentials/token.txt")))
        assert result.exit_code == 0, result.stderr
        assert "短期令牌" in result.stdout


class TestTheNetworkIsIsolated:
    def test_the_container_cannot_reach_the_internet(
        self, launcher: SandboxLauncher, sandbox_image: str, workspace: Path
    ) -> None:
        """默认网络是 ``none`` —— 出站网络默认关闭。"""
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image=sandbox_image,
            network="none",
            policy=default_sandbox_policy(),
        )
        probe = (
            "import socket,sys\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
            "except OSError:\n"
            "    print('BLOCKED')\n"
            "    sys.exit(0)\n"
            "print('REACHED')\n"
            "sys.exit(1)\n"
        )
        result = launcher.run(_with_command(spec, ("python3", "-c", probe)))
        assert result.exit_code == 0, result.stderr
        assert "BLOCKED" in result.stdout


def _with_command(spec: SandboxSpec, command: tuple[str, ...]) -> SandboxSpec:
    """给一份描述换掉容器里要跑的命令。

    ``SandboxSpec`` 是冻结的，所以换命令就是造一份新的 —— 这也顺带证明了
    "改一份描述"必须重新走一遍策略检查。
    """
    return replace(spec, command=command)
