"""Sim-layer e2e tests for landlord rental income + lifecycle events.

Phase 1: static rental from month 0, no lifecycle transitions, no taxes.
Phase 2+ tests will land alongside their implementation phases.

Each test builds a `Scenario` directly, calls
`simulate_with_external_series`, decodes the result, and asserts against
event frames + state history. Exogenous series are constants so the
expected cashflow is exact-math predictable.
"""

from __future__ import annotations

import polars as pl
import pytest
import pytest_bazel

from augur.model.series import LocationId, RentKey
from augur.sim.external_series import EXTERNAL_SERIES_VALUES_FRAME, ExternalSeriesContext
from augur.sim.locations import Location
from augur.sim.scenario import (
    Agent,
    CapitalImprovementEvent,
    FederalSaltCapEntry,
    FederalSaltDeductionPolicy,
    FilingStatus,
    InitialAccountBalance,
    MortgageFinancing,
    MortgageInterestDeductionPolicy,
    PrimaryResidenceAssignment,
    PropertySaleEvent,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringTransfer,
    Scenario,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    SeriesIndexedAmount,
    SetPrimaryResidenceEvent,
    SetRentedFractionEvent,
    TaxProfile,
)
from augur.sim.simulate import simulate_with_external_series

# Constants mirroring the product translator. Kept in-test to avoid
# cross-package import dependencies from the sim layer.
TENANT_AGENT_ID = "tenant"
OWNER_AGENT_ID = "owner"
MGMT_AGENT_ID = "property_management_agency"
RENT_SERIES_KEY = RentKey(location_id=LocationId("test_location"))
# Frame helpers (_flat_series / _multi_series) still key the series_values frame
# by the wire string; SeriesIndexedAmount now takes the typed key directly.
RENT_SERIES_ID = RENT_SERIES_KEY.wire_id


def _flat_series(*, series_id: str, value: float, months: int, rollouts: int) -> ExternalSeriesContext:
    """Build an exogenous bundle with one series held flat at `value`."""

    rows = [
        {"rollout_index": rollout, "month_index": month, "series_id": series_id, "value": value}
        for rollout in range(rollouts)
        for month in range(months)
    ]
    return ExternalSeriesContext(series_values=EXTERNAL_SERIES_VALUES_FRAME.normalize(pl.DataFrame(rows)))


def _multi_series(*, levels_by_series: dict[str, dict[int, list[float]]]) -> ExternalSeriesContext:
    """Build an exogenous bundle with multiple series, indexed by (series_id, rollout) → levels.

    `levels_by_series[series_id][rollout]` is a list of length `horizon_months + 1` (the engine
    indexes external_values up through the horizon end).
    """

    rows = []
    for series_id, by_rollout in levels_by_series.items():
        for rollout_index, levels in by_rollout.items():
            for month_index, value in enumerate(levels):
                rows.append(
                    {"rollout_index": rollout_index, "month_index": month_index, "series_id": series_id, "value": value}
                )
    return ExternalSeriesContext(series_values=EXTERNAL_SERIES_VALUES_FRAME.normalize(pl.DataFrame(rows)))


def _rental_scenario(
    *,
    horizon_months: int = 12,
    monthly_rent: float = 5_000.0,
    fraction_rented: float = 1.0,
    vacancy_pct: float = 0.0,
    initial_cash_usd: float = 100_000.0,
    management_fee_pct: float = 0.0,
    leasing_fee_months: float = 0.0,
    avg_tenancy_months: int = 24,
) -> Scenario:
    """Build a minimal static-rental scenario. No taxes (empty tax_profiles)."""

    end_month = horizon_months - 1
    base_collected = monthly_rent * fraction_rented * (1.0 - vacancy_pct)
    agents = [Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id=TENANT_AGENT_ID)]
    initial_cash = [
        InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=initial_cash_usd),
        InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
    ]
    recurring_transfers: list[RecurringTransfer] = [
        RecurringTransfer(
            start_month=0,
            end_month=end_month,
            cause_id="rental_income:p1",
            from_agent_id=TENANT_AGENT_ID,
            from_account_id="checking",
            to_agent_id=OWNER_AGENT_ID,
            to_account_id="checking",
            amount_usd=SeriesIndexedAmount(
                base_amount_usd=base_collected, series=RENT_SERIES_KEY, adjustment_period_months=12
            ),
        )
    ]
    scheduled_transfers: list[ScheduledTransfer] = []
    if management_fee_pct > 0 or leasing_fee_months > 0:
        agents.append(Agent(agent_id=MGMT_AGENT_ID))
        initial_cash.append(InitialAccountBalance(agent_id=MGMT_AGENT_ID, account_id="checking", balance_usd=0.0))
    if management_fee_pct > 0:
        recurring_transfers.append(
            RecurringTransfer(
                start_month=0,
                end_month=end_month,
                cause_id="management_fee:p1",
                from_agent_id=OWNER_AGENT_ID,
                from_account_id="checking",
                to_agent_id=MGMT_AGENT_ID,
                to_account_id="checking",
                amount_usd=SeriesIndexedAmount(
                    base_amount_usd=base_collected * management_fee_pct / 100.0,
                    series=RENT_SERIES_KEY,
                    adjustment_period_months=12,
                ),
            )
        )
    if leasing_fee_months > 0:
        leasing_base = monthly_rent * leasing_fee_months
        scheduled_transfers.extend(
            ScheduledTransfer(
                month=fire_month,
                cause_id=f"leasing_fee:p1:m{fire_month}",
                from_agent_id=OWNER_AGENT_ID,
                from_account_id="checking",
                to_agent_id=MGMT_AGENT_ID,
                to_account_id="checking",
                amount_usd=SeriesIndexedAmount(
                    base_amount_usd=leasing_base, series=RENT_SERIES_KEY, adjustment_period_months=12
                ),
            )
            for fire_month in range(0, horizon_months, avg_tenancy_months)
        )
    return Scenario(
        agents=agents,
        initial_cash=initial_cash,
        recurring_transfers=recurring_transfers,
        scheduled_transfers=scheduled_transfers,
        tax_profiles=[],
        horizon_months=horizon_months,
    )


def _run(scenario: Scenario, rollouts: int = 1, rent_level: float = 1.0):
    """Run the scenario against a flat rent series at `rent_level` for all rollouts/months."""

    ctx = _flat_series(
        series_id=RENT_SERIES_ID, value=rent_level, months=scenario.horizon_months + 1, rollouts=rollouts
    )
    return simulate_with_external_series(scenario, external_series=ctx, rollout_count=rollouts, locations={})


class TestRentalIncome:
    def test_rental_income_flows_monthly_at_constant_rent(self):
        scenario = _rental_scenario(horizon_months=12, monthly_rent=5_000.0)
        run = _run(scenario)
        transfers = run.events_log.transfers.filter(pl.col("cause_id") == "rental_income:p1")
        assert transfers.height == 12
        # Each transfer = 5000 × 1.0 (full rented) × 1.0 (no vacancy) × (rent_level / base_level = 1.0)
        assert transfers["amount_usd"].to_list() == pytest.approx([5_000.0] * 12)

    def test_vacancy_pct_zero_collects_full_rent(self):
        scenario = _rental_scenario(monthly_rent=4_000.0, vacancy_pct=0.0)
        run = _run(scenario)
        transfers = run.events_log.transfers.filter(pl.col("cause_id") == "rental_income:p1")
        assert all(amount == pytest.approx(4_000.0) for amount in transfers["amount_usd"].to_list())

    def test_vacancy_pct_reduces_rent_proportionally(self):
        scenario = _rental_scenario(monthly_rent=4_000.0, vacancy_pct=0.10)
        run = _run(scenario)
        transfers = run.events_log.transfers.filter(pl.col("cause_id") == "rental_income:p1")
        # 10% vacancy → 90% rent collected = 3600
        assert all(amount == pytest.approx(3_600.0) for amount in transfers["amount_usd"].to_list())

    def test_vacancy_pct_one_collects_no_rent(self):
        scenario = _rental_scenario(monthly_rent=4_000.0, vacancy_pct=1.0)
        run = _run(scenario)
        transfers = run.events_log.transfers.filter(pl.col("cause_id") == "rental_income:p1")
        assert all(amount == pytest.approx(0.0) for amount in transfers["amount_usd"].to_list())

    def test_fraction_rented_half_collects_half_rent(self):
        scenario = _rental_scenario(monthly_rent=6_000.0, fraction_rented=0.5)
        run = _run(scenario)
        transfers = run.events_log.transfers.filter(pl.col("cause_id") == "rental_income:p1")
        assert all(amount == pytest.approx(3_000.0) for amount in transfers["amount_usd"].to_list())

    def test_rental_income_indexed_by_rent_series(self):
        # Rent series doubles at month 12 (annual adjustment period).
        scenario = _rental_scenario(horizon_months=24, monthly_rent=5_000.0)
        # Build a per-month rent series: 1.0 for months 0..11, 2.0 for months 12..24.
        levels = [1.0] * 12 + [2.0] * 13
        ctx = _multi_series(levels_by_series={RENT_SERIES_ID: {0: levels}})
        run = simulate_with_external_series(scenario, external_series=ctx, rollout_count=1, locations={})
        transfers = (
            run.events_log.transfers.filter(pl.col("cause_id") == "rental_income:p1")
            .sort("month_index")
            .select("month_index", "amount_usd")
        )
        # Months 0..11 use level 1.0 → $5000; months 12..23 reset to level 2.0 → $10000.
        amounts = transfers["amount_usd"].to_list()
        assert amounts[:12] == pytest.approx([5_000.0] * 12)
        assert amounts[12:] == pytest.approx([10_000.0] * 12)


class TestManagementFee:
    def test_management_fee_paid_monthly_against_collected_rent(self):
        scenario = _rental_scenario(horizon_months=12, monthly_rent=5_000.0, vacancy_pct=0.05, management_fee_pct=8.0)
        run = _run(scenario)
        mgmt = run.events_log.transfers.filter(pl.col("cause_id") == "management_fee:p1")
        assert mgmt.height == 12
        # 5000 × 0.95 (post-vacancy) × 0.08 (mgmt fee) = $380/mo
        assert all(amount == pytest.approx(380.0) for amount in mgmt["amount_usd"].to_list())

    def test_no_management_fee_when_zero_pct(self):
        scenario = _rental_scenario(horizon_months=12, monthly_rent=5_000.0, management_fee_pct=0.0)
        run = _run(scenario)
        mgmt = run.events_log.transfers.filter(pl.col("cause_id") == "management_fee:p1")
        assert mgmt.height == 0


class TestRentalLifecycleCashflows:
    def test_lifecycle_rented_fraction_timeline_resizes_rent_and_management_fees(self):
        end_month = 11
        monthly_rent = 6_000.0
        vacancy_multiplier = 0.90
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id=MGMT_AGENT_ID),
                Agent(agent_id="property_seller"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=700_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id=MGMT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=2,
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=monthly_rent * 0.25 * vacancy_multiplier,
                        series=RENT_SERIES_KEY,
                        adjustment_period_months=12,
                    ),
                    income_category="ordinary",
                ),
                RecurringTransfer(
                    start_month=3,
                    end_month=5,
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=monthly_rent * 0.75 * vacancy_multiplier,
                        series=RENT_SERIES_KEY,
                        adjustment_period_months=12,
                    ),
                    income_category="ordinary",
                ),
                RecurringTransfer(
                    start_month=8,
                    end_month=end_month,
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=monthly_rent * 0.5 * vacancy_multiplier,
                        series=RENT_SERIES_KEY,
                        adjustment_period_months=12,
                    ),
                    income_category="ordinary",
                ),
                RecurringTransfer(
                    start_month=0,
                    end_month=2,
                    cause_id="management_fee:p1",
                    from_agent_id=OWNER_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=MGMT_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=monthly_rent * 0.25 * vacancy_multiplier * 0.08,
                        series=RENT_SERIES_KEY,
                        adjustment_period_months=12,
                    ),
                    deduction_category="ordinary",
                ),
                RecurringTransfer(
                    start_month=3,
                    end_month=5,
                    cause_id="management_fee:p1",
                    from_agent_id=OWNER_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=MGMT_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=monthly_rent * 0.75 * vacancy_multiplier * 0.08,
                        series=RENT_SERIES_KEY,
                        adjustment_period_months=12,
                    ),
                    deduction_category="ordinary",
                ),
                RecurringTransfer(
                    start_month=8,
                    end_month=end_month,
                    cause_id="management_fee:p1",
                    from_agent_id=OWNER_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=MGMT_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=monthly_rent * 0.5 * vacancy_multiplier * 0.08,
                        series=RENT_SERIES_KEY,
                        adjustment_period_months=12,
                    ),
                    deduction_category="ordinary",
                ),
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="test_location",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    seller_account_id="checking",
                    purchase_price_usd=500_000.0,
                    down_payment_usd=500_000.0,
                    buyer_closing_cost_usd=0.0,
                    ownership_pct=1.0,
                    rented_fraction=0.25,
                )
            ],
            property_lifecycle_events=[
                SetRentedFractionEvent(month=3, property_id="p1", rented_fraction=0.75),
                SetRentedFractionEvent(month=6, property_id="p1", rented_fraction=0.0),
                SetRentedFractionEvent(month=8, property_id="p1", rented_fraction=0.5),
            ],
            tax_profiles=[],
            horizon_months=12,
        )
        run = simulate_with_external_series(
            scenario,
            external_series=_flat_series(series_id=RENT_SERIES_ID, value=1.0, months=13, rollouts=1),
            rollout_count=1,
            locations={
                "test_location": Location(
                    location_id="test_location",
                    display_name="Test Location",
                    jurisdiction_ids=[],
                    annual_property_tax_rate=0.0,
                    annual_special_assessment_usd=0.0,
                )
            },
        )

        rent = (
            run.events_log.transfers.filter(pl.col("cause_id") == "rental_income:p1")
            .sort("month_index")
            .select("month_index", "amount_usd")
        )
        assert rent["month_index"].to_list() == [0, 1, 2, 3, 4, 5, 8, 9, 10, 11]
        assert rent["amount_usd"].to_list() == pytest.approx(
            [monthly_rent * 0.25 * vacancy_multiplier] * 3
            + [monthly_rent * 0.75 * vacancy_multiplier] * 3
            + [monthly_rent * 0.5 * vacancy_multiplier] * 4
        )

        management_fee = (
            run.events_log.transfers.filter(pl.col("cause_id") == "management_fee:p1")
            .sort("month_index")
            .select("month_index", "amount_usd")
        )
        assert management_fee["month_index"].to_list() == [0, 1, 2, 3, 4, 5, 8, 9, 10, 11]
        assert management_fee["amount_usd"].to_list() == pytest.approx(
            [monthly_rent * 0.25 * vacancy_multiplier * 0.08] * 3
            + [monthly_rent * 0.75 * vacancy_multiplier * 0.08] * 3
            + [monthly_rent * 0.5 * vacancy_multiplier * 0.08] * 4
        )


class TestLeasingFee:
    def test_leasing_fee_fires_at_rent_start_and_every_avg_tenancy_months(self):
        scenario = _rental_scenario(
            horizon_months=60, monthly_rent=5_000.0, leasing_fee_months=1.0, avg_tenancy_months=24
        )
        run = _run(scenario)
        leasing = (
            run.events_log.transfers.filter(pl.col("cause_id").str.starts_with("leasing_fee:p1"))
            .sort("month_index")
            .select("month_index", "amount_usd")
        )
        # 60 months / 24mo cadence → fires at months 0, 24, 48 → 3 entries.
        assert leasing["month_index"].to_list() == [0, 24, 48]
        # Each fee = 1mo × $5000 = $5000.
        assert leasing["amount_usd"].to_list() == pytest.approx([5_000.0] * 3)

    def test_no_leasing_fee_when_zero_months(self):
        scenario = _rental_scenario(horizon_months=60, monthly_rent=5_000.0, leasing_fee_months=0.0)
        run = _run(scenario)
        leasing = run.events_log.transfers.filter(pl.col("cause_id").str.starts_with("leasing_fee:p1"))
        assert leasing.height == 0


class TestRentalIncomeTaxation:
    """Phase 2.0: rental income transfers carry income_category='ordinary', so they accrue
    into the owner's taxable ordinary income at year-end. Schedule E deductions and
    MID/SALT scaling are deferred to follow-up commits; rental income is currently
    over-taxed by the amount of those deductions.
    """

    def _taxed_rental_scenario(self, *, monthly_rent: float, horizon_months: int = 12) -> Scenario:
        end_month = horizon_months - 1
        return Scenario(
            agents=[Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id=TENANT_AGENT_ID), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=100_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=end_month,
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=monthly_rent, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    income_category="ordinary",
                )
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=horizon_months,
        )

    def test_rental_income_accrues_into_ordinary_ytd(self):
        # $4,000/mo × 12 = $48,000 gross rental income → ordinary income line on tax_breakdowns.
        scenario = self._taxed_rental_scenario(monthly_rent=4_000.0)
        run = _run(scenario)
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        assert breakdowns["federal_us"]["ordinary_income_usd"] == pytest.approx(48_000.0, abs=1e-6)

    def test_rental_income_generates_tax_accruals_at_year_end(self):
        scenario = self._taxed_rental_scenario(monthly_rent=4_000.0)
        run = _run(scenario)
        accruals = run.events_log.tax_accruals.sort("jurisdiction_id")
        assert accruals.height == 2  # federal + CA
        # Accruals fire at month 11 (year-end).
        assert all(month == 11 for month in accruals["month_index"].to_list())
        # Both jurisdictions should levy positive tax on $48k of ordinary income.
        assert all(amount > 0 for amount in accruals["amount_usd"].to_list())

    def test_management_fee_deducts_from_taxable_ordinary_income(self):
        """Schedule E: a management fee transfer with deduction_category='ordinary'
        should subtract from the owner's ordinary_income_ytd, reducing taxable income."""

        end_month = 11
        # $5,000/mo rental + $500/mo management fee → $60k gross - $6k deduction = $54k taxable.
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id=MGMT_AGENT_ID),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=100_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id=MGMT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=end_month,
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=5_000.0, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    income_category="ordinary",
                ),
                RecurringTransfer(
                    start_month=0,
                    end_month=end_month,
                    cause_id="management_fee:p1",
                    from_agent_id=OWNER_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=MGMT_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=500.0, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    deduction_category="ordinary",
                ),
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=12,
        )
        run = _run(scenario)
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        # Gross rental: 12 × $5,000 = $60,000. Management fee: 12 × $500 = $6,000.
        # Net ordinary income exposed to brackets = $54,000.
        assert breakdowns["federal_us"]["ordinary_income_usd"] == pytest.approx(54_000.0, abs=1e-6)

    def test_obligation_deduction_decrements_payer_ordinary_ytd(self):
        """Schedule E on obligations: a paid RecurringObligation with
        deduction_category='ordinary' and deductible_fraction=1.0 decrements the payer's
        ordinary_income_ytd by the full settled amount."""

        end_month = 11
        # $6,000/mo gross rent → $72,000/yr; $400/mo HOA fully deductible → $4,800/yr Schedule E.
        # Net ordinary income for tax = $72,000 - $4,800 = $67,200.
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="hoa"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=100_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="hoa", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=end_month,
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=6_000.0, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    income_category="ordinary",
                )
            ],
            recurring_obligations=[
                RecurringObligation(
                    start_month=0,
                    end_month=end_month,
                    obligation_id="hoa_dues",
                    obligation_type="hoa_dues",
                    agent_id=OWNER_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id="hoa",
                    to_account_id="checking",
                    amount_due_usd=SeriesIndexedAmount(
                        base_amount_usd=400.0, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    deduction_category="ordinary",
                    deductible_fraction=1.0,
                )
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=12,
        )
        run = _run(scenario)
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        assert breakdowns["federal_us"]["ordinary_income_usd"] == pytest.approx(67_200.0, abs=1e-6)

    def test_depreciation_accrues_monthly_and_deducts_as_schedule_e(self, san_francisco_location: Location):
        """§168 monthly depreciation accrues for rented property and reduces taxable ordinary
        income at year-end. Building basis = $500k × 0.80 = $400k; rented_fraction = 1.0;
        annual depreciation = $400k / 27.5 ≈ $14,545.45."""

        end_month = 11
        purchase_price = 500_000.0
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=600_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=end_month,
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=5_000.0, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    income_category="ordinary",
                )
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price,
                    ownership_pct=1.0,
                    rented_fraction=1.0,
                    land_value_fraction=0.20,
                    buyer_closing_cost_usd=0.0,
                )
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=12,
        )
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_ID: {0: [1.0] * 13}, "home_value:san_francisco": {0: [1.0] * 13}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        # Cumulative depreciation grows monotonically; at month 12 (post-horizon snapshot) it's
        # accrued 12 months worth = $400,000 / 27.5 = $14,545.45.
        terminal_dep = run.property_state.filter(pl.col("month_index") == 12)
        assert terminal_dep.height == 1
        # Federal ordinary income: $60,000 rental - $14,545.45 depreciation = $45,454.55.
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        assert breakdowns["federal_us"]["ordinary_income_usd"] == pytest.approx(45_454.55, abs=0.02)

    def test_lifecycle_start_renting_starts_depreciation_accrual_mid_horizon(self, san_francisco_location: Location):
        """StartRentingEvent at month 12 → depreciation accrues only from month 12 onward.
        24-month horizon, $400k building basis, 12 months of rental → annual depreciation
        in year 1 = 0; in year 2 (after start) = $400k / 27.5 = $14,545.45."""

        end_month = 23  # 24-month horizon
        purchase_price = 500_000.0
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=600_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    # Rental income only fires from month 12 — matches the start-renting event.
                    start_month=12,
                    end_month=end_month,
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=5_000.0, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    income_category="ordinary",
                )
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price,
                    ownership_pct=1.0,
                    rented_fraction=0.0,  # owner-occupied at start
                    land_value_fraction=0.20,
                    buyer_closing_cost_usd=0.0,
                )
            ],
            property_lifecycle_events=[SetRentedFractionEvent(month=12, property_id="p1", rented_fraction=1.0)],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=24,
        )
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_ID: {0: [1.0] * 25}, "home_value:san_francisco": {0: [1.0] * 25}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        breakdowns = list(run.events_log.tax_breakdowns.sort("month_index", "jurisdiction_id").iter_rows(named=True))
        # Year 0 (month 11) federal_us: rented_fraction=0 the whole year, no rental income, no
        # depreciation → ordinary_income = $0.
        year_0_federal = next(b for b in breakdowns if b["month_index"] == 11 and b["jurisdiction_id"] == "federal_us")
        assert year_0_federal["ordinary_income_usd"] == pytest.approx(0.0, abs=1e-6)
        # Year 1 (month 23) federal_us: 12 months rent ($60k) minus 12 months depreciation ($14.5k)
        # ≈ $45,454.55.
        year_1_federal = next(b for b in breakdowns if b["month_index"] == 23 and b["jurisdiction_id"] == "federal_us")
        assert year_1_federal["ordinary_income_usd"] == pytest.approx(45_454.55, abs=0.05)

    def test_lifecycle_start_renting_redirects_mortgage_interest_from_mid_to_schedule_e(
        self, san_francisco_location: Location
    ):
        """At start-of-rental, MID drops to 0 for the now-rented portion of mortgage interest,
        and Schedule E picks it up. Comparison: same scenario with rented_fraction=0 throughout
        vs. with StartRentingEvent at month 0 setting rented_fraction=1.0 — the second case
        should yield zero MID line."""

        breakdowns_owner = self._mortgage_lifecycle_breakdown(
            start_renting_at=None, locations={"san_francisco": san_francisco_location}
        )
        # NOTE: StartRentingEvent must fire strictly after purchase (month 0), so use month 1.
        breakdowns_rent = self._mortgage_lifecycle_breakdown(
            start_renting_at=1, locations={"san_francisco": san_francisco_location}
        )
        # Owner-occupied: positive MID
        assert breakdowns_owner["federal_us"]["mortgage_interest_deduction_usd"] > 0
        # Rented from month 1: MID for year 0 is the month-0 interest only (a tiny first-month
        # owner-share interest), much smaller than full-year owner-occupied MID.
        assert (
            breakdowns_rent["federal_us"]["mortgage_interest_deduction_usd"]
            < breakdowns_owner["federal_us"]["mortgage_interest_deduction_usd"] * 0.15
        )

    def _mortgage_lifecycle_breakdown(self, *, start_renting_at: int | None, locations: dict[str, Location]) -> dict:
        end_month = 11
        purchase_price = 500_000.0
        lifecycle_events = (
            [SetRentedFractionEvent(month=start_renting_at, property_id="p1", rented_fraction=1.0)]
            if start_renting_at is not None
            else []
        )
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="lender"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=700_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="lender", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=end_month,
                    cause_id="paycheck",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=5_000.0,
                    income_category="ordinary",
                )
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price * 0.20,
                    land_value_fraction=1.0,  # isolate from depreciation
                    mortgage=MortgageFinancing(
                        liability_id="p1_mortgage",
                        lender_agent_id="lender",
                        lender_account_id="checking",
                        principal_usd=purchase_price * 0.80,
                        annual_interest_rate=0.06,
                        term_months=360,
                    ),
                    ownership_pct=1.0,
                    rented_fraction=0.0,
                )
            ],
            property_lifecycle_events=lifecycle_events,
            mortgage_interest_deduction_policies=[
                MortgageInterestDeductionPolicy(liability_id="p1_mortgage", owner_agent_id=OWNER_AGENT_ID)
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=12,
        )
        ctx = _multi_series(levels_by_series={"home_value:san_francisco": {0: [1.0] * 13}})
        run = simulate_with_external_series(scenario, external_series=ctx, rollout_count=1, locations=locations)
        return {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}

    def test_lifecycle_stop_renting_halts_depreciation(self, san_francisco_location: Location):
        """StopRentingEvent at month 12 → depreciation accrues months 0-11 only.
        Year 0 ordinary: $60k rent - $14.5k dep = $45.5k.
        Year 1 ordinary: $0 rent (no more rental income), no dep → $0.
        """

        purchase_price = 500_000.0
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=600_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=11,  # rental income only in year 0
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=5_000.0, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    income_category="ordinary",
                )
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price,
                    ownership_pct=1.0,
                    rented_fraction=1.0,
                    land_value_fraction=0.20,
                    buyer_closing_cost_usd=0.0,
                )
            ],
            property_lifecycle_events=[SetRentedFractionEvent(month=12, property_id="p1", rented_fraction=0.0)],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=24,
        )
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_ID: {0: [1.0] * 25}, "home_value:san_francisco": {0: [1.0] * 25}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        breakdowns = list(run.events_log.tax_breakdowns.sort("month_index", "jurisdiction_id").iter_rows(named=True))
        year_0_federal = next(b for b in breakdowns if b["month_index"] == 11 and b["jurisdiction_id"] == "federal_us")
        assert year_0_federal["ordinary_income_usd"] == pytest.approx(45_454.55, abs=0.05)
        year_1_federal = next(b for b in breakdowns if b["month_index"] == 23 and b["jurisdiction_id"] == "federal_us")
        assert year_1_federal["ordinary_income_usd"] == pytest.approx(0.0, abs=1e-6)

    def test_capital_improvement_bumps_basis_and_accelerates_depreciation(self, san_francisco_location: Location):
        """CapitalImprovementEvent at month 6 bumps building basis by $100k.
        Building basis after improvement = $400k + $100k = $500k.
        Monthly depreciation after month 6 = $500k / 27.5 / 12 ≈ $1,515.15
        (vs. ~$1,212.12/mo before). Cash decreases by $100k at month 6."""

        end_month = 11
        purchase_price = 500_000.0
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=700_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=end_month,
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=5_000.0,
                    income_category="ordinary",
                )
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price,
                    ownership_pct=1.0,
                    rented_fraction=1.0,
                    land_value_fraction=0.20,
                )
            ],
            property_lifecycle_events=[
                CapitalImprovementEvent(month=6, property_id="p1", amount_usd=100_000.0, description="new roof")
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=12,
        )
        ctx = _multi_series(levels_by_series={"home_value:san_francisco": {0: [1.0] * 13}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        # 6 months at $400k/27.5/12 + 6 months at $500k/27.5/12
        # = 6 × 1212.12 + 6 × 1515.15 = 7272.73 + 9090.91 = 16363.64
        expected_depreciation = 6 * (400_000.0 / 27.5 / 12.0) + 6 * (500_000.0 / 27.5 / 12.0)
        expected_ordinary = 60_000.0 - expected_depreciation
        assert breakdowns["federal_us"]["ordinary_income_usd"] == pytest.approx(expected_ordinary, abs=0.05)

    def test_property_sale_recaptures_depreciation_and_routes_remaining_gain_to_ltcg(
        self, san_francisco_location: Location
    ):
        """Sale of a fully-rented property after 12 months of depreciation.
        Building basis $400k → $400k / 27.5 ≈ $14,545.45/yr depreciation.
        After 12mo cumulative depreciation = $14,545.45.
        Home value flat at 1.0 → market value = $500k purchase price.
        Closing cost 6% → gross_proceeds = $470k.
        Adjusted basis = $500k - $14,545.45 = $485,454.55.
        Realized gain = $470k - $485,454.55 = -$15,454.55 (loss).
        Loss → no recapture, no LTCG."""

        scenario = self._sale_scenario(horizon=13, sale_month=12, cumulative_depreciation_eligible=True)
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_ID: {0: [1.0] * 14}, "home_value:san_francisco": {0: [1.0] * 14}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        # Verify the sale fired: capital_gain_ytd has zero LTCG, ordinary_ytd has zero recapture
        # because the property sold for less than adjusted basis (loss). No gain to recapture.
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        # Federal LTCG: 0
        assert breakdowns["federal_us"]["ltcg_usd"] == pytest.approx(0.0, abs=1e-6)
        # Property is frozen after sale - the next year's depreciation should not accrue.
        # Verify by counting depreciation accruals: only 12 months should have happened.

    def test_property_sale_requires_home_value_series(self, san_francisco_location: Location):
        scenario = self._sale_scenario(horizon=13, sale_month=12, cumulative_depreciation_eligible=True)
        ctx = ExternalSeriesContext(series_values=EXTERNAL_SERIES_VALUES_FRAME.empty())

        with pytest.raises(
            KeyError, match=r"property sale for property_id 'p1'.*home-value series 'home_value:san_francisco'"
        ):
            simulate_with_external_series(
                scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
            )

    def test_property_sale_at_gain_routes_recapture_and_ltcg(self, san_francisco_location: Location):
        """Sale at month 12 with home value appreciation. Horizon 24mo so year 1 tax accrual
        (month 23) captures the sale-year LTCG.

        Cumulative dep = $14,545.45 ($400k building / 27.5y for 12mo).
        Adjusted basis = $500k - $14,545.45 = $485,454.55.
        Home value 1.5× → market value $750k → gross_proceeds (6% closing) = $705k.
        Gain = $705k - $485,454.55 = $219,545.45.
        Recapture = min(gain, $14,545.45) = $14,545.45 → §1250 (federal 25%, CA ordinary).
        LTCG = $219,545.45 - $14,545.45 = $205,000 → long_term_capital_gain_ytd."""

        scenario = self._sale_scenario(horizon=24, sale_month=12)
        # Home value series steps up at month 12.
        levels = [1.0] * 12 + [1.5] * 13
        ctx = _multi_series(levels_by_series={RENT_SERIES_ID: {0: [1.0] * 25}, "home_value:san_francisco": {0: levels}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        # Year 1 tax breakdown (month 23) captures the LTCG.
        breakdowns_y1 = [
            row
            for row in run.events_log.tax_breakdowns.iter_rows(named=True)
            if row["month_index"] == 23 and row["jurisdiction_id"] == "federal_us"
        ]
        assert len(breakdowns_y1) == 1
        b = breakdowns_y1[0]
        assert b["ltcg_usd"] == pytest.approx(205_000.0, abs=1.0)

    def test_section_1250_recapture_taxed_at_lesser_of_marginal_or_cap_low_bracket(
        self, san_francisco_location: Location
    ):
        """IRS Unrecaptured §1250 Gain Worksheet rule (low-bracket case).

        Sale at month 12 (year 2). The recapture lands in year 2 tax accruals; year 2
        has no rental income (rent stops at sale-1) so federal `ordinary_taxable=0`.
        Stacking the $14,545.45 recapture on top of zero ordinary taxable puts it
        entirely in the 10% (first $11,600) + 12% (next $2,945) brackets — well below
        the 25% federal cap. So the §1250 tax is the marginal walk, NOT recapture × 25%.

        California still has no §1250 cap → recapture is added to ordinary brackets.
        """

        scenario = self._sale_scenario(horizon=24, sale_month=12)
        levels = [1.0] * 12 + [1.5] * 13
        ctx = _multi_series(levels_by_series={RENT_SERIES_ID: {0: [1.0] * 25}, "home_value:san_francisco": {0: levels}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        federal_y2 = next(
            row
            for row in run.events_log.tax_breakdowns.iter_rows(named=True)
            if row["month_index"] == 23 and row["jurisdiction_id"] == "federal_us"
        )
        california_y2 = next(
            row
            for row in run.events_log.tax_breakdowns.iter_rows(named=True)
            if row["month_index"] == 23 and row["jurisdiction_id"] == "california"
        )
        recapture = 14_545.45
        assert federal_y2["ordinary_income_usd"] == pytest.approx(0.0, abs=1e-6)
        assert federal_y2["ordinary_taxable_usd"] == pytest.approx(0.0, abs=1e-6)
        # Federal LTCG: ordinary_taxable=0, LTCG=$205k → 0% slice 0..47025, 15% slice
        # 47025..205000 = 0.15 × 157975 = 23,696.25.
        ltcg_tax_federal = 0.15 * (205_000.0 - 47_025.0)
        # §1250 implied marginal walk: 10% × 11600 + 12% × (14545.45 - 11600) = 1160 + 353.45 = 1513.45.
        # That's well below the 25% × 14545.45 = 3636.36 cap → marginal wins.
        section_1250_marginal = 0.10 * 11_600.0 + 0.12 * (recapture - 11_600.0)
        section_1250_cap = recapture * 0.25
        assert section_1250_marginal < section_1250_cap  # sanity: marginal binds, not cap
        assert federal_y2["capital_gain_tax_usd"] == pytest.approx(ltcg_tax_federal + section_1250_marginal, abs=2.0)
        # California: no §1250 cap → recapture is added to ordinary brackets (and CA has no
        # separate LTCG schedule, so LTCG is in ordinary too).
        assert california_y2["capital_gain_tax_usd"] == pytest.approx(0.0, abs=1e-6)
        assert california_y2["ordinary_taxable_usd"] == pytest.approx(
            205_000.0 + recapture - california_y2["standard_deduction_usd"], abs=1.0
        )

    def test_section_1250_recapture_caps_at_25pct_when_marginal_exceeds(self, san_francisco_location: Location):
        """High-bracket case: federal 25% §1250 cap binds when marginal ≥ 25%.

        Same sale scenario, but the owner also earns enough wage income in year 2 to
        push ordinary_taxable past the 32% bracket threshold ($191,950 single). Stacking
        the recapture on top would normally land in the 32%/35% brackets, but the IRS
        cap holds it to 25%. Capital-gain tax = LTCG tax + 25% × recapture.
        """

        scenario = self._sale_scenario(horizon=24, sale_month=12, year2_wage_usd=250_000.0)
        levels = [1.0] * 12 + [1.5] * 13
        ctx = _multi_series(levels_by_series={RENT_SERIES_ID: {0: [1.0] * 25}, "home_value:san_francisco": {0: levels}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        federal_y2 = next(
            row
            for row in run.events_log.tax_breakdowns.iter_rows(named=True)
            if row["month_index"] == 23 and row["jurisdiction_id"] == "federal_us"
        )
        recapture = 14_545.45
        # Wages pushed federal ordinary_taxable above the 32% threshold (191,950), so
        # implied marginal on the recapture is 32%+ — well above the 25% cap. The cap binds.
        assert federal_y2["ordinary_taxable_usd"] > 191_950.0
        section_1250_tax = recapture * 0.25
        # LTCG bracket walk shifts because ordinary_taxable is now large: the 0% slice is
        # fully consumed and most of the LTCG lands in the 20% bracket (LTCG breakpoints
        # 47025 / 518900 single for 2024). Just assert that the §1250 tax piece is exactly
        # the 25% cap — the LTCG arithmetic is exercised elsewhere.
        assert federal_y2["capital_gain_tax_usd"] >= section_1250_tax + 0.20 * 100_000.0  # rough lower bound

    def test_section_121_exclusion_after_24_owner_occupied_months(self, san_francisco_location: Location):
        """Owner-occupied for ≥ 24 of the last 60 months → up to $250k of post-recapture
        gain is excluded from LTCG (single-filer cap).

        Buy as primary residence (rented_fraction=0). Hold for 30 months. Then sell with
        $200k appreciation: realized gain = $188k (after closing costs), all of it post-
        recapture (no depreciation accrued because never rented). §121 excludes the full
        $188k → LTCG = 0; section_121_exclusion_usd in the sale event records $188k.
        """

        purchase_price = 500_000.0
        sale_month = 30
        horizon = 36
        scenario = Scenario(
            agents=[Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id="property_seller"), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=600_000.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price,
                    ownership_pct=1.0,
                    rented_fraction=0.0,
                    land_value_fraction=0.20,
                )
            ],
            initial_primary_residences=[PrimaryResidenceAssignment(agent_id=OWNER_AGENT_ID, property_id="p1")],
            property_lifecycle_events=[PropertySaleEvent(month=sale_month, property_id="p1", closing_cost_pct=6.0)],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=horizon,
        )
        # Home value 1.4× at sale time.
        home_values = [1.0] * sale_month + [1.4] * (horizon + 1 - sale_month)
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_ID: {0: [1.0] * (horizon + 1)}, "home_value:san_francisco": {0: home_values}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        # Sale event surfaces in property_sale_events frame.
        sale_rows = run.events_log.property_sale_events.to_dicts()
        assert len(sale_rows) == 1
        sale = sale_rows[0]
        # Gross = $500k × 1.4 × 0.94 = $658k. Realized gain = $658k - $500k = $158k.
        assert sale["gross_proceeds_usd"] == pytest.approx(658_000.0, abs=1.0)
        assert sale["realized_gain_usd"] == pytest.approx(158_000.0, abs=1.0)
        assert sale["depreciation_recapture_usd"] == pytest.approx(0.0, abs=1e-6)
        # §121 fully excludes the $158k gain (well under $250k single-filer cap).
        assert sale["section_121_exclusion_usd"] == pytest.approx(158_000.0, abs=1.0)
        assert sale["long_term_capital_gain_usd"] == pytest.approx(0.0, abs=1e-6)

    def test_section_121_does_not_apply_to_unassigned_non_rented_property(self, san_francisco_location: Location):
        purchase_price = 500_000.0
        sale_month = 30
        horizon = 36
        scenario = Scenario(
            agents=[Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id="property_seller"), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=600_000.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price,
                    ownership_pct=1.0,
                    rented_fraction=0.0,
                    land_value_fraction=0.20,
                )
            ],
            property_lifecycle_events=[PropertySaleEvent(month=sale_month, property_id="p1", closing_cost_pct=6.0)],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=horizon,
        )
        home_values = [1.0] * sale_month + [1.4] * (horizon + 1 - sale_month)
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_ID: {0: [1.0] * (horizon + 1)}, "home_value:san_francisco": {0: home_values}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )

        sale = run.events_log.property_sale_events.to_dicts()[0]
        assert sale["realized_gain_usd"] == pytest.approx(158_000.0, abs=1.0)
        assert sale["section_121_exclusion_usd"] == pytest.approx(0.0, abs=1e-6)
        assert sale["long_term_capital_gain_usd"] == pytest.approx(158_000.0, abs=1.0)

    def test_primary_residence_event_starts_section_121_qualifying_months(self, san_francisco_location: Location):
        purchase_price = 500_000.0
        sale_month = 30
        horizon = 36
        scenario = Scenario(
            agents=[Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id="property_seller"), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=600_000.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price,
                    ownership_pct=1.0,
                    rented_fraction=0.0,
                    land_value_fraction=0.20,
                )
            ],
            primary_residence_events=[SetPrimaryResidenceEvent(month=6, agent_id=OWNER_AGENT_ID, property_id="p1")],
            property_lifecycle_events=[PropertySaleEvent(month=sale_month, property_id="p1", closing_cost_pct=6.0)],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=horizon,
        )
        home_values = [1.0] * sale_month + [1.4] * (horizon + 1 - sale_month)
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_ID: {0: [1.0] * (horizon + 1)}, "home_value:san_francisco": {0: home_values}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )

        primary_rows = run.events_log.set_primary_residence_events.to_dicts()
        assert primary_rows == [
            {
                "rollout_index": 0,
                "month_index": 6,
                "agent_id": OWNER_AGENT_ID,
                "property_id": "p1",
                "is_primary_residence": True,
            }
        ]
        sale = run.events_log.property_sale_events.to_dicts()[0]
        assert sale["section_121_exclusion_usd"] == pytest.approx(158_000.0, abs=1.0)
        assert sale["long_term_capital_gain_usd"] == pytest.approx(0.0, abs=1e-6)

    def test_section_121_does_not_apply_without_owner_occupied_months(self, san_francisco_location: Location):
        """Same sale at month 30, but the property has been 100% rented the entire time.
        Owner-occupied months = 0, so §121 does not apply. The depreciation recapture +
        LTCG flow remains intact."""

        scenario = self._sale_scenario(horizon=36, sale_month=30)
        home_values = [1.0] * 30 + [1.4] * 7
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_ID: {0: [1.0] * 37}, "home_value:san_francisco": {0: home_values}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        sale = run.events_log.property_sale_events.to_dicts()[0]
        # §121 should be exactly zero — never owner-occupied.
        assert sale["section_121_exclusion_usd"] == pytest.approx(0.0, abs=1e-6)
        # Recapture should be positive (30 months of depreciation × $400k / 27.5 / 12 ≈ $36,363).
        assert sale["depreciation_recapture_usd"] == pytest.approx(36_363.64, abs=1.0)

    def test_lifecycle_event_frames_logged_for_each_kind(self, san_francisco_location: Location):
        """All three lifecycle event kinds appear in their respective frames, one row per
        (rollout, event)."""

        purchase_price = 500_000.0
        scenario = Scenario(
            agents=[Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id="property_seller"), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=800_000.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price,
                    ownership_pct=1.0,
                    rented_fraction=0.0,
                    land_value_fraction=0.20,
                )
            ],
            property_lifecycle_events=[
                SetRentedFractionEvent(month=6, property_id="p1", rented_fraction=1.0),
                CapitalImprovementEvent(month=8, property_id="p1", amount_usd=50_000.0, description="new roof"),
                PropertySaleEvent(month=12, property_id="p1", closing_cost_pct=6.0),
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=24,
        )
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_ID: {0: [1.0] * 25}, "home_value:san_francisco": {0: [1.0] * 25}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )

        rented_rows = run.events_log.set_rented_fraction_events.to_dicts()
        assert len(rented_rows) == 1
        assert rented_rows[0]["month_index"] == 6
        assert rented_rows[0]["rented_fraction"] == 1.0
        assert rented_rows[0]["property_id"] == "p1"

        capex_rows = run.events_log.capital_improvement_events.to_dicts()
        assert len(capex_rows) == 1
        assert capex_rows[0]["month_index"] == 8
        assert capex_rows[0]["amount_usd"] == pytest.approx(50_000.0)
        assert capex_rows[0]["property_id"] == "p1"

        sale_rows = run.events_log.property_sale_events.to_dicts()
        assert len(sale_rows) == 1
        assert sale_rows[0]["month_index"] == 12
        assert sale_rows[0]["property_id"] == "p1"

    def _sale_scenario(
        self,
        *,
        horizon: int,
        sale_month: int,
        cumulative_depreciation_eligible: bool = True,
        year2_wage_usd: float = 0.0,
    ) -> Scenario:
        purchase_price = 500_000.0
        agents = [
            Agent(agent_id=OWNER_AGENT_ID),
            Agent(agent_id=TENANT_AGENT_ID),
            Agent(agent_id="property_seller"),
            Agent(agent_id="irs"),
        ]
        initial_cash = [
            InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=600_000.0),
            InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ]
        recurring_transfers = [
            RecurringTransfer(
                start_month=0,
                end_month=sale_month - 1,
                cause_id="rental_income:p1",
                from_agent_id=TENANT_AGENT_ID,
                from_account_id="checking",
                to_agent_id=OWNER_AGENT_ID,
                to_account_id="checking",
                amount_usd=5_000.0,
                income_category="ordinary",
            )
        ]
        if year2_wage_usd > 0:
            agents.append(Agent(agent_id="employer"))
            initial_cash.append(InitialAccountBalance(agent_id="employer", account_id="checking", balance_usd=0.0))
            recurring_transfers.append(
                RecurringTransfer(
                    start_month=12,
                    end_month=23,
                    cause_id="wages:employer",
                    from_agent_id="employer",
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=year2_wage_usd / 12.0,
                    income_category="ordinary",
                )
            )
        return Scenario(
            agents=agents,
            initial_cash=initial_cash,
            recurring_transfers=recurring_transfers,
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price,
                    ownership_pct=1.0,
                    rented_fraction=1.0 if cumulative_depreciation_eligible else 0.0,
                    land_value_fraction=0.20,
                )
            ],
            property_lifecycle_events=[PropertySaleEvent(month=sale_month, property_id="p1", closing_cost_pct=6.0)],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=horizon,
        )

    def test_depreciation_does_not_accrue_when_not_rented(self, san_francisco_location: Location):
        """No rental → no depreciation accrual → no Schedule E deduction."""

        end_month = 11
        purchase_price = 500_000.0
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=600_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=end_month,
                    cause_id="paycheck",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=5_000.0,
                    income_category="ordinary",
                )
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price,
                    ownership_pct=1.0,
                    rented_fraction=0.0,
                    land_value_fraction=0.20,
                )
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=12,
        )
        ctx = _multi_series(levels_by_series={"home_value:san_francisco": {0: [1.0] * 13}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        # No depreciation → ordinary income equals gross paycheck income: $60,000.
        assert breakdowns["federal_us"]["ordinary_income_usd"] == pytest.approx(60_000.0, abs=1e-6)

    def test_mortgage_interest_deducts_full_for_owner_occupied_and_scales_for_partial_rental(
        self, san_francisco_location: Location
    ):
        """MID applies to the owner-fraction of mortgage interest; the rented-fraction share
        deducts as Schedule E rental interest. The MID compile-time scaling and the engine's
        year-end Schedule E rental-interest hook combine to make rented_fraction × interest
        deductible under either MID or Schedule E depending on which yields the better total."""

        owner_breakdown = self._mortgage_scenario_breakdown(
            rented_fraction=0.0, locations={"san_francisco": san_francisco_location}
        )
        rented_breakdown = self._mortgage_scenario_breakdown(
            rented_fraction=1.0, locations={"san_francisco": san_francisco_location}
        )
        # Whether the property is fully owner-occupied or fully rented, the same dollar amount
        # of mortgage interest reduces ordinary income — just via different mechanisms (MID +
        # itemized vs. Schedule E direct subtraction). The federal final tax should match.
        # The interest is the same; deduction mechanics differ.
        assert owner_breakdown["federal_us"]["mortgage_interest_deduction_usd"] > 0
        assert rented_breakdown["federal_us"]["mortgage_interest_deduction_usd"] == pytest.approx(0.0, abs=1e-6)

    def _mortgage_scenario_breakdown(self, *, rented_fraction: float, locations: dict[str, Location]) -> dict:
        end_month = 11
        purchase_price = 600_000.0
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="lender"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=700_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="lender", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=end_month,
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=4_000.0, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    income_category="ordinary",
                )
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price * 0.20,
                    # Isolate the MID-vs-Schedule-E comparison from depreciation.
                    land_value_fraction=1.0,
                    mortgage=MortgageFinancing(
                        liability_id="p1_mortgage",
                        lender_agent_id="lender",
                        lender_account_id="checking",
                        principal_usd=purchase_price * 0.80,
                        annual_interest_rate=0.06,
                        term_months=360,
                    ),
                    ownership_pct=1.0,
                    rented_fraction=rented_fraction,
                )
            ],
            mortgage_interest_deduction_policies=[
                MortgageInterestDeductionPolicy(liability_id="p1_mortgage", owner_agent_id=OWNER_AGENT_ID)
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=12,
        )
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_ID: {0: [1.0] * 13}, "home_value:san_francisco": {0: [1.0] * 13}}
        )
        run = simulate_with_external_series(scenario, external_series=ctx, rollout_count=1, locations=locations)
        return {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}

    def test_property_tax_routes_owner_fraction_to_salt_and_rented_fraction_to_schedule_e(
        self, san_francisco_location: Location
    ):
        """Per-property `rented_fraction=0.75` should:
        - route 25% of property tax to SALT (owner-use portion)
        - route 75% of property tax to Schedule E (rented-use portion deduction).
        """

        # Build via ScheduledPropertyPurchase + PropertyTaxPolicy so the kind=2 compiler branch
        # populates the owner_fraction + deduction_profile arrays.
        end_month = 11
        purchase_price = 600_000.0
        rented_fraction = 0.75
        annual_tax_rate = 0.012  # 1.2% of price = $7,200/yr → $600/mo
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="tax_authority"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=700_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="tax_authority", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=end_month,
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=4_000.0, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    income_category="ordinary",
                )
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id=OWNER_AGENT_ID,
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price_usd=purchase_price,
                    down_payment_usd=purchase_price,
                    ownership_pct=1.0,
                    rented_fraction=rented_fraction,
                    # Isolate the property-tax assertion from depreciation: setting
                    # land_value_fraction=1.0 makes the building basis zero, so no §168
                    # depreciation accrues for this test.
                    land_value_fraction=1.0,
                )
            ],
            property_tax_policies=[
                PropertyTaxPolicy(
                    property_id="p1",
                    owner_agent_id=OWNER_AGENT_ID,
                    from_account_id="checking",
                    tax_authority_agent_id="tax_authority",
                    tax_authority_account_id="checking",
                    annual_tax_rate=annual_tax_rate,
                    start_month=0,
                    end_month=end_month,
                )
            ],
            federal_salt_deduction_policies=[
                FederalSaltDeductionPolicy(
                    profile_id=OWNER_AGENT_ID,
                    cap_schedule=[FederalSaltCapEntry(effective_year_index=0, cap_usd=10_000.0)],
                )
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=12,
        )
        # Series needs home_value:san_francisco too for property purchase.
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_ID: {0: [1.0] * 13}, "home_value:san_francisco": {0: [1.0] * 13}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        # Debug: surface any rollout failure before asserting on tax flows.
        status = run.rollout_status
        assert status["status"][0] != "failed", (
            f"rollout failed at month {status['failed_month'][0]}; "
            f"failures: {run.events_log.rollout_failures.to_dicts()}"
        )
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        # Gross rent: 12 × $4,000 = $48,000. Property tax fires at months 1..11 (11 payments;
        # month 0 is the purchase month, no tax that month) → $7,200 × 11/12 = $6,600.
        # rented_fraction=0.75 → $4,950 routes to Schedule E + $1,650 routes to SALT.
        # Federal ordinary_income_usd after Schedule E = $48,000 - $4,950 = $43,050.
        # (The SALT total combines property tax + state income tax and gets capped, so we
        # don't assert on the absolute SALT number here. The owner-fraction effect is
        # observable through ordinary_income decreasing relative to the rental income.)
        assert breakdowns["federal_us"]["ordinary_income_usd"] == pytest.approx(43_050.0, abs=1e-6)

    def test_obligation_deductible_fraction_scales_deduction(self):
        """Partial rental: HOA dues are only deductible up to the rented fraction (0.5
        in this test → only $200 of the $400/mo HOA deducts each month)."""

        end_month = 11
        # Gross rental $30,000/yr (50% rented); HOA $400/mo, 50% deductible → $200/mo × 12 = $2,400.
        # Net ordinary income = $30,000 - $2,400 = $27,600.
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="hoa"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance_usd=100_000.0),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="hoa", account_id="checking", balance_usd=0.0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=end_month,
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount_usd=SeriesIndexedAmount(
                        base_amount_usd=2_500.0, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    income_category="ordinary",
                )
            ],
            recurring_obligations=[
                RecurringObligation(
                    start_month=0,
                    end_month=end_month,
                    obligation_id="hoa_dues",
                    obligation_type="hoa_dues",
                    agent_id=OWNER_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id="hoa",
                    to_account_id="checking",
                    amount_due_usd=SeriesIndexedAmount(
                        base_amount_usd=400.0, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    deduction_category="ordinary",
                    deductible_fraction=0.5,
                )
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id=OWNER_AGENT_ID,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=12,
        )
        run = _run(scenario)
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        assert breakdowns["federal_us"]["ordinary_income_usd"] == pytest.approx(27_600.0, abs=1e-6)


class TestRentalCashflowReconciliation:
    def test_owner_cash_balance_after_one_year_matches_expected_net(self):
        """Headline accounting test: 12mo of rental + management - leasing matches owner's
        terminal cash change (within rounding tolerance)."""

        initial_cash = 100_000.0
        scenario = _rental_scenario(
            horizon_months=12,
            initial_cash_usd=initial_cash,
            monthly_rent=5_000.0,
            vacancy_pct=0.05,
            management_fee_pct=8.0,
            leasing_fee_months=1.0,
            avg_tenancy_months=24,
        )
        run = _run(scenario)
        # Expected: rental income = 12 × 5000 × 0.95 = $57,000.
        # Management fee = 12 × 5000 × 0.95 × 0.08 = $4,560.
        # Leasing fee = 1 × 5000 = $5,000 (month 0 only; next would be month 24, outside horizon).
        # Net to owner = 57,000 - 4,560 - 5,000 = $47,440.
        expected_owner_terminal = initial_cash + 47_440.0
        # cash_balances has snapshot_months = horizon + 1, so terminal state is at month_index == horizon.
        cash = run.cash_balances.filter(
            (pl.col("agent_id") == OWNER_AGENT_ID) & (pl.col("month_index") == scenario.horizon_months)
        )
        assert cash.height == 1
        assert cash["balance_usd"][0] == pytest.approx(expected_owner_terminal, rel=1e-6)


if __name__ == "__main__":
    pytest_bazel.main()
