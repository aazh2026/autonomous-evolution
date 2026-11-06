"""TASK-P1-006 —— 沙箱镜像。

三条硬断言（对应任务书 T1 ①②③）：

1. 容器内 ``id -u != 0``；
2. 容器内不存在 ``~/.ssh``、``~/.aws``、``.docker/config.json``；
3. 默认网络下容器无法访问外网。

第 3 条的"默认"取自 ``Settings.sandbox_network`` —— 不是"我们记得加
``--network none``"，而是**配置里的默认值**就是 ``none``。断言的是配置与
行为一致，而不是断言某一条命令的写法。

镜像构建较慢，这些测试标了 ``slow``；``make test`` 不带 integration，
本地默认不会跑到它们。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from infrastructure.config.settings import Settings
from tests.integration.docker_utils import is_registry_unreachable

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.integration, pytest.mark.slow]

TARGET_IMAGE = "ases/target-template:test"

#: 用来探测外网可达性的目标。用公共 DNS，任何网络环境都能解析它 ——
#: 换成内网地址的话，"连不上"可能只是地址不对，断言就失去意义了。
PROBE_HOST = "1.1.1.1"
PROBE_PORT = 443


def _build(context: Path, tag: str, *args: str) -> None:
    result = subprocess.run(
        ["docker", "build", "-t", tag, *args, str(context)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        combined = result.stdout + result.stderr
        # 拉不到基础镜像是**环境限制**，不是代码缺陷 —— 断网的机器上
        # 不该报一堆红。但 Dockerfile 写错了必须照常报错。
        if is_registry_unreachable(combined):
            pytest.skip(f"镜像仓库不可达，无法构建 {tag}；跳过沙箱镜像集成测试")
        raise RuntimeError(f"构建 {tag} 失败：\n{result.stdout}\n{result.stderr}")


def _run(image: str, *command: str, network: str | None = None) -> subprocess.CompletedProcess[str]:
    args = ["docker", "run", "--rm"]
    if network is not None:
        args += ["--network", network]
    args += [image, *command]
    return subprocess.run(args, capture_output=True, text=True, check=False)


# ``docker_available`` 与 ``sandbox_image`` 两个夹具在 ``conftest.py`` 里 ——
# 与 ``test_sandbox_mounts.py`` 共用，镜像只构建一次。


@pytest.fixture(scope="session")
def target_image(
    docker_available: None,  # noqa: ARG001
) -> str:
    """构建目标项目测试镜像模板。会话级。"""
    # docker_available 是依赖注入守卫：Docker 不可用时先跳过，参数本身用不到。
    _build(
        REPO_ROOT / "docker" / "target",
        TARGET_IMAGE,
        "-f",
        str(REPO_ROOT / "docker" / "target" / "Dockerfile.tmpl"),
        "--build-arg",
        "INSTALL_CMD=echo installed",
        "--build-arg",
        "TEST_CMD=echo hello-from-target",
    )
    return TARGET_IMAGE


class TestSandboxImage:
    """沙箱基础镜像。"""

    def test_container_runs_as_non_root(self, sandbox_image: str) -> None:
        result = _run(sandbox_image, "id", "-u")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() != "0", "容器以 root 身份运行 —— 沙箱隔离形同虚设"

    def test_no_ssh_directory(self, sandbox_image: str) -> None:
        result = _run(sandbox_image, "sh", "-c", "test -e ~/.ssh && echo EXISTS || echo ABSENT")
        assert result.stdout.strip() == "ABSENT"

    def test_no_aws_directory(self, sandbox_image: str) -> None:
        result = _run(sandbox_image, "sh", "-c", "test -e ~/.aws && echo EXISTS || echo ABSENT")
        assert result.stdout.strip() == "ABSENT"

    def test_no_docker_config(self, sandbox_image: str) -> None:
        result = _run(
            sandbox_image, "sh", "-c", "test -e ~/.docker/config.json && echo EXISTS || echo ABSENT"
        )
        assert result.stdout.strip() == "ABSENT"

    def test_toolchain_is_installed(self, sandbox_image: str) -> None:
        for tool in ("git", "python3", "node"):
            result = _run(sandbox_image, "sh", "-c", f"command -v {tool}")
            assert result.returncode == 0, f"{tool} 不在沙箱镜像里：{result.stderr}"


class TestNetworkIsolation:
    """T1 ③ —— 默认网络下容器无法访问外网。"""

    def test_configured_default_network_is_none(self) -> None:
        """默认值本身就是"无网络" —— 不是靠调用点记得加参数。"""
        assert Settings().sandbox_network == "none"

    def test_container_cannot_reach_the_internet(self, sandbox_image: str) -> None:
        probe = (
            "import socket,sys\n"
            "try:\n"
            f"    socket.create_connection(({PROBE_HOST!r}, {PROBE_PORT}), timeout=3)\n"
            "except OSError as exc:\n"
            "    print('BLOCKED', exc)\n"
            "    sys.exit(0)\n"
            "print('REACHED')\n"
            "sys.exit(1)\n"
        )
        result = _run(sandbox_image, "python3", "-c", probe, network="none")
        assert result.returncode == 0, (
            f"沙箱容器连上了外网（{PROBE_HOST}:{PROBE_PORT}）—— 网络隔离失效"
        )
        assert "BLOCKED" in result.stdout


class TestTargetImageTemplate:
    """目标项目测试镜像模板。"""

    def test_template_builds_and_runs_as_non_root(self, target_image: str) -> None:
        result = _run(target_image, "id", "-u")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() != "0"

    def test_template_has_no_credentials(self, target_image: str) -> None:
        result = _run(
            target_image,
            "sh",
            "-c",
            'for p in ~/.ssh ~/.aws ~/.docker; do test -e $p && echo "EXISTS:$p"; done; echo DONE',
        )
        assert "EXISTS" not in result.stdout

    def test_template_runs_the_test_command(self, target_image: str) -> None:
        result = _run(target_image)
        assert result.returncode == 0, result.stderr
        assert "hello-from-target" in result.stdout
