"""已知缺陷的复现用例 —— **每条缺陷对应一个会失败的用例**。

这个目录**不在** ``pyproject.toml`` 的 ``testpaths`` 里（那里只有
``tests``）。理由是两条：

1. **仓库自己的测试套件要保持绿。** 这些用例是**故意失败**的 —— 它们复现
   的是还没修的缺陷。跑在默认套件里，仓库看上去就是坏的。
2. **已发布的覆盖率基线不能被它弄脏。** ``docs/evaluation/baselines.yaml``
   里那份基线是**试点仓库自己的测试套件**跑出来的；把会失败的用例混进去，
   基线测的就不是同一个东西了。

要单独跑这些用例：

    python -m pytest known_issues

**``xfail(strict=True)`` 是有意的。** 缺陷被修好之后，这些用例会变成
"意外通过"（XPASS），而 strict 模式把 XPASS 算作**失败** —— 那正是我们要
的：**缺陷修好了，就把对应的 ``xfail`` 标记去掉**，让这条用例回到
``tests/`` 里成为常规回归。

标注与判定见 ``docs/issues.md``；机器可读的那一份在
``evaluation/labels/pilot_labels.jsonl``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tinylib import cli
from tinylib.config import load_config
from tinylib.money import Money
from tinylib.schedule import is_due
from tinylib.strings import word_wrap

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.xfail(strict=True, reason="PILOT-001：空输入时 CLI 抛未捕获的 ValueError")
def test_the_cli_reports_an_empty_stats_input_instead_of_crashing() -> None:
    """空输入应当是一句用法错误 + 退出码，不是 traceback。"""
    assert cli.main(["stats", ""]) == 2


@pytest.mark.xfail(strict=True, reason="PILOT-002：百分位越界时 CLI 抛未捕获的 ValueError")
def test_the_cli_reports_an_out_of_range_percentile_instead_of_crashing() -> None:
    """``--percentile 101`` 应当被入口翻译成用法错误。"""
    assert cli.main(["stats", "1,2", "--percentile", "101"]) == 2


@pytest.mark.xfail(strict=True, reason="PILOT-003：--slug 与 --limit 同时给出时 --limit 被静默忽略")
def test_the_cli_does_not_silently_drop_the_limit_option(capsys: pytest.CaptureFixture[str]) -> None:
    """不能装作没看见用户给的选项。

    期望：**要么**退出码非零（报用法错误），**要么**输出确实体现了长度上限
    （不超过 5 个字符）。现在既不报错也不截断。
    """
    exit_code = cli.main(["text", "Hello World", "--slug", "--limit", "5"])
    printed = capsys.readouterr().out.strip()
    assert exit_code != 0 or len(printed) <= 5


@pytest.mark.xfail(strict=True, reason="PILOT-004：main 遇非法参数抛 SystemExit，与'返回退出码'的契约不符")
def test_main_returns_an_exit_code_instead_of_raising() -> None:
    """``main`` 的契约是"返回进程退出码" —— 非法参数也该走同一条路。"""
    assert cli.main(["nosuchcommand"]) == 2


@pytest.mark.xfail(strict=True, reason="PILOT-005：Money 声称不可变，amount 却可以直接改")
def test_money_amount_cannot_be_reassigned() -> None:
    """金额被就地改掉是账本上的事 —— 不可变必须是拦得住的。"""
    money = Money(1, "USD")
    with pytest.raises(AttributeError):
        money.amount = Decimal("999")


@pytest.mark.xfail(strict=True, reason="PILOT-006：load_config 接受空键")
def test_load_config_rejects_an_empty_key(tmp_path) -> None:
    """空键应当在读的时候就报错。"""
    path = tmp_path / "app.conf"
    path.write_text("=value\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.xfail(strict=True, reason="PILOT-007：重复键静默取最后一个")
def test_load_config_reports_a_duplicated_key(tmp_path) -> None:
    """重复键要么报错，要么警告 —— 不能静默。"""
    path = tmp_path / "app.conf"
    path.write_text("a=1\na=2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.xfail(strict=True, reason="PILOT-008：超长单词不被硬折行，行宽超过 width")
def test_word_wrap_breaks_a_word_longer_than_the_width() -> None:
    """折行函数的用处就是把长行变短 —— 折不断就等于没折。"""
    lines = word_wrap("averyverylongword", 4)
    assert all(len(line) <= 4 for line in lines)


@pytest.mark.xfail(strict=True, reason="PILOT-009：非正间隔在 next_run 里报错、在 is_due 里却静默返回")
def test_is_due_rejects_a_non_positive_interval_the_same_way_next_run_does() -> None:
    """同一个非法输入，模块里的两个函数必须给同一种反应。"""
    with pytest.raises(ValueError):
        is_due(NOW, 0, NOW)
