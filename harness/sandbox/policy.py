"""沙箱策略 —— 挂什么、给什么环境变量、怎么起容器。

设计依据：docs/design.md §3.5A（R-045~R-048：每个开发任务独立工作环境；
"不要让 Coding Agent 直接访问宿主机的 SSH 凭据、云平台管理员密钥和
生产数据库"）、§3.5D（默认禁止访问生产环境；网络白名单；凭证使用短期令牌，
不向 Prompt 暴露长期密钥）、docs/tasks.md TASK-P1-032（"只读挂载凭据目录
（无长期密钥）"）。

**这个模块的全部内容就是一条否定命题：沙箱里不该有的东西，一样都不许有。**
否定命题没法靠"我们记得别做"来保证，只能靠**构造上的不可能**：

- 环境变量**不是**从宿主机继承的，是按**白名单**重新造的 —— 白名单之外的
  一个都不进去。``AWS_SECRET_ACCESS_KEY`` 没在白名单里，所以它不在沙箱
  里；``HTTPS_PROXY`` 没在白名单里，所以沙箱不会借道宿主机出网。
- 挂载源**不是**"想挂什么挂什么"，是**黑名单**驱动的：宿主的
  ``~/.ssh``、``~/.aws``、``~/.config/gcloud``、``~/.docker``、``~/.kube``、
  ``~/.gnupg`` 一律拒绝，而且"包含"它们的上级目录（``~/.config``、``~``、
  ``/``）同样拒绝 —— 只挡叶子不挡根，等于没挡。
- 数据库连接串**不是**靠"记得别连生产库"，而是**连接串里的主机名**必须
  在允许清单里。生产库的主机名不在清单里，所以连接串进不了沙箱。

**凭据走"只读挂载"，不走环境变量。** 环境变量里出现看起来像凭据的名字
（``*_TOKEN``、``*_SECRET``、``*_PASSWORD``、``*_KEY``）一律拒绝。理由是
环境变量几乎一定会被打印、被写进日志、被塞进 Prompt —— 那正是 design.md
说的"不向 Prompt 暴露长期密钥"。要用的凭据应该是**一次性的短期令牌**，
放在一个专门的目录里，只读挂进沙箱，用完就扔。

**失败方向只有一个：拒绝。** 任何一条检查没过就抛
:class:`SandboxPolicyError`，绝不"打个警告继续"。沙箱隔离一旦破了，破的
是"Agent 碰不到生产库"这条底线 —— 那不是可以事后补救的事。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


class SandboxPolicyError(RuntimeError):
    """沙箱策略检查没过。

    这条异常**只**在"沙箱可能被配置得不安全"时抛出，绝不用于表达"沙箱
    跑失败了"。这两种失败必须分开：前者是配置错误，必须在启动前就暴露；
    后者是运行问题，由调用方按重试/失败处理。
    """


#: 宿主家目录下**绝对不许**作为挂载源的凭据目录（相对家目录）。
#:
#: 逐个列出来而不是"匹配一个模式"：模式会漏。漏掉的那一个，代价是宿主
#: 上的一把长期密钥进了沙箱。
CREDENTIAL_DIRS_IN_HOME: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".azure",
    ".config/gcloud",
    ".docker",
    ".kube",
    ".gnupg",
    ".netrc",
    ".pgpass",
)

#: 无论宿主机是谁，这些目录也一律不许作为挂载源。
#:
#: ``/root`` 单列是因为它不在"家目录"的意义下，但它是 root 用户的家 ——
#: 里面的 ``.ssh``、``.aws`` 同样是长期凭据。
ALWAYS_FORBIDDEN_DIRS: tuple[str, ...] = (
    "/root",
    "/etc/ssh",
    "/var/run/docker.sock",
)

#: 沙箱里**总是**设好的环境变量，与宿主机无关。
#:
#: 刻意不设 ``PATH`` 之外的任何"宿主机可能影响行为"的变量：``PATH`` 要
#: 给（容器里得能找到 git / python3），``HOME`` 要给（很多工具往那儿写
#: 缓存），其余的都是"设了会让行为变得不可预测"的东西。
BASE_ENV: Mapping[str, str] = MappingProxyType(
    {
        "HOME": "/home/ases",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "CI": "true",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
    }
)

#: 宿主机环境变量里，**允许**透传进沙箱的名字。
#:
#: 是一份很短的清单，而且清一色是"可复现性"相关（提交者身份、时间戳、
#: 终端类型）—— 它们影响产物长什么样，但**不带任何能力**。
#:
#: 刻意排除的：``*_PROXY``（会让沙箱借道宿主机出网）、``AWS_*`` /
#: ``GOOGLE_*`` / ``KUBECONFIG``（云凭据）、``*_TOKEN`` / ``*_SECRET`` /
#: ``*_PASSWORD``（长期密钥）、``DATABASE_URL``（生产库连接串）。
ALLOWED_HOST_ENV_NAMES: frozenset[str] = frozenset(
    {
        "TERM",
        "SOURCE_DATE_EPOCH",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "NODE_ENV",
        "PYTEST_ADDOPTS",
        "COVERAGE_RCFILE",
    }
)

#: 环境变量名里出现这些**词**，就判定它装的是凭据。
#:
#: 按 ``_`` 切成词再逐个比对，而不是子串匹配 —— 否则 ``TOKENIZERS_PARALLELISM``
#: 这种无害变量会被误判。代价是 ``SSH_KEY`` 这种"词是 KEY 但其实是公钥
#: 指纹"的用法也会被拒；那属于极少数情况，宁可拒。
SECRET_NAME_TOKENS: frozenset[str] = frozenset(
    {
        "SECRET",
        "SECRETS",
        "TOKEN",
        "TOKENS",
        "PASSWORD",
        "PASSWD",
        "PASSPHRASE",
        "CREDENTIAL",
        "CREDENTIALS",
        "KEY",
        "APIKEY",
    }
)

#: 数据库连接串里会出现的 scheme。只有这些 scheme 的 URL 才按"数据库连接
#: 串"检查 —— 否则一个普通的 ``https://example.com`` 会被当成连接串，然后
#: 因为主机名不在允许清单里而被拒，那只是误伤。
DATABASE_SCHEMES: frozenset[str] = frozenset(
    {
        "postgres",
        "postgresql",
        "postgresql+asyncpg",
        "postgresql+psycopg",
        "postgresql+psycopg2",
        "mysql",
        "mysql+aiomysql",
        "mysql+pymysql",
        "mariadb",
        "redis",
        "rediss",
        "mongodb",
        "mongodb+srv",
        "mssql",
        "sqlserver",
        "oracle",
        "cockroachdb",
    }
)

#: URL 形式的连接串：``scheme://[user:pass@]host[:port]``。
_URL_DSN_PATTERN = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://"
    r"(?:[^\s/@]*@)?"
    r"(?P<host>[^\s/:?#]+)"
    r"(?::(?P<port>\d+))?"
)

#: 关键字形式的连接串：``host=db.example.com port=5432``。
_KEYWORD_HOST_PATTERN = re.compile(r"(?:^|[\s;])host\s*=\s*(?P<host>[^\s;]+)", re.IGNORECASE)

#: 值里出现"凭据目录路径"时的形状。
#:
#: 要求目录名前面是路径分隔符（或字符串开头），后面**也是**路径分隔符或
#: 字符串结尾 —— 否则 ``.dockerignore`` 这种文件会被误判成 ``.docker``
#: 目录。
_CREDENTIAL_PATH_PATTERN = re.compile(
    r"(?:^|[/\\])(?:"
    + "|".join(re.escape(relative) for relative in CREDENTIAL_DIRS_IN_HOME)
    + r")(?:[/\\]|$)"
)

#: 主机名里出现这些词，一律判定为生产环境。
#:
#: 是"按词判定"而不是"按子串"：``reproduce.corp`` 里的 ``prod`` 不是
#: 生产环境，而 ``prod-db``、``db-prod``、``production`` 是。
_PRODUCTION_HOST_TOKENS: frozenset[str] = frozenset({"prod", "production"})

#: 默认允许连接的数据库主机。**只有本地与测试环境**。
#:
#: 生产库的主机名不在这里 —— 这就是"生产库连接串进不了沙箱"的全部机制。
DEFAULT_ALLOWED_DB_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "host.docker.internal",
        "postgres",
        "db",
        "test-db",
    }
)


@dataclass(frozen=True, slots=True)
class MountSpec:
    """一条挂载。

    :param source: 宿主机的源路径。必须**存在**且**绝对**。
    :param target: 容器内的目标路径。必须绝对。
    :param read_only: 是否只读。
    :param credential: 这是不是一份"凭据挂载"。凭据挂载**必须**只读 ——
        沙箱里的进程只要能写，就可能把令牌改成自己的、或者把令牌偷出去。
    """

    source: Path
    target: str
    read_only: bool = False
    credential: bool = False

    def to_docker_spec(self) -> str:
        """docker ``--volume`` 的参数值。"""
        suffix = ":ro" if self.read_only else ""
        return f"{self.source}:{self.target}{suffix}"


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """沙箱策略。

    :param allowed_env_names: 允许从宿主机透传进来的环境变量名。
    :param allowed_db_hosts: 允许出现在数据库连接串里的主机名。
    :param forbidden_mount_sources: 一律拒绝的挂载源。
    :param home: 宿主机的家目录 —— 凭据目录从它推出来。
    """

    allowed_env_names: frozenset[str]
    allowed_db_hosts: frozenset[str]
    forbidden_mount_sources: tuple[Path, ...]
    home: Path


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """一次沙箱运行的完整描述。

    描述与执行分开，是因为"沙箱应该长什么样"是需要被**检查**的东西，
    而"把它跑起来"只是执行。检查发生在构造之后、启动之前。
    """

    image: str
    network: str
    mounts: tuple[MountSpec, ...]
    env: Mapping[str, str]
    working_dir: str = "/workspace"
    command: tuple[str, ...] = ()
    container_name: str | None = None
    user: str | None = None
    drop_capabilities: tuple[str, ...] = ("ALL",)
    no_new_privileges: bool = True

    def __post_init__(self) -> None:
        # 冻结的是"这份描述不许被改"，不是"里面的字典不许被改" ——
        # 底层字典仍然可改的话，外层冻结就是个假象。
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """一次沙箱运行的结果。"""

    exit_code: int
    stdout: str
    stderr: str
    container_id: str = ""


def forbidden_mount_sources(home: Path | None = None) -> tuple[Path, ...]:
    """宿主机上一律不许作为挂载源的路径。

    包括：家目录下的凭据目录、它们的**上级目录**（``~/.config``、
    ``~``、``/``）、以及 :data:`ALWAYS_FORBIDDEN_DIRS` 里的那些。
    """
    base = Path(home) if home is not None else Path.home()
    sources: list[Path] = [base / relative for relative in CREDENTIAL_DIRS_IN_HOME]
    sources.extend(Path(item) for item in ALWAYS_FORBIDDEN_DIRS)
    # 家目录本身也要挡 —— 它是上面所有凭据目录的公共上级。
    sources.append(base)
    return tuple(sorted({path.resolve() for path in sources}, key=str))


def default_sandbox_policy(home: Path | None = None) -> SandboxPolicy:
    """默认策略。"""
    base = Path(home) if home is not None else Path.home()
    return SandboxPolicy(
        allowed_env_names=ALLOWED_HOST_ENV_NAMES,
        allowed_db_hosts=DEFAULT_ALLOWED_DB_HOSTS,
        forbidden_mount_sources=forbidden_mount_sources(base),
        home=base,
    )


# ---------- 环境变量 ----------


def _name_looks_like_secret(name: str) -> bool:
    """变量名是不是装凭据的。"""
    tokens = {part for part in re.split(r"[._]", name.upper()) if part}
    return bool(tokens & SECRET_NAME_TOKENS)


def _host_is_production(host: str) -> bool:
    """主机名是不是生产环境的。"""
    tokens = {part for part in re.split(r"[.\-]", host.lower()) if part}
    return bool(tokens & _PRODUCTION_HOST_TOKENS)


def _hosts_in_value(value: str) -> tuple[str, ...]:
    """一个值里出现的所有"数据库主机名"。"""
    hosts: list[str] = []
    for match in _URL_DSN_PATTERN.finditer(value):
        if match.group("scheme").lower() in DATABASE_SCHEMES:
            hosts.append(match.group("host"))
    for match in _KEYWORD_HOST_PATTERN.finditer(value):
        hosts.append(match.group("host"))
    return tuple(hosts)


def assert_sandbox_env_is_safe(env: Mapping[str, str], *, policy: SandboxPolicy) -> None:
    """检查一份环境变量能不能进沙箱。

    三条：

    1. **名字不像凭据** —— 凭据走只读挂载，不走环境变量。
    2. **值里没有宿主的凭据路径** —— 比如 ``/Users/x/.ssh`` 或 ``~/.aws``。
    3. **值里没有生产库的连接串** —— 连接串里的主机名必须在允许清单里，
       且主机名本身不能带 ``prod`` 之类的词。
    """
    for name, value in env.items():
        if _name_looks_like_secret(name):
            raise SandboxPolicyError(
                f"环境变量 {name!r} 看起来装的是凭据 —— 凭据必须走只读挂载，"
                "不能进环境变量（环境变量会被日志与 Prompt 看到）"
            )
        _assert_no_credential_path(name, value, policy)
        _assert_no_production_dsn(name, value, policy)


def _assert_no_credential_path(name: str, value: str, policy: SandboxPolicy) -> None:
    """值里不许出现凭据目录的路径。

    查两样：

    1. **宿主自己的**凭据目录的绝对路径 —— 比如 ``/Users/x/.ssh``；
    2. **任何人的**凭据目录路径 —— 靠 :data:`_CREDENTIAL_PATH_PATTERN` 按
       路径边界匹配，所以 ``/home/agent/.ssh/config`` 这种"不是宿主的家
       目录、但仍然是凭据目录"的路径也会被抓住。

    按路径边界匹配（目录名后面必须是路径分隔符或字符串结尾）是为了不误伤
    ``.dockerignore`` 这种**文件**名 —— 它含有 ``.docker``，但它不是凭据
    目录。
    """
    for forbidden in policy.forbidden_mount_sources:
        if str(forbidden) in value:
            raise SandboxPolicyError(f"环境变量 {name!r} 的值里含有宿主凭据路径 {forbidden}")
    match = _CREDENTIAL_PATH_PATTERN.search(value)
    if match is not None:
        raise SandboxPolicyError(f"环境变量 {name!r} 的值里含有凭据目录路径 {match.group(0)!r}")


def _assert_no_production_dsn(name: str, value: str, policy: SandboxPolicy) -> None:
    """值里不许有指向非允许主机的数据库连接串。"""
    for host in _hosts_in_value(value):
        normalized = host.strip("[]").lower()
        if _host_is_production(normalized):
            raise SandboxPolicyError(
                f"环境变量 {name!r} 的值里含有指向生产环境 {host!r} 的数据库连接串"
            )
        if normalized not in policy.allowed_db_hosts:
            raise SandboxPolicyError(
                f"环境变量 {name!r} 的值里含有指向 {host!r} 的数据库连接串，"
                f"而它不在允许清单 {sorted(policy.allowed_db_hosts)} 里"
            )


def build_sandbox_env(
    host_env: Mapping[str, str],
    *,
    policy: SandboxPolicy | None = None,
    base_env: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """按白名单造一份沙箱环境变量。

    **不继承宿主机的整个环境** —— 只取白名单里的那几个名字。白名单之外的
    一个都不进来，包括 ``HTTPS_PROXY``、``AWS_*``、``DATABASE_URL``。

    :param host_env: 宿主机的环境变量。
    :param policy: 策略。默认用 :func:`default_sandbox_policy`。
    :param base_env: 基准环境变量。默认用 :data:`BASE_ENV`。
    :param extra: 额外要加进去的变量 —— 它们**同样**要过安全检查。
    """
    active = policy or default_sandbox_policy()
    env: dict[str, str] = dict(base_env if base_env is not None else BASE_ENV)
    for name in sorted(active.allowed_env_names):
        value = host_env.get(name)
        if value is not None:
            env[name] = value
    if extra:
        env.update(extra)
    assert_sandbox_env_is_safe(env, policy=active)
    return env


# ---------- 挂载 ----------


def _is_within(child: Path, parent: Path) -> bool:
    """``child`` 是不是在 ``parent`` 里面（含相等）。"""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def assert_mounts_are_safe(
    mounts: Iterable[MountSpec], *, policy: SandboxPolicy | None = None
) -> None:
    """检查一份挂载清单能不能用。

    四条：

    1. 源必须**存在**且**绝对**。挂一个不存在的路径，docker 会创建一个
       **属于 root 的**空目录 —— 那是个既难查又难解释的坑。
    2. 源不能是宿主的凭据目录，**也不能是包含它们的上级目录**。
    3. 凭据挂载必须**只读**。
    4. 目标不能重复 —— 同一个目标挂两次，后一条会静默盖掉前一条。
    """
    active = policy or default_sandbox_policy()
    seen_targets: set[str] = set()
    for mount in mounts:
        source = mount.source
        if not source.is_absolute():
            raise SandboxPolicyError(f"挂载源 {source} 不是绝对路径")
        if not source.exists():
            raise SandboxPolicyError(f"挂载源 {source} 不存在")
        if not mount.target.startswith("/"):
            raise SandboxPolicyError(f"挂载目标 {mount.target!r} 不是绝对路径")
        if mount.target == "/":
            raise SandboxPolicyError("不允许把容器根目录整个覆盖掉")
        if mount.target in seen_targets:
            raise SandboxPolicyError(f"挂载目标 {mount.target!r} 重复")
        seen_targets.add(mount.target)
        if mount.credential and not mount.read_only:
            raise SandboxPolicyError(
                f"凭据挂载 {source} 必须是只读的 —— 可写的凭据挂载等于没有凭据挂载"
            )
        resolved = source.resolve()
        for forbidden in active.forbidden_mount_sources:
            if resolved == forbidden or _is_within(resolved, forbidden):
                raise SandboxPolicyError(f"挂载源 {source} 是宿主凭据目录（或位于其中），拒绝挂载")
        for forbidden in active.forbidden_mount_sources:
            if _is_within(forbidden, resolved):
                raise SandboxPolicyError(f"挂载源 {source} 包含宿主凭据目录 {forbidden}，拒绝挂载")


# ---------- 容器 ----------


def build_sandbox_command(spec: SandboxSpec) -> tuple[str, ...]:
    """把一份描述变成 ``docker run`` 的参数。"""
    args: list[str] = ["run", "--rm"]
    if spec.container_name:
        args += ["--name", spec.container_name]
    args += ["--network", spec.network]
    if spec.user:
        args += ["--user", spec.user]
    if spec.no_new_privileges:
        args += ["--security-opt", "no-new-privileges"]
    for capability in spec.drop_capabilities:
        args += ["--cap-drop", capability]
    for mount in spec.mounts:
        args += ["--volume", mount.to_docker_spec()]
    for name, value in sorted(spec.env.items()):
        args += ["--env", f"{name}={value}"]
    args += ["--workdir", spec.working_dir]
    args.append(spec.image)
    args += list(spec.command)
    return tuple(args)


def validate_sandbox_spec(spec: SandboxSpec, *, policy: SandboxPolicy | None = None) -> None:
    """把一份描述里所有能出问题的地方都检查一遍。

    **在启动之前检查。** 容器一旦跑起来，"改一下配置再重启"和"就这一次"
    之间只隔着一个念头。
    """
    active = policy or default_sandbox_policy()
    assert_sandbox_env_is_safe(spec.env, policy=active)
    assert_mounts_are_safe(spec.mounts, policy=active)


def build_workspace_sandbox_spec(
    *,
    workspace_path: Path,
    task_id: str,
    image: str,
    network: str = "none",
    policy: SandboxPolicy | None = None,
    host_env: Mapping[str, str] | None = None,
    credential_dir: Path | None = None,
    command: tuple[str, ...] = (),
    working_dir: str = "/workspace",
) -> SandboxSpec:
    """给一个工作空间造一份标准的沙箱描述。

    - 工作空间挂到 ``/workspace``（可写 —— Agent 要在里面改代码）；
    - 凭据目录（如果给了）挂到 ``/etc/ases/credentials``（**只读**）；
    - 环境变量按白名单从宿主机取。

    容器名是 ``ases-sandbox-<task_id>`` —— 确定的，不是随机的。取消传播
    （TASK-P1-033）要能准确地 ``docker stop`` 到这一个容器；随机的名字
    做不到这件事。
    """
    active = policy or default_sandbox_policy()
    mounts = [MountSpec(source=Path(workspace_path), target=working_dir, read_only=False)]
    if credential_dir is not None:
        mounts.append(
            MountSpec(
                source=Path(credential_dir),
                target="/etc/ases/credentials",
                read_only=True,
                credential=True,
            )
        )
    env = build_sandbox_env(host_env or {}, policy=active)
    return SandboxSpec(
        image=image,
        network=network,
        mounts=tuple(mounts),
        env=env,
        working_dir=working_dir,
        command=command,
        container_name=f"ases-sandbox-{task_id}",
    )


__all__ = [
    "ALLOWED_HOST_ENV_NAMES",
    "ALWAYS_FORBIDDEN_DIRS",
    "BASE_ENV",
    "CREDENTIAL_DIRS_IN_HOME",
    "DATABASE_SCHEMES",
    "DEFAULT_ALLOWED_DB_HOSTS",
    "SECRET_NAME_TOKENS",
    "MountSpec",
    "SandboxPolicy",
    "SandboxPolicyError",
    "SandboxResult",
    "SandboxSpec",
    "assert_mounts_are_safe",
    "assert_sandbox_env_is_safe",
    "build_sandbox_command",
    "build_sandbox_env",
    "build_workspace_sandbox_spec",
    "default_sandbox_policy",
    "forbidden_mount_sources",
    "validate_sandbox_spec",
]
