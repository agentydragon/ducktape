"""Sim-level e2e for property-stake decoding with more than one property.

Regression test for a output-layout bug in `decode_property_stakes`: the active
mask was taken from an R-first view of shape `(snapshot, rollout, property)` but
applied to the *raw* `(snapshot, property, rollout)` contribution / equity
buffers. With a single property the two flattenings coincide, so the bug was
invisible; with `property_count > 1` and `rollout_count > 1` it
cross-assigns each property's stake values to the wrong (property, rollout)
cells.

The scenario is fully deterministic (no exogenous series), so every property's
stake values must be identical across all rollouts and post-purchase months. The
bug breaks exactly that invariant.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.series import HomeValueKey, LevelSeriesKey, LocationId
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    CapitalImprovementEvent,
    FilingStatus,
    InitialAccountBalance,
    MortgageFinancing,
    PrimaryResidenceAssignment,
    PropertySaleEvent,
    PropertyTaxPolicy,
    RecurringTransfer,
    Scenario,
    ScheduledPropertyPurchase,
    SetRentedFractionEvent,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate, simulate_with_external_series
from finance.augur.sim.testing.state_helpers import liabilities, property_stakes, property_state

LOCATION_ID = "loc"
LOCATIONS = {
    LOCATION_ID: Location(
        location_id=LOCATION_ID, display_name="Loc", jurisdiction_ids=[], annual_property_tax_rate=0.0
    )
}
HOME_LOCATION_ID = "home_loc"
RENTAL_LOCATION_ID = "rental_loc"
MULTI_PROPERTY_LOCATIONS = {
    HOME_LOCATION_ID: Location(
        location_id=HOME_LOCATION_ID,
        display_name="Primary Home",
        jurisdiction_ids=["federal_us", "california"],
        annual_property_tax_rate=0.012,
    ),
    RENTAL_LOCATION_ID: Location(
        location_id=RENTAL_LOCATION_ID,
        display_name="Rental",
        jurisdiction_ids=["federal_us", "california"],
        annual_property_tax_rate=0.024,
    ),
}


def _series_context(*, levels_by_series: dict[LevelSeriesKey, list[float]]) -> ExternalSeriesContext:
    horizon_months = max(len(levels) for levels in levels_by_series.values()) - 1
    return ExternalSeriesContext.from_level_blocks(
        [(key, np.asarray([levels], dtype=np.float64)) for key, levels in levels_by_series.items()],
        rollout_count=1,
        horizon_months=horizon_months,
    )


def _two_property_scenario(*, horizon_months: int = 3) -> Scenario:
    """Two all-cash purchases by one buyer with distinct price/down-payment."""
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="property_seller")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=2000000),
            InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
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
                purchase_price=1000000,
                down_payment=200000,
            ),
            ScheduledPropertyPurchase(
                month=0,
                cause_id="buy_p2",
                property_id="p2",
                location_id=LOCATION_ID,
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="property_seller",
                purchase_price=500000,
                down_payment=500000,
            ),
        ],
        tax_profiles=[],
        horizon_months=horizon_months,
    )


def test_property_stakes_not_cross_assigned_across_properties() -> None:
    # Two properties × several rollouts is the exact shape that the (snapshot, rollout, property)
    # vs (snapshot, property, rollout) flattening mismatch scrambles.
    run = simulate(_two_property_scenario(), rollout_count=4, locations=LOCATIONS)
    stakes = property_stakes(run)

    # equity_ledger = purchase_price - mortgage_principal (no mortgage here);
    # contribution_used_usd = down_payment + closing_cost.
    expected = {
        "p1": {"contribution_used_quanta": 20_000_000, "equity_ledger_quanta": 100_000_000},
        "p2": {"contribution_used_quanta": 50_000_000, "equity_ledger_quanta": 50_000_000},
    }
    for property_id, fields in expected.items():
        rows = stakes.filter(pl.col("property_id") == property_id)
        assert rows.height > 0, f"no stake rows decoded for {property_id}"
        for column, value in fields.items():
            distinct = set(rows[column].to_list())
            # Deterministic inputs ⇒ exactly one value per property across all rollouts/months.
            assert len(distinct) == 1, f"{property_id}.{column} varies across rollouts: {distinct}"
            assert distinct.pop() == value, f"{property_id}.{column} != {value}"


def test_property_purchase_transfer_is_derived_from_active_and_stake() -> None:
    """Only active purchases with a positive stake emit the buyer-cash transfer."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="property_seller")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=300000),
            InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
                month=1,
                cause_id="buy_zero_stake",
                property_id="zero_stake",
                location_id=LOCATION_ID,
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="property_seller",
                purchase_price=100000,
                down_payment=0,
            ),
            ScheduledPropertyPurchase(
                month=2,
                cause_id="buy_positive_stake",
                property_id="positive_stake",
                location_id=LOCATION_ID,
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="property_seller",
                purchase_price=200000,
                down_payment=200000,
            ),
        ],
        tax_profiles=[],
        horizon_months=3,
    )

    run = simulate(scenario, rollout_count=1, locations=LOCATIONS)

    purchases = run.events_log.property_purchases.sort("month_index")
    assert purchases.select("month_index", "cause_id").to_dicts() == [
        {"month_index": 1, "cause_id": "buy_zero_stake"},
        {"month_index": 2, "cause_id": "buy_positive_stake"},
    ]
    transfers = run.events_log.transfers
    assert transfers.select("month_index", "cause_id", "amount_quanta").to_dicts() == [
        {"month_index": 2, "cause_id": "buy_positive_stake_buyer_cash", "amount_quanta": 20_000_000}
    ]


def test_multi_property_lifecycle_tax_and_sale_state_is_property_scoped() -> None:
    """One owner holds a primary home and a rental; only the rental changes and sells.

    This pins the multi-property behavior that is most likely to regress when outputs are indexed
    by property slot: property-tax obligations, Schedule E depreciation/deductions, capex/sale
    basis, §121 primary-residence eligibility, and mortgage payoff must all stay property-scoped.
    """

    horizon = 36
    rental_sale_month = 24
    rental_purchase_price = 300_000
    rental_capex = 30_000
    rental_monthly_tax = rental_purchase_price * 0.024 / 12.0
    rental_building_basis_before = rental_purchase_price * 0.80
    rental_building_basis_after = rental_building_basis_before + rental_capex
    monthly_dep_before = rental_building_basis_before / 27.5 / 12.0
    monthly_dep_after_half_rented = rental_building_basis_after * 0.5 / 27.5 / 12.0

    scenario = Scenario(
        agents=[
            Agent(agent_id="alice"),
            Agent(agent_id="tenant"),
            Agent(agent_id="seller"),
            Agent(agent_id="bank"),
            Agent(agent_id="county"),
            Agent(agent_id="irs"),
        ],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=1500000),
            InitialAccountBalance(agent_id="tenant", account_id="checking", balance=0),
            InitialAccountBalance(agent_id="seller", account_id="checking", balance=0),
            InitialAccountBalance(agent_id="bank", account_id="checking", balance=0),
            InitialAccountBalance(agent_id="county", account_id="checking", balance=0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=rental_sale_month - 1,
                cause_id="rental_income:rental",
                from_agent_id="tenant",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount=2000,
                income_category=ORDINARY_INCOME,
            )
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
                month=0,
                cause_id="buy_home",
                property_id="home",
                location_id=HOME_LOCATION_ID,
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="seller",
                purchase_price=500000,
                down_payment=100000,
                buyer_closing_cost=0,
                rented_fraction=0.0,
                land_value_fraction=0.20,
                mortgage=MortgageFinancing(
                    liability_id="home_mortgage",
                    lender_agent_id="bank",
                    principal=400000,
                    annual_interest_rate=0.06,
                    term_months=360,
                ),
            ),
            ScheduledPropertyPurchase(
                month=0,
                cause_id="buy_rental",
                property_id="rental",
                location_id=RENTAL_LOCATION_ID,
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="seller",
                purchase_price=rental_purchase_price,
                down_payment=rental_purchase_price,
                buyer_closing_cost=0,
                rented_fraction=1.0,
                land_value_fraction=0.20,
            ),
        ],
        initial_primary_residences=[PrimaryResidenceAssignment(agent_id="alice", property_id="home")],
        property_lifecycle_events=[
            SetRentedFractionEvent(month=12, property_id="rental", rented_fraction=0.5),
            CapitalImprovementEvent(month=12, property_id="rental", amount=rental_capex, description="new roof"),
            PropertySaleEvent(month=rental_sale_month, property_id="rental", closing_cost_pct=6.0),
        ],
        property_tax_policies=[
            PropertyTaxPolicy(property_id="home", owner_agent_id="alice", tax_authority_agent_id="county"),
            PropertyTaxPolicy(property_id="rental", owner_agent_id="alice", tax_authority_agent_id="county"),
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=horizon,
    )
    ctx = _series_context(
        levels_by_series={
            HomeValueKey(location_id=LocationId(HOME_LOCATION_ID)): [1.0] * (horizon + 1),
            HomeValueKey(location_id=LocationId(RENTAL_LOCATION_ID)): [1.0] * rental_sale_month
            + [1.5] * (horizon + 1 - rental_sale_month),
        }
    )

    run = simulate_with_external_series(
        scenario, external_series=ctx, rollout_count=1, locations=MULTI_PROPERTY_LOCATIONS
    )
    assert run.events_log.rollout_failures.is_empty()

    transfers = run.events_log.transfers
    home_tax = transfers.filter(pl.col("cause_id").str.starts_with("home_property_tax_m")).sort("month_index")
    assert home_tax.get_column("month_index").to_list() == list(range(1, horizon))
    assert home_tax.get_column("amount_quanta").to_list() == [50_000] * (horizon - 1)

    rental_tax = transfers.filter(pl.col("cause_id").str.starts_with("rental_property_tax_m")).sort("month_index")
    assert rental_tax.get_column("month_index").to_list() == list(range(1, rental_sale_month))
    assert rental_tax.get_column("amount_quanta").to_list() == pytest.approx(
        [rental_monthly_tax * 100] * (rental_sale_month - 1)
    )

    assert run.events_log.set_rented_fraction_events.to_dicts() == [
        {"rollout_index": 0, "month_index": 12, "property_id": "rental", "rented_fraction": 0.5}
    ]
    capex_rows = run.events_log.capital_improvement_events.to_dicts()
    assert len(capex_rows) == 1
    assert capex_rows[0]["rollout_index"] == 0
    assert capex_rows[0]["month_index"] == 12
    assert capex_rows[0]["property_id"] == "rental"
    assert capex_rows[0]["amount_quanta"] == 3_000_000

    cumulative_depreciation = 12 * monthly_dep_before + 12 * monthly_dep_after_half_rented
    expected_gross_proceeds = rental_purchase_price * 1.5 * 0.94
    expected_realized_gain = expected_gross_proceeds - (rental_purchase_price + rental_capex - cumulative_depreciation)
    expected_ltcg = expected_realized_gain - cumulative_depreciation
    sale = run.events_log.property_sale_events.to_dicts()[0]
    assert sale["month_index"] == rental_sale_month
    assert sale["property_id"] == "rental"
    assert sale["gross_proceeds_quanta"] == pytest.approx(expected_gross_proceeds * 100, abs=100)
    assert sale["mortgage_payoff_quanta"] == 0
    assert sale["realized_gain_quanta"] == pytest.approx(expected_realized_gain * 100, abs=100)
    assert sale["depreciation_recapture_quanta"] == pytest.approx(cumulative_depreciation * 100, abs=100)
    # Alice has a qualifying primary residence, but it is `home`; that must not leak onto `rental`.
    assert sale["section_121_exclusion_quanta"] == 0
    assert sale["long_term_capital_gain_quanta"] == pytest.approx(expected_ltcg * 100, abs=100)

    terminal_properties = property_state(run).filter(pl.col("month_index") == horizon)
    assert terminal_properties.get_column("property_id").to_list() == ["home"]
    terminal_home = terminal_properties.row(0, named=True)
    assert terminal_home["adjusted_basis_quanta"] == 50_000_000

    terminal_mortgage = liabilities(run).filter(
        (pl.col("month_index") == horizon) & (pl.col("liability_id") == "home_mortgage")
    )
    assert terminal_mortgage.height == 1
    assert terminal_mortgage.get_column("principal_quanta").item() > 0

    federal_by_month = {
        row["month_index"]: row
        for row in run.events_log.tax_breakdowns.filter(pl.col("jurisdiction_id") == "federal_us").iter_rows(named=True)
    }
    expected_year_0_ordinary = 24_000.0 - 12 * monthly_dep_before - 11 * rental_monthly_tax
    expected_year_1_ordinary = 24_000.0 - 12 * monthly_dep_after_half_rented - 12 * rental_monthly_tax * 0.5
    assert federal_by_month[11]["ordinary_income_quanta"] == pytest.approx(expected_year_0_ordinary * 100, abs=5)
    assert federal_by_month[23]["ordinary_income_quanta"] == pytest.approx(expected_year_1_ordinary * 100, abs=5)
    assert federal_by_month[35]["ltcg_quanta"] == pytest.approx(expected_ltcg * 100, abs=100)


if __name__ == "__main__":
    pytest_bazel.main()
