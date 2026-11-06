"""TASK-P1-032 —— 沙箱策略。

覆盖任务书的 T1 ②（沙箱内不存在宿主 ``~/.ssh``、云 CLI 凭据、生产库连接
串）。

**这一组测试守的是一条否定命题。** 否定命题没法靠"我们记得别做"来保证，
只能靠构造上的不可能 —— 所以测试大多长成"**这个必须被拒绝**"的样子，
而不是"那个应该在里面"。

三类否定：

1. **宿主的环境变量整体不进沙箱** —— 只有白名单里的名字进得来；
2. **宿主的凭据目录挂不进来** —— 而且只挡叶子不挡根的挡法没有意义，
   ``~/.config``、``~``、``/`` 一律拒绝；
3. **生产库的连接串进不来** —— 连接串里的主机名必须在允许清单里。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.sandbox.launcher import SandboxLauncher
from harness.sandbox.policy import (
    BASE_ENV,
    CREDENTIAL_DIRS_IN_HOME,
    MountSpec,
    SandboxPolicy,
    SandboxPolicyError,
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


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """一个假想的宿主机家目录，里面摆着各种凭据目录。"""
    base = (tmp_path / "home").resolve()
    base.mkdir()
    for relative in CREDENTIAL_DIRS_IN_HOME:
        (base / relative).mkdir(parents=True, exist_ok=True)
    (base / ".config").mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture
def policy(home: Path) -> SandboxPolicy:
    return default_sandbox_policy(home)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = (tmp_path / "workspaces" / "task-1").resolve()
    path.mkdir(parents=True)
    return path


class TestTheHostEnvironmentIsNotInherited:
    """环境变量是**重新造**的，不是继承来的。"""

    def test_the_base_environment_is_always_there(self, policy: SandboxPolicy) -> None:
        env = build_sandbox_env({}, policy=policy)
        assert env["HOME"] == "/home/ases"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_an_allowlisted_name_passes_through(self, policy: SandboxPolicy) -> None:
        env = build_sandbox_env({"GIT_AUTHOR_NAME": "ASES"}, policy=policy)
        assert env["GIT_AUTHOR_NAME"] == "ASES"

    def test_a_name_outside_the_allowlist_does_not(self, policy: SandboxPolicy) -> None:
        env = build_sandbox_env({"SOME_RANDOM_THING": "x"}, policy=policy)
        assert "SOME_RANDOM_THING" not in env

    def test_the_proxy_is_not_inherited(self, policy: SandboxPolicy) -> None:
        """代理会让沙箱借道宿主机出网 —— 那等于没有网络隔离。"""
        env = build_sandbox_env(
            {"HTTPS_PROXY": "http://proxy.corp:3128", "HTTP_PROXY": "http://proxy.corp:3128"},
            policy=policy,
        )
        assert "HTTPS_PROXY" not in env
        assert "HTTP_PROXY" not in env

    def test_the_cloud_credentials_are_not_inherited(self, policy: SandboxPolicy) -> None:
        env = build_sandbox_env(
            {
                "AWS_ACCESS_KEY_ID": "AKIA...",
                "AWS_SECRET_ACCESS_KEY": "...",
                "GOOGLE_APPLICATION_CREDENTIALS": "/creds.json",
                "KUBECONFIG": "/root/.kube/config",
            },
            policy=policy,
        )
        for name in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "KUBECONFIG",
        ):
            assert name not in env

    def test_the_database_url_is_not_inherited(self, policy: SandboxPolicy) -> None:
        env = build_sandbox_env(
            {"DATABASE_URL": "postgresql://user:pw@prod-db.internal:5432/ases"},
            policy=policy,
        )
        assert "DATABASE_URL" not in env
        assert "prod-db.internal" not in "".join(env.values())

    def test_the_whole_host_environment_is_not_copied(self, policy: SandboxPolicy) -> None:
        """白名单之外的名字，一个都不许出现。"""
        env = build_sandbox_env({"WHATEVER": "1", "ANOTHER": "2"}, policy=policy)
        assert "WHATEVER" not in env
        assert "ANOTHER" not in env
        assert set(env) == set(BASE_ENV)


class TestTheCredentialsDoNotTravelByEnvironment:
    """凭据走只读挂载，不走环境变量 —— 环境变量会被日志与 Prompt 看到。"""

    @pytest.mark.parametrize(
        "name",
        [
            "AWS_SECRET_ACCESS_KEY",
            "GITHUB_TOKEN",
            "DB_PASSWORD",
            "SSH_PRIVATE_KEY",
            "ANTHROPIC_API_KEY",
            "MY_CREDENTIALS",
        ],
    )
    def test_a_secret_looking_name_is_rejected(self, name: str, policy: SandboxPolicy) -> None:
        with pytest.raises(SandboxPolicyError, match="凭据"):
            build_sandbox_env({}, policy=policy, extra={name: "whatever"})

    def test_an_ordinary_name_is_fine(self, policy: SandboxPolicy) -> None:
        env = build_sandbox_env({}, policy=policy, extra={"MY_APP_MODE": "fast"})
        assert env["MY_APP_MODE"] == "fast"

    def test_a_name_that_merely_contains_a_word_is_fine(self, policy: SandboxPolicy) -> None:
        """``TOKENIZERS_PARALLELISM`` 不是凭据 —— 按词判定，不是按子串。"""
        env = build_sandbox_env({}, policy=policy, extra={"TOKENIZERS_PARALLELISM": "false"})
        assert env["TOKENIZERS_PARALLELISM"] == "false"


class TestTheProductionConnectionStringIsRejected:
    @pytest.mark.parametrize(
        "value",
        [
            "postgresql://user:pw@prod-db.internal:5432/ases",
            "postgresql+asyncpg://user:pw@db.production.corp/ases",
            "mysql://user:pw@prod-db/ases",
            "redis://prod-cache:6379/0",
            "host=prod-db port=5432 dbname=ases",
            "host=db-prod.internal",
            # 这一条主机名里**没有** prod —— 它靠的不是"生产库关键词"，
            # 而是"不在允许清单"。允许清单才是主机制：默认只放行本地与
            # 测试主机，其余一律拒。
            "postgresql://user:pw@db.internal.corp:5432/ases",
        ],
    )
    def test_a_production_dsn_is_rejected(self, value: str, policy: SandboxPolicy) -> None:
        with pytest.raises(SandboxPolicyError, match=r"生产环境|不在允许清单"):
            build_sandbox_env({}, policy=policy, extra={"APP_DSN": value})

    @pytest.mark.parametrize(
        "value",
        [
            "postgresql://user:pw@localhost:5432/ases_test",
            "postgresql://user:pw@127.0.0.1:5432/ases_test",
            "redis://localhost:6379/0",
            "host=localhost port=5432 dbname=ases_test",
        ],
    )
    def test_a_local_dsn_is_allowed(self, value: str, policy: SandboxPolicy) -> None:
        env = build_sandbox_env({}, policy=policy, extra={"APP_DSN": value})
        assert env["APP_DSN"] == value

    def test_an_ordinary_url_is_not_a_connection_string(self, policy: SandboxPolicy) -> None:
        """``https://example.com`` 不是数据库连接串 —— 不能误伤。"""
        env = build_sandbox_env({}, policy=policy, extra={"REGISTRY": "https://registry.npmjs.org"})
        assert env["REGISTRY"] == "https://registry.npmjs.org"


class TestTheCredentialPathsAreRejected:
    @pytest.mark.parametrize(
        "value",
        [
            "~/.ssh/config",
            "~/.aws/credentials",
            "/home/agent/.ssh/config",
            "/var/lib/secrets/.config/gcloud/application_default_credentials.json",
            "~/.kube/config",
            "~/.gnupg/secring.gpg",
        ],
    )
    def test_a_credential_path_in_a_value_is_rejected(
        self, value: str, policy: SandboxPolicy
    ) -> None:
        with pytest.raises(SandboxPolicyError, match="凭据"):
            build_sandbox_env({}, policy=policy, extra={"CONFIG_PATH": value})

    def test_a_file_that_merely_starts_like_one_is_fine(self, policy: SandboxPolicy) -> None:
        """``.dockerignore`` 是文件，不是凭据目录。"""
        env = build_sandbox_env({}, policy=policy, extra={"FILE": "repo/.dockerignore"})
        assert env["FILE"] == "repo/.dockerignore"


class TestTheCredentialDirectoriesCannotBeMounted:
    @pytest.mark.parametrize("relative", CREDENTIAL_DIRS_IN_HOME)
    def test_a_credential_directory_is_refused(
        self, relative: str, home: Path, policy: SandboxPolicy
    ) -> None:
        with pytest.raises(SandboxPolicyError, match="凭据目录"):
            assert_mounts_are_safe(
                [MountSpec(source=home / relative, target="/creds")], policy=policy
            )

    def test_a_directory_inside_a_credential_directory_is_refused(
        self, home: Path, policy: SandboxPolicy
    ) -> None:
        keys = home / ".ssh" / "keys"
        keys.mkdir()
        with pytest.raises(SandboxPolicyError, match="凭据目录"):
            assert_mounts_are_safe([MountSpec(source=keys, target="/creds")], policy=policy)

    def test_the_parent_of_a_credential_directory_is_refused(
        self, home: Path, policy: SandboxPolicy
    ) -> None:
        """只挡叶子不挡根等于没挡 —— ``~/.config`` 里就有 gcloud 凭据。"""
        with pytest.raises(SandboxPolicyError, match="凭据目录"):
            assert_mounts_are_safe(
                [MountSpec(source=home / ".config", target="/creds")], policy=policy
            )

    def test_the_home_itself_is_refused(self, home: Path, policy: SandboxPolicy) -> None:
        with pytest.raises(SandboxPolicyError, match="凭据目录"):
            assert_mounts_are_safe([MountSpec(source=home, target="/home")], policy=policy)

    def test_the_root_is_refused(self, policy: SandboxPolicy) -> None:
        with pytest.raises(SandboxPolicyError, match="凭据目录"):
            assert_mounts_are_safe([MountSpec(source=Path("/"), target="/host")], policy=policy)

    def test_the_docker_socket_is_refused(self, policy: SandboxPolicy) -> None:
        """挂了 docker socket 就等于给了 root。"""
        with pytest.raises(SandboxPolicyError, match="凭据目录"):
            assert_mounts_are_safe(
                [MountSpec(source=Path("/var/run/docker.sock"), target="/var/run/docker.sock")],
                policy=policy,
            )


class TestTheMountsAreWellFormed:
    def test_the_workspace_can_be_mounted(self, workspace: Path, policy: SandboxPolicy) -> None:
        assert_mounts_are_safe([MountSpec(source=workspace, target="/workspace")], policy=policy)

    def test_a_missing_source_is_refused(self, tmp_path: Path, policy: SandboxPolicy) -> None:
        """挂一个不存在的路径，docker 会建一个属于 root 的空目录。"""
        with pytest.raises(SandboxPolicyError, match="不存在"):
            assert_mounts_are_safe(
                [MountSpec(source=tmp_path / "nope", target="/workspace")], policy=policy
            )

    def test_a_relative_source_is_refused(self, policy: SandboxPolicy) -> None:
        with pytest.raises(SandboxPolicyError, match="绝对路径"):
            assert_mounts_are_safe([MountSpec(source=Path("relative"), target="/w")], policy=policy)

    def test_a_relative_target_is_refused(self, workspace: Path, policy: SandboxPolicy) -> None:
        with pytest.raises(SandboxPolicyError, match="绝对路径"):
            assert_mounts_are_safe([MountSpec(source=workspace, target="workspace")], policy=policy)

    def test_the_root_target_is_refused(self, workspace: Path, policy: SandboxPolicy) -> None:
        with pytest.raises(SandboxPolicyError, match="根目录"):
            assert_mounts_are_safe([MountSpec(source=workspace, target="/")], policy=policy)

    def test_a_duplicated_target_is_refused(
        self, workspace: Path, tmp_path: Path, policy: SandboxPolicy
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        with pytest.raises(SandboxPolicyError, match="重复"):
            assert_mounts_are_safe(
                [
                    MountSpec(source=workspace, target="/workspace"),
                    MountSpec(source=other, target="/workspace"),
                ],
                policy=policy,
            )

    def test_a_writable_credential_mount_is_refused(
        self, tmp_path: Path, policy: SandboxPolicy
    ) -> None:
        """可写的凭据挂载等于没有凭据挂载 —— 沙箱能改令牌，也能偷令牌。"""
        creds = tmp_path / "creds"
        creds.mkdir()
        with pytest.raises(SandboxPolicyError, match="只读"):
            assert_mounts_are_safe(
                [MountSpec(source=creds, target="/creds", credential=True)], policy=policy
            )

    def test_a_read_only_credential_mount_is_allowed(
        self, tmp_path: Path, policy: SandboxPolicy
    ) -> None:
        creds = tmp_path / "creds"
        creds.mkdir()
        assert_mounts_are_safe(
            [MountSpec(source=creds, target="/creds", read_only=True, credential=True)],
            policy=policy,
        )


class TestTheForbiddenSourcesAreEnumerated:
    def test_every_credential_directory_is_covered(self, home: Path) -> None:
        sources = forbidden_mount_sources(home)
        for relative in CREDENTIAL_DIRS_IN_HOME:
            assert (home / relative).resolve() in sources

    def test_the_home_is_covered(self, home: Path) -> None:
        assert home.resolve() in forbidden_mount_sources(home)


class TestTheCommandIsBuilt:
    def _spec(self, workspace: Path, policy: SandboxPolicy) -> SandboxSpec:
        return build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image="ases/sandbox:test",
            policy=policy,
        )

    def test_the_network_is_none_by_default(self, workspace: Path, policy: SandboxPolicy) -> None:
        command = build_sandbox_command(self._spec(workspace, policy))
        assert command[command.index("--network") + 1] == "none"

    def test_the_workspace_is_mounted(self, workspace: Path, policy: SandboxPolicy) -> None:
        command = build_sandbox_command(self._spec(workspace, policy))
        volumes = [command[i + 1] for i, part in enumerate(command) if part == "--volume"]
        assert f"{workspace}:/workspace" in volumes

    def test_the_capabilities_are_dropped(self, workspace: Path, policy: SandboxPolicy) -> None:
        command = build_sandbox_command(self._spec(workspace, policy))
        assert "--cap-drop" in command
        assert command[command.index("--cap-drop") + 1] == "ALL"

    def test_the_new_privileges_are_denied(self, workspace: Path, policy: SandboxPolicy) -> None:
        command = build_sandbox_command(self._spec(workspace, policy))
        assert "no-new-privileges" in command

    def test_the_environment_is_passed_as_flags(
        self, workspace: Path, policy: SandboxPolicy
    ) -> None:
        command = build_sandbox_command(self._spec(workspace, policy))
        assert "HOME=/home/ases" in command

    def test_the_image_comes_after_the_options(
        self, workspace: Path, policy: SandboxPolicy
    ) -> None:
        command = build_sandbox_command(self._spec(workspace, policy))
        assert command.index("ases/sandbox:test") > command.index("--network")


class TestTheWorkspaceSpec:
    def test_the_container_name_is_deterministic(
        self, workspace: Path, policy: SandboxPolicy
    ) -> None:
        """取消传播要能准确地停掉**这一个**容器。"""
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image="ases/sandbox:test",
            policy=policy,
        )
        assert spec.container_name == "ases-sandbox-task-1"

    def test_the_workspace_is_writable(self, workspace: Path, policy: SandboxPolicy) -> None:
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image="ases/sandbox:test",
            policy=policy,
        )
        mount = spec.mounts[0]
        assert mount.target == "/workspace"
        assert not mount.read_only

    def test_the_credentials_are_read_only(
        self, workspace: Path, tmp_path: Path, policy: SandboxPolicy
    ) -> None:
        creds = tmp_path / "creds"
        creds.mkdir()
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image="ases/sandbox:test",
            policy=policy,
            credential_dir=creds,
        )
        mount = spec.mounts[1]
        assert mount.read_only
        assert mount.credential
        assert mount.target == "/etc/ases/credentials"

    def test_the_environment_comes_from_the_allowlist(
        self, workspace: Path, policy: SandboxPolicy
    ) -> None:
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image="ases/sandbox:test",
            policy=policy,
            host_env={"GIT_AUTHOR_NAME": "ASES", "AWS_SECRET_ACCESS_KEY": "x"},
        )
        assert spec.env["GIT_AUTHOR_NAME"] == "ASES"
        assert "AWS_SECRET_ACCESS_KEY" not in spec.env

    def test_the_description_cannot_be_mutated_afterwards(
        self, workspace: Path, policy: SandboxPolicy
    ) -> None:
        """冻结的是"这份描述不许被改"，不是"里面的字典不许被改"。"""
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image="ases/sandbox:test",
            policy=policy,
        )
        with pytest.raises(TypeError):
            spec.env["HOME"] = "/root"  # type: ignore[index]


class TestTheSpecIsValidatedBeforeItRuns:
    def test_an_unsafe_spec_is_rejected(self, policy: SandboxPolicy) -> None:
        spec = SandboxSpec(
            image="ases/sandbox:test",
            network="none",
            mounts=(),
            env={"AWS_SECRET_ACCESS_KEY": "x"},
        )
        with pytest.raises(SandboxPolicyError):
            validate_sandbox_spec(spec, policy=policy)

    def test_the_launcher_refuses_to_build_an_unsafe_command(self, policy: SandboxPolicy) -> None:
        """不安全的描述**压根不该变成命令** —— 容器一旦跑起来就来不及了。"""
        spec = SandboxSpec(
            image="ases/sandbox:test",
            network="none",
            mounts=(),
            env={"DATABASE_URL": "postgresql://u:p@prod-db:5432/ases"},
        )
        launcher = SandboxLauncher(policy=policy)
        with pytest.raises(SandboxPolicyError):
            launcher.command_for(spec)

    def test_a_safe_spec_produces_a_command(self, workspace: Path, policy: SandboxPolicy) -> None:
        spec = build_workspace_sandbox_spec(
            workspace_path=workspace,
            task_id="task-1",
            image="ases/sandbox:test",
            policy=policy,
        )
        command = SandboxLauncher(policy=policy).command_for(spec)
        assert command[0] == "docker"
        assert "ases/sandbox:test" in command

    def test_the_environment_check_is_callable_on_its_own(self, policy: SandboxPolicy) -> None:
        assert_sandbox_env_is_safe({"HOME": "/home/ases"}, policy=policy)
        with pytest.raises(SandboxPolicyError):
            assert_sandbox_env_is_safe({"GITHUB_TOKEN": "x"}, policy=policy)
