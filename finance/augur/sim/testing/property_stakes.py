"""Property state stays scoped to the property it belongs to.

Two properties is the smallest shape in which an output indexed by property slot can be
wrong. With one property every flattening of `(snapshot, rollout, property)` coincides, so a
reader that applies a rollout-major mask to a property-major buffer is correct by accident;
with two, each property's values land in the other's cells. That was a real bug in the JAX
decoder, and it is why the first case below is deliberately as small as it is.

The rest is the same claim over a whole lifecycle: property tax, Schedule E depreciation,
capex, sale basis, §121 eligibility and mortgage payoff are all per property, and a scenario
holding a primary home and a rental is where a leak between them shows.

Stated against the channels every engine answers in, because "a property's state is its own"
is a claim about what a simulator is rather than about how one indexes its buffers.
"""

from __future__ import annotations

from decimal import Decimal

import polars as pl
import pytest

from finance.augur.model.series import HomeValueKey, LocationId
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    CapitalImprovementEvent,
    MortgageFinancing,
    PrimaryResidenceAssignment,
    PropertySaleEvent,
    PropertyTaxPolicy,
    RecurringTransfer,
    ScheduledPropertyPurchase,
    SetRentedFractionEvent,
)
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.testing.fixtures import checking, taxed
from finance.augur.sim.testing.simulation_result import Backend, SimulationResult

QUANTA_PER_UNIT = 100

LOCATION_ID = "loc"
LOCATIONS = {
    LOCATION_ID: Location(
        location_id=LOCATION_ID, display_name="Loc", jurisdiction_ids=[], annual_property_tax_rate=0.0
    )
}

HOME_LOCATION_ID, RENTAL_LOCATION_ID = "home_loc", "rental_loc"
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

# The lifecycle case, in the units its expectations are derived in.
LIFECYCLE_HORIZON = 36
RENTAL_SALE_MONTH = 24
RENTAL_PURCHASE_PRICE = 300_000
RENTAL_CAPEX = 30_000
RENTAL_MONTHLY_TAX = RENTAL_PURCHASE_PRICE * 0.024 / 12.0
RENTAL_BUILDING_BASIS_BEFORE = RENTAL_PURCHASE_PRICE * 0.80
RENTAL_BUILDING_BASIS_AFTER = RENTAL_BUILDING_BASIS_BEFORE + RENTAL_CAPEX
MONTHLY_DEP_BEFORE = RENTAL_BUILDING_BASIS_BEFORE / 27.5 / 12.0
MONTHLY_DEP_AFTER_HALF_RENTED = RENTAL_BUILDING_BASIS_AFTER * 0.5 / 27.5 / 12.0
MONTHLY_RENT = 2_000


def two_property_case(*, horizon_months: int = 3, rollout_count: int = 4) -> Case:
    """One financed purchase and one all-cash purchase by the same buyer.

    Four distinct stake values, so a cross-assignment between properties and a swap between
    the two columns are both visible. p1 is financed so that its equity ledger differs from
    its purchase price as well: reading the price where the ledger is meant would pass on p2
    alone. The scenario is fully deterministic, so every value must be identical across all
    rollouts and post-purchase months, which is exactly what the flattening bug breaks.
    """

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(2_000_000)), ("property_seller", Decimal(0)), ("lender", Decimal(0))),
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="buy-p1",
                    property_id="p1",
                    location_id=LOCATION_ID,
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price=Decimal(1_000_000),
                    down_payment=Decimal(200_000),
                    buyer_closing_cost=Decimal(30_000),
                    mortgage=MortgageFinancing(
                        liability_id="p1-mortgage",
                        lender_agent_id="lender",
                        principal=Decimal(800_000),
                        annual_interest_rate=0.06,
                        term_months=360,
                    ),
                ),
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="buy-p2",
                    property_id="p2",
                    location_id=LOCATION_ID,
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price=Decimal(500_000),
                    down_payment=Decimal(500_000),
                    buyer_closing_cost=Decimal(10_000),
                ),
            ],
            tax_profiles=[],
            horizon_months=horizon_months,
        ),
        rollout_count=rollout_count,
        locations=LOCATIONS,
    )


def zero_stake_case() -> Case:
    """One purchase financed to the hilt and one paid for in cash, a month apart."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(300_000)), ("property_seller", Decimal(0)), ("lender", Decimal(0))),
            scheduled_property_purchases=[
                # No down payment and no closing cost, so the stake is zero while the purchase
                # itself is fully funded.
                ScheduledPropertyPurchase(
                    month=1,
                    cause_id="buy-zero-stake",
                    property_id="zero-stake",
                    location_id=LOCATION_ID,
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price=Decimal(100_000),
                    down_payment=Decimal(0),
                    mortgage=MortgageFinancing(
                        liability_id="zero-stake-mortgage",
                        lender_agent_id="lender",
                        principal=Decimal(100_000),
                        annual_interest_rate=0.06,
                        term_months=360,
                    ),
                ),
                ScheduledPropertyPurchase(
                    month=2,
                    cause_id="buy-positive-stake",
                    property_id="positive-stake",
                    location_id=LOCATION_ID,
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price=Decimal(200_000),
                    down_payment=Decimal(200_000),
                ),
            ],
            tax_profiles=[],
            horizon_months=3,
        ),
        rollout_count=1,
        locations=LOCATIONS,
    )


def home_and_rental_case() -> Case:
    """One owner holds a primary home and a rental; only the rental changes and sells."""

    flat_home = [Decimal(1)] * (LIFECYCLE_HORIZON + 1)
    rental_values = [Decimal(1)] * RENTAL_SALE_MONTH + [Decimal("1.5")] * (LIFECYCLE_HORIZON + 1 - RENTAL_SALE_MONTH)
    return Case(
        scenario=scenario(
            checking(
                ("alice", Decimal(1_500_000)),
                ("tenant", Decimal(0)),
                ("seller", Decimal(0)),
                ("bank", Decimal(0)),
                ("county", Decimal(0)),
                ("irs", Decimal(0)),
            ),
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=RENTAL_SALE_MONTH - 1,
                    cause_id="rental-income:rental",
                    from_agent_id="tenant",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=Decimal(MONTHLY_RENT),
                    income_category=ORDINARY_INCOME,
                )
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="buy-home",
                    property_id="home",
                    location_id=HOME_LOCATION_ID,
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="seller",
                    purchase_price=Decimal(500_000),
                    down_payment=Decimal(100_000),
                    buyer_closing_cost=Decimal(0),
                    rented_fraction=0.0,
                    land_value_fraction=0.20,
                    mortgage=MortgageFinancing(
                        liability_id="home-mortgage",
                        lender_agent_id="bank",
                        principal=Decimal(400_000),
                        annual_interest_rate=0.06,
                        term_months=360,
                    ),
                ),
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="buy-rental",
                    property_id="rental",
                    location_id=RENTAL_LOCATION_ID,
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="seller",
                    purchase_price=Decimal(RENTAL_PURCHASE_PRICE),
                    down_payment=Decimal(RENTAL_PURCHASE_PRICE),
                    buyer_closing_cost=Decimal(0),
                    rented_fraction=1.0,
                    land_value_fraction=0.20,
                ),
            ],
            initial_primary_residences=[PrimaryResidenceAssignment(agent_id="alice", property_id="home")],
            property_lifecycle_events=[
                SetRentedFractionEvent(month=12, property_id="rental", rented_fraction=0.5),
                CapitalImprovementEvent(
                    month=12, property_id="rental", amount=Decimal(RENTAL_CAPEX), description="new roof"
                ),
                PropertySaleEvent(month=RENTAL_SALE_MONTH, property_id="rental", closing_cost_pct=6.0),
            ],
            property_tax_policies=[
                PropertyTaxPolicy(property_id="home", owner_agent_id="alice", tax_authority_agent_id="county"),
                PropertyTaxPolicy(property_id="rental", owner_agent_id="alice", tax_authority_agent_id="county"),
            ],
            tax_profiles=[taxed("alice", "federal_us", "california")],
            horizon_months=LIFECYCLE_HORIZON,
        ),
        rollout_count=1,
        locations=MULTI_PROPERTY_LOCATIONS,
        series={
            HomeValueKey(location_id=LocationId(HOME_LOCATION_ID)): levels([flat_home]),
            HomeValueKey(location_id=LocationId(RENTAL_LOCATION_ID)): levels([rental_values]),
        },
    )


def _tax_transfers(result: SimulationResult, prefix: str) -> pl.DataFrame:
    return result.events.transfers.filter(pl.col("cause_id").str.starts_with(prefix)).sort("month_index")


class PropertyStakeAcceptance:
    """Inherit and supply `backend`. Add nothing unless the engine owes something extra."""

    @pytest.fixture
    def backend(self) -> Backend:
        raise NotImplementedError("an acceptance module names the engine it runs")

    def test_property_stakes_are_not_cross_assigned_across_properties(self, backend: Backend) -> None:
        stakes = backend(two_property_case()).property_stakes

        # equity_ledger = purchase_price - mortgage_principal; contribution_used = down_payment
        # + closing_cost. All four differ, so no pair can be swapped without changing a value.
        expected = {
            "p1": {"contribution_used_quanta": 23_000_000, "equity_ledger_quanta": 20_000_000},
            "p2": {"contribution_used_quanta": 51_000_000, "equity_ledger_quanta": 50_000_000},
        }
        for property_id, fields in expected.items():
            rows = stakes.filter(pl.col("property_id") == property_id)
            assert rows.height > 0, f"no stake rows decoded for {property_id}"
            for column, value in fields.items():
                # Deterministic inputs ⇒ exactly one value per property across rollouts and months.
                assert set(rows.get_column(column).to_list()) == {value}, f"{property_id}.{column}"

    def test_only_a_purchase_with_a_stake_moves_the_buyer_s_cash(self, backend: Backend) -> None:
        result = backend(zero_stake_case())

        assert result.events.property_purchases.sort("month_index").select("month_index", "cause_id").to_dicts() == [
            {"month_index": 1, "cause_id": "buy-zero-stake"},
            {"month_index": 2, "cause_id": "buy-positive-stake"},
        ]
        # Only the settlement transfers: the financed purchase also pays its mortgage every
        # month, which is not what emits a buyer-cash transfer.
        buyer_cash = result.events.transfers.filter(pl.col("cause_id").str.ends_with("_buyer_cash"))
        assert buyer_cash.select("month_index", "cause_id", "amount_quanta").to_dicts() == [
            {"month_index": 2, "cause_id": "buy-positive-stake_buyer_cash", "amount_quanta": 20_000_000}
        ]

    def test_property_tax_is_charged_at_each_property_s_own_rate(self, backend: Backend) -> None:
        """Two locations, two rates, and the rental's stops at its sale while the home's does not."""

        result = backend(home_and_rental_case())

        home_tax = _tax_transfers(result, "home_property_tax_m")
        assert home_tax.get_column("month_index").to_list() == list(range(1, LIFECYCLE_HORIZON))
        assert home_tax.get_column("amount_quanta").to_list() == [50_000] * (LIFECYCLE_HORIZON - 1)

        rental_tax = _tax_transfers(result, "rental_property_tax_m")
        assert rental_tax.get_column("month_index").to_list() == list(range(1, RENTAL_SALE_MONTH))
        assert rental_tax.get_column("amount_quanta").to_list() == pytest.approx(
            [RENTAL_MONTHLY_TAX * QUANTA_PER_UNIT] * (RENTAL_SALE_MONTH - 1)
        )

    def test_a_lifecycle_event_lands_on_the_property_it_names(self, backend: Backend) -> None:
        result = backend(home_and_rental_case())

        assert result.events.set_rented_fraction_events.to_dicts() == [
            {"rollout_index": 0, "month_index": 12, "property_id": "rental", "rented_fraction": 0.5}
        ]
        assert result.events.capital_improvement_events.select(
            "rollout_index", "month_index", "property_id", "amount_quanta"
        ).to_dicts() == [{"rollout_index": 0, "month_index": 12, "property_id": "rental", "amount_quanta": 3_000_000}]

    def test_the_rental_sale_carries_its_own_basis_and_not_the_home_s_exclusion(self, backend: Backend) -> None:
        """§121 belongs to the primary residence, and alice's is `home`.

        The rental's gain is its own: purchase price plus capex less the depreciation actually
        taken, against proceeds net of closing costs. Recapture comes off the long-term gain.
        """

        result = backend(home_and_rental_case())
        cumulative_depreciation = 12 * MONTHLY_DEP_BEFORE + 12 * MONTHLY_DEP_AFTER_HALF_RENTED
        gross_proceeds = RENTAL_PURCHASE_PRICE * 1.5 * 0.94
        realized_gain = gross_proceeds - (RENTAL_PURCHASE_PRICE + RENTAL_CAPEX - cumulative_depreciation)

        assert result.events.rollout_failures.is_empty()
        sale = result.events.property_sale_events.to_dicts()[0]
        assert sale["month_index"] == RENTAL_SALE_MONTH
        assert sale["property_id"] == "rental"
        assert sale["gross_proceeds_quanta"] == pytest.approx(gross_proceeds * QUANTA_PER_UNIT, abs=100)
        assert sale["mortgage_payoff_quanta"] == 0
        assert sale["realized_gain_quanta"] == pytest.approx(realized_gain * QUANTA_PER_UNIT, abs=100)
        assert sale["depreciation_recapture_quanta"] == pytest.approx(
            cumulative_depreciation * QUANTA_PER_UNIT, abs=100
        )
        assert sale["section_121_exclusion_quanta"] == 0
        assert sale["long_term_capital_gain_quanta"] == pytest.approx(
            (realized_gain - cumulative_depreciation) * QUANTA_PER_UNIT, abs=100
        )

    def test_the_sold_property_leaves_and_the_held_one_stays(self, backend: Backend) -> None:
        result = backend(home_and_rental_case())

        terminal = result.properties.filter(pl.col("month_index") == LIFECYCLE_HORIZON)
        assert terminal.get_column("property_id").to_list() == ["home"]
        assert terminal.row(0, named=True)["adjusted_basis_quanta"] == 50_000_000

        mortgage = result.liabilities.filter(
            (pl.col("month_index") == LIFECYCLE_HORIZON) & (pl.col("liability_id") == "home-mortgage")
        )
        assert mortgage.height == 1
        assert mortgage.get_column("principal_quanta").item() > 0

    def test_each_year_deducts_only_the_depreciation_and_tax_that_year_earned(self, backend: Backend) -> None:
        """The rented share changes mid-horizon, so a deduction read from the wrong year — or
        from the wrong property — shows as a different ordinary income."""

        result = backend(home_and_rental_case())
        federal = {
            row["month_index"]: row
            for row in result.events.tax_breakdowns.filter(pl.col("jurisdiction_id") == "federal_us").iter_rows(
                named=True
            )
        }
        year_rent = 12 * MONTHLY_RENT
        year_0 = year_rent - 12 * MONTHLY_DEP_BEFORE - 11 * RENTAL_MONTHLY_TAX
        year_1 = year_rent - 12 * MONTHLY_DEP_AFTER_HALF_RENTED - 12 * RENTAL_MONTHLY_TAX * 0.5

        assert federal[11]["ordinary_income_quanta"] == pytest.approx(year_0 * QUANTA_PER_UNIT, abs=5)
        assert federal[23]["ordinary_income_quanta"] == pytest.approx(year_1 * QUANTA_PER_UNIT, abs=5)
