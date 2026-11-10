"""试点仓库的**修复清单** —— 长跑里每一轮改什么。

设计依据：docs/tasks.md TASK-P3-018（连续 10 周期长跑）、R-130（首个里程碑）；
试点仓库的缺陷台账 ``samples/pilot_repo/docs/issues.md``。

**为什么这份清单写在代码里，而不是让模型现场改。** 阶段一的执行器是本地
补丁器（见 :mod:`apps.director.m1_executor` 的理由）：链路上真正需要判断的地
方全是真的（Critic 审查、验收契约、合并闸门、独立验证），唯一被替换掉的是
"写代码"那一段。**长跑要量的是"系统能不能连续十轮自己走完"**，而不是"模型
能不能写出补丁" —— 把补丁换成模型，长跑测的就不是这件事了。

**九条复现型缺陷 + 一条复核型缺陷 = 十轮。** 前九条是台账里 ``repro`` 类型
的有效发现（每一条都有一条会失败的复现用例）；第十条是 ``review`` 类型的一条
有效发现 —— ``Money`` 的 ``is_zero`` / ``__hash__`` / 与非 ``Money`` 比较三条路
径一条用例都没有（台账 PILOT-011）。**为什么最后一条不是复现型**：``review``
类型的发现复现的是"缺一样东西"，不是"行为不对" —— 它没有会失败的用例可写。
它照样是一条有效发现，修它照样能改善达成度。

**每条修复都做三件事。** 改源码、把复现用例搬回 ``tests/`` 当常规回归、从
复现清单里删掉那一条。**三件都要做**：只改源码而不搬用例，缺陷"修好了"却没
有回归保护；只搬用例而不改源码，用例会永远红着。

**搬走用例之后要清理 import。** 复现清单里每删掉一条用例，它独占的那个
import 就没人用了 —— 而验证计划里的 lint 检查跑的是 ruff 的默认规则集
（``--select E4,E7,E9,F``），其中 ``F401`` 就是"未使用的 import"。**留着不用
的 import 会让这一轮的验收直接失败** —— 所以清理是修复的一部分，不是收尾的
装饰。每一轮的清理安排写在各自的补丁里。

**补丁用"整段替换"，原文不在就报错。** 见 :mod:`apps.director.patching`。
**每条补丁的原文都与试点仓库逐字节相同** —— 由
``tests/unit/director/test_pilot_fixes.py`` 逐条钉住（在试点仓库的一份副本上
真打一遍补丁、真跑一遍用例）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from apps.director.patching import Patch

#: 复现清单的路径 —— 相对仓库根。
KNOWN_ISSUES_PATH: Final[str] = "known_issues/test_known_issues.py"


@dataclass(frozen=True, slots=True)
class IssueFix:
    """一条缺陷的修复 —— 改什么、搬哪条用例、怎么判它好了。

    :param issue: 台账里的编号（``PILOT-001``）。
    :param title: **候选标题**。复现型缺陷的标题是**复现用例的函数名** ——
        Critic 的第一问靠词面匹配去测试报告里找失败用例，标题对不上就查不成。
        标题带上编号前缀反而匹配不上（那一问会退成"未核实"）。
    :param hypothesis: 这条候选的假设 —— 一句话。
    :param expected_outcome: 修好了应该是什么样。
    :param patches: 这一轮要打的补丁。**顺序有意义**：删用例要先于删 import。
    :param repro_test: 复现用例的函数名。**复核型缺陷没有复现用例**（空串）。
    :param evidence: 证据 —— ``(引用, 说明)``。**引用必须指得到真的东西**：
        Critic 的第二问会去仓库里核，核不到就是否决。
    """

    issue: str
    title: str
    hypothesis: str
    expected_outcome: tuple[str, ...]
    patches: tuple[Patch, ...]
    repro_test: str
    evidence: tuple[tuple[str, str], ...]


# --------------------------------------------------------------------------
# 复现清单里那几条用例的原文 —— 删它们要用
# --------------------------------------------------------------------------

_BLOCK_001 = '''\
@pytest.mark.xfail(strict=True, reason="PILOT-001：空输入时 CLI 抛未捕获的 ValueError")
def test_the_cli_reports_an_empty_stats_input_instead_of_crashing() -> None:
    """空输入应当是一句用法错误 + 退出码，不是 traceback。"""
    assert cli.main(["stats", ""]) == 2


'''

_BLOCK_002 = '''\
@pytest.mark.xfail(strict=True, reason="PILOT-002：百分位越界时 CLI 抛未捕获的 ValueError")
def test_the_cli_reports_an_out_of_range_percentile_instead_of_crashing() -> None:
    """``--percentile 101`` 应当被入口翻译成用法错误。"""
    assert cli.main(["stats", "1,2", "--percentile", "101"]) == 2


'''

#: **下面三段原文被拆成了相邻的两个字面量。** 那三行比本仓库的 100 列宽 ——
#: 而它们是要**逐字节复制进仓库**的源码，行长由被复制的那个文件决定，不由这里
#: 的规矩决定。相邻字面量在编译期拼接，内容一个字节都不变。
_BLOCK_003 = (
    """\
@pytest.mark.xfail(strict=True, reason="PILOT-003：--slug 与 --limit 同时给出时 --limit 被静默忽略")
def test_the_cli_does_not_silently_drop_the_limit_option(capsys: pytest."""
    '''CaptureFixture[str]) -> None:
    """不能装作没看见用户给的选项。

    期望：**要么**退出码非零（报用法错误），**要么**输出确实体现了长度上限
    （不超过 5 个字符）。现在既不报错也不截断。
    """
    exit_code = cli.main(["text", "Hello World", "--slug", "--limit", "5"])
    printed = capsys.readouterr().out.strip()
    assert exit_code != 0 or len(printed) <= 5


'''
)

_BLOCK_004 = (
    """\
@pytest.mark.xfail(strict=True, reason="PILOT-004：main 遇非法参数抛 SystemExit，"""
    '''与'返回退出码'的契约不符")
def test_main_returns_an_exit_code_instead_of_raising() -> None:
    """``main`` 的契约是"返回进程退出码" —— 非法参数也该走同一条路。"""
    assert cli.main(["nosuchcommand"]) == 2


'''
)

_BLOCK_005 = '''\
@pytest.mark.xfail(strict=True, reason="PILOT-005：Money 声称不可变，amount 却可以直接改")
def test_money_amount_cannot_be_reassigned() -> None:
    """金额被就地改掉是账本上的事 —— 不可变必须是拦得住的。"""
    money = Money(1, "USD")
    with pytest.raises(AttributeError):
        money.amount = Decimal("999")


'''

_BLOCK_006 = '''\
@pytest.mark.xfail(strict=True, reason="PILOT-006：load_config 接受空键")
def test_load_config_rejects_an_empty_key(tmp_path) -> None:
    """空键应当在读的时候就报错。"""
    path = tmp_path / "app.conf"
    path.write_text("=value\\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


'''

_BLOCK_007 = '''\
@pytest.mark.xfail(strict=True, reason="PILOT-007：重复键静默取最后一个")
def test_load_config_reports_a_duplicated_key(tmp_path) -> None:
    """重复键要么报错，要么警告 —— 不能静默。"""
    path = tmp_path / "app.conf"
    path.write_text("a=1\\na=2\\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


'''

_BLOCK_008 = '''\
@pytest.mark.xfail(strict=True, reason="PILOT-008：超长单词不被硬折行，行宽超过 width")
def test_word_wrap_breaks_a_word_longer_than_the_width() -> None:
    """折行函数的用处就是把长行变短 —— 折不断就等于没折。"""
    lines = word_wrap("averyverylongword", 4)
    assert all(len(line) <= 4 for line in lines)


'''

_BLOCK_009 = (
    """\
@pytest.mark.xfail(strict=True, reason="PILOT-009：非正间隔在 next_run 里报错、"""
    '''在 is_due 里却静默返回")
def test_is_due_rejects_a_non_positive_interval_the_same_way_next_run_does() -> None:
    """同一个非法输入，模块里的两个函数必须给同一种反应。"""
    with pytest.raises(ValueError):
        is_due(NOW, 0, NOW)
'''
)


def _drop_block(block: str) -> Patch:
    """造一条"把复现用例从清单里删掉"的补丁。

    **删的是"用例 + 它后面的两个空行"。** 留着那两个空行，删掉一条用例之后
    文件里就会多出一段空隙 —— 而后面那条用例的删除补丁靠的正是"用例之间隔
    两个空行"这个事实，间距乱了，后面的补丁就对不上。
    """
    return Patch(path=KNOWN_ISSUES_PATH, old=block, new="")


def _drop_last_block(block: str) -> Patch:
    """造一条"把清单里最后一条用例删掉"的补丁。

    **最后一条前面那两个空行一起删。** 不删的话，文件末尾会多出两个空行 ——
    而"末尾多出空行"这件事在下一轮会被当成"清单被改过"。
    """
    return Patch(path=KNOWN_ISSUES_PATH, old="\n\n" + block, new="")


def _drop_import(line: str) -> Patch:
    """造一条"删掉一个没人用的 import"的补丁。"""
    return Patch(path=KNOWN_ISSUES_PATH, old=line, new="")


def _add_test(path: str, body: str) -> Patch:
    """造一条"新建一个回归用例文件"的补丁。"""
    return Patch(path=path, old="", new=body)


# --------------------------------------------------------------------------
# 九条复现型缺陷
# --------------------------------------------------------------------------

_FIX_001 = IssueFix(
    issue="PILOT-001",
    title="test_the_cli_reports_an_empty_stats_input_instead_of_crashing",
    hypothesis="空输入应当是一句用法错误 + 退出码，不是 traceback",
    expected_outcome=("cli.main(['stats', '']) 返回 2，不再抛 ValueError",),
    repro_test="test_the_cli_reports_an_empty_stats_input_instead_of_crashing",
    evidence=(
        ("src/tinylib/cli.py", "stats 分支没有空输入守卫"),
        (KNOWN_ISSUES_PATH, "PILOT-001 的复现用例"),
    ),
    patches=(
        Patch(
            path="src/tinylib/cli.py",
            old="""    if args.command == "stats":
        values = _numbers(args.values)
        if args.percentile is not None:""",
            new="""    if args.command == "stats":
        values = _numbers(args.values)
        if not values:
            print("用法：tinylib stats <逗号分隔的数>", file=sys.stderr)
            return 2
        if args.percentile is not None:""",
        ),
        _drop_block(_BLOCK_001),
        _add_test(
            "tests/test_regression_001.py",
            '''"""CLI 入口的回归用例 —— PILOT-001 修好之后搬回常规回归。"""

from __future__ import annotations

from tinylib import cli


def test_the_cli_reports_an_empty_stats_input_instead_of_crashing() -> None:
    """空输入应当是一句用法错误 + 退出码，不是 traceback。"""
    assert cli.main(["stats", ""]) == 2
''',
        ),
    ),
)

_FIX_002 = IssueFix(
    issue="PILOT-002",
    title="test_the_cli_reports_an_out_of_range_percentile_instead_of_crashing",
    hypothesis="``--percentile 101`` 应当被入口翻译成用法错误",
    expected_outcome=("cli.main(['stats', '1,2', '--percentile', '101']) 返回 2",),
    repro_test="test_the_cli_reports_an_out_of_range_percentile_instead_of_crashing",
    evidence=(
        ("src/tinylib/cli.py", "百分位越界时入口没有把领域层的异常翻成用法错误"),
        (KNOWN_ISSUES_PATH, "PILOT-002 的复现用例"),
    ),
    patches=(
        Patch(
            path="src/tinylib/cli.py",
            old="""        if args.percentile is not None:
            print(percentile(values, args.percentile))""",
            new="""        if args.percentile is not None:
            if not 0 <= args.percentile <= 100:
                print("用法：tinylib stats --percentile 取 0 到 100 之间", file=sys.stderr)
                return 2
            print(percentile(values, args.percentile))""",
        ),
        _drop_block(_BLOCK_002),
        _add_test(
            "tests/test_regression_002.py",
            '''"""CLI 百分位的回归用例 —— PILOT-002 修好之后搬回常规回归。"""

from __future__ import annotations

from tinylib import cli


def test_the_cli_reports_an_out_of_range_percentile_instead_of_crashing() -> None:
    """``--percentile 101`` 应当被入口翻译成用法错误。"""
    assert cli.main(["stats", "1,2", "--percentile", "101"]) == 2
''',
        ),
    ),
)

_FIX_003 = IssueFix(
    issue="PILOT-003",
    title="test_the_cli_does_not_silently_drop_the_limit_option",
    hypothesis="不能装作没看见用户给的选项",
    expected_outcome=("--slug 与 --limit 同时给出时报用法错误并返回非零退出码",),
    repro_test="test_the_cli_does_not_silently_drop_the_limit_option",
    evidence=(
        ("src/tinylib/cli.py", "text 分支里 --limit 被 --slug 静默盖掉"),
        (KNOWN_ISSUES_PATH, "PILOT-003 的复现用例"),
    ),
    patches=(
        Patch(
            path="src/tinylib/cli.py",
            old="""    if args.command == "text":
        if args.slug:""",
            new="""    if args.command == "text":
        if args.slug and args.limit is not None:
            print("用法：--slug 与 --limit 不能同时给出", file=sys.stderr)
            return 2
        if args.slug:""",
        ),
        _drop_block(_BLOCK_003),
        _add_test(
            "tests/test_regression_003.py",
            '''"""CLI 文本子命令的回归用例 —— PILOT-003 修好之后搬回常规回归。"""

from __future__ import annotations

import pytest

from tinylib import cli


def test_the_cli_does_not_silently_drop_the_limit_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """不能装作没看见用户给的选项。"""
    exit_code = cli.main(["text", "Hello World", "--slug", "--limit", "5"])
    printed = capsys.readouterr().out.strip()
    assert exit_code != 0 or len(printed) <= 5
''',
        ),
    ),
)

_FIX_004 = IssueFix(
    issue="PILOT-004",
    title="test_main_returns_an_exit_code_instead_of_raising",
    hypothesis="main 的契约是返回进程退出码，非法参数也该走同一条路",
    expected_outcome=("cli.main(['nosuchcommand']) 返回 2，而不是抛 SystemExit",),
    repro_test="test_main_returns_an_exit_code_instead_of_raising",
    evidence=(
        ("src/tinylib/cli.py", "main 直接把 parse_args 的 SystemExit 抛给调用方"),
        (KNOWN_ISSUES_PATH, "PILOT-004 的复现用例"),
    ),
    patches=(
        Patch(
            path="src/tinylib/cli.py",
            old="""    parser = build_parser()
    args = parser.parse_args(argv)""",
            new="""    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2""",
        ),
        _drop_block(_BLOCK_004),
        _drop_import("from tinylib import cli\n"),
        _add_test(
            "tests/test_regression_004.py",
            '''"""CLI 退出码的回归用例 —— PILOT-004 修好之后搬回常规回归。"""

from __future__ import annotations

from tinylib import cli


def test_main_returns_an_exit_code_instead_of_raising() -> None:
    """``main`` 的契约是"返回进程退出码" —— 非法参数也该走同一条路。"""
    assert cli.main(["nosuchcommand"]) == 2
''',
        ),
    ),
)

_FIX_005 = IssueFix(
    issue="PILOT-005",
    title="test_money_amount_cannot_be_reassigned",
    hypothesis="金额被就地改掉是账本上的事，不可变必须是拦得住的",
    expected_outcome=("Money 的 amount 只能读不能写，赋值抛 AttributeError",),
    repro_test="test_money_amount_cannot_be_reassigned",
    evidence=(
        ("src/tinylib/money.py", "Money 用 __slots__ 存了可写的属性，声称的不可变拦不住"),
        (KNOWN_ISSUES_PATH, "PILOT-005 的复现用例"),
    ),
    patches=(
        Patch(
            path="src/tinylib/money.py",
            old="""    __slots__ = ("amount", "currency")

    def __init__(self, amount, currency):
        if not isinstance(amount, Decimal):
            amount = Decimal(str(amount))
        if not currency or not str(currency).strip():
            raise ValueError("币种不能是空白")
        self.amount = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        self.currency = str(currency).strip().upper()
""",
            new='''    __slots__ = ("_amount", "_currency")

    def __init__(self, amount, currency):
        if not isinstance(amount, Decimal):
            amount = Decimal(str(amount))
        if not currency or not str(currency).strip():
            raise ValueError("币种不能是空白")
        self._amount = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        self._currency = str(currency).strip().upper()

    @property
    def amount(self):
        """金额 —— 只读。"""
        return self._amount

    @property
    def currency(self):
        """币种 —— 只读。"""
        return self._currency
''',
        ),
        _drop_block(_BLOCK_005),
        _drop_import("from decimal import Decimal\n"),
        _drop_import("from tinylib.money import Money\n"),
        _add_test(
            "tests/test_regression_005.py",
            '''"""金额不可变的回归用例 —— PILOT-005 修好之后搬回常规回归。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tinylib.money import Money


def test_money_amount_cannot_be_reassigned() -> None:
    """金额被就地改掉是账本上的事 —— 不可变必须是拦得住的。"""
    money = Money(1, "USD")
    with pytest.raises(AttributeError):
        money.amount = Decimal("999")
''',
        ),
    ),
)

_FIX_006 = IssueFix(
    issue="PILOT-006",
    title="test_load_config_rejects_an_empty_key",
    hypothesis="空键应当在读的时候就报错",
    expected_outcome=("load_config 读到空键时抛 ValueError",),
    repro_test="test_load_config_rejects_an_empty_key",
    evidence=(
        ("src/tinylib/config.py", "load_config 直接把空键收进结果"),
        (KNOWN_ISSUES_PATH, "PILOT-006 的复现用例"),
    ),
    patches=(
        Patch(
            path="src/tinylib/config.py",
            old='        key, _, value = stripped.partition("=")\n',
            new=(
                '        key, _, value = stripped.partition("=")\n'
                "        if not key.strip():\n"
                '            raise ValueError(f"第 {number} 行的键是空白：{stripped!r}")\n'
            ),
        ),
        _drop_block(_BLOCK_006),
        _add_test(
            "tests/test_regression_006.py",
            '''"""配置空键的回归用例 —— PILOT-006 修好之后搬回常规回归。"""

from __future__ import annotations

import pytest

from tinylib.config import load_config


def test_load_config_rejects_an_empty_key(tmp_path) -> None:
    """空键应当在读的时候就报错。"""
    path = tmp_path / "app.conf"
    path.write_text("=value\\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)
''',
        ),
    ),
)

_FIX_007 = IssueFix(
    issue="PILOT-007",
    title="test_load_config_reports_a_duplicated_key",
    hypothesis="重复键要么报错，要么警告，不能静默",
    expected_outcome=("load_config 读到重复键时抛 ValueError",),
    repro_test="test_load_config_reports_a_duplicated_key",
    evidence=(
        ("src/tinylib/config.py", "load_config 重复键静默取最后一个"),
        (KNOWN_ISSUES_PATH, "PILOT-007 的复现用例"),
    ),
    patches=(
        Patch(
            path="src/tinylib/config.py",
            old="        result[key.strip()] = value.strip()\n",
            new=(
                "        if key.strip() in result:\n"
                '            raise ValueError(f"第 {number} 行的键重复：{key.strip()!r}")\n'
                "        result[key.strip()] = value.strip()\n"
            ),
        ),
        _drop_block(_BLOCK_007),
        _drop_import("from tinylib.config import load_config\n"),
        _add_test(
            "tests/test_regression_007.py",
            '''"""配置重复键的回归用例 —— PILOT-007 修好之后搬回常规回归。"""

from __future__ import annotations

import pytest

from tinylib.config import load_config


def test_load_config_reports_a_duplicated_key(tmp_path) -> None:
    """重复键要么报错，要么警告 —— 不能静默。"""
    path = tmp_path / "app.conf"
    path.write_text("a=1\\na=2\\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)
''',
        ),
    ),
)

_FIX_008 = IssueFix(
    issue="PILOT-008",
    title="test_word_wrap_breaks_a_word_longer_than_the_width",
    hypothesis="折行函数的用处就是把长行变短，折不断就等于没折",
    expected_outcome=("word_wrap 遇到超长单词时按宽度硬折，每一行都不超过 width",),
    repro_test="test_word_wrap_breaks_a_word_longer_than_the_width",
    evidence=(
        ("src/tinylib/strings.py", "word_wrap 遇到比 width 还长的单词时原样输出"),
        (KNOWN_ISSUES_PATH, "PILOT-008 的复现用例"),
    ),
    patches=(
        Patch(
            path="src/tinylib/strings.py",
            old="""    words = normalize(text).split(" ")
    lines = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current = current + " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
""",
            new="""    words = normalize(text).split(" ")
    lines = []
    current = ""
    for word in words:
        if len(word) > width:
            if current:
                lines.append(current)
                current = ""
            for start in range(0, len(word), width):
                lines.append(word[start : start + width])
            continue
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current = current + " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
""",
        ),
        _drop_block(_BLOCK_008),
        _drop_import("from tinylib.strings import word_wrap\n"),
        _add_test(
            "tests/test_regression_008.py",
            '''"""折行的回归用例 —— PILOT-008 修好之后搬回常规回归。"""

from __future__ import annotations

from tinylib.strings import word_wrap


def test_word_wrap_breaks_a_word_longer_than_the_width() -> None:
    """折行函数的用处就是把长行变短 —— 折不断就等于没折。"""
    lines = word_wrap("averyverylongword", 4)
    assert all(len(line) <= 4 for line in lines)
''',
        ),
    ),
)

_FIX_009 = IssueFix(
    issue="PILOT-009",
    title="test_is_due_rejects_a_non_positive_interval_the_same_way_next_run_does",
    hypothesis="同一个非法输入，模块里的两个函数必须给同一种反应",
    expected_outcome=("is_due 遇到非正间隔时抛 ValueError，与 next_run 一致",),
    repro_test="test_is_due_rejects_a_non_positive_interval_the_same_way_next_run_does",
    evidence=(
        ("src/tinylib/schedule.py", "is_due 对非正间隔静默返回 False，next_run 却报错"),
        (KNOWN_ISSUES_PATH, "PILOT-009 的复现用例"),
    ),
    patches=(
        Patch(
            path="src/tinylib/schedule.py",
            old='''def is_due(last_run, interval_seconds, now=None):
    """到点了没有。"""
    if last_run is None:
        return True''',
            new='''def is_due(last_run, interval_seconds, now=None):
    """到点了没有。"""
    if interval_seconds <= 0:
        raise ValueError(f"间隔必须是正数：{interval_seconds}")
    if last_run is None:
        return True''',
        ),
        Patch(
            path="src/tinylib/schedule.py",
            old="""    if interval_seconds <= 0:
        return False
    elapsed = (moment - last_run).total_seconds()""",
            new="    elapsed = (moment - last_run).total_seconds()",
        ),
        _drop_last_block(_BLOCK_009),
        _drop_import("from tinylib.schedule import is_due\n"),
        _drop_import("import pytest\n"),
        _add_test(
            "tests/test_regression_009.py",
            '''"""调度间隔的回归用例 —— PILOT-009 修好之后搬回常规回归。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tinylib.schedule import is_due

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_is_due_rejects_a_non_positive_interval_the_same_way_next_run_does() -> None:
    """同一个非法输入，模块里的两个函数必须给同一种反应。"""
    with pytest.raises(ValueError):
        is_due(NOW, 0, NOW)
''',
        ),
    ),
)

#: 第十轮 —— 台账里一条 ``review`` 类型的有效发现。
#:
#: **它没有复现用例。** ``review`` 类型的发现复现的是"缺一样东西"，不是"行为不对"
#: —— 缺用例这件事写不成一条会失败的用例。这条候选的"问题是否真实存在"那一问
#: 会是"未核实"（测试报告里没有失败用例能证明它），而**未核实不否决**。
_FIX_010 = IssueFix(
    issue="PILOT-011",
    title="PILOT-011：补上 Money 的 is_zero / __hash__ / 与非 Money 比较三条路径的用例",
    hypothesis="这三条路径原来一条用例都没有，走一遍才算有覆盖",
    expected_outcome=("Money 的 is_zero、__hash__ 与与非 Money 比较三条路径都有用例走到",),
    repro_test="",
    evidence=(
        ("src/tinylib/money.py", "is_zero / __hash__ / 与非 Money 比较三条路径没有用例"),
        ("tests/test_money.py", "现有用例只覆盖了量化、币种、加乘与合计"),
    ),
    patches=(
        _add_test(
            "tests/test_money_gap.py",
            '''"""PILOT-011 的补齐 —— Money 的三条路径原来一条用例都没有。

**为什么单独一个文件。** ``tests/test_money.py`` 覆盖的是这个模块的主干行为；
这三条路径是台账里单独记下来的缺口，单独一个文件让"补的是哪三条"一眼能
对上。
"""

from __future__ import annotations

from decimal import Decimal

from tinylib.money import Money


def test_is_zero_is_true_for_a_zero_amount() -> None:
    """金额为零时 is_zero 为真。"""
    assert Money(0, "USD").is_zero() is True


def test_is_zero_is_false_for_a_non_zero_amount() -> None:
    """金额不为零时 is_zero 为假。"""
    assert Money(1, "USD").is_zero() is False


def test_equal_money_has_the_same_hash() -> None:
    """金额与币种都相同的两笔钱，哈希相同。"""
    assert hash(Money(1, "USD")) == hash(Money(1, "USD"))


def test_money_is_not_equal_to_a_non_money() -> None:
    """与非 Money 比较时不相等。"""
    assert Money(1, "USD") != Decimal("1")
''',
        ),
    ),
)


#: 十条修复 —— 按台账里的次序。
#:
#: **次序有意义。** 复现型缺陷按编号推进：每一轮修的是"第一条仍然会失败的复
#: 用例"，而编号顺序就是台账顺序，所以轮次与编号一一对应。
FIXES: Final[tuple[IssueFix, ...]] = (
    _FIX_001,
    _FIX_002,
    _FIX_003,
    _FIX_004,
    _FIX_005,
    _FIX_006,
    _FIX_007,
    _FIX_008,
    _FIX_009,
    _FIX_010,
)

#: 按候选标题索引的修复表 —— 执行器靠它把任务落到补丁上。
#:
#: **键是候选标题，不是缺陷编号。** 执行器拿到的只有任务标题 —— 那是这一轮
#: 系统自己选中的候选的标题。用编号做键就得在标题里塞编号，而标题里塞了编号
#: 就与测试报告里的 node id 对不上，Critic 的第一问会退成"未核实"。
FIX_BY_TITLE: Final[dict[str, IssueFix]] = {item.title: item for item in FIXES}

#: 按缺陷编号索引的修复表。
FIX_BY_ISSUE: Final[dict[str, IssueFix]] = {item.issue: item for item in FIXES}


__all__ = [
    "FIXES",
    "FIX_BY_ISSUE",
    "FIX_BY_TITLE",
    "KNOWN_ISSUES_PATH",
    "IssueFix",
]
