"""TASK-P1-043 —— 安全门（``forbidden`` 检查）。

覆盖任务书的 T1：① diff 中注入测试密钥 → 检查命中；② 新增未授权依赖 →
检查命中；③ 删除/改签名公共接口 → 检查命中；④ 干净 diff → 三项均不命中。

**这一组测试守的是三条否定命题加一条肯定命题。** 否定命题（"不该命中时
不能命中"）没法靠"我们记得别写"来保证，只能靠构造上的不可能；肯定命题
（"该命中时必须命中"）则决定了这道门是不是摆设 —— 一道永远不会响的门，
和没有门在后果上是一样的。

**最要紧的是两组断言的分工**：

- *命中* —— 每一条禁止项在 diff 里埋一颗雷，门必须响。
- *不命中* —— 同一份 diff 里放干净的东西，门**必须不响**。这里最容易出
  假阳性，而假阳性多了，门就会被人加白名单绕过 —— 那等于没有门。

**还有一组断言守的是"不回显"。** 命中的详情里不能有密钥原文：详情会进
日志、进审计表、进看板，把密钥抄一遍等于把它复制到第二个地方 —— 那正是
这条检查要防的事本身。

**最后一组断言守的是"没结论 ≠ 通过"。** 公共接口面没配置时，这条规则
给不出判定；这时候 :attr:`GateReport.passed` 是 ``True`` 而
:attr:`GateReport.complete` 是 ``False``。两个布尔量必须分开看。
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

import pytest

from harness.security.gate import (
    PLACEHOLDER_MARKERS,
    GateReport,
    SecurityGate,
    SecurityPolicy,
    Violation,
    main,
    normalize_dependency,
    parse_unified_diff,
    requirement_name,
)
from harness.verification.contract import ForbiddenRule

REPO_ROOT = Path(__file__).resolve().parents[4]

#: 一枚**假的** AWS 形状凭据。
#:
#: 它出现在仓库里 —— 所以它必须在门禁扫描器的白名单里登记（见
#: ``tests/unit/infrastructure/test_no_hardcoded_secrets.py``）。它不是真的
#: 凭据，但也**不能靠占位符词绕过** —— "example" 在占位符清单里，用带
#: 这个词的样本，这份测试就变成"测的是占位符逻辑"而不是"测的是凭据识别"。
FAKE_AWS_KEY = "AKIAQWERTYUIOPASDFGH"


def _diff(
    path: str,
    *,
    added: Sequence[str] = (),
    removed: Sequence[str] = (),
    context: Sequence[str] = (),
) -> str:
    """拼一份统一 diff。

    参数里给的是**文件里的原文行**，不带 diff 前缀 —— 前缀由这里补。
    """
    lines = [
        f"diff --git a/{path} b/{path}",
        "index 1111111..2222222 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
    ]
    body: list[str] = []
    for line in context:
        body.append(f" {line}")
    for line in removed:
        body.append(f"-{line}")
    for line in added:
        body.append(f"+{line}")
    lines.append(f"@@ -1,{len(body) + 1} +1,{len(body) + 1} @@")
    lines.extend(body)
    return "\n".join(lines) + "\n"


def _gate() -> SecurityGate:
    """默认的策略：授权 ``requests``，保护 ``core`` 这个顶层包。"""
    policy = SecurityPolicy(
        authorized_dependencies=frozenset({"requests"}),
        protected_packages=frozenset({"core"}),
        protected_symbols=frozenset(),
    )
    return SecurityGate(policy=policy)


def _rules(report: GateReport) -> set[ForbiddenRule]:
    return {item.rule for item in report.violations}


class TestTheSecretScan:
    """T1 ① —— diff 里埋了密钥，门必须响。"""

    def test_a_credential_in_the_diff_is_flagged(self) -> None:
        diff = _diff("app/settings.py", added=[f'AWS_KEY = "{FAKE_AWS_KEY}"'])
        report = _gate().check(diff)
        assert ForbiddenRule.SECRETS_IN_DIFF in _rules(report)
        assert not report.passed

    def test_the_violation_does_not_echo_the_credential(self) -> None:
        """详情会进日志与审计表 —— 把密钥抄一遍等于把它复制到第二个地方。"""
        diff = _diff("app/settings.py", added=[f'AWS_KEY = "{FAKE_AWS_KEY}"'])
        report = _gate().check(diff)
        violation = next(
            item for item in report.violations if item.rule is ForbiddenRule.SECRETS_IN_DIFF
        )
        assert FAKE_AWS_KEY not in violation.detail
        assert "AKIA" in violation.detail
        assert str(len(FAKE_AWS_KEY)) in violation.detail

    def test_a_removed_credential_is_not_flagged(self) -> None:
        """删掉一行密钥是好事。改动它必然产生一行新增行，已被上面那条覆盖。"""
        diff = _diff("app/settings.py", removed=[f'AWS_KEY = "{FAKE_AWS_KEY}"'])
        report = _gate().check(diff)
        assert ForbiddenRule.SECRETS_IN_DIFF not in _rules(report)

    def test_a_placeholder_is_not_flagged(self) -> None:
        """占位符不是凭据 —— 否则每个 ``.env.example`` 都会是违规。"""
        diff = _diff("app/settings.py", added=['api_key = "changeme-not-a-real-key"'])
        report = _gate().check(diff)
        assert ForbiddenRule.SECRETS_IN_DIFF not in _rules(report)

    def test_the_assignment_shape_is_flagged(self) -> None:
        """不认识的厂商格式也要靠"赋值形"兜底 —— 否则换个厂商就漏了。"""
        diff = _diff("app/settings.py", added=['api_key = "Zx9-quiet-otter-42"'])
        report = _gate().check(diff)
        assert ForbiddenRule.SECRETS_IN_DIFF in _rules(report)

    def test_a_normal_line_is_not_flagged(self) -> None:
        """干净的 diff 不能响 —— 假阳性多了，门就会被人绕过。"""
        diff = _diff(
            "app/handlers.py",
            added=["def handle(request):", "    return {'ok': True}"],
        )
        report = _gate().check(diff)
        assert not report.violations


class TestTheDependencyScan:
    """T1 ② —— 依赖清单被擅改，门必须响。"""

    _BASE = '[project]\nname = "ases"\n'

    def _content(self, *requirements: str) -> str:
        """改动**之后**的 ``pyproject.toml`` 正文。"""
        listed = ", ".join(f'"{item}"' for item in requirements)
        return f"{self._BASE}dependencies = [{listed}]\n"

    def test_a_new_unauthorized_dependency_is_flagged(self) -> None:
        diff = _diff("pyproject.toml", added=['    "httpx>=0.27",'])
        report = _gate().check(
            diff,
            files={"pyproject.toml": self._content("requests>=2.0", "httpx>=0.27")},
        )
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE in _rules(report)

    def test_an_authorized_dependency_is_not_flagged(self) -> None:
        diff = _diff("pyproject.toml", added=['    "requests>=2.0",'])
        report = _gate().check(diff, files={"pyproject.toml": self._content("requests>=2.0")})
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE not in _rules(report)

    def test_dependency_names_are_normalized(self) -> None:
        """``Requests`` 与 ``requests`` 是同一个依赖 —— 不归一化就能被绕过。"""
        diff = _diff("pyproject.toml", added=['    "Requests>=2.0",'])
        report = _gate().check(diff, files={"pyproject.toml": self._content("Requests>=2.0")})
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE not in _rules(report)

    def test_a_different_package_with_a_similar_name_is_still_unauthorized(self) -> None:
        """归一化不等于"名字相近就算同一个"。"""
        diff = _diff("pyproject.toml", added=['    "re-quests>=2.0",'])
        report = _gate().check(diff, files={"pyproject.toml": self._content("re-quests>=2.0")})
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE in _rules(report)

    def test_only_dependencies_added_after_the_baseline_are_checked(self) -> None:
        """给了改动前的清单，就只查**新增**的那些。"""
        diff = _diff("pyproject.toml", added=['    "httpx>=0.27",'])
        report = _gate().check(
            diff,
            files={"pyproject.toml": self._content("requests>=2.0", "httpx>=0.27")},
            baseline_files={"pyproject.toml": self._content("requests>=2.0")},
        )
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE in _rules(report)

    def test_a_baseline_removes_the_preexisting_dependencies_from_the_blame(self) -> None:
        """改动前就有的依赖不算这次引入的。"""
        diff = _diff("pyproject.toml", added=['    "httpx>=0.27",'])
        policy = SecurityPolicy(
            authorized_dependencies=frozenset({"requests", "httpx"}),
            protected_packages=frozenset({"core"}),
        )
        report = SecurityGate(policy=policy).check(
            diff,
            files={"pyproject.toml": self._content("requests>=2.0", "httpx>=0.27")},
            baseline_files={"pyproject.toml": self._content("requests>=2.0")},
        )
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE not in _rules(report)

    def test_an_unparseable_manifest_is_treated_as_unauthorized(self) -> None:
        """读不出来的清单不能当成"没声明依赖"。"""
        diff = _diff("setup.py", added=["install_requires=['requests']"])
        report = _gate().check(diff)
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE in _rules(report)

    def test_a_malformed_manifest_is_treated_as_unauthorized(self) -> None:
        """正文存在但解析不了 —— 同样按未授权处理。"""
        diff = _diff("pyproject.toml", added=["[project"])
        report = _gate().check(diff, files={"pyproject.toml": "[project\n"})
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE in _rules(report)

    def test_a_lockfile_change_is_not_a_dependency_declaration(self) -> None:
        """锁文件是声明的**结果**，不是新的声明。"""
        diff = _diff("uv.lock", added=['name = "httpx"'])
        report = _gate().check(diff)
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE not in _rules(report)

    def test_a_lockfile_listing_is_not_a_dependency_declaration(self) -> None:
        """锁文件里也有一行一行的依赖 —— 但那不是**声明**。

        少了这条，一次依赖变更会在锁文件上再报一遍 —— 一个依赖带来几十行
        哈希，于是"改了一个依赖"变成"报了三十条未授权依赖"。
        """
        diff = _diff("uv.lock", added=['    "httpx>=0.27",'])
        report = _gate().check(diff)
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE not in _rules(report)

    def test_the_baseline_decides_whose_fault_a_dependency_is(self) -> None:
        """改动前就有的依赖不算这次引入的 —— 所以授权清单里可以没有它。

        这一条是"基线减法"与"授权清单"两条规则的**交叉点**：不做减法，
        授权清单就得列全项目历史上出现过的每一个依赖。
        """
        diff = _diff("pyproject.toml", added=['    "httpx>=0.27",'])
        policy = SecurityPolicy(
            authorized_dependencies=frozenset({"httpx"}),
            protected_packages=frozenset({"core"}),
        )
        report = SecurityGate(policy=policy).check(
            diff,
            files={"pyproject.toml": self._content("requests>=2.0", "httpx>=0.27")},
            baseline_files={"pyproject.toml": self._content("requests>=2.0")},
        )
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE not in _rules(report)

    def test_package_json_dependencies_are_checked(self) -> None:
        diff = _diff("package.json", added=['    "lodash": "^4.17.21"'])
        content = '{"dependencies": {"lodash": "^4.17.21"}}'
        report = _gate().check(diff, files={"package.json": content})
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE in _rules(report)

    def test_go_mod_dependencies_are_checked(self) -> None:
        """两种写法都要认：单行 ``require x v1`` 与块形式 ``require ( … )``。"""
        content = (
            "module example.com/app\n\ngo 1.22\n\n"
            "require github.com/stretchr/testify v1.9.0\n\n"
            "require (\n\tgithub.com/pkg/errors v0.9.1\n\tgolang.org/x/sys v0.15.0\n)\n"
        )
        diff = _diff("go.mod", added=["require github.com/stretchr/testify v1.9.0"])
        report = _gate().check(diff, files={"go.mod": content})
        violation = next(
            item
            for item in report.violations
            if item.rule is ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE
        )
        assert "github.com/stretchr/testify" in violation.detail
        # 块形式里取的是**第一个**词 —— 取第二个就取到版本号上去了。
        assert "github.com/pkg/errors" in violation.detail
        assert "v0.9.1" not in violation.detail

    def test_a_manifest_without_a_body_is_judged_coarsely(self) -> None:
        """没有正文时只看新增行里的**列表项**。"""
        diff = _diff("pyproject.toml", added=['    "httpx>=0.27",'])
        report = _gate().check(diff)
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE in _rules(report)

    def test_a_key_value_line_is_not_mistaken_for_a_dependency(self) -> None:
        """``name = "ases"`` 是项目自己的名字，不是依赖。"""
        diff = _diff("pyproject.toml", added=['name = "ases"'])
        report = _gate().check(diff)
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE not in _rules(report)

    def test_a_quoted_key_value_line_is_not_mistaken_for_a_dependency(self) -> None:
        """带引号的键（``"name" = "ases"``）同样不是依赖。"""
        diff = _diff("pyproject.toml", added=['"name" = "ases"'])
        report = _gate().check(diff)
        assert ForbiddenRule.UNAUTHORIZED_DEPENDENCY_CHANGE not in _rules(report)


class TestThePublicApiScan:
    """T1 ③ —— 公共接口被破坏，门必须响。"""

    def test_removing_a_public_symbol_is_flagged(self) -> None:
        diff = _diff("core/task/outcome.py", removed=["def build_outcome(self) -> Outcome:"])
        report = _gate().check(diff)
        assert ForbiddenRule.PUBLIC_API_BREAKING_CHANGE in _rules(report)

    def test_changing_a_public_signature_is_flagged(self) -> None:
        diff = _diff(
            "core/task/outcome.py",
            removed=["def build_outcome(self, strict: bool) -> Outcome:"],
            added=["def build_outcome(self, strict: bool, extra: int) -> Outcome:"],
        )
        report = _gate().check(diff)
        assert ForbiddenRule.PUBLIC_API_BREAKING_CHANGE in _rules(report)

    def test_an_unchanged_signature_is_not_flagged(self) -> None:
        """只改函数体不算破坏 —— 定义行在 diff 里是上下文行。"""
        diff = _diff(
            "core/task/outcome.py",
            context=["def build_outcome(self) -> Outcome:"],
            added=["    return Outcome()"],
        )
        report = _gate().check(diff)
        assert ForbiddenRule.PUBLIC_API_BREAKING_CHANGE not in _rules(report)

    def test_a_moved_symbol_with_the_same_signature_is_not_flagged(self) -> None:
        diff = _diff(
            "core/task/outcome.py",
            removed=["def build_outcome(self) -> Outcome:"],
            added=["def build_outcome(self) -> Outcome:"],
        )
        report = _gate().check(diff)
        assert ForbiddenRule.PUBLIC_API_BREAKING_CHANGE not in _rules(report)

    def test_private_symbols_are_not_public(self) -> None:
        """下划线开头的是私有符号，不属于公共接口。"""
        diff = _diff("core/task/outcome.py", removed=["def _helper(self) -> None:"])
        report = _gate().check(diff)
        assert ForbiddenRule.PUBLIC_API_BREAKING_CHANGE not in _rules(report)

    def test_an_unprotected_package_is_not_checked(self) -> None:
        diff = _diff("app/thing.py", removed=["def public_thing(self) -> None:"])
        report = _gate().check(diff)
        assert ForbiddenRule.PUBLIC_API_BREAKING_CHANGE not in _rules(report)

    def test_deleting_a_whole_module_removes_its_public_symbols(self) -> None:
        diff = "\n".join(
            [
                "diff --git a/core/legacy.py b/core/legacy.py",
                "deleted file mode 100644",
                "--- a/core/legacy.py",
                "+++ /dev/null",
                "@@ -1,2 +0,0 @@",
                "-def legacy_api() -> None:",
                "-    pass",
            ]
        )
        report = _gate().check(diff)
        assert ForbiddenRule.PUBLIC_API_BREAKING_CHANGE in _rules(report)


class TestTheReport:
    """T1 ④ —— 干净 diff 三项均不命中；且"没结论"必须看得出来。"""

    def test_a_clean_diff_triggers_nothing(self) -> None:
        diff = _diff(
            "app/handlers.py",
            added=["def handle(request):", "    return {'ok': True}"],
        )
        report = _gate().check(diff)
        assert not report.violations
        assert report.passed
        assert report.complete

    def test_the_public_api_rule_is_unevaluated_without_a_protected_surface(self) -> None:
        """公共接口面没配置 → 这条规则给不出判定。"""
        diff = _diff("core/task/outcome.py", removed=["def build_outcome(self) -> Outcome:"])
        report = SecurityGate().check(diff)
        assert ForbiddenRule.PUBLIC_API_BREAKING_CHANGE in report.unevaluated
        assert not report.complete

    def test_a_report_without_a_verdict_is_not_a_pass(self) -> None:
        """``passed`` 与 ``complete`` 必须分开 —— 合并了就一定会有人误用。"""
        diff = _diff("core/task/outcome.py", removed=["def build_outcome(self) -> Outcome:"])
        report = SecurityGate().check(diff)
        assert report.passed
        assert not report.complete

    def test_the_evaluated_rules_cover_everything_else(self) -> None:
        diff = _diff("app/handlers.py", added=["x = 1"])
        report = SecurityGate().check(diff)
        assert report.evaluated | report.unevaluated == frozenset(ForbiddenRule)
        assert not (report.evaluated & report.unevaluated)

    def test_describe_mentions_the_unevaluated_rules(self) -> None:
        diff = _diff("app/handlers.py", added=["x = 1"])
        text = SecurityGate().check(diff).describe()
        assert ForbiddenRule.PUBLIC_API_BREAKING_CHANGE.value in text


class TestTheCommandLine:
    """命令行入口 —— 退出码是唯一稳定的接口。"""

    def test_the_cli_exits_one_on_a_hit(self, tmp_path: Path) -> None:
        diff_file = tmp_path / "change.diff"
        diff_file.write_text(
            _diff("app/settings.py", added=[f'AWS_KEY = "{FAKE_AWS_KEY}"']), encoding="utf-8"
        )
        assert main(["--diff-file", str(diff_file)]) == 1

    def test_the_cli_exits_zero_on_a_clean_diff(self, tmp_path: Path) -> None:
        diff_file = tmp_path / "change.diff"
        diff_file.write_text(_diff("app/handlers.py", added=["x = 1"]), encoding="utf-8")
        assert main(["--diff-file", str(diff_file)]) == 0

    def test_the_cli_rejects_conflicting_arguments(self, tmp_path: Path) -> None:
        diff_file = tmp_path / "change.diff"
        diff_file.write_text("", encoding="utf-8")
        assert main(["--diff-file", str(diff_file), "--stdin"]) == 2

    def test_the_cli_requires_a_source(self) -> None:
        assert main([]) == 2

    def test_the_cli_reads_the_manifest_from_the_workspace(self, tmp_path: Path) -> None:
        """给了工作区就按清单正文判定 —— 不给就退化成粗判。"""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "ases"\ndependencies = ["requests>=2.0"]\n', encoding="utf-8"
        )
        diff_file = tmp_path / "change.diff"
        diff_file.write_text(
            _diff("pyproject.toml", added=['    "requests>=2.0",']), encoding="utf-8"
        )
        assert (
            main(
                [
                    "--diff-file",
                    str(diff_file),
                    "--workspace",
                    str(workspace),
                    "--authorized-dependency",
                    "requests",
                ]
            )
            == 0
        )

    def test_the_cli_flags_an_unauthorized_dependency(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "ases"\ndependencies = ["requests>=2.0", "httpx>=0.27"]\n',
            encoding="utf-8",
        )
        diff_file = tmp_path / "change.diff"
        diff_file.write_text(
            _diff("pyproject.toml", added=['    "httpx>=0.27",']), encoding="utf-8"
        )
        assert (
            main(
                [
                    "--diff-file",
                    str(diff_file),
                    "--workspace",
                    str(workspace),
                    "--authorized-dependency",
                    "requests",
                ]
            )
            == 1
        )


class TestTheDiffParser:
    """diff 解析 —— 报"哪一行"比报"哪一段文本"有用得多。"""

    def test_it_extracts_the_path_and_the_lines(self) -> None:
        changes = parse_unified_diff(_diff("app/a.py", added=["x = 1"], removed=["y = 2"]))
        assert len(changes) == 1
        change = changes[0]
        assert change.path == "app/a.py"
        assert [line.text for line in change.added] == ["x = 1"]
        assert [line.text for line in change.removed] == ["y = 2"]

    def test_it_numbers_the_lines(self) -> None:
        changes = parse_unified_diff(_diff("app/a.py", added=["x = 1"]))
        assert changes[0].added[0].number >= 1

    def test_it_marks_a_deleted_file(self) -> None:
        diff = "\n".join(
            [
                "diff --git a/app/a.py b/app/a.py",
                "deleted file mode 100644",
                "--- a/app/a.py",
                "+++ /dev/null",
                "@@ -1,1 +0,0 @@",
                "-x = 1",
            ]
        )
        changes = parse_unified_diff(diff)
        assert changes[0].deleted
        assert changes[0].path == "app/a.py"


class TestTheVocabulary:
    """归一化与取名 —— 两个小函数，但整套依赖判定都建立在它们上面。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("requests", "requests"),
            ("requests>=2.0", "requests"),
            ("requests[security]>=2.0", "requests"),
            ('requests>=2.0 ; python_version>"3.10"', "requests"),
            ("", ""),
        ],
    )
    def test_requirement_name(self, raw: str, expected: str) -> None:
        assert requirement_name(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("requests", "requests"),
            ("Requests", "requests"),
            ("re_quests", "re-quests"),
            ("my.pkg", "my-pkg"),
        ],
    )
    def test_normalize_dependency(self, raw: str, expected: str) -> None:
        assert normalize_dependency(raw) == expected


def _scanner_placeholder_markers(path: Path) -> tuple[str, ...]:
    """从门禁测试的源码里读出它的占位符清单。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if not isinstance(target, ast.Name) or target.id != "PLACEHOLDER_MARKERS":
            continue
        if node.value is None:
            continue
        value = ast.literal_eval(node.value)
        assert isinstance(value, tuple)
        return value
    raise AssertionError(f"{path} 里找不到 PLACEHOLDER_MARKERS")


class TestThePlaceholderListsStayInSync:
    """两份占位符清单必须逐字一致。

    分叉的后果是**一边开始误报、另一边开始漏报**，而两边都"看起来在工作"
    —— 所以这里用一条测试把这件事钉死，而不是靠注释提醒。
    """

    def test_the_gate_and_the_repo_scanner_agree(self) -> None:
        scanner = REPO_ROOT / "tests/unit/infrastructure/test_no_hardcoded_secrets.py"
        markers = _scanner_placeholder_markers(scanner)
        assert set(markers) == set(PLACEHOLDER_MARKERS)
        assert len(markers) == len(PLACEHOLDER_MARKERS)


def test_the_report_is_immutable() -> None:
    """报告是结论，不是可改的中间状态。"""
    report = GateReport(violations=(Violation(ForbiddenRule.SECRETS_IN_DIFF, "a.py", "x"),))
    with pytest.raises((AttributeError, TypeError)):
        report.violations = ()  # type: ignore[misc]
