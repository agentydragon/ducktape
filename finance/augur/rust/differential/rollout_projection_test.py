"""The selected-rollout trace, rendered from either engine by one projection.

`project_product_rollout` reads `CompiledSimulation` plus `DenseSimulationOutput` — JAX's own
array layout — which is why the rollout endpoint could not be served by Rust.
`project_product_rollout_from_events` reads the canonical event frames instead, and both
engines emit those.

The first test is scaffolding: it holds the new projection to the old one so the rewrite can
be shown not to change what the product reports, and it goes when the old one does. The
second is the lasting claim — the two engines render the same trace, because one projection
read both rather than because two projections agreed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest_bazel

from finance.augur.product.projection import (
    ProductRolloutProjection,
    project_product_rollout,
    project_product_rollout_from_events,
)
from finance.augur.rust.backend import RustEngine
from finance.augur.rust.differential.case import Case, levels, scenario
from finance.augur.rust.differential.fixtures import VTI, checking, taxed
from finance.augur.sim.engine.jax_backend import JaxEngine
from finance.augur.sim.engine.jax_engine import run_jax_scan
from finance.augur.sim.scenario import InitialLot, ScheduledAssetSale

PRIMARY_AGENT = "alice"
HORIZON_MONTHS = 30
SALE_MONTH = 14
LOT_BASIS = Decimal(10_000)
SALE_PRICE = Decimal(60_000)


def traced_case() -> Case:
    """A rollout with something to trace: a long-term sale, and the tax year that follows it.

    Deliberately not the feature-rich scenario. This suite is about whether one projection can
    read either engine, and a case whose events a reader can enumerate says more about a
    mismatch than one with hundreds of rows.
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


def _jax_projections(case: Case) -> tuple[ProductRolloutProjection, ProductRolloutProjection]:
    engine = JaxEngine()
    metrics = engine.product_metrics(case.compiled_run, primary_agent_id=PRIMARY_AGENT)
    from_arrays = project_product_rollout(
        case.plan,
        run_jax_scan(case.plan),
        metrics,
        rollout_index=0,
        primary_agent_id=PRIMARY_AGENT,
        asset_label_by_id={},
    )
    from_events = project_product_rollout_from_events(
        engine.events(case.compiled_run), metrics, rollout_index=0, primary_agent_id=PRIMARY_AGENT, asset_label_by_id={}
    )
    return from_arrays, from_events


def test_the_case_really_traces_something() -> None:
    """Without this, two empty event tuples would compare equal and prove nothing."""

    from_arrays, _ = _jax_projections(traced_case())
    kinds = {event.kind for event in from_arrays.events}
    assert "holding_sale" in kinds, f"expected the sale in the trace, got {sorted(kinds)}"
    assert "tax_accrual" in kinds, f"expected the year-end accrual in the trace, got {sorted(kinds)}"


def test_reading_the_event_frames_reports_what_reading_the_arrays_did() -> None:
    """Migration proof: the rewrite does not change what the product renders."""

    from_arrays, from_events = _jax_projections(traced_case())
    assert from_events.events == from_arrays.events
    assert from_events.failed_month_index == from_arrays.failed_month_index
    assert from_events.currency_code == from_arrays.currency_code
    assert from_events.currency_quantum == from_arrays.currency_quantum


def test_both_engines_render_the_same_trace() -> None:
    """The claim the rollout endpoint needs before Rust can serve it."""

    case = traced_case()
    jax, rust = JaxEngine(), RustEngine()
    projections = [
        project_product_rollout_from_events(
            engine.events(case.compiled_run),
            engine.product_metrics(case.compiled_run, primary_agent_id=PRIMARY_AGENT),
            rollout_index=0,
            primary_agent_id=PRIMARY_AGENT,
            asset_label_by_id={},
        )
        for engine in (jax, rust)
    ]
    assert projections[1].events == projections[0].events


if __name__ == "__main__":
    pytest_bazel.main()
