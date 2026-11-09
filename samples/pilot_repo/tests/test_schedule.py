"""``tinylib.schedule`` 的测试。"""

from datetime import UTC, datetime, timedelta

from tinylib.schedule import due_tasks, is_due, next_run

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_next_run_adds_the_interval():
    assert next_run(60, NOW) == NOW + timedelta(seconds=60)


def test_next_run_rejects_a_non_positive_interval():
    try:
        next_run(0, NOW)
    except ValueError:
        return
    raise AssertionError("间隔非正应当报错")


def test_next_run_treats_a_naive_moment_as_utc():
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert next_run(60, naive) == NOW + timedelta(seconds=60)


def test_is_due_without_a_previous_run():
    assert is_due(None, 60, NOW) is True


def test_is_due_after_the_interval():
    assert is_due(NOW - timedelta(seconds=61), 60, NOW) is True


def test_is_not_due_before_the_interval():
    assert is_due(NOW - timedelta(seconds=59), 60, NOW) is False


def test_is_not_due_when_the_clock_went_back():
    assert is_due(NOW + timedelta(seconds=10), 60, NOW) is False


def test_due_tasks_picks_only_the_due_ones():
    tasks = [
        ("a", NOW - timedelta(seconds=61), 60),
        ("b", NOW - timedelta(seconds=10), 60),
        ("c", None, 60),
    ]
    assert due_tasks(tasks, NOW) == ["a", "c"]
