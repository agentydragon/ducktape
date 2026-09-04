"""Forward simulation entrypoints.

`simulate(scenario, rollout_count) -> SimulationRun` materializes external series and runs the JAX
engine: the whole month loop compiles into one XLA program, whose stacked outputs return as one
host-resident dense tree. Analytics consumers decode that output as Polars frames; selected product
rollout detail projects directly from the plan and dense output.
"""

from __future__ import annotations

from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.compiler.plan import CompiledSimulation
from finance.augur.sim.engine import run_dense_simulation, run_dense_simulation_with_product_metrics
from finance.augur.sim.external_series import ExternalSeriesContext, materialize_external_series
from finance.augur.sim.locations import Location
from finance.augur.sim.output import DenseSimulationOutput
from finance.augur.sim.product_metrics import ProductMetricArrays
from finance.augur.sim.scenario import Scenario


def simulate(scenario: Scenario, *, rollout_count: int, locations: dict[str, Location]) -> SimulationRun:
    if rollout_count <= 0:
        msg = f"rollout_count must be positive; got {rollout_count}"
        raise ValueError(msg)
    external_series = materialize_external_series(
        scenario.external_series, rollout_seeds=tuple(range(rollout_count)), horizon_months=int(scenario.horizon_months)
    )
    return simulate_with_external_series(
        scenario, rollout_count=rollout_count, external_series=external_series, locations=locations
    )


def simulate_with_external_series(
    scenario: Scenario, *, rollout_count: int, external_series: ExternalSeriesContext, locations: dict[str, Location]
) -> SimulationRun:
    if rollout_count <= 0:
        msg = f"rollout_count must be positive; got {rollout_count}"
        raise ValueError(msg)
    return run_dense_simulation(
        scenario, rollout_count=rollout_count, external_series=external_series, locations=locations
    )


def simulate_with_external_series_and_product_metrics(
    scenario: Scenario,
    *,
    rollout_count: int,
    external_series: ExternalSeriesContext,
    locations: dict[str, Location],
    primary_agent_id: str,
) -> tuple[CompiledSimulation, DenseSimulationOutput, ProductMetricArrays]:
    """Return raw dense outputs and selected-product metrics in one engine dispatch."""

    if rollout_count <= 0:
        msg = f"rollout_count must be positive; got {rollout_count}"
        raise ValueError(msg)
    return run_dense_simulation_with_product_metrics(
        scenario,
        rollout_count=rollout_count,
        external_series=external_series,
        locations=locations,
        primary_agent_id=primary_agent_id,
    )
