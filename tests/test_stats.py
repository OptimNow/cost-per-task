from __future__ import annotations

import pytest

from cost_per_task.stats import (
    bootstrap_interval,
    capped_retry_success,
    pass_k,
    percentile,
    wilson_interval,
)


def test_wilson_interval_known_value():
    # 9 successes out of 12 at z = 1.96
    low, high = wilson_interval(9, 12)
    assert low == pytest.approx(0.4677, abs=1e-3)
    assert high == pytest.approx(0.9111, abs=1e-3)


def test_wilson_interval_extremes_stay_in_range():
    assert wilson_interval(0, 5)[0] == 0.0
    assert wilson_interval(5, 5)[1] == 1.0
    with pytest.raises(ValueError):
        wilson_interval(1, 0)


def test_percentile_interpolates():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 1, 1, 2, 2, 2, 2, 3], 90) == pytest.approx(2.3)
    assert percentile([7], 90) == 7
    with pytest.raises(ValueError):
        percentile([], 50)


def test_capped_retry_success():
    assert capped_retry_success(0.5, 3) == pytest.approx(0.875)
    assert capped_retry_success(1.0, 1) == 1.0


def test_pass_k():
    outcomes = {
        "t1": [True, True, True],
        "t2": [True, False, True],
        "t3": [True],
    }
    assert pass_k(outcomes, 1) == pytest.approx(1.0)
    assert pass_k(outcomes, 2) == pytest.approx(0.5)  # t3 has fewer than 2 attempts
    assert pass_k(outcomes, 4) is None


def test_bootstrap_interval_brackets_the_estimate():
    clusters = [[1.0], [2.0], [3.0], [4.0], [5.0], [6.0]]
    interval = bootstrap_interval(clusters, lambda xs: sum(xs) / len(xs), resamples=2000, seed=7)
    assert interval is not None
    low, high = interval
    assert 1.0 <= low <= 3.5 <= high <= 6.0


def test_bootstrap_is_reproducible_with_seed():
    clusters = [[1.0, 2.0], [3.0], [4.0, 9.0]]
    first = bootstrap_interval(clusters, lambda xs: max(xs), resamples=500, seed=1)
    second = bootstrap_interval(clusters, lambda xs: max(xs), resamples=500, seed=1)
    assert first == second


def test_bootstrap_undefined_statistic():
    assert bootstrap_interval([], lambda xs: 1.0) is None
    assert bootstrap_interval([[1.0]], lambda xs: None, resamples=10) is None
