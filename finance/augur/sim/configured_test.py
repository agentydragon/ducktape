"""Configured capture and product reductions against independent financial expectations."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.series import HomeValueKey, LocationId, SecurityKey, SecuritySymbol
from finance.augur.sim import configured
from finance.augur.sim.compiler.execution import compile_run
from finance.augur.sim.configured import (
    execute,
    project_events,
    project_product_metrics,
    simulate_events,
    simulate_product_metrics,
)
from finance.augur.sim.events import EVENT_FRAME_SPECS
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.locations import Location
from finance.augur.sim.metric_composition import METRIC_NAMES
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.product_metrics import metric_fan, projection_summaries, terminal_summary
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    InitialLot,
    PropertySaleEvent,
    Scenario,
    ScheduledAssetSale,
    ScheduledPropertyPurchase,
    TaxProfile,
)
from finance.augur.sim.session import Capture

AGENT = "alice"
HORIZON_MONTHS = 30
SALE_MONTH = 14
UNITS = 2.0
LOT_BASIS = Decimal(10_000)
SALE_PRICE = Decimal(60_000)
VTI = SecurityKey(symbol=SecuritySymbol("vti"))

LOCATION = "acceptance-town"
HOME_VALUE = HomeValueKey(location_id=LocationId(LOCATION))
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


def sale_and_tax_year(*, rollout_count: int = 1) -> CompiledRun:
    """One long-term lot sold mid-horizon, and the tax year that closes after it.

    Small on purpose. The contract below is about shapes, schemas and consequences the
    scenario forces, and a case whose rows a reader can count says more about a violation
    than a feature-rich one.
    """

    scenario = Scenario(
        agents=[Agent(agent_id=AGENT), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id=agent_id, account_id="checking", balance=Decimal(0))
            for agent_id in (AGENT, "irs")
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice-vti",
                agent_id=AGENT,
                account_id="checking",
                asset=VTI,
                purchase_month_index=-24,  # comfortably long-term
                quantity=UNITS,
                cost_basis=Decimal(str(UNITS)) * LOT_BASIS,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=SALE_MONTH,
                cause_id="sell-vti",
                agent_id=AGENT,
                source_account_id="checking",
                asset=VTI,
                quantity=UNITS,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[TaxProfile(agent_id=AGENT, jurisdiction_ids=["federal_us"], tax_authority_agent_id="irs")],
        horizon_months=HORIZON_MONTHS,
    )
    external_series = ExternalSeriesContext.from_level_blocks(
        [(VTI, np.full((rollout_count, HORIZON_MONTHS + 1), float(SALE_PRICE)))],
        rollout_count=rollout_count,
        horizon_months=HORIZON_MONTHS,
    )
    jurisdictions = load_jurisdictions_for(scenario)
    return compile_run(
        scenario,
        rollout_count=rollout_count,
        external_series=external_series,
        jurisdictions=jurisdictions,
        locations={},
    )


def a_property_bought_and_sold(closing_cost_pct: float = 0.0) -> CompiledRun:
    """One all-cash property, bought at what it is worth and sold while it is worth more.

    The home-value levels are deliberately not whole cents. A property is valued from that
    series in two places — the net-worth series and the sale — and a level carries an exact
    integer representation beside its sampled float, so which one an engine reads is
    observable exactly when the level is fractional.
    """

    scenario = Scenario(
        agents=[Agent(agent_id=AGENT), Agent(agent_id="seller")],
        initial_cash=[
            InitialAccountBalance(agent_id=agent_id, account_id="checking", balance=Decimal(1_000_000))
            for agent_id in (AGENT, "seller")
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
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
        ],
        property_lifecycle_events=[
            PropertySaleEvent(month=PROPERTY_SALE_MONTH, property_id="house", closing_cost_pct=closing_cost_pct)
        ],
        # Untaxed on purpose: what the gain is assessed at is the statute suites' business, and
        # here it would only put a bracket walk between the series and the number under test.
        tax_profiles=[],
        horizon_months=HORIZON_MONTHS,
    )
    levels = np.full((1, HORIZON_MONTHS + 1), HOME_VALUE_AT_PURCHASE)
    levels[:, PROPERTY_SALE_MONTH] = HOME_VALUE_AT_SALE
    external_series = ExternalSeriesContext.from_level_blocks(
        [(HOME_VALUE, levels)], rollout_count=1, horizon_months=HORIZON_MONTHS
    )
    locations = {
        LOCATION: Location(
            location_id=LOCATION, display_name="Acceptance Town", jurisdiction_ids=[], annual_property_tax_rate=0.0
        )
    }
    jurisdictions = load_jurisdictions_for(scenario)
    return compile_run(
        scenario, rollout_count=1, external_series=external_series, jurisdictions=jurisdictions, locations=locations
    )


class TestConfigured:
    @pytest.fixture(scope="class")
    def run(self) -> CompiledRun:
        return sale_and_tax_year()

    @pytest.fixture(scope="class")
    def property_run(self) -> CompiledRun:
        return a_property_bought_and_sold()

    @pytest.fixture(scope="class")
    def fractional_closing_cost_run(self) -> CompiledRun:
        return a_property_bought_and_sold(closing_cost_pct=FRACTIONAL_CLOSING_COST_PCT)

    def test_events_carry_every_canonical_frame(self, run: CompiledRun) -> None:
        """A frame an engine omits reads downstream as "nothing happened", not as a gap."""

        events = simulate_events(run)
        for spec in EVENT_FRAME_SPECS:
            frame = getattr(events, spec.name)
            assert isinstance(frame, pl.DataFrame), f"{spec.name} is not a frame"
            assert frame.schema == spec.schema, f"{spec.name} does not match its declared schema"

    def test_the_scheduled_sale_is_reported_as_a_disposition(self, run: CompiledRun) -> None:
        """Proceeds and basis follow from the scenario, so every engine owes the same ones."""

        rows = simulate_events(run).lot_dispositions.filter(pl.col("month_index") == SALE_MONTH).to_dicts()
        assert len(rows) == 1, f"one lot sold once, got {len(rows)} rows"
        sold = rows[0]
        assert sold["agent_id"] == AGENT
        assert sold["units_sold"] == UNITS
        assert sold["proceeds_quanta"] == int(SALE_PRICE * 100) * UNITS
        assert sold["cost_basis_consumed_quanta"] == int(LOT_BASIS * 100) * UNITS

    def test_the_gain_is_assessed_at_the_tax_year_that_closes_after_it(self, run: CompiledRun) -> None:
        """A realized gain reaches an accrual. Which figure is the statute suites' business."""

        accruals = simulate_events(run).tax_accruals.filter(pl.col("agent_id") == AGENT)
        assert accruals.height, "a long-term gain went unassessed"
        assert accruals.filter(pl.col("month_index") > SALE_MONTH).height, "no accrual after the sale"

    def test_product_metrics_cover_every_metric_the_product_renders(self, run: CompiledRun) -> None:
        """Configured base series support every derived product metric."""

        metrics = simulate_product_metrics(run, primary_agent_id=AGENT)
        arrays = metrics.metric_arrays()
        assert set(arrays) == {"month_index", *METRIC_NAMES}
        assert len(arrays["month_index"]) == HORIZON_MONTHS + 1
        for name in METRIC_NAMES:
            assert arrays[name].shape == (HORIZON_MONTHS + 1, 1), f"{name} is not snapshots by rollouts"
        assert metrics.failed_month.shape == (1,)
        assert metrics.currency_code == run.currency_code

    def test_a_funded_rollout_does_not_report_a_failure(self, run: CompiledRun) -> None:
        """Anti-vacuity for the assertions above: they describe a rollout that ran to the end."""

        assert int(simulate_product_metrics(run, primary_agent_id=AGENT).failed_month[0]) < 0
        assert simulate_events(run).rollout_failures.height == 0

    def test_the_fan_is_ordered_and_agrees_with_the_terminal_samples(self, run: CompiledRun) -> None:
        """The two reductions are of one population, so the fan must sit inside its range.

        An engine that reduced the wrong axis, or reduced a different run, passes every shape
        assertion above and fails this one.
        """

        percentiles = (5.0, 50.0, 95.0)
        fan = metric_fan(
            simulate_product_metrics(run, primary_agent_id=AGENT), metric="cash_quanta", percentiles=percentiles
        )
        samples = terminal_summary(
            simulate_product_metrics(run, primary_agent_id=AGENT), metric="cash_quanta"
        ).terminal_samples

        assert fan.percentiles == percentiles
        assert fan.monthly_percentiles.shape == (HORIZON_MONTHS + 1, len(percentiles))
        assert fan.terminal_percentiles is not None
        assert list(fan.terminal_percentiles) == sorted(fan.terminal_percentiles), "percentiles must not decrease"
        assert min(samples) <= min(fan.terminal_percentiles)
        assert max(fan.terminal_percentiles) <= max(samples)

    def test_a_property_sells_for_what_the_series_says_it_is_worth(self, property_run: CompiledRun) -> None:
        """A money level is money, whichever cube an engine happens to keep it in.

        The house was bought at the month-0 home value and sold at 0% closing cost, so its gross
        proceeds are the home value at the sale month — as quanta, since that is what a money
        series is. Reading the sampled float instead lands a cent low here, and the same way for
        every fractional level a real sampled path produces.
        """

        rows = simulate_events(property_run).property_sale_events.to_dicts()
        assert len(rows) == 1, f"one property sold once, got {len(rows)} rows"
        assert rows[0]["month_index"] == PROPERTY_SALE_MONTH
        assert rows[0]["gross_proceeds_quanta"] == HOME_VALUE_AT_SALE_QUANTA, (
            f"sold for {rows[0]['gross_proceeds_quanta']} quanta, not the {HOME_VALUE_AT_SALE_QUANTA} "
            "the home value is worth"
        )

    def test_a_closing_cost_finer_than_a_basis_point_is_charged_exactly(
        self, fractional_closing_cost_run: CompiledRun
    ) -> None:
        """A rate is a rate; nothing about it has to land on a hundredth of a percent.

        6.375% of the sale is 56,175,000.93625 quanta of cost against a 60,000,001 quanta
        house, so the seller keeps 56,175,001 -- the rounding, once, where the rate becomes
        money. The scenario is authorable at all only because closing costs cross on the same
        grid as every other rate; spelled in basis points this one had no exact form and the
        whole scenario was refused.
        """

        rows = simulate_events(fractional_closing_cost_run).property_sale_events.to_dicts()
        assert len(rows) == 1, f"one property sold once, got {len(rows)} rows"
        assert rows[0]["gross_proceeds_quanta"] == FRACTIONAL_CLOSING_COST_PROCEEDS_QUANTA

    def test_combined_and_separate_summaries_agree(self, run: CompiledRun) -> None:
        """Combined and separate reducers agree on the same captured population."""

        percentiles = (5.0, 50.0, 95.0)
        arrays = simulate_product_metrics(run, primary_agent_id=AGENT)
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
    completed = execute(run, capture, product_actor=AGENT)
    arrays = project_product_metrics(run, completed)
    compact = simulate_product_metrics(run, primary_agent_id=AGENT)

    assert arrays.rollout_ids == compact.rollout_ids
    np.testing.assert_array_equal(arrays.failed_month, compact.failed_month)
    for actual, expected in zip(arrays.base_series, compact.base_series, strict=True):
        np.testing.assert_array_equal(actual, expected)
    if capture == "summary":
        assert all(result.financial is None and result.events is None for result in completed)
        with pytest.raises(RuntimeError, match="event projection requires"):
            project_events(completed)
    else:
        assert all(result.configured_summary is None for result in completed)
        assert project_events(completed) == simulate_events(run)
        assert all(result.financial is not None for result in completed)
        for result in completed:
            assert result.financial is not None
            assert bool(result.financial.journal) == (capture == "forensic")


def test_projection_preserves_selected_original_path_identity() -> None:
    run = sale_and_tax_year(rollout_count=3)
    completed = execute(run, "dense", product_actor=AGENT)
    selected = (completed[-1], completed[0])
    arrays = project_product_metrics(run, selected)
    expected = project_product_metrics(run, completed).select(tuple(result.rollout_id for result in selected))

    assert arrays.rollout_ids == project_events(selected).rollout_ids == expected.rollout_ids
    for actual, block in zip(arrays.base_series, expected.base_series, strict=True):
        np.testing.assert_array_equal(actual, block)


def test_in_process_events_do_not_export_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_export(*_args, **_kwargs):
        raise AssertionError("in-process event projection must not serialize a configured artifact")

    monkeypatch.setattr(configured, "export_results", reject_export)
    events = simulate_events(sale_and_tax_year())
    assert events.lot_dispositions.height > 0
    assert events.tax_accruals.height > 0


def test_metrics_require_product_capture() -> None:
    run = sale_and_tax_year()
    completed = execute(run, "dense")
    with pytest.raises(ValueError, match="product snapshots, expected"):
        project_product_metrics(run, completed)


if __name__ == "__main__":
    pytest_bazel.main()
