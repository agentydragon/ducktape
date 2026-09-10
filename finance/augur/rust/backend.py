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
from typing import Any, cast, overload

import numpy as np
from jaxtyping import Int64

from finance.augur.rust import simulator
from finance.augur.rust.event_log import decode_event_log
from finance.augur.sim.backend import Engine
from finance.augur.sim.events import EventLog
from finance.augur.sim.metric_composition import BASE_METRIC_NAMES
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.product_metrics import (
    ProductMetricArrays,
    ProductMetricFanSummary,
    ProductProjectionSummaries,
    ProductTerminalSummary,
    metric_fan,
    projection_summaries,
    terminal_summary,
)


def _base_series(metrics: simulator.ProductMetrics) -> tuple[Int64[np.ndarray, " snapshot rollout"], ...]:
    """Reshape each flat `[snapshot][rollout]` block the extension returns."""

    shape = (metrics.snapshot_count, metrics.rollout_count)
    names = tuple(metrics.metric_names)
    if names != BASE_METRIC_NAMES:
        raise ValueError(f"Rust base metric order {names} does not match Python's {BASE_METRIC_NAMES}")
    return tuple(np.asarray(block, dtype=np.int64).reshape(shape) for block in metrics.base_series)


def run_rust_product_metric_arrays(run: CompiledRun, *, primary_agent_id: str) -> ProductMetricArrays:
    """Every base metric series for one population, from one Rust execution."""

    metrics = simulator.simulate_product_metrics(run, primary_agent_id)
    return ProductMetricArrays(
        # Configured full runs emit every prepared row in its original order.
        rollout_ids=tuple(range(metrics.rollout_count)),
        month_index=np.arange(metrics.snapshot_count, dtype=np.int64),
        failed_month=np.asarray(metrics.failed_month, dtype=np.int64),
        currency_code=run.currency_code,
        currency_quantum=run.currency_quantum,
        base_series=_base_series(metrics),
    )


@overload
def run_rust_product_summary(
    run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
) -> ProductMetricFanSummary: ...


@overload
def run_rust_product_summary(
    run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: None
) -> ProductTerminalSummary: ...


def run_rust_product_summary(
    run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...] | None
) -> ProductMetricFanSummary | ProductTerminalSummary:
    """Either projection for one metric, from one Rust execution.

    The `percentiles`-shaped overload pair is the product service's own dispatch shape, so a
    backend answers both projections without a second call shape to keep aligned.
    """

    arrays = run_rust_product_metric_arrays(run, primary_agent_id=primary_agent_id)
    if percentiles is None:
        return terminal_summary(arrays, metric=metric)
    return metric_fan(arrays, metric=metric, percentiles=percentiles)


def run_rust_product_summaries(
    run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
) -> ProductProjectionSummaries:
    """Fan and terminal summaries for one metric, from one Rust execution."""

    arrays = run_rust_product_metric_arrays(run, primary_agent_id=primary_agent_id)
    return projection_summaries(arrays, metric=metric, percentiles=percentiles)


class RustEngine(Engine):
    """The Rust engine as an `Engine`.

    Each method transports the already-prepared input and makes one in-process call.
    Execution never rereads the authored scenario or recompiles its financial rules.
    """

    @property
    def name(self) -> str:
        return "rust"

    def product_metrics(self, run: CompiledRun, *, primary_agent_id: str) -> ProductMetricArrays:
        return run_rust_product_metric_arrays(run, primary_agent_id=primary_agent_id)

    def product_fan(
        self, run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
    ) -> ProductMetricFanSummary:
        return run_rust_product_summary(run, primary_agent_id=primary_agent_id, metric=metric, percentiles=percentiles)

    def product_terminal(self, run: CompiledRun, *, primary_agent_id: str, metric: str) -> ProductTerminalSummary:
        return run_rust_product_summary(run, primary_agent_id=primary_agent_id, metric=metric, percentiles=None)

    def product_summaries(
        self, run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
    ) -> ProductProjectionSummaries:
        return run_rust_product_summaries(
            run, primary_agent_id=primary_agent_id, metric=metric, percentiles=percentiles
        )

    def events(self, run: CompiledRun) -> EventLog:
        # Dense, not forensic: both carry the canonical frames, and the balanced journal the
        # forensic run adds is Rust's own double-entry invariant with no reader here.
        dense = cast(dict[str, Any], json.loads(simulator.simulate_dense_json(run)))
        return decode_event_log(dense)
