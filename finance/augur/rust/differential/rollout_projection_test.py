"""The selected-rollout trace, rendered from either engine by one projection.

`project_product_rollout` used to read `CompiledSimulation` plus `DenseSimulationOutput` —
JAX's own array layout — which is why the rollout endpoint could not be served by anything
else. It reads the canonical event frames now, and both engines emit those.

So the claim here is not that two projections agree. It is that one projection, given either
engine's frames, renders the same trace.
"""

from __future__ import annotations

from decimal import Decimal

import pytest_bazel

from finance.augur.product.projection import ProductRolloutProjection, project_product_rollout
from finance.augur.rust.backend import RustEngine
from finance.augur.rust.differential.fixtures import VTI, checking, taxed
from finance.augur.sim.backend import Engine
from finance.augur.sim.engine.jax_backend import JaxEngine
from finance.augur.sim.scenario import InitialLot, ScheduledAssetSale
from finance.augur.sim.testing.case import Case, levels, scenario

PRIMARY_AGENT = "alice"
HORIZON_MONTHS = 30
SALE_MONTH = 14
LOT_BASIS = Decimal(10_000)
SALE_PRICE = Decimal(60_000)


def traced_case() -> Case:
    """A rollout with something to trace: a long-term sale, and the tax year that follows it.

    Deliberately not the feature-rich scenario. This suite asks whether one projection can read
    either engine, and a case whose events a reader can enumerate says more about a mismatch
    than one with hundreds of rows.
    """

    return Case(
        scenario=scenario(
            checking((PRIMARY_AGENT, Decimal(0)), ("irs", Decimal(0))),
            horizon_months=HORIZON_MONTHS,
            initial_lots=[
                InitialLot(
                    lot_id="alice-vti",
                    agent_id=PRIMARY_AGENT,
                    account_id="checking",
                    asset=VTI,
                    purchase_month_index=-24,
                    quantity=1.0,
                    cost_basis_per_unit=LOT_BASIS,
                )
            ],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=SALE_MONTH,
                    cause_id="sell-vti",
                    agent_id=PRIMARY_AGENT,
                    source_account_id="checking",
                    asset=VTI,
                    quantity=1.0,
                    proceeds_account_id="checking",
                )
            ],
            tax_profiles=[taxed(PRIMARY_AGENT, "federal_us")],
        ),
        rollout_count=1,
        series={VTI: levels([[SALE_PRICE] * (HORIZON_MONTHS + 1)])},
    )


def projected(case: Case, engine: Engine) -> ProductRolloutProjection:
    return project_product_rollout(
        engine.events(case.compiled_run),
        engine.product_metrics(case.compiled_run, primary_agent_id=PRIMARY_AGENT),
        rollout_index=0,
        primary_agent_id=PRIMARY_AGENT,
        asset_label_by_id={},
    )


def test_the_case_really_traces_something() -> None:
    """Without this, two empty event tuples would compare equal and prove nothing."""

    kinds = {event.kind for event in projected(traced_case(), JaxEngine()).events}
    assert "holding_sale" in kinds, f"expected the sale in the trace, got {sorted(kinds)}"
    assert "tax_accrual" in kinds, f"expected the year-end accrual in the trace, got {sorted(kinds)}"


def test_both_engines_render_the_same_trace() -> None:
    """What the rollout endpoint needs before either engine can serve it."""

    case = traced_case()
    assert projected(case, RustEngine()).events == projected(case, JaxEngine()).events


if __name__ == "__main__":
    pytest_bazel.main()
