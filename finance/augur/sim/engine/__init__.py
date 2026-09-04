"""Dense-array simulation engine."""

from __future__ import annotations

from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.compiler.plan import CompiledSimulation, compile_simulation
from finance.augur.sim.engine.jax_engine import run_jax_scan, run_jax_scan_with_product_metrics
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.locations import Location
from finance.augur.sim.output import DenseSimulationOutput
from finance.augur.sim.product_metrics import ProductMetricArrays
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.scenario import Scenario


def run_dense_simulation(
    scenario: Scenario, *, rollout_count: int, external_series: ExternalSeriesContext, locations: dict[str, Location]
) -> SimulationRun:
    plan = compile_simulation(
        scenario,
        rollout_count=rollout_count,
        external_series=external_series,
        jurisdictions=load_jurisdictions_for(scenario),
        locations=locations,
    )
    return SimulationRun(plan=plan, output=run_jax_scan(plan), external_series=external_series)


def run_dense_simulation_with_product_metrics(
    scenario: Scenario,
    *,
    rollout_count: int,
    external_series: ExternalSeriesContext,
    locations: dict[str, Location],
    primary_agent_id: str,
) -> tuple[CompiledSimulation, DenseSimulationOutput, ProductMetricArrays]:
    """Return raw dense output + selected-actor metrics from one engine dispatch."""

    plan = compile_simulation(
        scenario,
        rollout_count=rollout_count,
        external_series=external_series,
        jurisdictions=load_jurisdictions_for(scenario),
        locations=locations,
    )
    output, metrics = run_jax_scan_with_product_metrics(plan, primary_agent_id=primary_agent_id)
    return plan, output, metrics
