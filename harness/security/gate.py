"""安全门 —— 三项 ``forbidden`` 检查器。

设计依据：docs/design.md §3.5B（``forbidden``：diff 里有密钥、依赖被擅改、
公共接口被破坏 —— **一票否决**）、§3.5D（默认禁止访问生产环境、网络白名单）；
docs/tasks.md TASK-P1-043、R-048 / R-054 / R-060。

**这个模块只对它真的读过的东西下结论。** 它读的是一份 diff 与若干份清单
正文，于是它的结论只覆盖这些输入 —— 读不到的部分只有两种归宿：

- **违规**：依赖清单读不出来（无法解析）→ 按未授权处理。宁可误报一次，
  也不要"清单看不懂所以放过"。
- **没结论**：公共接口面没配置 → 记进 :attr:`GateReport.unevaluated`，
  由调用方决定怎么办。**不产出结论不等于通过**。

其余的规则都给出了结论（"查过了，没命中"），记在
:attr:`GateReport.evaluated` 里。**这两种"没出事"不能合并** ——
合并成一句"通过"，就等于把"没查"当成了"查过"，而这两件事的处置完全
不同：前者什么都不用做，后者得有人先补上配置。


**违规详情里不回显密钥本身。** 详情会进日志、进审计表、进看板 ——
把密钥抄一遍等于把它复制到第二个地方，那正是这条检查要防的事。
所以详情只给"哪一行、什么形状、多长"，不给内容。

三条检查各自的判据：

``secrets_in_diff``
    只看**新增行**。删掉一行密钥是好事；把它留在原地（或者改动它）才是问题
    —— 而"改动它"必然产生一行新增行，所以新增行已经覆盖了两种情况。
``unauthorized_dependency_change``
    变更后的依赖清单**声明的依赖**必须落在授权范围内。授权集合是外部配置
    （:attr:`SecurityPolicy.authorized_dependencies`），代码里没有隐式默认值。
``public_api_breaking_change``
    受保护模块里的公共符号，**被删掉**或**改了签名**就是破坏性变更。

**签名变化一律当成破坏性的。** 严格说"加一个带默认值的参数"是兼容的 ——
但要判断这件事得知道调用方怎么调的，而这个模块看不到调用方。所以按
"签名变了就报"处理：多报一次的代价是看一眼，漏报的代价是下游炸掉。

**依赖名按 PyPI 规范归一化后比较**（大小写不敏感、``_``/``.`` 视作 ``-``）。
不归一化的话，授权 ``requests`` 就能被 ``Requests``、``re_quests`` 绕过去 ——
那这道门就成了摆设。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from harness.verification.contract import ForbiddenRule

#: 凭据的形状 —— 按**形状**判定，与代码怎么写无关。
#:
#: 前七条是已知厂商的凭证格式；最后一条是"赋值形"的兜底：任何看起来像
#: 密钥被赋给了值的写法都算。
#:
#: 刻意**不**包含"看起来像密钥但其实是正常代码"的形状 —— 一条只会误报的
#: 规则比没有规则更糟：它会让人开始习惯无视告警。
DEFAULT_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # AWS access key id / 临时凭据。
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    # GitHub personal access token / app token。
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    # Slack token。
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    # 常见的"以 sk- 开头"的厂商密钥。
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    # PEM 私钥块。
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    # JWT（头部以 eyJ 开头）。
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    # 赋值形兜底：``api_key = "..."``、``password: "..."``。
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|credential)s?\b"
        r"\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"
    ),
)

#: 占位符。命中任一即视为"不是真凭据"。
#:
#: 与 ``tests/unit/infrastructure/test_no_hardcoded_secrets.py`` 的同名清单
#: **必须逐字一致**（那里有一条测试守着这件事）：两处一旦分叉，一边开始
#: 误报、另一边开始漏报 —— 而且没人会发现，因为两边都"看起来在工作"。
PLACEHOLDER_MARKERS: Final[tuple[str, ...]] = (
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

#: 锁文件。**不作为依赖声明处理**。
#:
#: 锁文件的变化是**声明文件变化的结果**，不是新的依赖声明 —— 对着锁文件
#: 数一遍，会把一次改动报成 N 次（一个依赖会带来几十行哈希）。
LOCKFILE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "composer.lock",
    }
)

#: 认得、但**解析不了**的依赖清单。
#:
#: 列出来是为了让它们**落到"无法解析 → 未授权"那条路上**，而不是安静地
#: 跳过。安静跳过等于"这些格式的依赖改动不受检查" —— 那是一个没人知道的
#: 洞，比误报严重得多。
UNSUPPORTED_MANIFEST_NAMES: Final[frozenset[str]] = frozenset(
    {
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Cargo.toml",
        "Gemfile",
        "composer.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "environment.yml",
    }
)

#: 默认受保护的顶层包 —— 它们的公共符号算公共接口。
#:
#: **空的**。公共接口面因项目而异：一刀切地认为"所有包都是公共接口"会
#: 到处误报，"没有包是公共接口"则等于关掉这项检查。所以这里不给默认值，
#: 由外部配置给出（:attr:`SecurityPolicy.protected_packages`）——
#: 没配置时这项检查会**记进 unevaluated**，而不是假装通过。
DEFAULT_PROTECTED_PACKAGES: Final[frozenset[str]] = frozenset()

#: 依赖名的合法构成（PEP 508 的名字部分）。
_REQUIREMENT_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

#: 一条要求串的形状：名字 + 可选的 extras + 可选的版本约束 + 可选的环境标记。
#:
#: 粗判（没有清单正文时）用它把"这确实是一条依赖声明"与"这只是一个带引号的
#: 字符串"分开 —— ``"key" = "value"`` 里引号里的内容不是要求串。
_REQUIREMENT_SHAPE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[^\]]+\])?"
    r"(?:\s*(?:===|[<>!~]=)\s*[^\s,]+)?"
    r"(?:\s*;[^,]+)?$"
)

#: 一个带引号的列表项，至少要有这么长 —— 短于它就只是一对引号。
_MIN_QUOTED_LENGTH: Final[int] = 2

#: 统一 diff 的 hunk 头。
_HUNK_HEADER: Final[re.Pattern[str]] = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? \+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@"
)

#: Python 定义行（函数与类）。用来提取"公共符号"与"签名"。
_DEFINITION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"^\s*(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>.*?)\)\s*(?:->[^:]*)?:\s*$"
    ),
    re.compile(r"^\s*class\s+(?P<name>[A-Za-z_]\w*)\s*(?:\((?P<params>.*?)\))?\s*:\s*$"),
)


@dataclass(frozen=True, slots=True)
class ChangedLine:
    """diff 里的一行改动。

    :attr:`number` 是该行在**改动后**文件里的行号（删除行则是改动前的）。
    """

    number: int
    text: str


@dataclass(frozen=True, slots=True)
class FileChange:
    """一个文件在 diff 里的改动。"""

    path: str
    added: tuple[ChangedLine, ...] = ()
    removed: tuple[ChangedLine, ...] = ()
    #: 整个文件被删除。删除文件里的公共符号也算破坏性变更。
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class Violation:
    """一处命中。

    :attr:`detail` **不得包含密钥原文** —— 详情会进日志与审计表。
    """

    rule: ForbiddenRule
    path: str
    detail: str


@dataclass(frozen=True, slots=True)
class GateReport:
    """一次安全门检查的结论。

    三个字段把"查了"这件事拆成三种互不相同的状态：

    :attr:`violations`
        命中了。**一票否决**。
    :attr:`evaluated`
        查过了，没命中。
    :attr:`unevaluated`
        **没有结论** —— 这条规则这次没能给出判定。它与"查了、没命中"是
        两回事：前者需要有人来决定怎么办，后者什么都不用做。

    :attr:`passed` 只看 :attr:`violations` —— 所以它**不能单独用**。一次
    "没命中、但有一条规则没结论"的检查，:attr:`passed` 是 ``True``，
    :attr:`complete` 是 ``False``。把两者合成一个布尔量，就一定会有人
    在其中一种情况下按"安全"处理。
    """

    violations: tuple[Violation, ...] = ()
    unevaluated: frozenset[ForbiddenRule] = frozenset()
    evaluated: frozenset[ForbiddenRule] = frozenset()

    @property
    def passed(self) -> bool:
        """没有命中违规。**"没有结论"不算命中，也不算通过。**"""
        return not self.violations

    @property
    def complete(self) -> bool:
        """每一条规则都给出了结论吗。

        **"没命中"与"查过了"不是一回事** —— 这个属性守的就是那条缝。
        """
        return not self.unevaluated

    def describe(self) -> str:
        """给人看的摘要。"""
        if not self.violations:
            summary = "安全门：未命中"
        else:
            lines = [f"安全门：命中 {len(self.violations)} 处"]
            lines.extend(
                f"  - [{item.rule.value}] {item.path}：{item.detail}" for item in self.violations
            )
            summary = "\n".join(lines)
        if self.unevaluated:
            names = "、".join(sorted(rule.value for rule in self.unevaluated))
            summary = f"{summary}\n  - 未产出结论的规则：{names}"
        return summary


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    """安全门的策略。

    全部来自外部配置 —— 代码里没有隐式默认值。理由同
    :mod:`core.policy.autonomy`：**授权规则写在代码里，就等于没有授权基线**
    （没有人能对着代码 review 一条"这个依赖为什么被允许"）。

    :param authorized_dependencies: 允许出现在依赖清单里的依赖名（已归一化）。
    :param protected_packages: 受保护的顶层包 —— 它们的公共符号算公共接口。
    :param protected_symbols: 逐个指定的公共符号（``模块.符号``）。
    :param secret_patterns: 凭据的形状。
    """

    authorized_dependencies: frozenset[str] = frozenset()
    protected_packages: frozenset[str] = DEFAULT_PROTECTED_PACKAGES
    protected_symbols: frozenset[str] = frozenset()
    secret_patterns: tuple[re.Pattern[str], ...] = DEFAULT_SECRET_PATTERNS

    def protects_module(self, module: str) -> bool:
        """这个模块里有没有受保护的公共接口。"""
        if f"{module}." in {f"{item}." for item in self.protected_symbols}:
            return True
        top = module.split(".", 1)[0]
        return top in self.protected_packages

    def protects_symbol(self, module: str, name: str) -> bool:
        """这个符号是不是受保护的公共接口。"""
        if f"{module}.{name}" in self.protected_symbols:
            return True
        if name.startswith("_"):
            # 下划线开头的是私有符号，不属于公共接口。
            return False
        return module.split(".", 1)[0] in self.protected_packages


@dataclass(frozen=True, slots=True)
class SecurityGate:
    """安全门。

    :param policy: 策略。缺省时用一份"什么都不授权"的策略 —— 也就是默认
        拒绝一切新增依赖、不保护任何公共接口。这是 fail-closed 的默认值。
    """

    policy: SecurityPolicy = field(default_factory=SecurityPolicy)

    def check(
        self,
        diff: str,
        *,
        files: Mapping[str, str] | None = None,
        baseline_files: Mapping[str, str] | None = None,
    ) -> GateReport:
        """检查一份 diff。

        :param diff: 统一 diff 文本。
        :param files: 被改动文件的**改动后**正文（路径 → 内容）。依赖清单
            需要它才能被正确解析；没有它就退化成"只认要求串"的粗判。
        :param baseline_files: 被改动文件的**改动前**正文。给了它就只检查
            **新增**的依赖；不给则把清单里**全部**声明的依赖都当作待检查项
            —— 后者更严，但前提是授权集合里本来就包含项目已有的依赖。
        """
        changes = parse_unified_diff(diff)
        violations: list[Violation] = []
        violations.extend(self._check_secrets(changes))
        violations.extend(self._check_dependencies(changes, files, baseline_files))
        violations.extend(self._check_public_api(changes))

        unevaluated: set[ForbiddenRule] = set()
        if not self.policy.protected_packages and not self.policy.protected_symbols:
            unevaluated.add(ForbiddenRule.PUBLIC_API_BREAKING_CHANGE)

        return GateReport(
            violations=tuple(violations),
            unevaluated=frozenset(unevaluated),
            evaluated=frozenset(ForbiddenRule) - unevaluated,
        )

    # ---------- 密钥 ----------

    def _check_secrets(self, changes: Sequence[FileChange]) -> list[Violation]:
        """新增行里的凭据。"""
        violations: list[Violation] = []
        for change in changes:
            for line in change.added:
                for pattern in self.policy.secret_patterns:
                    match = pattern.search(line.text)
                    if match is None:
                        continue
                    if _is_placeholder(match.group(0)):
                        continue
                    violations.append(
                        Violation(
                            rule=ForbiddenRule.SECRETS_IN_DIFF,
                            path=change.path,
                            detail=f"第 {line.number} 行命中凭据形状（{_redact(match.group(0))}）",
                        )
                    )
                    break
        return violations

    # ---------- 依赖 ----------

    def _check_dependencies(
        self,
        changes: Sequence[FileChange],
        files: Mapping[str, str] | None,
        baseline_files: Mapping[str, str] | None,
    ) -> list[Violation]:
        """依赖清单变更。"""
        violations: list[Violation] = []
        authorized = {normalize_dependency(name) for name in self.policy.authorized_dependencies}
        for change in changes:
            kind = _manifest_kind(change.path)
            if kind is None or kind == "lock":
                # 锁文件不是依赖**声明** —— 它是声明的结果。数它一遍，一次
                # 依赖变更会被报成几十条（一个依赖带来几十行哈希）。
                continue
            content = files.get(change.path) if files is not None else None
            declared = _declared_dependencies(kind, content, change)
            if declared is None:
                violations.append(
                    Violation(
                        rule=ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE,
                        path=change.path,
                        detail="依赖清单无法解析 —— 按未授权处理",
                    )
                )
                continue
            baseline = None
            if baseline_files is not None and change.path in baseline_files:
                baseline = _declared_dependencies(kind, baseline_files[change.path], change)
            candidates = declared if baseline is None else declared - baseline
            unauthorized = sorted(
                name for name in candidates if normalize_dependency(name) not in authorized
            )
            if unauthorized:
                violations.append(
                    Violation(
                        rule=ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE,
                        path=change.path,
                        detail="未授权的依赖：" + "、".join(unauthorized),
                    )
                )
        return violations

    # ---------- 公共接口 ----------

    def _check_public_api(self, changes: Sequence[FileChange]) -> list[Violation]:
        """公共符号被删掉或改了签名。"""
        violations: list[Violation] = []
        for change in changes:
            module = _module_of(change.path)
            if module is None:
                continue
            if not self.policy.protects_module(module):
                continue
            removed = _definitions(change.removed)
            added = _definitions(change.added)
            for name, signature in removed.items():
                if not self.policy.protects_symbol(module, name):
                    continue
                new_signature = added.get(name)
                if new_signature is None:
                    violations.append(
                        Violation(
                            rule=ForbiddenRule.PUBLIC_API_BREAKING_CHANGE,
                            path=change.path,
                            detail=f"公共接口 {module}.{name} 被删除",
                        )
                    )
                elif _normalize_signature(new_signature) != _normalize_signature(signature):
                    violations.append(
                        Violation(
                            rule=ForbiddenRule.PUBLIC_API_BREAKING_CHANGE,
                            path=change.path,
                            detail=f"公共接口 {module}.{name} 的签名变了",
                        )
                    )
        return violations


# ---------- diff 解析 ----------


def parse_unified_diff(diff: str) -> tuple[FileChange, ...]:
    """把统一 diff 拆成"每个文件改了哪些行"。

    只关心三件事：文件路径、新增行、删除行。行号取自 hunk 头 —— 报"哪一行"
    比报"哪一段文本"有用得多：人可以直接去看那一行。

    **删掉的文件**也算一个文件改动（``deleted=True``）：它的每一行都是删除
    行，于是里面的公共符号会被判成"被删除"。
    """
    changes: list[FileChange] = []
    path: str | None = None
    deleted = False
    added: list[ChangedLine] = []
    removed: list[ChangedLine] = []
    old_no = 0
    new_no = 0

    def flush() -> None:
        nonlocal path, deleted, added, removed
        if path is not None:
            changes.append(
                FileChange(
                    path=path,
                    added=tuple(added),
                    removed=tuple(removed),
                    deleted=deleted,
                )
            )
        path = None
        deleted = False
        added = []
        removed = []

    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            flush()
            continue
        if raw.startswith("--- "):
            target = raw[4:].strip()
            if target != "/dev/null" and path is None:
                path = _strip_diff_prefix(target)
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                deleted = True
            else:
                path = _strip_diff_prefix(target)
            continue
        header = _HUNK_HEADER.match(raw)
        if header is not None:
            old_no = int(header.group("old"))
            new_no = int(header.group("new"))
            continue
        if raw.startswith("+"):
            added.append(ChangedLine(new_no, raw[1:]))
            new_no += 1
            continue
        if raw.startswith("-"):
            removed.append(ChangedLine(old_no, raw[1:]))
            old_no += 1
            continue
        if raw.startswith(" "):
            old_no += 1
            new_no += 1
            continue
        # 其余行（index/mode/\ No newline…）与内容无关。
    flush()
    return tuple(changes)


def _strip_diff_prefix(target: str) -> str:
    """去掉 diff 路径的 ``a/`` / ``b/`` 前缀。"""
    for prefix in ("a/", "b/"):
        if target.startswith(prefix):
            return target[len(prefix) :]
    return target


# ---------- 密钥的小工具 ----------


def _is_placeholder(value: str) -> bool:
    """这段文本是不是占位符。"""
    lowered = value.strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _redact(value: str) -> str:
    """把一段疑似凭据缩成"形状 + 长度"，**不回显内容**。"""
    head = value[:4]
    return f"{head}…（共 {len(value)} 字符）"


# ---------- 依赖清单 ----------


#: 依赖清单的文件名 → 清单类型。
_MANIFEST_KINDS: Final[dict[str, str]] = {
    "pyproject.toml": "pyproject",
    "package.json": "package.json",
    "go.mod": "go.mod",
}

#: ``require x v1`` 这一行至少要有几个词才像个依赖声明（模块 + 版本）。
_GO_REQUIRE_MIN_PARTS: Final[int] = 2


def _manifest_kind(path: str) -> str | None:
    """这个路径是不是依赖清单。是的话返回清单类型，不是返回 ``None``。

    返回 ``"lock"`` 表示它是锁文件 —— 锁文件**不作为依赖声明**处理
    （见 :data:`LOCKFILE_NAMES`）。
    """
    name = Path(path).name
    if name in LOCKFILE_NAMES:
        return "lock"
    kind = _MANIFEST_KINDS.get(name)
    if kind is not None:
        return kind
    if name == "requirements.txt" or (name.startswith("requirements") and name.endswith(".txt")):
        return "requirements"
    if name in UNSUPPORTED_MANIFEST_NAMES:
        return "unsupported"
    return None


def _declared_dependencies(
    kind: str,
    content: str | None,
    change: FileChange,
) -> set[str] | None:
    """清单声明了哪些依赖。

    返回 ``None`` 表示**读不出来**（无法解析）—— 调用方要把它当成违规，
    不能当成"没声明任何依赖"。

    没有清单正文时（``content is None``）退化成"只认列表项"的粗判：只从
    **新增行**里找**整行就是一个带引号的要求串**的列表项
    （``"pkg>=1.0",``）。键值对（``name = "..."``）与 JSON 的
    ``"name": "value"`` 在没有正文时**认不出来** —— 无法区分它是依赖名还是
    项目自己的名字（``[project] name = "ases"``）。认不出来就不报，比误报好。
    """
    if kind == "unsupported":
        return None
    if content is None:
        return _requirement_names_from_lines(line.text for line in change.added)
    parser = _MANIFEST_PARSERS.get(kind)
    if parser is None:
        return None
    return parser(content)


def _pyproject_dependencies(content: str) -> set[str] | None:
    """``pyproject.toml`` 声明的依赖。

    **只认依赖声明处，不认 ``[project] name``** —— 那是项目自己的名字，
    不是依赖。认错的话，每改一次项目名都会被报成"引入了未授权依赖"。
    """
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return None

    names: set[str] = set()
    names |= _pep621_dependencies(data)
    names |= _dependency_group_dependencies(data)
    names |= _poetry_dependencies(data)
    return names


def _pep621_dependencies(data: Mapping[str, object]) -> set[str]:
    """PEP 621 的 ``[project] dependencies`` 与 ``optional-dependencies``。"""
    names: set[str] = set()
    project = data.get("project")
    if not isinstance(project, dict):
        return names
    for requirement in project.get("dependencies") or []:
        if isinstance(requirement, str):
            names.add(requirement_name(requirement))
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        for group in optional.values():
            for requirement in group or []:
                if isinstance(requirement, str):
                    names.add(requirement_name(requirement))
    return names


def _dependency_group_dependencies(data: Mapping[str, object]) -> set[str]:
    """PEP 735 的 ``[dependency-groups]``。"""
    names: set[str] = set()
    groups = data.get("dependency-groups")
    if not isinstance(groups, dict):
        return names
    for group in groups.values():
        for requirement in group or []:
            if isinstance(requirement, str):
                names.add(requirement_name(requirement))
    return names


def _poetry_dependencies(data: Mapping[str, object]) -> set[str]:
    """``[tool.poetry.dependencies]`` 及其分组。"""
    names: set[str] = set()
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return names
    poetry = tool.get("poetry")
    if not isinstance(poetry, dict):
        return names
    for key, value in (poetry.get("dependencies") or {}).items():
        if isinstance(value, str):
            names.add(requirement_name(key))
    for group in (poetry.get("group") or {}).values():
        if isinstance(group, dict):
            for key in group.get("dependencies") or {}:
                names.add(requirement_name(key))
    return names


def _package_json_dependencies(content: str) -> set[str] | None:
    """``package.json`` 声明的依赖。"""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    names: set[str] = set()
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = data.get(section)
        if isinstance(block, dict):
            names.update(str(key) for key in block)
    return names


def _go_mod_dependencies(content: str) -> set[str] | None:
    """``go.mod`` 声明的依赖。

    两种写法都要认：单行 ``require x v1.2.3`` 与块形式 ``require ( … )``。
    块形式里的行是"模块路径 + 版本"，取的是**第一个**词 —— 取第二个就取到
    版本号上去了。
    """
    names: set[str] = set()
    in_block = False
    for raw in content.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block:
            if line.startswith(")"):
                in_block = False
                continue
            parts = line.split()
            if parts:
                names.add(parts[0])
            continue
        if line.startswith("require "):
            parts = line.split()
            if len(parts) >= _GO_REQUIRE_MIN_PARTS:
                names.add(parts[1])
    return names


def _requirements_dependencies(content: str) -> set[str] | None:
    """``requirements.txt`` 声明的依赖。"""
    names: set[str] = set()
    for raw in content.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            # ``-r other.txt`` / ``-e .`` 之类是指令，不是依赖名。
            continue
        name = requirement_name(line)
        if name:
            names.add(name)
    return names


#: 清单类型 → 解析函数。
#:
#: 放在解析函数**之后**定义：函数体在调用时才查这张表，所以定义顺序
#: 不影响使用，但读代码时"表 → 函数"的顺序更顺。
_MANIFEST_PARSERS: Final[dict[str, Callable[[str], set[str] | None]]] = {
    "pyproject": _pyproject_dependencies,
    "package.json": _package_json_dependencies,
    "go.mod": _go_mod_dependencies,
    "requirements": _requirements_dependencies,
}


def _requirement_names_from_lines(lines: Iterable[str]) -> set[str]:
    """从文本行里认出要求串。

    **只认"整行就是一个带引号的列表项"。** 这是粗判里唯一能站得住的形状：

    - 依赖声明在清单里是列表项 —— ``"requests>=2.0",``；
    - 键值对 ``name = "ases"`` 认不准 —— 那可能是项目自己的名字；
    - JSON 的 ``"name": "value"`` 同样认不准 —— 那是键，不是列表项。

    放宽一点（"行里有引号就算"）会把上面两种误当成依赖，于是每改一次项目
    描述都会被报成"引入了未授权依赖"。误报多了，这条规则就没人看了。

    引号里的内容还必须**长得像一条要求串**（:data:`_REQUIREMENT_SHAPE`）——
    这一层把 ``"key" = "value"`` 这种带引号的键值对挡在外面。
    """
    names: set[str] = set()
    for raw in lines:
        text = raw.strip()
        if text.endswith(","):
            text = text[:-1].strip()
        if len(text) < _MIN_QUOTED_LENGTH or text[0] not in "\"'":
            continue
        quote = text[0]
        if not text.endswith(quote):
            continue
        inner = text[1:-1].strip()
        if not inner or _REQUIREMENT_SHAPE.match(inner) is None:
            continue
        name = requirement_name(inner)
        if name:
            names.add(name)
    return names


def requirement_name(requirement: str) -> str:
    """从一条要求串里取出依赖名。

    ``pkg[extra]>=1.0 ; python_version>"3.10"`` → ``pkg``。
    """
    text = requirement.split(";", 1)[0].split("#", 1)[0].strip()
    text = text.split("[", 1)[0].strip()
    match = _REQUIREMENT_NAME.match(text)
    return match.group(0) if match is not None else ""


def normalize_dependency(name: str) -> str:
    """依赖名的归一化（PEP 503）。

    大小写不敏感，``_`` 与 ``.`` 与 ``-`` 等价。不归一化的话，授权
    ``requests`` 就能被 ``Requests`` 绕过去。
    """
    return re.sub(r"[-_.]+", "-", name.strip().lower())


# ---------- 公共接口 ----------


def _module_of(path: str) -> str | None:
    """文件路径对应的模块名。

    ``core/task/outcome.py`` → ``core.task.outcome``；
    ``core/task/__init__.py`` → ``core.task``。不是 Python 文件返回 ``None``。
    """
    cleaned = _strip_diff_prefix(path)
    if not cleaned.endswith(".py"):
        return None
    without_suffix = cleaned[: -len(".py")]
    parts = [part for part in without_suffix.split("/") if part]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def _definitions(lines: Iterable[ChangedLine]) -> dict[str, str]:
    """从改动行里抽出"名字 → 签名"。

    只认**定义行**（``def`` / ``class``）。函数体改了不算签名变了 ——
    而函数体改动时定义行在 diff 里是**上下文行**，根本不会出现在这里。
    """
    found: dict[str, str] = {}
    for line in lines:
        for pattern in _DEFINITION_PATTERNS:
            match = pattern.match(line.text)
            if match is None:
                continue
            found[match.group("name")] = match.group("params") or ""
            break
    return found


def _normalize_signature(signature: str) -> str:
    """签名归一化 —— 只比"参数形状"，不比空白。"""
    return re.sub(r"\s+", " ", signature.strip())


# ---------- 命令行 ----------


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。

    退出码：``0`` = 没有命中；``1`` = 命中违规；``2`` = 用法/读取错误。

    **退出码 0 不等于"每一项都查过了"** —— 没有结论的规则会打到 stderr。
    """
    parser = argparse.ArgumentParser(
        prog="harness.security.gate",
        description="对一份 diff 做三项 forbidden 检查",
    )
    parser.add_argument("--diff-file", type=Path, help="统一 diff 的文件路径")
    parser.add_argument("--stdin", action="store_true", help="从标准输入读 diff")
    parser.add_argument("--workspace", type=Path, help="改动后工作区（用来读依赖清单正文）")
    parser.add_argument(
        "--baseline-workspace", type=Path, help="改动前工作区（用来只检查新增的依赖）"
    )
    parser.add_argument(
        "--authorized-dependency",
        action="append",
        default=[],
        metavar="NAME",
        help="允许出现在依赖清单里的依赖名（可重复）",
    )
    parser.add_argument(
        "--protected-package",
        action="append",
        default=[],
        metavar="NAME",
        help="受保护的顶层包（可重复）",
    )
    parser.add_argument(
        "--protected-symbol",
        action="append",
        default=[],
        metavar="MODULE.NAME",
        help="受保护的公共符号（可重复）",
    )
    args = parser.parse_args(argv)

    if args.stdin and args.diff_file is not None:
        print("不能同时用 --stdin 与 --diff-file", file=sys.stderr)
        return 2
    if not args.stdin and args.diff_file is None:
        print("必须给 --diff-file 或 --stdin", file=sys.stderr)
        return 2

    try:
        if args.stdin:
            diff = sys.stdin.read()
        else:
            assert args.diff_file is not None
            diff = args.diff_file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读不到 diff：{exc}", file=sys.stderr)
        return 2

    changes = parse_unified_diff(diff)
    files = _read_manifests(args.workspace, changes)
    baseline = _read_manifests(args.baseline_workspace, changes)

    policy = SecurityPolicy(
        authorized_dependencies=frozenset(args.authorized_dependency),
        protected_packages=frozenset(args.protected_package),
        protected_symbols=frozenset(args.protected_symbol),
    )
    report = SecurityGate(policy=policy).check(diff, files=files, baseline_files=baseline)

    print(report.describe())
    for rule in sorted(report.unevaluated, key=lambda item: item.value):
        print(f"注意：本次未对规则 {rule.value} 产出结论", file=sys.stderr)
    return 1 if report.violations else 0


def _read_manifests(workspace: Path | None, changes: Sequence[FileChange]) -> dict[str, str]:
    """读出被改动的依赖清单的正文。"""
    if workspace is None:
        return {}
    contents: dict[str, str] = {}
    for change in changes:
        kind = _manifest_kind(change.path)
        if kind is None or kind == "lock":
            continue
        candidate = workspace / change.path
        try:
            contents[change.path] = candidate.read_text(encoding="utf-8")
        except OSError:
            # 读不到就当没有正文 —— 依赖检查会退化成粗判，而不是崩掉。
            continue
    return contents


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_PROTECTED_PACKAGES",
    "DEFAULT_SECRET_PATTERNS",
    "LOCKFILE_NAMES",
    "PLACEHOLDER_MARKERS",
    "UNSUPPORTED_MANIFEST_NAMES",
    "ChangedLine",
    "FileChange",
    "GateReport",
    "SecurityGate",
    "SecurityPolicy",
    "Violation",
    "main",
    "normalize_dependency",
    "parse_unified_diff",
    "requirement_name",
]
