"""The Rust simulator behind the product read model's backend-neutral entry points.

The engine supplies the seven base metric series and the failure vector; every reduction
above that — the derived metrics, the order statistics, the interpolation — lives above
this module and reads only those. That split is deliberate rather than incidental: it is
what the product read model needs a backend to owe, and no more.

Fixtures cross the boundary as JSON text because that is the simulator's own input
contract. Nothing else does: results come back as Python integers the caller wraps in
numpy, so the 100,000-rollout fan never pays for a dense JSON round trip.
"""

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
import json
from collections.abc import Mapping
from typing import Any, cast, overload

import numpy as np
from jaxtyping import Int64

from finance.augur.rust import simulator
from finance.augur.rust.event_log import decode_event_log
from finance.augur.sim.backend import CompiledRun, Engine
from finance.augur.sim.events import EventLog
from finance.augur.sim.metric_composition import BASE_METRIC_NAMES, compose_metric, terminal_series
from finance.augur.sim.product_metrics import (
    OutcomeBasis,
    ProductMetricArrays,
    ProductMetricFanSummary,
    ProductProjectionSummaries,
    ProductTerminalSummary,
)
from finance.augur.sim.quantiles import currency_quantiles


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


def _metric_series(
    fixture: Mapping[str, Any], *, primary_agent_id: str, metric: str
) -> tuple[ProductMetricArrays, Int64[np.ndarray, " snapshot rollout"], Int64[np.ndarray, " rollout"], OutcomeBasis]:
    """One Rust execution, composed into the requested metric and its terminal reduction."""

    arrays = run_rust_product_metric_arrays(fixture, primary_agent_id=primary_agent_id)
    base = dict(zip(BASE_METRIC_NAMES, arrays.base_series, strict=True))
    series = compose_metric(metric, base.__getitem__)
    basis = OutcomeBasis.OBSERVED_THROUGH_STOP if metric == "shortfall_quanta" else OutcomeBasis.COMPLETED_HORIZON
    return arrays, series, terminal_series(metric, series), basis


def _metric_fan(
    arrays: ProductMetricArrays,
    *,
    basis: OutcomeBasis,
    percentiles: tuple[float, ...],
    series: Int64[np.ndarray, " snapshot rollout"],
    terminal: Int64[np.ndarray, " rollout"],
) -> ProductMetricFanSummary:
    observed = arrays.observed if basis == OutcomeBasis.OBSERVED_THROUGH_STOP else arrays.scheduled_observed
    observed_count = observed.sum(axis=1)
    monthly = np.zeros((series.shape[0], len(percentiles)), dtype=np.int64)
    for month, mask in enumerate(observed):
        if observed_count[month]:
            monthly[month] = currency_quantiles(series[month, mask], percentiles)
    samples = terminal if basis == OutcomeBasis.OBSERVED_THROUGH_STOP else terminal[arrays.failed_month < 0]
    return ProductMetricFanSummary(
        basis=basis,
        month_index=arrays.month_index,
        failed_count=int((arrays.failed_month >= 0).sum()),
        currency_code=arrays.currency_code,
        currency_quantum=arrays.currency_quantum,
        percentiles=percentiles,
        terminal_percentiles=np.asarray(currency_quantiles(samples, percentiles), dtype=np.int64)
        if samples.size
        else None,
        monthly_percentiles=monthly,
        observed_count=observed_count,
    )


def _terminal_summary(
    arrays: ProductMetricArrays, terminal: Int64[np.ndarray, " rollout"], basis: OutcomeBasis
) -> ProductTerminalSummary:
    return ProductTerminalSummary(
        basis=basis,
        failed_month=arrays.failed_month,
        currency_code=arrays.currency_code,
        currency_quantum=arrays.currency_quantum,
        terminal_samples=np.asarray(terminal, dtype=np.int64),
    )


@overload
def run_rust_product_summary(
    fixture: Mapping[str, Any], *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
) -> ProductMetricFanSummary: ...


@overload
def run_rust_product_summary(
    fixture: Mapping[str, Any], *, primary_agent_id: str, metric: str, percentiles: None
) -> ProductTerminalSummary: ...


def run_rust_product_summary(
    fixture: Mapping[str, Any], *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...] | None
) -> ProductMetricFanSummary | ProductTerminalSummary:
    """Either projection for one metric, from one Rust execution.

    The `percentiles`-shaped overload pair is the product service's own dispatch shape, so a
    backend answers both projections without a second call shape to keep aligned.
    """

    arrays, series, terminal, basis = _metric_series(fixture, primary_agent_id=primary_agent_id, metric=metric)
    if percentiles is None:
        return _terminal_summary(arrays, terminal, basis)
    return _metric_fan(arrays, basis=basis, percentiles=percentiles, series=series, terminal=terminal)


def run_rust_product_summaries(
    fixture: Mapping[str, Any], *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
) -> ProductProjectionSummaries:
    """Fan and terminal summaries for one metric, from one Rust execution."""

    arrays, series, terminal, basis = _metric_series(fixture, primary_agent_id=primary_agent_id, metric=metric)
    return ProductProjectionSummaries(
        metric_fan=_metric_fan(arrays, basis=basis, percentiles=percentiles, series=series, terminal=terminal),
        terminal_distribution=_terminal_summary(arrays, terminal, basis),
    )


class RustEngine(Engine):
    """The Rust engine as an `Engine`.

    Each method transports the already-prepared input and makes one in-process call.
    Execution never rereads the authored scenario or recompiles its financial rules.
    """

    @property
    def name(self) -> str:
        return "rust"

    def product_metrics(self, run: CompiledRun, *, primary_agent_id: str) -> ProductMetricArrays:
        return run_rust_product_metric_arrays(run.execution_input, primary_agent_id=primary_agent_id)

    def product_fan(
        self, run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
    ) -> ProductMetricFanSummary:
        return run_rust_product_summary(
            run.execution_input, primary_agent_id=primary_agent_id, metric=metric, percentiles=percentiles
        )

    def product_terminal(self, run: CompiledRun, *, primary_agent_id: str, metric: str) -> ProductTerminalSummary:
        return run_rust_product_summary(
            run.execution_input, primary_agent_id=primary_agent_id, metric=metric, percentiles=None
        )

    def product_summaries(
        self, run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
    ) -> ProductProjectionSummaries:
        return run_rust_product_summaries(
            run.execution_input, primary_agent_id=primary_agent_id, metric=metric, percentiles=percentiles
        )

    def events(self, run: CompiledRun) -> EventLog:
        # Dense, not forensic: both carry the canonical frames, and the balanced journal the
        # forensic run adds is Rust's own double-entry invariant with no reader here.
        dense = cast(dict[str, Any], json.loads(simulator.simulate_dense_json(json.dumps(run.execution_input))))
        return decode_event_log(dense)
