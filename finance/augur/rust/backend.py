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
from typing import Any, cast, overload

import numpy as np
from jaxtyping import Int64

from finance.augur.product.metric_composition import BASE_METRIC_NAMES, compose_metric, terminal_series
from finance.augur.product.quantiles import currency_quantile_plan, interpolate_currency_quantiles
from finance.augur.rust import simulator
from finance.augur.rust.event_log import decode_event_log
from finance.augur.rust.fixture_encoder import encode_fixture
from finance.augur.sim.backend import CompiledRun, Engine
from finance.augur.sim.events import EventLog
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


def _metric_series(
    fixture: Mapping[str, Any], *, primary_agent_id: str, metric: str
) -> tuple[ProductMetricArrays, Int64[np.ndarray, " snapshot rollout"], Int64[np.ndarray, " rollout"]]:
    """One Rust execution, composed into the requested metric and its terminal reduction."""

    arrays = run_rust_product_metric_arrays(fixture, primary_agent_id=primary_agent_id)
    base = dict(zip(BASE_METRIC_NAMES, arrays.base_series, strict=True))
    series = compose_metric(metric, base.__getitem__)
    return arrays, series, terminal_series(metric, series)


def _metric_fan(
    arrays: ProductMetricArrays,
    *,
    metric: str,
    percentiles: tuple[float, ...],
    series: Int64[np.ndarray, " snapshot rollout"],
    terminal: Int64[np.ndarray, " rollout"],
) -> ProductMetricFanSummary:
    quantile_plan = currency_quantile_plan(int(series.shape[1]), percentiles)
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
    return ProductMetricFanSummary(
        month_index=arrays.month_index,
        failed_count=int((arrays.failed_month >= 0).sum()),
        currency_code=arrays.currency_code,
        currency_quantum=arrays.currency_quantum,
        percentiles=percentiles,
        terminal_percentiles=interpolate_currency_quantiles(terminal_lower, terminal_upper, quantile_plan),
        monthly_percentiles=interpolate_currency_quantiles(monthly_lower, monthly_upper, quantile_plan),
    )


def _terminal_summary(arrays: ProductMetricArrays, terminal: Int64[np.ndarray, " rollout"]) -> ProductTerminalSummary:
    return ProductTerminalSummary(
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

    The `percentiles`-shaped overload pair mirrors `run_jax_product_summary`, so the product
    service dispatches to a backend without a second call shape to keep aligned.
    """

    arrays, series, terminal = _metric_series(fixture, primary_agent_id=primary_agent_id, metric=metric)
    if percentiles is None:
        return _terminal_summary(arrays, terminal)
    return _metric_fan(arrays, metric=metric, percentiles=percentiles, series=series, terminal=terminal)


def run_rust_product_summaries(
    fixture: Mapping[str, Any], *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
) -> ProductProjectionSummaries:
    """Fan and terminal summaries for one metric, from one Rust execution."""

    arrays, series, terminal = _metric_series(fixture, primary_agent_id=primary_agent_id, metric=metric)
    return ProductProjectionSummaries(
        metric_fan=_metric_fan(arrays, metric=metric, percentiles=percentiles, series=series, terminal=terminal),
        terminal_distribution=_terminal_summary(arrays, terminal),
    )


class RustEngine(Engine):
    """The Rust engine as an `Engine`.

    Each method encodes the compiled run as the strict integer fixture and makes one
    in-process call. The fixture is derived here rather than held on `CompiledRun` because
    it is this engine's input shape, and nothing else should have to know it exists.
    """

    @property
    def name(self) -> str:
        return "rust"

    def _fixture(self, run: CompiledRun) -> dict[str, Any]:
        return encode_fixture(
            run.scenario,
            run.plan,
            external_series=run.external_series,
            jurisdictions=run.jurisdictions,
            locations=run.locations,
        )

    def product_metrics(self, run: CompiledRun, *, primary_agent_id: str) -> ProductMetricArrays:
        return run_rust_product_metric_arrays(self._fixture(run), primary_agent_id=primary_agent_id)

    def product_fan(
        self, run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
    ) -> ProductMetricFanSummary:
        return run_rust_product_summary(
            self._fixture(run), primary_agent_id=primary_agent_id, metric=metric, percentiles=percentiles
        )

    def product_terminal(self, run: CompiledRun, *, primary_agent_id: str, metric: str) -> ProductTerminalSummary:
        return run_rust_product_summary(
            self._fixture(run), primary_agent_id=primary_agent_id, metric=metric, percentiles=None
        )

    def product_summaries(
        self, run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
    ) -> ProductProjectionSummaries:
        return run_rust_product_summaries(
            self._fixture(run), primary_agent_id=primary_agent_id, metric=metric, percentiles=percentiles
        )

    def events(self, run: CompiledRun) -> EventLog:
        # Dense, not forensic: both carry the canonical frames, and the balanced journal the
        # forensic run adds is Rust's own double-entry invariant with no reader here.
        dense = cast(dict[str, Any], json.loads(simulator.simulate_dense_json(json.dumps(self._fixture(run)))))
        return decode_event_log(dense)
