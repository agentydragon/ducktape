"""PIT metrics shared by the prediction-market calibration scripts."""

import math
import random
from collections.abc import Callable

BINS = 10
TAIL_NULL = 0.2  # Uniform mass in the two end deciles.
_BLOCK_MONTHS = 4


def jsd_to_uniform(pit_values: list[float], bins: int = BINS) -> float | None:
    """Jensen-Shannon divergence in bits between the PIT histogram and uniform bin masses."""
    if len(pit_values) < bins:
        return None
    counts = [0.0] * bins
    for u in pit_values:
        counts[min(bins - 1, int(u * bins))] += 1
    n = sum(counts)
    p = [c / n for c in counts]
    q = 1.0 / bins
    m = [(pi + q) / 2 for pi in p]

    def _kl(a: list[float], b: list[float]) -> float:
        return sum(ai * math.log2(ai / bi) for ai, bi in zip(a, b, strict=True) if ai > 0)

    return 0.5 * _kl(p, m) + 0.5 * _kl([q] * bins, m)


def tail_rate(vals: list[float]) -> float:
    return sum(1 for u in vals if u <= 0.1 or u >= 0.9) / len(vals)


def block_bootstrap_ci(
    month_pits: list[list[float]], statistic: Callable[[list[float]], float], reps: int = 4000
) -> tuple[float, float]:
    """Resample contiguous blocks of months to retain dependence within each block."""
    nblocks = math.ceil(len(month_pits) / _BLOCK_MONTHS)
    out = []
    for _ in range(reps):
        sampled: list[list[float]] = []
        for _ in range(nblocks):
            start = random.randint(0, len(month_pits) - _BLOCK_MONTHS)
            sampled += month_pits[start : start + _BLOCK_MONTHS]
        pooled = [p for mm in sampled[: len(month_pits)] for p in mm]
        out.append(statistic(pooled))
    out.sort()
    return out[int(0.025 * reps)], out[int(0.975 * reps)]
