"""The JAX engine as an `Engine`.

A thin adapter: every method here is one call into `jax_engine`, which already answers in
the canonical shapes. It exists so a caller can hold an engine rather than a branch, and so
the Rust engine has a counterpart to be substitutable with.
"""

from __future__ import annotations

from finance.augur.sim.backend import CompiledRun, Engine
from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.engine.jax_engine import (
    run_jax_product_metric_arrays,
    run_jax_product_summaries,
    run_jax_product_summary,
    run_jax_scan,
)
from finance.augur.sim.events import EventLog
from finance.augur.sim.product_metrics import (
    ProductMetricArrays,
    ProductMetricFanSummary,
    ProductProjectionSummaries,
    ProductTerminalSummary,
)


class JaxEngine(Engine):
    @property
    def name(self) -> str:
        return "jax"

    def product_metrics(self, run: CompiledRun, *, primary_agent_id: str) -> ProductMetricArrays:
        return run_jax_product_metric_arrays(run.plan, primary_agent_id=primary_agent_id)

    def product_fan(
        self, run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
    ) -> ProductMetricFanSummary:
        return run_jax_product_summary(
            run.plan, primary_agent_id=primary_agent_id, metric=metric, percentiles=percentiles
        )

    def product_terminal(self, run: CompiledRun, *, primary_agent_id: str, metric: str) -> ProductTerminalSummary:
        return run_jax_product_summary(run.plan, primary_agent_id=primary_agent_id, metric=metric, percentiles=None)

    def product_summaries(
        self, run: CompiledRun, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
    ) -> ProductProjectionSummaries:
        return run_jax_product_summaries(
            run.plan, primary_agent_id=primary_agent_id, metric=metric, percentiles=percentiles
        )

    def events(self, run: CompiledRun) -> EventLog:
        dense = SimulationRun(plan=run.plan, output=run_jax_scan(run.plan), external_series=run.external_series)
        return dense.events_log
