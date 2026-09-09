"""Exact currency percentile arithmetic shared by engine and app reductions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class CurrencyQuantileInterpolation:
    lower_index: int
    upper_index: int
    fraction: Decimal


def currency_quantile_plan(
    sample_count: int, percentiles: tuple[float, ...]
) -> tuple[CurrencyQuantileInterpolation, ...]:
    """Return the order-statistic indices and exact interpolation fractions."""

    if sample_count <= 0:
        raise ValueError("cannot calculate percentiles from no samples")
    last = sample_count - 1
    plan: list[CurrencyQuantileInterpolation] = []
    for percentile in percentiles:
        if not 0 <= percentile <= 100:
            raise ValueError(f"percentile must be between 0 and 100; got {percentile}")
        rank = Decimal(str(percentile)) * last / Decimal(100)
        lower = int(rank // 1)
        plan.append(
            CurrencyQuantileInterpolation(lower_index=lower, upper_index=min(lower + 1, last), fraction=rank - lower)
        )
    return tuple(plan)


def interpolate_currency_quantiles(
    lower_values: NDArray[np.int64], upper_values: NDArray[np.int64], plan: tuple[CurrencyQuantileInterpolation, ...]
) -> NDArray[np.int64]:
    """Interpolate preselected order statistics and round half-up to a quantum."""

    lower = np.asarray(lower_values, dtype=np.int64)
    upper = np.asarray(upper_values, dtype=np.int64)
    if lower.shape != upper.shape:
        raise ValueError(f"quantile bracket shapes differ: {lower.shape} != {upper.shape}")
    if lower.shape[-1:] != (len(plan),):
        raise ValueError(f"quantile bracket width {lower.shape[-1:]} != plan width {(len(plan),)}")
    result = np.empty(lower.shape, dtype=np.int64)
    for percentile_index, interpolation in enumerate(plan):
        for sample_index in np.ndindex(lower.shape[:-1]):
            lower_value = int(lower[*sample_index, percentile_index])
            upper_value = int(upper[*sample_index, percentile_index])
            value = Decimal(lower_value) + Decimal(upper_value - lower_value) * interpolation.fraction
            result[*sample_index, percentile_index] = np.int64(value.to_integral_value(rounding=ROUND_HALF_UP))
    return result


def currency_quantiles(samples: NDArray[np.int64], percentiles: tuple[float, ...]) -> tuple[int, ...]:
    """Calculate exact linear percentiles over one integer-money sample vector."""

    ordered = np.sort(np.asarray(samples, dtype=np.int64))
    if ordered.ndim != 1:
        raise ValueError(f"currency quantile samples must be one-dimensional; got shape {ordered.shape}")
    plan = currency_quantile_plan(ordered.size, percentiles)
    lower = ordered[np.asarray([item.lower_index for item in plan], dtype=np.int64)]
    upper = ordered[np.asarray([item.upper_index for item in plan], dtype=np.int64)]
    return tuple(int(value) for value in interpolate_currency_quantiles(lower, upper, plan))
