"""``tinylib.stats`` 的测试。"""

from tinylib.stats import mean, median, moving_average, percentile, variance


def test_mean_of_three():
    assert mean([1, 2, 3]) == 2.0


def test_mean_of_empty_raises():
    try:
        mean([])
    except ValueError:
        return
    raise AssertionError("空序列应当报错")


def test_median_of_odd_count():
    assert median([3, 1, 2]) == 2


def test_median_of_even_count_averages_the_middle_two():
    assert median([1, 2, 3, 4]) == 2.5


def test_percentile_of_the_middle():
    assert percentile([1, 2, 3, 4], 50) == 2.5


def test_percentile_at_the_top():
    assert percentile([1, 2, 3, 4], 100) == 4


def test_percentile_rejects_an_out_of_range_p():
    try:
        percentile([1, 2], 101)
    except ValueError:
        return
    raise AssertionError("百分位超出范围应当报错")


def test_moving_average_of_two_windows():
    assert moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]


def test_moving_average_window_longer_than_the_series():
    assert moving_average([1, 2], 3) == []


def test_variance_of_two_samples():
    assert variance([1, 3]) == 2.0
