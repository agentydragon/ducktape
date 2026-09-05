"""JAX's projection into the shared result shape.

Separate from the shape itself so that naming the shape does not drag the JAX engine in:
`rust/result.py` is the counterpart, and a Rust-only suite depends on neither.
"""

from __future__ import annotations

from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.engine.jax_engine import run_jax_scan
from finance.augur.sim.testing.case import Case
from finance.augur.sim.testing.simulation_result import CHANNEL, SimulationResult, held_lots, realized_gains
from finance.augur.sim.testing.state_helpers import (
    asset_lots,
    capital_gains_ytd,
    cash_balances,
    liabilities,
    property_stakes,
    property_state,
    rollout_status,
    tax_liabilities,
)


def run_jax(case: Case) -> SimulationResult:
    """Run the case on the Python/JAX engine, and report what it says.

    Off the same compiled plan any other engine's input is derived from, so a difference in
    these channels is a difference between engines rather than between two compilations.
    """

    run = SimulationRun(plan=case.plan, output=run_jax_scan(case.plan), external_series=case.external_series)
    taxed = {profile.agent_id for profile in case.scenario.tax_profiles}
    return SimulationResult(
        backend="jax",
        events=run.events_log,
        cash=CHANNEL["cash"].conform(cash_balances(run)),
        lots=CHANNEL["lots"].conform(held_lots(asset_lots(run))),
        capital_gains=CHANNEL["capital_gains"].conform(realized_gains(capital_gains_ytd(run), taxed)),
        tax_liabilities=CHANNEL["tax_liabilities"].conform(tax_liabilities(run)),
        properties=CHANNEL["properties"].conform(property_state(run)),
        property_stakes=CHANNEL["property_stakes"].conform(property_stakes(run)),
        liabilities=CHANNEL["liabilities"].conform(liabilities(run)),
        rollout_status=CHANNEL["rollout_status"].conform(rollout_status(run)),
    )
