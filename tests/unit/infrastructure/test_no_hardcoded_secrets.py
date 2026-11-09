"""TASK-P1-002 ② —— 代码库里不能有硬编码密钥。

这是一个**门禁**：它跑在 CI 里，有人把真凭证提交上来时构建必须变红。

两条规则，覆盖两种不同的漏法：

- **形状规则**：文本里出现已知格式的凭证（``sk-...`` / ``AKIA...`` / PEM 私钥…）。
  与代码怎么写无关，所以扫**所有**文件 —— 文档、配置、测试都算。
- **命名规则**：字面量被赋给一个密钥风格的名字（``api_key = "..."``）。
  只扫生产代码：测试本来就要写假密钥才能验证脱敏逻辑。

一个**只会漏报不会误报**的扫描器比没有扫描器更糟 —— 它给人虚假的安全感。
所以本文件同时测试扫描器本身：喂给它一段确实含密钥的代码，它必须报出来。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 不扫描的目录。
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "htmlcov",
        "node_modules",
    }
)

#: 形状规则：已知厂商的凭证前缀。带长度下限，避免把 ``sk-`` 这种短片段算进来。
CREDENTIAL_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),  # JWT
)

#: 命名规则：变量名/关键字名以这些词**结尾**时，才认为它承载凭证。
#:
#: 用后缀而不是"包含"是刻意的：``TOKEN_USAGE``、``NO_LONG_TERM_SECRETS_IN_PROMPT``
#: 这些名字里也有 token/secret，但它们是**指标名和策略名**，不是凭证。
#: 真正的凭证字段长这样：``api_key``、``s3_secret_key``、``auth_token``。
SECRET_NAME_SUFFIXES: tuple[str, ...] = (
    "api_key",
    "apikey",
    "access_key",
    "secret_key",
    "client_secret",
    "private_key",
    "api_token",
    "access_token",
    "auth_token",
    "bearer_token",
    "session_token",
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "credentials",
)

#: 占位符。命中任一即视为"不是真凭证"。
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "changeme",
    "change-me",
    "change_me",
    "your-",
    "your_",
    "example",
    "placeholder",
    "todo",
    "fixme",
    "dummy",
    "fake",
    "notset",
    "unset",
    "redacted",
    "***",
    "xxxx",
    "minioadmin",
    "local-dev",
    "local_dev",
    "fake-key",
    "fake_key",
)

#: 已知是**故意写的假密钥**。每条都要说清为什么它安全。
#:
#: 允许清单按 (文件, 值) 精确匹配 —— 位置写错或值改了就失效，
#: 这样"顺手把真密钥加进白名单"会立刻被 review 看见。
ALLOWED_CANARIES: dict[tuple[str, str], str] = {
    (
        "tests/unit/infrastructure/test_logging.py",
        "sk-ant-super-secret-value",
    ): "脱敏测试的哨兵值 —— 该测试断言它不出现在日志里",
    (
        "tests/unit/infrastructure/test_no_hardcoded_secrets.py",
        "sk-ant-super-secret-value",
    ): "脱敏测试的哨兵值 —— 本文件的白名单本身也写着它",
    (
        "tests/unit/infrastructure/test_no_hardcoded_secrets.py",
        "sk-live-abcdefghijklmnop",
    ): "扫描器自检样本 —— 它必须长得像真密钥，才能证明扫描器有效",
    (
        "tests/unit/harness/security/test_gate.py",
        "AKIAQWERTYUIOPASDFGH",
    ): "安全门测试用的假 AWS 形状凭据 —— 该测试断言安全门能认出它",
    (
        "tests/unit/infrastructure/test_no_hardcoded_secrets.py",
        "AKIAQWERTYUIOPASDFGH",
    ): "上一条白名单里的值在本文件里也要出现 —— 位置写错或值改了就失效",
    (
        "tests/unit/evaluation/test_executor_benchmark_driver.py",
        "sk-cp-abcdefghijklmnopqrstuvwxyz012345",
    ): "脱敏测试的哨兵值 —— 该测试断言它被 redact 抹掉；形状规则只认白名单",
    (
        "tests/unit/evaluation/test_executor_benchmark_driver.py",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
    ): "JWT 脱敏测试的哨兵值 —— 该测试断言它被 redact 抹掉；形状规则只认白名单",
    (
        "tests/unit/infrastructure/test_no_hardcoded_secrets.py",
        "sk-cp-abcdefghijklmnopqrstuvwxyz012345",
    ): "上一条白名单里的值在本文件里也要出现 —— 位置写错或值改了就失效",
    (
        "tests/unit/infrastructure/test_no_hardcoded_secrets.py",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
    ): "上一条白名单里的值在本文件里也要出现 —— 位置写错或值改了就失效",
}


@dataclass(frozen=True)
class Finding:
    """一处疑似硬编码密钥。"""

    path: Path
    line: int
    kind: str
    detail: str

    def describe(self) -> str:
        try:
            relative = self.path.relative_to(REPO_ROOT)
        except ValueError:
            relative = self.path
        return f"{relative}:{self.line} [{self.kind}] {self.detail}"


def _is_placeholder(value: str) -> bool:
    """判断一个字符串是否只是占位符。"""
    lowered = value.strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _iter_files() -> list[Path]:
    """仓库里所有值得扫描的文本文件。"""
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if (
            path.suffix in {".py", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".json", ".env"}
            or path.suffix in {".md", ".sh"}
            or path.name == ".env.example"
        ):
            files.append(path)
    return sorted(files)


def _scan_shapes(path: Path, text: str) -> list[Finding]:
    """形状规则：整份文件里找已知格式的凭证。"""
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern in CREDENTIAL_SHAPES:
            for match in pattern.finditer(line):
                value = match.group(0)
                if (str(path.relative_to(REPO_ROOT)), value) in ALLOWED_CANARIES:
                    continue
                findings.append(Finding(path, lineno, "形状", f"疑似凭证字面量 {value[:12]}…"))
    return findings


def _scan_named_secrets(path: Path, source: str) -> list[Finding]:
    """命名规则：字面量被赋给密钥风格的名字。"""
    tree = ast.parse(source)
    findings: list[Finding] = []

    def check(name: str | None, node: ast.AST) -> None:
        if name is None:
            return
        lowered = name.lower()
        if not any(lowered.endswith(suffix) for suffix in SECRET_NAME_SUFFIXES):
            return
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            return
        if _is_placeholder(node.value):
            return
        findings.append(
            Finding(
                path,
                getattr(node, "lineno", 0),
                "命名",
                f"{name} = {node.value[:12]!r}…",
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    check(target.id, node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                check(node.target.id, node.value)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg is not None:
                    check(keyword.arg, keyword.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    check(key.value, value)

    return findings


@pytest.fixture(scope="module")
def findings() -> list[Finding]:
    """扫全仓库一遍，返回所有疑似硬编码密钥。"""
    found: list[Finding] = []
    for path in _iter_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        found.extend(_scan_shapes(path, text))
        if path.suffix == ".py" and "tests" not in path.relative_to(REPO_ROOT).parts:
            found.extend(_scan_named_secrets(path, text))
    return found


class TestScannerItselfWorks:
    """扫描器必须真的会报 —— 否则上面的门禁是空的。"""

    def test_it_flags_a_named_assignment(self) -> None:
        source = 'API_KEY = "sk-live-abcdefghijklmnop"\n'
        findings = _scan_named_secrets(Path("sample.py"), source)
        assert findings, "扫描器漏报了赋给 api_key 的字面量"

    def test_it_flags_a_keyword_argument(self) -> None:
        source = 'client = connect(password="hunter2hunter2")\n'
        findings = _scan_named_secrets(Path("sample.py"), source)
        assert findings, "扫描器漏报了 password= 关键字参数"

    def test_it_flags_a_dict_entry(self) -> None:
        source = 'creds = {"secret": "Zx9-quiet-otter-42"}\n'
        findings = _scan_named_secrets(Path("sample.py"), source)
        assert findings, "扫描器漏报了字典里的 secret 项"

    def test_it_ignores_placeholders(self) -> None:
        source = 'api_key = "your-api-key-here"\ntoken = ""\n'
        assert not _scan_named_secrets(Path("sample.py"), source)

    def test_it_ignores_non_string_values(self) -> None:
        source = "api_key = None\ntoken = 12345\n"

        assert not _scan_named_secrets(Path("sample.py"), source)


class TestNoHardcodedSecretsInRepo:
    """T1 ② —— 门禁本体。"""

    def test_repo_has_no_hardcoded_secrets(self, findings: list[Finding]) -> None:
        assert not findings, "发现疑似硬编码密钥：\n" + "\n".join(
            finding.describe() for finding in findings
        )

    def test_scanner_actually_scans_something(self) -> None:
        """防空转：扫描的文件数不能是 0，否则上面的断言恒真。"""
        files = _iter_files()
        assert len(files) > 20, f"只扫到 {len(files)} 个文件 —— 扫描范围写错了？"
        assert any(path.suffix == ".py" for path in files)

    def test_the_allowlist_is_not_a_dump(self) -> None:
        """白名单只允许"位置 + 值"精确匹配，且每条都要写明理由。"""
        for (path, value), reason in ALLOWED_CANARIES.items():
            assert reason.strip(), f"{path} 里的 {value} 没写放行理由"
            target = REPO_ROOT / path
            assert target.exists(), f"白名单指向的文件不存在：{path}"
            assert value in target.read_text(encoding="utf-8"), (
                f"白名单里的 {value} 已经不在 {path} 里了 —— 请清理白名单"
            )


class TestEnvExampleIsClean:
    """``.env.example`` 不能带真实值。"""

    def test_env_example_has_no_credential_shaped_values(self) -> None:
        path = REPO_ROOT / ".env.example"
        text = path.read_text(encoding="utf-8")
        findings = _scan_shapes(path, text)
        assert not findings, "\n".join(finding.describe() for finding in findings)
