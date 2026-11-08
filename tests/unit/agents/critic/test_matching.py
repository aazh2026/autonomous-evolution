"""TASK-P1-055 —— 词面匹配。

非目标检查能不能站得住，全看这一段：**命中了哪一段文字**是报告里给出的
否决理由，复核的人要能照着它去看一眼对不对。所以这里盯三件事：

1. **剥掉否定前缀** —— "不做 X"里被禁的是 **X**，不是"做 X"；前缀没剥干净，
   匹配就会跑偏（"不做移动端适配"被当成"做移动端适配"去比）。
2. **命中长度下限** —— 两三个字的通用词几乎每条候选都会撞上，拿它们当否决
   理由就是用噪声否决真候选。
3. **命中要指得出位置** —— 返回的是**对上的那一段**，不是一个布尔值。
"""

from __future__ import annotations

from agents.critic.matching import (
    DEFAULT_MIN_COMMON_LENGTH,
    common_hit,
    forbidden_object,
    longest_common_substring,
    match_forbidden,
)

# ---------------------------------------------------------------------------
# 剥掉否定前缀
# ---------------------------------------------------------------------------


def test_the_prohibition_prefix_is_stripped() -> None:
    assert forbidden_object("不做移动端适配") == "移动端适配"


def test_a_longer_prefix_wins_over_a_shorter_one() -> None:
    """先匹"不做"再匹"不"，"不做移动端适配"不能被剥成"做移动端适配"。"""
    assert forbidden_object("不使用旧版接口") == "旧版接口"
    assert forbidden_object("不重构核心模块") == "核心模块"


def test_a_description_without_a_prefix_is_used_whole() -> None:
    """剥不掉前缀只是让匹配变宽，不会让该拦的漏掉。"""
    description = "本项目暂不引入新的第三方支付依赖"

    assert forbidden_object(description) == description


def test_a_prefix_with_nothing_after_it_keeps_the_original_text() -> None:
    assert forbidden_object("不做") == "不做"


# ---------------------------------------------------------------------------
# 公共子串
# ---------------------------------------------------------------------------


def test_the_longest_common_substring_is_found() -> None:
    assert longest_common_substring("移动端适配改造", "移动端适配") == "移动端适配"


def test_the_match_points_at_the_middle() -> None:
    assert longest_common_substring("做一次移动端适配改造", "移动端适配") == "移动端适配"


def test_two_unrelated_texts_share_nothing() -> None:
    assert longest_common_substring("登录超时", "移动端适配") == ""


def test_an_empty_text_shares_nothing() -> None:
    assert longest_common_substring("", "移动端适配") == ""
    assert longest_common_substring("移动端适配", "") == ""


# ---------------------------------------------------------------------------
# 命中长度下限
# ---------------------------------------------------------------------------


def test_a_short_common_run_is_not_a_hit() -> None:
    """两三个字的通用词不算命中 —— 否则每条候选都会被拦。"""
    assert common_hit("移动端改造", "移动端") is None


def test_a_four_character_run_is_a_hit() -> None:
    assert common_hit("移动端适配改造", "移动端适配") == "移动端适配"


def test_the_floor_is_four_characters() -> None:
    assert DEFAULT_MIN_COMMON_LENGTH == 4


def test_the_floor_can_be_overridden() -> None:
    assert common_hit("移动端改造", "移动端", min_length=3) == "移动端"


# ---------------------------------------------------------------------------
# 匹配候选与"不做什么"
# ---------------------------------------------------------------------------


def test_a_candidate_touching_a_prohibition_is_matched() -> None:
    assert match_forbidden("移动端适配改造", "不做移动端适配") == "移动端适配"


def test_an_unrelated_candidate_is_not_matched() -> None:
    assert match_forbidden("登录超时偶发失败", "不做移动端适配") is None


def test_a_candidate_touching_a_short_prohibition_is_not_matched() -> None:
    """被禁的东西太泛（"移动端"三个字）时不据此否决 —— 宁可漏也不错。"""
    assert match_forbidden("移动端改造", "不做移动端") is None


def test_an_empty_prohibition_matches_nothing() -> None:
    assert match_forbidden("移动端适配改造", "   ") is None
