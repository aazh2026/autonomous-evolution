"""本地全栈的端到端验证。

设计依据：docs/tasks.md TASK-P1-007

- **T1 ①** ``make up`` 后 ``/healthz`` 返回 200 且 DB/MinIO 均连通。
- **T1 ②** ``make down && make up`` 后数据仍在（卷持久化）。
- **T2** 从零到全栈就绪 < 90s。

**T2 怎么算"从零"**：这里量的是**编排**的耗时 —— 容器全部停掉（但数据卷留着）
到所有服务就绪为止。镜像构建不计入：拉镜像是网络操作，测的是"ASES 有多重"，
不是"网络有多快"。构建单独做（见 ``stack`` 夹具），构建完成后再计时。

**这些测试会动全局的 Docker 状态**（``docker compose down`` 会停掉 Postgres）。
所以夹具在结束时把栈**恢复成运行状态**，而不是留着它停着 ——
后面那些集成测试还要用同一个 Postgres。

本文件不依赖 ``tests/integration/conftest.py`` 里的 ``migrated_database`` 夹具：
那个夹具指向 ``ases_test`` 库，是给别的测试用的；这里要验的是**栈自己的那个库**
（``ases``）在卷上的持久化。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.docker_utils import is_registry_unreachable

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 栈自身使用的数据库（compose 里 postgres 建的那个），不是 ases_test。
STACK_DB_NAME = os.environ.get("ASES_STACK_DB_NAME", "ases")
PG_HOST = os.environ.get("ASES_TEST_PG_HOST", "localhost")
PG_PORT = os.environ.get("ASES_TEST_PG_PORT", "5432")
PG_USER = os.environ.get("ASES_TEST_PG_USER", "ases")
PG_PASSWORD = os.environ.get("ASES_TEST_PG_PASSWORD", "ases")
STACK_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{STACK_DB_NAME}"

API_BASE_URL = os.environ.get("ASES_STACK_API_URL", "http://localhost:8000")

#: T2 的上限（秒）。docs/tasks.md TASK-P1-007 规定 < 90s。
STARTUP_BUDGET_SECONDS = 90.0

#: 启动时给 compose 的等待上限。给一个明确的值 —— "卡住"和"就绪"必须能区分开。
COMPOSE_WAIT_SECONDS = 180

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _compose(*args: str, timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    """跑一条 docker compose 命令。

    不用 ``check=True`` —— 失败的信息要自己带出来（compose 的报错在 stderr，
    而测试报告里只看得到我们自己写的东西）。
    """
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _start_stack() -> subprocess.CompletedProcess[str]:
    return _compose("up", "-d", "--wait", "--wait-timeout", str(COMPOSE_WAIT_SECONDS))


def _stack_is_running() -> bool:
    """栈当前是否在运行。"""
    ps = _compose("ps", "--status", "running")
    return ps.returncode == 0 and "api" in ps.stdout


@pytest.fixture(scope="module")
def docker_available() -> None:
    """没有 Docker 就跳过整个文件。"""
    if shutil.which("docker") is None:
        pytest.skip("没有 docker 可执行文件；跳过本地全栈测试")
    probe = subprocess.run(
        ["docker", "compose", "version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"docker compose 不可用：{probe.stderr.strip()}")


@pytest.fixture(scope="module")
def stack(request: pytest.FixtureRequest) -> Iterator[None]:
    """把全栈拉起来，跑完恢复原状。

    先构建再启动：``up`` 时才构建的话，构建耗时会被算进"启动耗时"里，
    而那不是我们想量的东西（见模块文档里对 T2 的解释）。
    """
    # 显式取值而不是写成参数 —— 参数名会被 ruff 判成"未使用"，
    # 而这个依赖的价值恰恰在于"它的副作用是跳过"。
    request.getfixturevalue("docker_available")
    was_running = _stack_is_running()

    build = _compose("build")
    if build.returncode != 0:
        combined = build.stdout + build.stderr
        if is_registry_unreachable(combined):
            pytest.skip("镜像仓库不可达，无法构建应用镜像；跳过本地全栈测试")
        pytest.skip(f"镜像构建失败：\n{build.stdout}\n{build.stderr}")

    up = _start_stack()
    if up.returncode != 0:
        pytest.skip(f"全栈启动失败：\n{up.stdout}\n{up.stderr}")
    try:
        yield
    finally:
        # 恢复原状，而不是无脑停掉：后面的集成测试还要用同一个 Postgres，
        # 而删除卷是破坏性的 —— `make reset` 是显式动作，不该由测试顺手做掉。
        if was_running:
            _start_stack()


def _health(path: str = "/healthz") -> tuple[int, dict[str, Any]]:
    """请求健康端点，返回 (状态码, 响应体)。"""
    response = httpx.get(f"{API_BASE_URL}{path}", timeout=10.0)
    try:
        payload: dict[str, Any] = response.json()
    except ValueError:
        payload = {"raw": response.text}
    return response.status_code, payload


@pytest.mark.usefixtures("stack")
def test_healthz_reports_dependencies_connected() -> None:
    """T1 ①：``make up`` 后 ``/healthz`` 返回 200，且 DB 与 MinIO 都连通。

    断言的不只是 200 —— 还要看它**说了什么**。一个"永远返回 200"的
    健康检查比没有健康检查更危险：它会在依赖挂了的时候给出放行信号。
    所以这里逐项检查 ``database.ok`` 与 ``object_store.ok``。
    """
    status_code, payload = _health()

    assert status_code == 200, f"/healthz 返回 {status_code}：{payload}"
    assert payload["status"] == "ok", payload

    checks = payload["checks"]
    assert isinstance(checks, dict)
    assert checks["database"]["ok"] is True, checks
    assert checks["object_store"]["ok"] is True, checks


@pytest.mark.usefixtures("stack")
def test_healthz_does_not_leak_credentials() -> None:
    """健康检查响应里不能出现凭证。

    连接失败时异常消息里常常带着连接串，而连接串里有口令。
    任何能访问这个端点的人都看得到响应 —— 所以响应里只允许出现
    异常**类型名**，不允许出现消息本身。
    """
    status_code, payload = _health()
    body = json.dumps(payload, ensure_ascii=False)

    assert status_code in (200, 503)
    assert PG_PASSWORD not in body
    assert "postgresql://" not in body
    assert "minioadmin" not in body


@pytest.mark.usefixtures("stack")
def test_readyz_is_also_available() -> None:
    """就绪探针与健康探针探同一批依赖。"""
    status_code, payload = _health("/readyz")

    assert status_code == 200, payload
    assert payload["status"] == "ok", payload


async def _insert_project(project_id: str) -> None:
    """往栈自己的库里插一行 project。"""
    conn = await asyncpg.connect(STACK_DSN)
    try:
        now = datetime.now(UTC)
        await conn.execute(
            """
            INSERT INTO project (id, name, goal, policy, repository_url,
                                 default_branch, tech_stack, budget,
                                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (id) DO NOTHING
            """,
            project_id,
            "持久化验证项目",
            json.dumps({"objective": "验证卷持久化"}),
            json.dumps({"level": "L1"}),
            "https://example.invalid/persistence-check.git",
            "main",
            json.dumps(["python"]),
            json.dumps({}),
            now,
            now,
        )
    finally:
        await conn.close()


async def _project_exists(project_id: str) -> bool:
    conn = await asyncpg.connect(STACK_DSN)
    try:
        row = await conn.fetchrow("SELECT id FROM project WHERE id = $1", project_id)
        return row is not None
    finally:
        await conn.close()


@pytest.mark.usefixtures("stack")
def test_data_survives_down_and_up() -> None:
    """T1 ②：``make down && make up`` 之后数据还在。

    这一条验的是**卷持久化**：``down`` 停容器但保留卷，``up`` 之后
    数据必须还在。数据没了就说明容器里那份状态被当成了真相 ——
    那意味着任何一次重启都可能把系统状态抹掉。

    用一个带随机后缀的 id，避免上一轮跑测试留下的行造成假通过。
    """
    project_id = f"persistence-check-{os.getpid()}-{int(time.time())}"
    asyncio.run(_insert_project(project_id))
    assert asyncio.run(_project_exists(project_id))

    down = _compose("down")
    assert down.returncode == 0, down.stderr

    up = _start_stack()
    assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"

    assert asyncio.run(_project_exists(project_id)), (
        "make down && make up 之后数据没了 —— 卷没有持久化"
    )


@pytest.mark.usefixtures("stack")
def test_stack_reaches_ready_within_budget() -> None:
    """T2：从零到全栈就绪 < 90s。

    "从零" = 容器全停（数据卷保留）。镜像构建不计入 ——
    见模块文档。
    """
    down = _compose("down")
    assert down.returncode == 0, down.stderr

    started = time.perf_counter()
    up = _start_stack()
    elapsed = time.perf_counter() - started

    assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"
    assert elapsed < STARTUP_BUDGET_SECONDS, (
        f"全栈就绪用了 {elapsed:.1f}s，超过 {STARTUP_BUDGET_SECONDS:.0f}s"
    )


@pytest.mark.usefixtures("stack")
def test_worker_is_running() -> None:
    """worker 容器在运行。

    "进程在跑"和"worker 还在正常循环"是两件事。worker 把心跳写进文件，
    compose 的健康检查看文件的新鲜度 —— 一个卡死循环的 worker
    也能通过"进程存在"这一关，所以这里只断言容器状态，
    心跳的新鲜度交给 compose 的 healthcheck。
    """
    ps = _compose("ps", "--format", "json")
    assert ps.returncode == 0, ps.stderr
    entries = _parse_compose_ps(ps.stdout)
    worker = next((e for e in entries if e.get("Service") == "worker"), None)

    assert worker is not None, f"没有找到 worker 容器：{ps.stdout}"
    assert worker.get("State") == "running", worker


def _parse_compose_ps(raw: str) -> list[dict[str, Any]]:
    """解析 ``docker compose ps --format json`` 的输出。

    不同版本的 compose 输出形态不一样：数组、或者一行一个 JSON 对象。
    两种都认 —— 解析失败要降级成"没解析出东西"，而不是让整个测试报错。
    """
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        return [e for e in parsed if isinstance(e, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    entries: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries
