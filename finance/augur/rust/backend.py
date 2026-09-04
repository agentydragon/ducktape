"""The Rust simulator behind the product read model's backend-neutral entry points.

The Rust engine supplies the seven base metric series and the failure vector; every
reduction above that — the derived metrics, the order statistics, the interpolation — is
the same shared code the JAX backend runs, so a fan produced here is identical to a JAX
fan by construction rather than by a second implementation agreeing.

Fixtures cross the boundary as JSON text because that is the simulator's own input
contract. Nothing else does: results come back as Python integers the caller wraps in
numpy, so the 100,000-rollout fan never pays for a dense JSON round trip.
"""

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
import json
from collections.abc import Mapping
from typing import Any, cast

import numpy as np
from jaxtyping import Int64

from finance.augur.product.metric_composition import BASE_METRIC_NAMES, compose_metric, terminal_series
from finance.augur.product.quantiles import currency_quantile_plan, interpolate_currency_quantiles
from finance.augur.rust import simulator
from finance.augur.sim.product_metrics import (
    ProductMetricArrays,
    ProductMetricFanSummary,
    ProductProjectionSummaries,
    ProductTerminalSummary,
)


def _base_series(metrics: simulator.ProductMetrics) -> tuple[Int64[np.ndarray, " snapshot rollout"], ...]:
    """Reshape each flat `[snapshot][rollout]` block the extension returns."""

    shape = (metrics.snapshot_count, metrics.rollout_count)
    names = tuple(metrics.metric_names)
    if names != BASE_METRIC_NAMES:
        raise ValueError(f"Rust base metric order {names} does not match Python's {BASE_METRIC_NAMES}")
    return tuple(np.asarray(block, dtype=np.int64).reshape(shape) for block in metrics.base_series)


def run_rust_product_metric_arrays(fixture: Mapping[str, Any], *, primary_agent_id: str) -> ProductMetricArrays:
    """Every base metric series for one population, from one Rust execution."""

    metrics = simulator.simulate_product_metrics(json.dumps(fixture), primary_agent_id)
    return ProductMetricArrays(
        month_index=np.arange(metrics.snapshot_count, dtype=np.int64),
        failed_month=np.asarray(metrics.failed_month, dtype=np.int64),
        currency_code=cast(str, fixture["currency_code"]),
        currency_quantum=cast(str, fixture["currency_quantum"]),
        base_series=_base_series(metrics),
    )


def run_rust_product_summaries(
    fixture: Mapping[str, Any], *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
) -> ProductProjectionSummaries:
    """Fan and terminal summaries for one metric, from one Rust execution."""

    arrays = run_rust_product_metric_arrays(fixture, primary_agent_id=primary_agent_id)
    base = dict(zip(BASE_METRIC_NAMES, arrays.base_series, strict=True))
    series = compose_metric(metric, base.__getitem__)
    terminal = terminal_series(metric, series)

    rollout_count = int(series.shape[1])
    quantile_plan = currency_quantile_plan(rollout_count, percentiles)
    lower_indices = np.asarray([item.lower_index for item in quantile_plan], dtype=np.int64)
    upper_indices = np.asarray([item.upper_index for item in quantile_plan], dtype=np.int64)
    ordered = np.sort(series, axis=1)
    monthly_lower = ordered[:, lower_indices]
    monthly_upper = ordered[:, upper_indices]
    if metric == "shortfall_quanta":
        # Terminal shortfall is a sum over months, so its order statistics are its own.
        ordered_terminal = np.sort(terminal)
        terminal_lower = ordered_terminal[lower_indices]
        terminal_upper = ordered_terminal[upper_indices]
    else:
        terminal_lower = monthly_lower[-1]
        terminal_upper = monthly_upper[-1]

    metric_fan = ProductMetricFanSummary(
        month_index=arrays.month_index,
        failed_count=int((arrays.failed_month >= 0).sum()),
        currency_code=arrays.currency_code,
        currency_quantum=arrays.currency_quantum,
        percentiles=percentiles,
        terminal_percentiles=interpolate_currency_quantiles(terminal_lower, terminal_upper, quantile_plan),
        monthly_percentiles=interpolate_currency_quantiles(monthly_lower, monthly_upper, quantile_plan),
    )
    terminal_distribution = ProductTerminalSummary(
        failed_month=arrays.failed_month,
        currency_code=arrays.currency_code,
        currency_quantum=arrays.currency_quantum,
        terminal_samples=np.asarray(terminal, dtype=np.int64),
    )
    return ProductProjectionSummaries(metric_fan=metric_fan, terminal_distribution=terminal_distribution)
