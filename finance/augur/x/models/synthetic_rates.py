"""Synthetic monthly rate paths for fit tests: a generated Ornstein-Uhlenbeck process with known
parameters, which is the only way to test a rate estimator (see `ornstein_uhlenbeck.py`'s module
docstring) — real FRED data could only assert that today's numbers equal today's numbers.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from finance.evidence.loading import MonthlyLevel

TRUE_REVERSION = 0.02
TRUE_MEAN = 0.035
TRUE_SIGMA = 0.004


def months(count: int, start_year: int = 1900) -> list[date]:
    return [date(start_year + index // 12, index % 12 + 1, 1) for index in range(count)]


def ou_path(
    count: int,
    *,
    reversion: float = TRUE_REVERSION,
    mean: float = TRUE_MEAN,
    sigma: float = TRUE_SIGMA,
    seed: int = 0,
    initial: float | None = None,
) -> list[MonthlyLevel]:
    rng = np.random.default_rng(seed)
    value = mean if initial is None else initial
    values = []
    for shock in rng.standard_normal(count):
        value = value + reversion * (mean - value) + sigma * shock
        values.append(value)
    return [MonthlyLevel(month=month, value=v) for month, v in zip(months(count), values, strict=True)]
