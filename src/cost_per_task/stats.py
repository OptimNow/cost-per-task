"""Statistics from Section 4 of the DoiT framework, standard library only.

- Wilson score interval on a binomial success rate (better than the normal
  approximation for small n and rates near 0 or 1).
- Nonparametric bootstrap: tasks are resampled with replacement (a task is
  the natural cluster, since its attempts share difficulty) and the
  statistic is recomputed on each resample.
- Percentiles with linear interpolation, for P90 of attempt cost.
- Capped-retry success p_N = 1 - (1 - p)^N.
- pass^k: the share of tasks solved on every one of their first k attempts.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be between 0 and n")
    p = successes / n
    z2 = z * z
    denominator = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    half_width = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denominator
    return max(0.0, centre - half_width), min(1.0, centre + half_width)


def percentile(values: Sequence[float], q: float) -> float:
    """q in [0, 100]; linear interpolation between order statistics."""
    if not values:
        raise ValueError("percentile of empty sequence")
    if not 0 <= q <= 100:
        raise ValueError("q must be between 0 and 100")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def capped_retry_success(p: float, n_max: int) -> float:
    if n_max < 1:
        raise ValueError("n_max must be at least 1")
    return 1 - (1 - p) ** n_max


def pass_k(outcomes_by_task: dict[str, Sequence[bool]], k: int) -> float | None:
    """Fraction of tasks, among those with at least k attempts in order, whose
    first k attempts all passed. None when no task has k attempts."""
    if k < 1:
        raise ValueError("k must be at least 1")
    eligible = [outcomes for outcomes in outcomes_by_task.values() if len(outcomes) >= k]
    if not eligible:
        return None
    return sum(1 for outcomes in eligible if all(outcomes[:k])) / len(eligible)


def bootstrap_interval(
    clusters: Sequence[Sequence[T]],
    statistic: Callable[[list[T]], float | None],
    resamples: int = 10_000,
    seed: int | None = None,
    alpha: float = 0.05,
) -> tuple[float, float] | None:
    """Percentile bootstrap interval of ``statistic`` over resampled clusters.

    Each resample draws len(clusters) clusters with replacement and applies
    ``statistic`` to their pooled items. Resamples where the statistic is
    undefined (returns None) are dropped; None is returned if fewer than two
    resamples remain.
    """
    if not clusters:
        return None
    rng = random.Random(seed)
    n = len(clusters)
    estimates: list[float] = []
    for _ in range(resamples):
        pooled: list[T] = []
        for _ in range(n):
            pooled.extend(clusters[rng.randrange(n)])
        value = statistic(pooled)
        if value is not None and math.isfinite(value):
            estimates.append(value)
    if len(estimates) < 2:
        return None
    return percentile(estimates, 100 * alpha / 2), percentile(estimates, 100 * (1 - alpha / 2))
