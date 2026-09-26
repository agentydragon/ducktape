"""Configured capture and product reductions against independent financial expectations."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.series import HomeValueKey, LocationId, SecurityKey, SecuritySymbol
from finance.augur.policy.configured_household import ConfiguredHousehold
from finance.augur.product.metric_composition import METRIC_NAMES
from finance.augur.product.metrics import ProductMetricArrays, metric_fan, projection_summaries, terminal_summary
from finance.augur.product.simulation import (
    execute,
    project_events,
    project_product_metrics,
    simulate_events,
    simulate_product_metrics,
)
from finance.augur.sim.compiler.execution import (
    compile_accounts,
    compile_holding_pools,
    compile_housing,
    compile_jurisdictions,
    compile_locations,
    compile_lots,
    compile_series,
)
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.events import EVENT_FRAME_SPECS
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.ids import AccountId, AgentId, AssetId
from finance.augur.sim.locations import Location
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import _ScheduledSale
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Currency,
    InitialAccountBalance,
    InitialLot,
    PropertySaleEvent,
    ScheduledPropertyPurchase,
    TaxProfile,
)
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import Capture, World

AGENT = AgentId("alice")
CURRENCY = Currency()
HORIZON_MONTHS = 30
SALE_MONTH = 14
UNITS = 2.0
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
FRACTIONAL_CLOSING_COST_PCT = 6.375
FRACTIONAL_CLOSING_COST_PROCEEDS_QUANTA = 56_175_001

# Fresh, unstarted worlds, one per path: a world runs once, so every simulation composes its own.
type Worlds = Callable[[], list[World]]


def sale_and_tax_year(*, rollout_count: int = 1) -> Worlds:
    """One long-term lot sold mid-horizon, and the tax year that closes after it.

    Small on purpose. The contract below is about shapes, schemas and consequences the
    situation forces, and a case whose rows a reader can count says more about a violation
    than a feature-rich one.
    """

    lot = InitialLot(
        lot_id="alice-vti",
        agent_id=AGENT,
        account_id="checking",
        asset=VTI,
        purchase_month_index=-24,  # comfortably long-term
        quantity=UNITS,
        cost_basis=Decimal(str(UNITS)) * LOT_BASIS,
    )
    sale = _ScheduledSale(
        month=SALE_MONTH,
        cause_id="sell-vti",
        agent_id=AGENT,
        account_id=AccountId("checking"),
        asset_id=AssetId(VTI.symbol),
        units=int(quantity_to_quanta(UNITS, scale=quantity_scale_for_asset(VTI))),
        proceeds_account_id=AccountId("checking"),
    )
    profile = TaxProfile(agent_id=AGENT, jurisdiction_ids=["federal_us"], tax_authority_agent_id="irs")
    jurisdictions = load_jurisdictions_for([profile])
    series = compile_series(
        ExternalSeriesContext.from_level_blocks(
            [(VTI, np.full((rollout_count, HORIZON_MONTHS + 1), float(SALE_PRICE)))],
            rollout_count=rollout_count,
            horizon_months=HORIZON_MONTHS,
        ),
        rollout_count=rollout_count,
        horizon_months=HORIZON_MONTHS,
        currency_quantum=CURRENCY.quantum,
    )

    def compose(rollout_id: int) -> World:
        world = World(
            MarketPath(series, rollout_id, rollout_count=rollout_count),
            horizon_months=HORIZON_MONTHS,
            income_sources=(ORDINARY_INCOME,),
            jurisdictions=compile_jurisdictions(jurisdictions, bonds=(), distributions=()),
        )
        for account in compile_accounts(
            [
                InitialAccountBalance(agent_id=agent_id, account_id="checking", balance=Decimal(0))
                for agent_id in (AGENT, "irs")
            ],
            quantum=CURRENCY.quantum,
        ):
            world.declare_account(account)
        world.track(TaxAuthority(compile_profile(profile, jurisdictions, quantum=CURRENCY.quantum)))
        for pool in compile_holding_pools(lots=[lot]):
            world.declare_pool(pool)
        for held in compile_lots([lot], quantum=CURRENCY.quantum):
            world.hold(held)
        household = ConfiguredHousehold(AgentId(AGENT), (), scheduled_sales=(sale,))
        household.check(world)
        world.track(household)
        return world

    return lambda: [compose(rollout_id) for rollout_id in range(rollout_count)]


def a_property_bought_and_sold(closing_cost_pct: float = 0.0) -> Worlds:
    """One all-cash property, bought at what it is worth and sold while it is worth more.

    The home-value levels are deliberately not whole cents. A property is valued from that
    series in two places — the net-worth series and the sale — and a level carries an exact
    integer representation beside its sampled float, so which one an engine reads is
    observable exactly when the level is fractional.
    """

    purchase = ScheduledPropertyPurchase(
        month=0,
        cause_id="buy-house",
        property_id="house",
        location_id=LOCATION,
        buyer_agent_id=AGENT,
        buyer_account_id="checking",
        seller_agent_id="seller",
        # Bought for exactly what the series says it is worth, so the sale's proceeds are
        # the home value itself rather than a figure a reader has to recompute.
        purchase_price=PURCHASE_PRICE,
        down_payment=PURCHASE_PRICE,
    )
    levels = np.full((1, HORIZON_MONTHS + 1), HOME_VALUE_AT_PURCHASE)
    levels[:, PROPERTY_SALE_MONTH] = HOME_VALUE_AT_SALE
    series = compile_series(
        ExternalSeriesContext.from_level_blocks([(HOME_VALUE, levels)], rollout_count=1, horizon_months=HORIZON_MONTHS),
        rollout_count=1,
        horizon_months=HORIZON_MONTHS,
        currency_quantum=CURRENCY.quantum,
    )
    location = Location(
        location_id=LOCATION, display_name="Acceptance Town", jurisdiction_ids=[], annual_property_tax_rate=0.0
    )

    def compose() -> World:
        # Untaxed on purpose: what the gain is assessed at is the statute suites' business, and
        # here it would only put a bracket walk between the series and the number under test.
        world = World(
            MarketPath(series, 0, rollout_count=1), horizon_months=HORIZON_MONTHS, income_sources=(ORDINARY_INCOME,)
        )
        for account in compile_accounts(
            [
                InitialAccountBalance(agent_id=agent_id, account_id="checking", balance=Decimal(1_000_000))
                for agent_id in (AGENT, "seller")
            ],
            quantum=CURRENCY.quantum,
        ):
            world.declare_account(account)
        world.declare_housing(
            compile_housing(
                purchases=[purchase],
                initial_residences=(),
                residence_events=(),
                lifecycle_events=[
                    PropertySaleEvent(month=PROPERTY_SALE_MONTH, property_id="house", closing_cost_pct=closing_cost_pct)
                ],
                quantum=CURRENCY.quantum,
            ),
            (),
            compile_locations([purchase], {LOCATION: location}, quantum=CURRENCY.quantum),
        )
        world.track(ConfiguredHousehold(AgentId(AGENT), ()))
        return world

    return lambda: [compose()]


def product_metrics(worlds: Worlds) -> ProductMetricArrays:
    return simulate_product_metrics(worlds(), horizon_months=HORIZON_MONTHS, currency=CURRENCY, primary_agent_id=AGENT)


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

    def test_events_carry_every_canonical_frame(self, run: Worlds) -> None:
        """A frame an engine omits reads downstream as "nothing happened", not as a gap."""

        events = simulate_events(run(), AGENT)
        for spec in EVENT_FRAME_SPECS:
            frame = getattr(events, spec.name)
            assert isinstance(frame, pl.DataFrame), f"{spec.name} is not a frame"
            assert frame.schema == spec.schema, f"{spec.name} does not match its declared schema"

    def test_the_scheduled_sale_is_reported_as_a_disposition(self, run: Worlds) -> None:
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

    def test_product_metrics_cover_every_metric_the_product_renders(self, run: Worlds) -> None:
        """Configured base series support every derived product metric."""

        metrics = product_metrics(run)
        arrays = metrics.metric_arrays()
        assert set(arrays) == {"month_index", *METRIC_NAMES}
        assert len(arrays["month_index"]) == HORIZON_MONTHS + 1
        for name in METRIC_NAMES:
            assert arrays[name].shape == (HORIZON_MONTHS + 1, 1), f"{name} is not snapshots by rollouts"
        assert metrics.failed_month.shape == (1,)
        assert metrics.currency_code == CURRENCY.code

    def test_a_funded_rollout_does_not_report_a_failure(self, run: Worlds) -> None:
        """Anti-vacuity for the assertions above: they describe a rollout that ran to the end."""

        assert int(product_metrics(run).failed_month[0]) < 0
        assert simulate_events(run(), AGENT).rollout_failures.height == 0

    def test_the_fan_is_ordered_and_agrees_with_the_terminal_samples(self, run: Worlds) -> None:
        """The two reductions are of one population, so the fan must sit inside its range.

        An engine that reduced the wrong axis, or reduced a different run, passes every shape
        assertion above and fails this one.
        """

        percentiles = (5.0, 50.0, 95.0)
        fan = metric_fan(product_metrics(run), metric="cash_quanta", percentiles=percentiles)
        samples = terminal_summary(product_metrics(run), metric="cash_quanta").terminal_samples

        assert fan.percentiles == percentiles
        assert fan.monthly_percentiles.shape == (HORIZON_MONTHS + 1, len(percentiles))
        assert fan.terminal_percentiles is not None
        assert list(fan.terminal_percentiles) == sorted(fan.terminal_percentiles), "percentiles must not decrease"
        assert min(samples) <= min(fan.terminal_percentiles)
        assert max(fan.terminal_percentiles) <= max(samples)

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

    def test_combined_and_separate_summaries_agree(self, run: Worlds) -> None:
        """Combined and separate reducers agree on the same captured population."""

        percentiles = (5.0, 50.0, 95.0)
        arrays = product_metrics(run)
        both = projection_summaries(arrays, metric="cash_quanta", percentiles=percentiles)
        separate = metric_fan(arrays, metric="cash_quanta", percentiles=percentiles)
        assert both.metric_fan.terminal_percentiles is not None
        assert separate.terminal_percentiles is not None
        assert list(both.metric_fan.terminal_percentiles) == list(separate.terminal_percentiles)
        assert list(both.terminal_distribution.terminal_samples) == list(
            terminal_summary(arrays, metric="cash_quanta").terminal_samples
        )


@pytest.mark.parametrize("capture", ["summary", "dense", "forensic"])
def test_completed_capture_projects_same_financial_metrics(capture: Capture) -> None:
    run = sale_and_tax_year()
    completed = execute(run(), capture, AGENT)
    arrays = project_product_metrics(completed, horizon_months=HORIZON_MONTHS, currency=CURRENCY)
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
    arrays = project_product_metrics(selected, horizon_months=HORIZON_MONTHS, currency=CURRENCY)
    expected = project_product_metrics(completed, horizon_months=HORIZON_MONTHS, currency=CURRENCY).select(
        tuple(result.rollout_id for result in selected)
    )

    assert arrays.rollout_ids == project_events(selected).rollout_ids == expected.rollout_ids
    for actual, block in zip(arrays.base_series, expected.base_series, strict=True):
        np.testing.assert_array_equal(actual, block)


if __name__ == "__main__":
    pytest_bazel.main()
