"""集成测试的公共夹具 —— 需要真实 PostgreSQL 与 MinIO。

设计依据：docs/tasks.md TASK-P1-003（迁移可逆、空库迁移 < 30s）、
TASK-P1-020（CRUD 往返）、TASK-P1-022（DB 级约束）、
TASK-P1-004（对象存储）。

没有 PostgreSQL / MinIO 时**跳过**而不是失败 —— ``make test`` 只跑单元测试，
不该因为本机没装 Docker 就红。装了之后 ``make test-all`` 会真正执行这些断言。

连接参数可用环境变量覆盖：

    ASES_TEST_PG_HOST / ASES_TEST_PG_PORT / ASES_TEST_PG_USER / ASES_TEST_PG_PASSWORD
    ASES_TEST_DB_NAME
    ASES_TEST_S3_ENDPOINT_URL / ASES_TEST_S3_ACCESS_KEY / ASES_TEST_S3_SECRET_KEY
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from infrastructure.config.settings import Settings
from infrastructure.storage.object_store import ContentAddressedStore, S3ObjectStore
from tests.integration.docker_utils import is_registry_unreachable

REPO_ROOT = Path(__file__).resolve().parents[2]

PG_HOST = os.environ.get("ASES_TEST_PG_HOST", "localhost")
PG_PORT = os.environ.get("ASES_TEST_PG_PORT", "5432")
PG_USER = os.environ.get("ASES_TEST_PG_USER", "ases")
PG_PASSWORD = os.environ.get("ASES_TEST_PG_PASSWORD", "ases")
TEST_DB_NAME = os.environ.get("ASES_TEST_DB_NAME", "ases_test")

S3_ENDPOINT_URL = os.environ.get(
    "ASES_TEST_S3_ENDPOINT_URL",
    # 端口与 docker-compose.yml 的 MINIO_PORT 默认值一致 —— 两边读同一个变量，
    # 就不会因为"compose 改了端口、测试还指着旧端口"而出现查无此桶的假失败。
    f"http://localhost:{os.environ.get('MINIO_PORT', '9100')}",
)
S3_ACCESS_KEY = os.environ.get("ASES_TEST_S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.environ.get("ASES_TEST_S3_SECRET_KEY", "minioadmin")

ADMIN_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/postgres"
TEST_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
TEST_DATABASE_URL = (
    f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
)

#: 全部业务表。清理时按这个顺序 TRUNCATE ... CASCADE。
ALL_TABLES = (
    "verification_run",
    "artifact",
    "execution_event",
    "agent_session",
    "task_dependency",
    "task",
    "evolution_decision",
    "evolution_candidate",
    "evolution_cycle",
    "project_snapshot",
    "architecture_decision",
    "project",
    "budget_entry",
    "budget_account",
)

#: append-only 表。清理时需要临时关掉 TRUNCATE 守卫 —— 守卫存在的目的
#: 是挡住误删，不是挡住测试重建环境。
#:
#: 表名与触发器名遵循 ``trg_{table}_append_only_truncate`` 约定，所以清理逻辑
#: 不需要为每张表单独配置。
APPEND_ONLY_TABLES = (
    "verification_run",
    "artifact",
    "budget_entry",
    "budget_account",
)

#: 十二个核心实体 + 两张预算账本表的表名。
ENTITY_TABLES = ALL_TABLES


# --------------------------------------------------------------------------
# 数据库管理
# --------------------------------------------------------------------------


def _run_alembic(*args: str, database_url: str) -> float:
    """在子进程里跑 Alembic，返回耗时（秒）。

    之所以放到子进程：``migrations/env.py`` 内部用 ``asyncio.run()``，
    在已有事件循环的进程里调用会直接抛 "cannot be called from a running
    event loop"。子进程隔离掉了这个问题。
    """
    env = dict(os.environ)
    env["ASES_DATABASE_URL"] = database_url
    env["PYTHONPATH"] = str(REPO_ROOT)
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.perf_counter() - started
    if result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} 失败：\n{result.stdout}\n{result.stderr}")
    return elapsed


def _database_exists(name: str) -> bool:
    async def _check() -> bool:
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            return bool(await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name))
        finally:
            await conn.close()

    return asyncio.run(_check())


def _create_database(name: str) -> None:
    async def _create() -> None:
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    asyncio.run(_create())


def _drop_database(name: str) -> None:
    async def _drop() -> None:
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await conn.close()

    asyncio.run(_drop())


def _url_for(name: str) -> str:
    return f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{name}"


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_available() -> None:
    """PostgreSQL 不可用时跳过整个集成测试目录。"""

    async def _ping() -> None:
        conn = await asyncpg.connect(ADMIN_DSN, timeout=3)
        await conn.close()

    try:
        asyncio.run(_ping())
    except Exception as exc:
        pytest.skip(f"PostgreSQL 不可用（{exc}）；跳过集成测试")


@pytest.fixture(scope="session")
def migrated_database(request: pytest.FixtureRequest) -> str:
    """准备一个已迁移到 head 的测试库。"""
    # 显式取值而不是写成参数 —— 参数名会被 ruff 判成"未使用"，
    # 而这个依赖的价值恰恰在于"它的副作用是跳过"。
    request.getfixturevalue("postgres_available")
    if not _database_exists(TEST_DB_NAME):
        _create_database(TEST_DB_NAME)
    _run_alembic("upgrade", "head", database_url=TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest_asyncio.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    """每个测试一个引擎。

    必须是**函数级**的：pytest-asyncio 默认给每个测试开一个新的事件循环，
    而连接池里的连接绑定在创建它的事件循环上。跨循环复用连接会抛
    "got Future ... attached to a different loop"。

    用 NullPool 是同一件事的另一种解法：每次取连接都新建，不存在跨循环复用。
    测试环境不需要连接池。
    """
    async_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        yield async_engine
    finally:
        await async_engine.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """一个干净会话。每个测试开始前清空所有表。"""
    async with engine.begin() as conn:
        for table in APPEND_ONLY_TABLES:
            await conn.execute(
                text(f"ALTER TABLE {table} DISABLE TRIGGER trg_{table}_append_only_truncate")
            )
        await conn.execute(text(f"TRUNCATE TABLE {', '.join(ALL_TABLES)} CASCADE"))
        for table in APPEND_ONLY_TABLES:
            await conn.execute(
                text(f"ALTER TABLE {table} ENABLE TRIGGER trg_{table}_append_only_truncate")
            )

    async with AsyncSession(engine, expire_on_commit=False) as db_session:
        yield db_session


@pytest.fixture(scope="session")
def scratch_database(request: pytest.FixtureRequest) -> Iterator[str]:
    """一个全新的空库，用完即删。给"空库从零迁移"这类测试用。"""
    request.getfixturevalue("postgres_available")
    name = f"ases_scratch_{os.getpid()}"
    _create_database(name)
    try:
        yield _url_for(name)
    finally:
        with contextlib.suppress(Exception):
            _drop_database(name)


# --------------------------------------------------------------------------
# 对象存储（MinIO）
# --------------------------------------------------------------------------


@pytest.fixture
def s3_backend() -> Iterator[S3ObjectStore]:
    """一个指向 MinIO 的 S3 后端，桶是**每个测试一个**的。

    内容寻址的 key 是内容哈希，同一份内容在哪个桶里都指向同一个 key。
    共享一个桶的话，"库里只有一个对象"这条断言会被上一个测试留下的
    对象破坏 —— 所以桶必须是函数级的。

    清理用 aioboto3 直接做，不走 ``S3ObjectStore`` —— 生产类里不留
    "删对象"的口子（证据是 append-only 的）。
    清理失败不算失败 —— 残留一个测试桶无害，
    但把它当错误抛出去会让本地开发体验很差。
    """
    try:
        import aioboto3  # noqa: F401, PLC0415
    except ImportError:  # pragma: no cover - 取决于环境是否装了 s3 附加依赖
        pytest.skip("缺少 aioboto3（uv sync --extra s3）；跳过对象存储集成测试")

    bucket = f"ases-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    settings = Settings(
        s3_endpoint_url=S3_ENDPOINT_URL,
        s3_access_key=SecretStr(S3_ACCESS_KEY),
        s3_secret_key=SecretStr(S3_SECRET_KEY),
        s3_bucket=bucket,
    )
    backend = S3ObjectStore(settings)
    try:
        asyncio.run(backend.ensure_bucket())
    except Exception as exc:
        pytest.skip(f"MinIO 不可用（{exc}）；跳过对象存储集成测试")

    try:
        yield backend
    finally:
        with contextlib.suppress(Exception):
            asyncio.run(_delete_bucket(bucket))


async def _delete_bucket(bucket: str) -> None:
    """删掉测试桶（含桶内所有对象）。"""
    import aioboto3  # noqa: PLC0415

    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
    ) as client:
        token: str | None = None
        keys: list[dict[str, str]] = []
        while True:
            kwargs: dict[str, object] = {"Bucket": bucket}
            if token:
                kwargs["ContinuationToken"] = token
            response = await client.list_objects_v2(**kwargs)
            for item in response.get("Contents", []):
                keys.append({"Key": str(item["Key"])})
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                break
        if keys:
            await client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
        await client.delete_bucket(Bucket=bucket)


@pytest.fixture
def s3_store(s3_backend: S3ObjectStore) -> ContentAddressedStore:
    """内容寻址的 S3 存储。"""
    return ContentAddressedStore(s3_backend)


# --------------------------------------------------------------------------
# Docker（沙箱镜像）
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_available() -> None:
    """Docker 不可用时跳过。"""
    if shutil.which("docker") is None:
        pytest.skip("Docker 不可用；跳过沙箱集成测试")
    result = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip("Docker 守护进程不可用；跳过沙箱集成测试")


def _image_exists(image: str) -> bool:
    """本地有没有这个镜像。

    **问 docker 自己，不要问文件系统。** 镜像的存在与否是 docker 的账 ——
    磁盘上有没有那几层文件，docker 说了算。
    """
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


@pytest.fixture(scope="session")
def sandbox_image(request: pytest.FixtureRequest) -> str:
    """构建沙箱基础镜像。会话级 —— 只构建一次。

    与 ``test_sandbox_image.py`` 共用同一个夹具，所以两个测试模块不会各
    构建一次镜像。

    **仓库不可达但本地已经有这个镜像时，用本地那个。** 跳过的测试**什么
    都证明不了** —— 而"本地明明有一个能跑的镜像"却整组跳过，等于把能验的
    事白白放过。本地的镜像可能比 Dockerfile 旧一点，但内容变过时重建一次
    就行；跳过的代价却是这一轮完全没有证据。
    """
    # 显式取值而不是写成参数 —— 参数名会被 ruff 判成"未使用"，
    # 而这个依赖的价值恰恰在于"它的副作用是跳过"。
    request.getfixturevalue("docker_available")

    context = REPO_ROOT / "docker" / "sandbox"
    result = subprocess.run(
        ["docker", "build", "-t", "ases/sandbox:test", str(context)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return "ases/sandbox:test"
    combined = result.stdout + result.stderr
    # 拉不到基础镜像是**环境限制**，不是代码缺陷 —— 断网的机器上
    # 不该报一堆红。但 Dockerfile 写错了必须照常报错。
    if not is_registry_unreachable(combined):
        raise RuntimeError(f"构建沙箱镜像失败：\n{result.stdout}\n{result.stderr}")
    if _image_exists("ases/sandbox:test"):
        return "ases/sandbox:test"
    pytest.skip("镜像仓库不可达且本地没有沙箱镜像；跳过沙箱集成测试")
