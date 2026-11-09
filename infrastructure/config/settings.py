"""ASES 运行配置。

设计依据：docs/design.md §3.5D（凭证用短期令牌，不向 Prompt 暴露长期密钥）、
docs/tasks.md TASK-P1-002。

配置分三类，边界由本模块强制：

1. **外部配置**（治理类）—— 权限策略、验收规则、预算上限、目标定义。
   存放在 YAML 文件或数据库中，路径由本模块的 ``governance_*`` 字段指向。
   绝不允许硬编码在代码里：否则系统无法"修改自身治理策略"（design.md §7）。
2. **运行时配置**—— 模型名、超时、并发数、日志级别。可安全地写死在代码默认值里。
3. **密钥**—— 只从环境注入，统一用 ``SecretStr`` 承载，repr 与日志中不泄露。
"""

from __future__ import annotations

from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOVERNANCE_DIR = REPO_ROOT / "config" / "governance"


class Environment(StrEnum):
    """运行环境。决定配置校验的严格程度。"""

    LOCAL = "local"
    TEST = "test"
    CI = "ci"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    """从环境变量读取的运行配置。

    所有字段的环境变量前缀为 ``ASES_``，例如 ``ASES_DATABASE_URL``。
    密钥字段使用 ``SecretStr``：即使被打印也只会显示 ``**********``。
    """

    model_config = SettingsConfigDict(
        env_prefix="ASES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ---------- 运行时配置 ----------

    environment: Environment = Environment.LOCAL
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: LogFormat = LogFormat.JSON
    service_name: str = "ases"

    # ---------- 数据库 ----------

    # 连接串里带口令，所以它是**密钥字段**而非普通字符串：默认脱敏，
    # 取明文要走 :attr:`database_dsn`。
    database_url: SecretStr = SecretStr("postgresql+asyncpg://ases:ases@localhost:5432/ases")
    database_pool_size: int = Field(default=10, ge=1)
    database_max_overflow: int = Field(default=20, ge=0)
    database_echo: bool = False

    # ---------- 对象存储（MinIO / S3）----------

    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = "ases-artifacts"
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_force_path_style: bool = True
    #: 没配 S3 时本地文件系统后端的根目录。相对路径按进程 CWD 解析。
    local_store_root: Path = Path("/tmp/ases-artifacts")

    # ---------- 模型 ----------

    #: 角色化 Prompt 用的模型名。
    #:
    #: **换模型是运维的事，不是改代码的事。** 这一栏在这儿，是因为
    #: design.md §3.5D 把"模型名、超时、并发数、日志级别"列为**运行时配置**
    #: —— 可以有安全的默认值，但不该写死在调用点上。写死了的话，换一次模型
    #: 要改六处 Prompt 集，而漏掉一处就意味着"只有一部分角色换了"。
    model_name: str = "claude-sonnet-4-6"
    #: 采样温度，0~2。
    #:
    #: **默认低。** 这套系统的输出要能被下游独立核验，而"发散"正是核验的
    #: 对立面 —— 一份每次跑出来不一样的结论，没法被拿去跟别的结论比。
    model_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    #: 单次输出的 token 上限。
    model_max_tokens: int = Field(default=4096, ge=1)

    # ---------- 观测 ----------

    otel_enabled: bool = False
    otel_endpoint: str = "http://localhost:4318"
    langfuse_enabled: bool = False
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None

    # ---------- Worker ----------
    #: worker 心跳文件路径。
    #:
    #: 容器编排靠它判断 worker 是否**还在正常循环**。进程活着不等于还在干活
    #: —— 一个死锁的 worker 同样是"运行中"。心跳文件每次循环刷新一次，
    #: 健康检查看它的新鲜度。
    worker_heartbeat_file: Path = Path("/tmp/ases-worker/heartbeat")

    # ---------- 执行器 ----------

    executor_default: str = "claude_code"
    claude_code_bin: str = "claude"
    claude_code_api_key: SecretStr | None = None
    codex_bin: str = "codex"

    # ---------- 沙箱 ----------

    sandbox_image: str = "ases/sandbox:latest"
    sandbox_network: str = "none"
    workspace_root: Path = Path("/tmp/ases-workspaces")
    workspace_lease_ttl_seconds: int = Field(default=3600, ge=60)

    # ---------- 外部配置（治理类）路径 ----------

    governance_dir: Path = DEFAULT_GOVERNANCE_DIR
    policy_file: Path | None = None
    acceptance_rules_file: Path | None = None
    budget_file: Path | None = None

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value

    @model_validator(mode="after")
    def _check_environment_specific_requirements(self) -> Settings:
        """非本地环境必须有真实凭证，且禁止使用默认 MinIO 密钥。

        这条校验的意义：本地开发的 ``minioadmin`` 绝不能被带到生产。
        """
        if self.environment is Environment.LOCAL:
            return self

        if self.s3_access_key is not None and self.s3_secret_key is not None:
            default_pair = ("minioadmin", "minioadmin")
            actual = (
                self.s3_access_key.get_secret_value(),
                self.s3_secret_key.get_secret_value(),
            )
            if actual == default_pair:
                raise ValueError(
                    f"环境 {self.environment} 禁止使用默认 MinIO 凭证 minioadmin/minioadmin"
                )

        if self.environment is Environment.PRODUCTION:
            if self.langfuse_enabled and (
                self.langfuse_secret_key is None or self.langfuse_public_key is None
            ):
                raise ValueError(
                    "生产环境启用 Langfuse 时必须提供 public_key 与 secret_key"
                    "（ASES_LANGFUSE_PUBLIC_KEY / ASES_LANGFUSE_SECRET_KEY）"
                )
            if not self.s3_secret_key:
                raise ValueError("生产环境必须提供对象存储密钥（ASES_S3_SECRET_KEY）")

        return self

    @property
    def is_local(self) -> bool:
        return self.environment is Environment.LOCAL

    @property
    def database_dsn(self) -> str:
        """数据库连接串明文。

        单开一个属性，而不是让每个调用点自己写 ``get_secret_value()``：
        需要明文的地方只有两处（建引擎、跑迁移），但"这里为什么需要明文"
        写在一个地方比散落在各处清楚。返回值不要进日志。
        """
        return self.database_url.get_secret_value()


def load_settings(**overrides: Any) -> Settings:
    """构造 :class:`Settings`。

    进程启动时调用一次。若缺少必需环境变量，构造过程直接抛错 —— 宁可启动失败，
    也不要在缺少凭证的情况下带着不确定行为运行（design.md §3.5D）。
    """
    return Settings(**overrides)


@cache
def get_settings() -> Settings:
    """返回进程级单例配置。

    用 ``functools.cache`` 而非手写全局变量：缓存语义交给标准库，
    单元测试需要重置时调用 ``get_settings.cache_clear()``。
    """
    return load_settings()
