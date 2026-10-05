"""Configured capture and product reductions against independent financial expectations."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.series import HomeValueKey, LocationId, SecurityKey, SecuritySymbol
from finance.augur.policy.funding import ClaimPayer
from finance.augur.product.metrics import ProductMetricArrays
from finance.augur.product.simulation import (
    execute,
    project_events,
    project_product_metrics,
    simulate_events,
    simulate_product_metrics,
)
from finance.augur.sim.actions import LotSale, Sell
from finance.augur.sim.books import AccountRef
from finance.augur.sim.fixed_point import quantity_scale_for_asset, quantity_to_quanta, rate_to_ppb
from finance.augur.sim.ids import AccountId, AgentId, AssetId, JurisdictionId, LotId, PropertyId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import USD
from finance.augur.sim.property import Housing, ScheduledPurchase, ScheduledSale
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tax_indexation import FixedNominalLaw
from finance.augur.sim.tax_profile import TaxProfile, compile_profile
from finance.augur.sim.testing.scripted import Scripted
from finance.augur.sim.testing.series import level_series
from finance.augur.sim.testing.situs import UNTAXED
from finance.augur.sim.world import Capture, World

CHECKING = AccountId("checking")

AGENT = AgentId("alice")
IRS = AgentId("irs")
SELLER = AgentId("seller")
HORIZON_MONTHS = 30
SALE_MONTH = 14
UNITS = 2
LOT_BASIS = Decimal(10_000)
SALE_PRICE = Decimal(60_000)
VTI = SecurityKey(symbol=SecuritySymbol("vti"))

LOCATION = LocationId("acceptance-town")
HOME_VALUE = HomeValueKey(location_id=LOCATION)
PROPERTY_SALE_MONTH = 12
# Sampled levels that are not a whole number of cents. A level already on a cent reads the same
# out of either representation, which is exactly what the property assertion has to rule out.
HOME_VALUE_AT_PURCHASE = 400_000.004
HOME_VALUE_AT_SALE = 600_000.007
# The same levels as money: cents, rounded half up, which is how the simulator's one
# float-to-money boundary quantizes a sampled path.
PURCHASE_PRICE = Decimal("400000.00")
HOME_VALUE_AT_SALE_QUANTA = 60_000_001

# 6.375% is 637.5 basis points -- representable as a rate, never as a whole number of them.
FRACTIONAL_CLOSING_COST_PCT = Decimal("6.375")
FRACTIONAL_CLOSING_COST_PROCEEDS_QUANTA = 56_175_001

# Fresh, unstarted worlds, one per path: a world runs once, so every simulation composes its own.
type Worlds = Callable[[], list[World]]


def sale_and_tax_year(*, rollout_count: int = 1) -> Worlds:
    """One long-term lot sold mid-horizon, and the tax year that closes after it.

    Small on purpose. The contract below is about shapes, schemas and consequences the
    situation forces, and a case whose rows a reader can count says more about a violation
    than a feature-rich one.
    """

    scale = quantity_scale_for_asset(VTI)
    lot_id, asset_id, units = LotId("alice-vti"), AssetId(VTI.symbol), quantity_to_quanta(UNITS, scale=scale)
    sale = Sell(
        cause_id="sell-vti",
        agent_id=AGENT,
        proceeds_account_id=CHECKING,
        asset_id=asset_id,
        lots=(LotSale(account_id=CHECKING, lot_id=lot_id, units=units),),
    )
    profile = TaxProfile(agent_id=AGENT, jurisdiction_ids=[JurisdictionId("federal_us")], tax_authority_agent_id=IRS)
    jurisdictions = load_jurisdictions_for([profile])
    series = level_series(
        {VTI: np.full((rollout_count, HORIZON_MONTHS + 1), float(SALE_PRICE))},
        rollout_count=rollout_count,
        horizon_months=HORIZON_MONTHS,
    )

    def compose(rollout_id: int) -> World:
        world = World(
            MarketPath(series, rollout_id, rollout_count=rollout_count),
            horizon_months=HORIZON_MONTHS,
            income_sources=(ORDINARY_INCOME,),
        )
        for agent_id in (AGENT, IRS):
            world.declare_account(account=AccountRef(agent_id=agent_id, account_id=CHECKING), opening_balance=0)
        world.track(TaxAuthority(compile_profile(profile, jurisdictions, currency=USD), indexation=FixedNominalLaw()))
        world.declare_pool(agent_id=AGENT, account_id=CHECKING, asset_id=asset_id, quantity_scale=scale)
        world.hold_lot(
            lot_id=lot_id,
            agent_id=AGENT,
            account_id=CHECKING,
            asset_id=asset_id,
            purchase_month=-24,  # comfortably long-term
            quantity_scale=scale,
            units=units,
            basis=USD.quanta(UNITS * LOT_BASIS),
        )
        world.track(Scripted(ClaimPayer(AgentId(AGENT)), {SALE_MONTH: (sale,)}))
        return world

    return lambda: [compose(rollout_id) for rollout_id in range(rollout_count)]


def a_property_bought_and_sold(closing_cost_pct: Decimal = Decimal(0)) -> Worlds:
    """One all-cash property, bought at what it is worth and sold while it is worth more.

    The home-value levels are deliberately not whole cents. A property is valued from that
    series in two places — the net-worth series and the sale — and a level carries an exact
    integer representation beside its sampled float, so which one an engine reads is
    observable exactly when the level is fractional.
    """

    purchase = ScheduledPurchase(
        month=0,
        cause_id="buy-house",
        property_id=PropertyId("house"),
        parcel=UNTAXED,
        market=LOCATION,
        buyer_agent_id=AGENT,
        buyer_account_id=CHECKING,
        seller_agent_id=SELLER,
        seller_account_id=CHECKING,
        # Bought for exactly what the series says it is worth, so the sale's proceeds are
        # the home value itself rather than a figure a reader has to recompute.
        purchase_price=USD.quanta(PURCHASE_PRICE),
        down_payment=USD.quanta(PURCHASE_PRICE),
        buyer_closing_cost=0,
        rented_fraction_ppb=0,
        land_value_fraction_ppb=rate_to_ppb(Decimal("0.20")),
        mortgage=None,
    )
    levels = np.full((1, HORIZON_MONTHS + 1), HOME_VALUE_AT_PURCHASE)
    levels[:, PROPERTY_SALE_MONTH] = HOME_VALUE_AT_SALE
    series = level_series({HOME_VALUE: levels}, rollout_count=1, horizon_months=HORIZON_MONTHS)

    def compose() -> World:
        # Untaxed on purpose: what the gain is assessed at is the statute suites' business, and
        # here it would only put a bracket walk between the series and the number under test.
        world = World(
            MarketPath(series, 0, rollout_count=1), horizon_months=HORIZON_MONTHS, income_sources=(ORDINARY_INCOME,)
        )
        for agent_id in (AGENT, SELLER):
            world.declare_account(
                account=AccountRef(agent_id=agent_id, account_id=CHECKING),
                opening_balance=USD.quanta(Decimal(1_000_000)),
            )
        world.declare_housing(
            Housing(
                purchases=(purchase,),
                sales=(
                    ScheduledSale(
                        month=PROPERTY_SALE_MONTH,
                        property_id=PropertyId("house"),
                        commission_ppb=rate_to_ppb(closing_cost_pct / 100),
                        escrow_title_ppb=0,
                    ),
                ),
            ),
            (),
        )
        world.track(ClaimPayer(AgentId(AGENT)))
        return world

    return lambda: [compose()]


def product_metrics(worlds: Worlds) -> ProductMetricArrays:
    return simulate_product_metrics(worlds(), horizon_months=HORIZON_MONTHS, currency=USD, primary_agent_id=AGENT)


class TestConfigured:
    @pytest.fixture(scope="class")
    def run(self) -> Worlds:
        return sale_and_tax_year()

    @pytest.fixture(scope="class")
    def property_run(self) -> Worlds:
        return a_property_bought_and_sold()

    @pytest.fixture(scope="class")
    def fractional_closing_cost_run(self) -> Worlds:
        return a_property_bought_and_sold(closing_cost_pct=FRACTIONAL_CLOSING_COST_PCT)

    def test_the_sale_is_reported_as_a_disposition(self, run: Worlds) -> None:
        """Proceeds and basis follow from the scenario, so every engine owes the same ones."""

        rows = simulate_events(run(), AGENT).lot_dispositions.filter(pl.col("month_index") == SALE_MONTH).to_dicts()
        assert len(rows) == 1, f"one lot sold once, got {len(rows)} rows"
        sold = rows[0]
        assert sold["agent_id"] == AGENT
        assert sold["units_sold"] == UNITS
        assert sold["proceeds_quanta"] == int(SALE_PRICE * 100) * UNITS
        assert sold["cost_basis_consumed_quanta"] == int(LOT_BASIS * 100) * UNITS

    def test_the_gain_is_assessed_at_the_tax_year_that_closes_after_it(self, run: Worlds) -> None:
        """A realized gain reaches an accrual. Which figure is the statute suites' business."""

        accruals = simulate_events(run(), AGENT).tax_accruals.filter(pl.col("agent_id") == AGENT)
        assert accruals.height, "a long-term gain went unassessed"
        assert accruals.filter(pl.col("month_index") > SALE_MONTH).height, "no accrual after the sale"

    def test_a_funded_rollout_does_not_report_a_failure(self, run: Worlds) -> None:
        """Anti-vacuity for the assertions above: they describe a rollout that ran to the end."""

        assert int(product_metrics(run).failed_month[0]) < 0
        assert simulate_events(run(), AGENT).rollout_failures.height == 0

    def test_a_property_sells_for_what_the_series_says_it_is_worth(self, property_run: Worlds) -> None:
        """A money level is money, whichever cube an engine happens to keep it in.

        The house was bought at the month-0 home value and sold at 0% closing cost, so its gross
        proceeds are the home value at the sale month — as quanta, since that is what a money
        series is. Reading the sampled float instead lands a cent low here, and the same way for
        every fractional level a real sampled path produces.
        """

        rows = simulate_events(property_run(), AGENT).property_sale_events.to_dicts()
        assert len(rows) == 1, f"one property sold once, got {len(rows)} rows"
        assert rows[0]["month_index"] == PROPERTY_SALE_MONTH
        assert rows[0]["gross_proceeds_quanta"] == HOME_VALUE_AT_SALE_QUANTA, (
            f"sold for {rows[0]['gross_proceeds_quanta']} quanta, not the {HOME_VALUE_AT_SALE_QUANTA} "
            "the home value is worth"
        )

    def test_a_closing_cost_finer_than_a_basis_point_is_charged_exactly(
        self, fractional_closing_cost_run: Worlds
    ) -> None:
        """A rate is a rate; nothing about it has to land on a hundredth of a percent.

        6.375% of the sale is 56,175,000.93625 quanta of cost against a 60,000,001 quanta
        house, so the seller keeps 56,175,001 -- the rounding, once, where the rate becomes
        money. The scenario is authorable at all only because closing costs cross on the same
        grid as every other rate; spelled in basis points this one had no exact form and the
        whole scenario was refused.
        """

        rows = simulate_events(fractional_closing_cost_run(), AGENT).property_sale_events.to_dicts()
        assert len(rows) == 1, f"one property sold once, got {len(rows)} rows"
        assert rows[0]["gross_proceeds_quanta"] == FRACTIONAL_CLOSING_COST_PROCEEDS_QUANTA


@pytest.mark.parametrize("capture", ["summary", "dense", "forensic"])
def test_completed_capture_projects_same_financial_metrics(capture: Capture) -> None:
    run = sale_and_tax_year()
    completed = execute(run(), capture, AGENT)
    arrays = project_product_metrics(completed, horizon_months=HORIZON_MONTHS, currency=USD)
    compact = product_metrics(run)

    assert arrays.rollout_ids == compact.rollout_ids
    np.testing.assert_array_equal(arrays.failed_month, compact.failed_month)
    for actual, expected in zip(arrays.base_series, compact.base_series, strict=True):
        np.testing.assert_array_equal(actual, expected)
    if capture == "summary":
        assert all(result.financial is None and result.events is None for result in completed)
        with pytest.raises(RuntimeError, match="event projection requires"):
            project_events(completed)
    else:
        assert project_events(completed) == simulate_events(run(), AGENT)
        assert all(result.financial is not None for result in completed)
        for result in completed:
            assert result.financial is not None
            assert bool(result.financial.journal) == (capture == "forensic")


def test_projection_preserves_selected_original_path_identity() -> None:
    run = sale_and_tax_year(rollout_count=3)
    completed = execute(run(), "dense", AGENT)
    selected = (completed[-1], completed[0])
    arrays = project_product_metrics(selected, horizon_months=HORIZON_MONTHS, currency=USD)
    expected = project_product_metrics(completed, horizon_months=HORIZON_MONTHS, currency=USD).select(
        tuple(result.rollout_id for result in selected)
    )

    assert arrays.rollout_ids == project_events(selected).rollout_ids == expected.rollout_ids
    for actual, block in zip(arrays.base_series, expected.base_series, strict=True):
        np.testing.assert_array_equal(actual, block)


if __name__ == "__main__":
    pytest_bazel.main()
