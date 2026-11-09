"""``tinylib.strings`` 的测试。"""

from tinylib.strings import normalize, slugify, truncate, word_wrap


def test_normalize_collapses_whitespace():
    assert normalize("  a   b \n c ") == "a b c"


def test_normalize_keeps_single_spaces():
    assert normalize("a b") == "a b"


def test_slugify_lowercases_and_replaces():
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_drops_empty_parts():
    assert slugify("---a---b---") == "a-b"


def test_slugify_of_empty_is_empty():
    assert slugify("   ") == ""


def test_truncate_keeps_short_text():
    assert truncate("hello", 10) == "hello"


def test_truncate_keeps_text_at_the_limit():
    assert truncate("hello", 100) == "hello"


def test_truncate_rejects_a_non_positive_limit():
    try:
        truncate("hello", 0)
    except ValueError:
        return
    raise AssertionError("长度上限为 0 应当报错")


def test_word_wrap_splits_on_width():
    assert word_wrap("aaa bbb ccc", 7) == ["aaa bbb", "ccc"]


def test_word_wrap_keeps_short_text_on_one_line():
    assert word_wrap("aaa", 7) == ["aaa"]
