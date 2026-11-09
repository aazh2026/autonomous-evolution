"""``tinylib.config`` 的测试。"""

from tinylib.config import load_config, merge, parse_bool


def test_load_reads_key_values(tmp_path):
    path = tmp_path / "app.conf"
    path.write_text("# 注释\nname=tinylib\nport = 8080 \n", encoding="utf-8")
    assert load_config(path) == {"name": "tinylib", "port": "8080"}


def test_load_skips_blank_lines(tmp_path):
    path = tmp_path / "app.conf"
    path.write_text("\n\nname=tinylib\n", encoding="utf-8")
    assert load_config(path) == {"name": "tinylib"}


def test_load_rejects_a_line_without_an_equals(tmp_path):
    path = tmp_path / "app.conf"
    path.write_text("name=tinylib\nbroken\n", encoding="utf-8")
    try:
        load_config(path)
    except ValueError:
        return
    raise AssertionError("不是 key=value 的行应当报错")


def test_merge_prefers_the_overlay():
    assert merge({"a": 1, "b": 2}, {"b": 3}) == {"a": 1, "b": 3}


def test_merge_does_not_touch_the_base():
    base = {"a": 1}
    merge(base, {"b": 2})
    assert base == {"a": 1}


def test_parse_bool_reads_yes():
    assert parse_bool("yes") is True


def test_parse_bool_reads_off():
    assert parse_bool("off") is False


def test_parse_bool_falls_back_to_the_default():
    assert parse_bool("maybe", default=True) is True
