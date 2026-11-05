"""TASK-P1-002 ① ③ —— 配置与密钥的单元断言。

两条硬约束：

1. **缺必需凭证时进程启动即失败**，且报错指名缺哪一项 ——
   "启动失败"如果只说"配置无效"，运维要自己猜哪个变量没给。
2. **密钥字段一律 ``SecretStr``**，``repr()`` / ``str()`` 不泄露明文。

还有一条容易被忽略的：**连接串是密钥字段**。DSN 里带着库口令，
用普通 ``str`` 承载就等于让 ``print(settings)`` 把口令打进日志。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from infrastructure.config.settings import (
    Environment,
    Settings,
    load_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 视作密钥的字段。连接串也算 —— 它带着库口令。
SECRET_FIELDS = (
    "database_url",
    "s3_access_key",
    "s3_secret_key",
    "langfuse_public_key",
    "langfuse_secret_key",
    "claude_code_api_key",
)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把构造配置时能读到的外部输入全部切断。

    两件事：清掉环境里的 ``ASES_*`` 变量；把工作目录切到一个空目录，
    这样 ``Settings`` 配置里的 ``env_file=".env"``（相对当前目录解析）
    找不到文件。

    否则本机 ``.env`` 或 shell 里残留的变量会悄悄改变断言结果 ——
    那类"在我机器上是绿的"正是这类测试最常见的失败方式。
    """
    for name in list(os.environ):
        if name.startswith("ASES_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def _settings(**overrides: Any) -> Settings:
    """构造一个只由显式实参决定的配置。

    实参类型用 ``Any``：这里刻意要传"某个字段不存在/为空"这类
    按字段名拼出来的实参，写死联合类型只会让测试读起来更难。
    """
    return Settings(**overrides)


class TestMissingSecretFailsFast:
    """T1 ① —— 缺必需凭证时启动即失败。"""

    def test_production_without_object_storage_secret_fails(self) -> None:
        with pytest.raises(ValidationError) as exc:
            _settings(environment=Environment.PRODUCTION, s3_secret_key=None)
        message = str(exc.value)
        assert "对象存储密钥" in message
        assert "ASES_S3_SECRET_KEY" in message, "报错必须指名是哪个环境变量没给"

    def test_production_without_langfuse_secret_fails_when_enabled(self) -> None:
        with pytest.raises(ValidationError) as exc:
            _settings(
                environment=Environment.PRODUCTION,
                s3_secret_key=SecretStr("a-real-looking-secret"),
                langfuse_enabled=True,
                langfuse_public_key=None,
                langfuse_secret_key=None,
            )
        message = str(exc.value)
        assert "ASES_LANGFUSE_PUBLIC_KEY" in message
        assert "ASES_LANGFUSE_SECRET_KEY" in message

    def test_local_environment_needs_no_secret(self) -> None:
        """本地开发不该被凭证卡住 —— 否则没人愿意起服务。"""
        cfg = _settings(environment=Environment.LOCAL)
        assert cfg.s3_secret_key is None
        assert cfg.claude_code_api_key is None

    def test_production_with_secrets_loads(self) -> None:
        cfg = _settings(
            environment=Environment.PRODUCTION,
            s3_secret_key=SecretStr("a-real-looking-secret"),
        )
        assert cfg.environment is Environment.PRODUCTION

    def test_load_settings_raises_the_same_error(self) -> None:
        """``load_settings`` 是进程启动路径 —— 它必须同样失败。"""
        with pytest.raises(ValidationError):
            load_settings(environment=Environment.PRODUCTION, s3_secret_key=None)


class TestDefaultCredentialsAreRejectedOutsideLocal:
    """默认 MinIO 口令绝不能离开本地。"""

    def test_non_local_environment_rejects_default_minio_pair(self) -> None:
        with pytest.raises(ValidationError) as exc:
            _settings(
                environment=Environment.STAGING,
                s3_access_key=SecretStr("minioadmin"),
                s3_secret_key=SecretStr("minioadmin"),
            )
        assert "minioadmin" in str(exc.value)

    def test_local_environment_allows_default_minio_pair(self) -> None:
        cfg = _settings(
            environment=Environment.LOCAL,
            s3_access_key=SecretStr("minioadmin"),
            s3_secret_key=SecretStr("minioadmin"),
        )
        assert cfg.s3_access_key is not None


class TestSecretFieldsAreSecretStr:
    """T1 ③ —— 密钥字段类型与脱敏。"""

    @pytest.mark.parametrize("field_name", SECRET_FIELDS)
    def test_field_is_secret_str(self, field_name: str) -> None:
        annotation = Settings.model_fields[field_name].annotation
        assert annotation is SecretStr or annotation == SecretStr | None, (
            f"{field_name} 必须是 SecretStr —— 普通 str 会被 repr/日志带出明文"
        )

    @pytest.mark.parametrize("field_name", SECRET_FIELDS)
    def test_repr_does_not_leak_the_value(self, field_name: str) -> None:
        marker = "leak-canary-9f3a"
        cfg = _settings(**{field_name: SecretStr(marker)})
        assert marker not in repr(cfg), f"{field_name} 的明文出现在 repr() 里"
        assert marker not in str(cfg), f"{field_name} 的明文出现在 str() 里"

    def test_repr_masks_the_database_password(self) -> None:
        """DSN 里的口令不能出现在 repr 里。"""
        cfg = _settings(
            database_url=SecretStr("postgresql+asyncpg://u:canary-pw@localhost:5432/ases")
        )
        assert "canary-pw" not in repr(cfg)
        assert "canary-pw" not in str(cfg)

    def test_database_dsn_returns_the_plaintext(self) -> None:
        """需要明文的地方（建引擎、跑迁移）仍能拿到 —— 脱敏不是让人没法用。"""
        dsn = "postgresql+asyncpg://u:pw@localhost:5432/ases"
        cfg = _settings(database_url=SecretStr(dsn))
        assert cfg.database_dsn == dsn

    def test_settings_are_frozen(self) -> None:
        """已加载的凭证不能在运行期被就地改写。"""
        cfg = _settings()
        with pytest.raises(ValidationError):
            cfg.s3_secret_key = SecretStr("swapped")


class TestGovernanceConfigIsExternal:
    """治理配置必须是外部文件，不是代码里的常量。"""

    @pytest.mark.parametrize(
        "field_name",
        ("policy_file", "acceptance_rules_file", "budget_file"),
    )
    def test_governance_fields_are_paths(self, field_name: str) -> None:
        annotation = Settings.model_fields[field_name].annotation
        assert annotation == Path | None, f"{field_name} 应指向外部文件，而不是内嵌内容"

    def test_governance_dir_defaults_outside_the_package(self) -> None:
        cfg = _settings()
        assert cfg.governance_dir.is_absolute()
        assert "infrastructure/config" not in str(cfg.governance_dir)


class TestEnvExampleHasNoRealValues:
    """``.env.example`` 是模板，不是密钥的 dumping ground。"""

    def test_secret_entries_are_empty(self) -> None:
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        secret_lines = [
            line
            for line in text.splitlines()
            if line.strip().startswith("ASES_")
            and any(marker in line.upper() for marker in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
        ]
        assert secret_lines, "``.env.example`` 里没有密钥条目 —— 是模板写漏了？"
        for line in secret_lines:
            name, _, value = line.partition("=")
            value = value.strip()
            # 本地开发用的 MinIO 默认口令是唯一的例外：它不是真凭证，
            # 且 ``Settings`` 在非本地环境会拒绝它。
            if name.strip() in {"ASES_S3_ACCESS_KEY", "ASES_S3_SECRET_KEY"}:
                assert value == "minioadmin", f"{name.strip()} 只允许填默认本地口令"
                continue
            assert value == "", f"{name.strip()} 不应有真实值，当前是 {value!r}"

    def test_env_file_is_gitignored(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        entries = {line.strip() for line in gitignore.splitlines()}
        assert ".env" in entries
