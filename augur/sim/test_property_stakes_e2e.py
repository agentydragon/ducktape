"""Sim-level e2e for property-stake decoding with more than one property.

Regression test for a buffer-layout bug in `decode_property_stakes`: the active
mask was taken from an R-first view of shape `(snapshot, rollout, property)` but
applied to the *raw* `(snapshot, property, rollout)` ownership / contribution /
equity buffers. With a single property the two flattenings coincide, so the bug
was invisible; with `property_count > 1` and `rollout_count > 1` it
cross-assigns each property's stake values to the wrong (property, rollout)
cells.

The scenario is fully deterministic (no exogenous series), so every property's
stake values must be identical across all rollouts and post-purchase months. The
bug breaks exactly that invariant.
"""

from __future__ import annotations

import polars as pl
import pytest
import pytest_bazel

from augur.sim.locations import Location
from augur.sim.scenario import Agent, InitialAccountBalance, Scenario, ScheduledPropertyPurchase
from augur.sim.simulate import simulate

LOCATION_ID = "loc"
LOCATIONS = {
    LOCATION_ID: Location(
        location_id=LOCATION_ID, display_name="Loc", jurisdiction_ids=[], annual_property_tax_rate=0.0
    )
}


def _two_property_scenario(*, horizon_months: int = 3) -> Scenario:
    """Two all-cash purchases by one buyer with distinct ownership/price/down-payment."""
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="property_seller")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=2_000_000.0),
            InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
                month=0,
                cause_id="buy_p1",
                property_id="p1",
                location_id=LOCATION_ID,
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="property_seller",
                purchase_price_usd=1_000_000.0,
                down_payment_usd=200_000.0,
                ownership_pct=1.0,
            ),
            ScheduledPropertyPurchase(
                month=0,
                cause_id="buy_p2",
                property_id="p2",
                location_id=LOCATION_ID,
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="property_seller",
                purchase_price_usd=500_000.0,
                down_payment_usd=500_000.0,
                ownership_pct=0.6,
            ),
        ],
        tax_profiles=[],
        horizon_months=horizon_months,
    )


def test_property_stakes_not_cross_assigned_across_properties() -> None:
    # Two properties × several rollouts is the exact shape that the (snapshot, rollout, property)
    # vs (snapshot, property, rollout) flattening mismatch scrambles.
    run = simulate(_two_property_scenario(), rollout_count=4, locations=LOCATIONS)
    stakes = run.property_stakes

    # equity_ledger = purchase_price - mortgage_principal (no mortgage here);
    # contribution_used_usd = down_payment + closing_cost.
    expected = {
        "p1": {"ownership_pct": 1.0, "contribution_used_usd": 200_000.0, "equity_ledger_usd": 1_000_000.0},
        "p2": {"ownership_pct": 0.6, "contribution_used_usd": 500_000.0, "equity_ledger_usd": 500_000.0},
    }
    for property_id, fields in expected.items():
        rows = stakes.filter(pl.col("property_id") == property_id)
        assert rows.height > 0, f"no stake rows decoded for {property_id}"
        for column, value in fields.items():
            distinct = set(rows[column].to_list())
            # Deterministic inputs ⇒ exactly one value per property across all rollouts/months.
            assert len(distinct) == 1, f"{property_id}.{column} varies across rollouts: {distinct}"
            assert distinct.pop() == pytest.approx(value), f"{property_id}.{column} != {value}"


if __name__ == "__main__":
    pytest_bazel.main()
