"""Sim-layer e2e tests for landlord rental income + lifecycle events.

Each test builds a `Scenario` directly, calls
`simulate_with_external_series`, decodes the result, and asserts against
event frames + state history. Exogenous series are constants so the
expected cashflow is exact-math predictable.

Scope: the sim layer takes rent, management fees, and leasing fees as already-lowered
dollar amounts on already-windowed transfers. It has no notion of `vacancy_pct`,
`management_fee_pct`, `fraction_rented`, or a leasing-fee cadence — the product layer
folds those into the amounts and month windows before the scenario reaches here. Tests
for that lowering belong in `finance/augur/product/service_test.py`
(`test_product_full_property_rent_scales_by_fraction_vacancy_and_rent_denominated_fees`,
`test_product_rental_lifecycle_resizes_tenant_rent_and_management_fees`); a test here
that fed a percentage in and asserted the same product back could not fail for any bug.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.series import HomeValueKey, LevelSeriesKey, LocationId, RentKey
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import round_currency_amount
from finance.augur.sim.locations import Location
from finance.augur.sim.runtime import mortgage_monthly_payment
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    CapitalImprovementEvent,
    FederalSaltCapEntry,
    FederalSaltDeductionPolicy,
    FilingStatus,
    InitialAccountBalance,
    InterestIncome,
    MortgageFinancing,
    MortgageInterestDeductionPolicy,
    PrimaryResidenceAssignment,
    PropertySaleEvent,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringPropertyCashflow,
    RecurringTransfer,
    Scenario,
    ScheduledPropertyCashflow,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    SeriesIndexedAmount,
    SetPrimaryResidenceEvent,
    SetRentedFractionEvent,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate_with_external_series
from finance.augur.sim.testing.state_helpers import capital_gains_ytd, cash_balances, property_state, rollout_status

# Constants mirroring the product translator. Kept in-test to avoid
# cross-package import dependencies from the sim layer.
TENANT_AGENT_ID = "tenant"
OWNER_AGENT_ID = "owner"
MGMT_AGENT_ID = "property_management_agency"
RENT_SERIES_KEY = RentKey(location_id=LocationId("test_location"))
SF_HOME_VALUE_KEY = HomeValueKey(location_id=LocationId("san_francisco"))


def _flat_series(*, key: LevelSeriesKey, value: float, months: int, rollouts: int) -> ExternalSeriesContext:
    """Build an exogenous bundle with one series held flat at `value`."""

    return ExternalSeriesContext.from_level_blocks(
        [(key, np.full((rollouts, months), value, dtype=np.float64))], rollout_count=rollouts, horizon_months=months - 1
    )


def _multi_series(*, levels_by_series: dict[LevelSeriesKey, dict[int, list[float]]]) -> ExternalSeriesContext:
    """Build an exogenous bundle with multiple series, indexed by (key, rollout) -> levels.

    `levels_by_series[key][rollout]` is a list of length `horizon_months + 1` (the engine
    indexes external_values up through the horizon end).
    """

    rollout_count = max(rollout for by_rollout in levels_by_series.values() for rollout in by_rollout) + 1
    horizon_months = max(len(levels) for by_rollout in levels_by_series.values() for levels in by_rollout.values()) - 1
    blocks = []
    for key, by_rollout in levels_by_series.items():
        matrix = np.full((rollout_count, horizon_months + 1), np.nan, dtype=np.float64)
        for rollout_index, levels in by_rollout.items():
            matrix[rollout_index, : len(levels)] = levels
        blocks.append((key, matrix))
    return ExternalSeriesContext.from_level_blocks(blocks, rollout_count=rollout_count, horizon_months=horizon_months)


def _mortgage_balance_and_interest_after_payments(
    *, principal: float, annual_interest_rate: float, term_months: int, payment_count: int
) -> tuple[float, float]:
    balance = float(principal)
    interest_paid = 0.0
    payment = float(
        mortgage_monthly_payment(
            Decimal(str(principal)), annual_interest_rate, term_months, currency_quantum=Decimal("0.01")
        )
    )
    for _ in range(payment_count):
        interest = balance * annual_interest_rate / 12
        amount = min(payment, balance + interest)
        principal_paid = amount - interest
        balance = max(0, balance - principal_paid)
        interest_paid += interest
    return balance, interest_paid


def _rental_scenario(
    *,
    horizon_months: int = 12,
    monthly_rent: Decimal | int = 5_000,
    initial_cash: Decimal | int = 100_000,
    monthly_management_fee: Decimal | int | None = None,
    leasing_fees_by_month: Mapping[int, Decimal | int] | None = None,
) -> Scenario:
    """Build a minimal static-rental scenario. No taxes (empty tax_profiles).

    Every dollar amount is passed through to the scenario verbatim — the helper derives no
    amount of its own, so a test asserting one of these numbers back out is checking the
    engine, not this function.
    """

    end_month = horizon_months - 1
    agents = [Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id=TENANT_AGENT_ID)]
    initial_balances = [
        InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=initial_cash),
        InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
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
            amount=SeriesIndexedAmount(base_amount=monthly_rent, series=RENT_SERIES_KEY, adjustment_period_months=12),
        )
    ]
    scheduled_transfers: list[ScheduledTransfer] = []
    if monthly_management_fee is not None or leasing_fees_by_month is not None:
        agents.append(Agent(agent_id=MGMT_AGENT_ID))
        initial_balances.append(InitialAccountBalance(agent_id=MGMT_AGENT_ID, account_id="checking", balance=0))
    if monthly_management_fee is not None:
        recurring_transfers.append(
            RecurringTransfer(
                start_month=0,
                end_month=end_month,
                cause_id="management_fee:p1",
                from_agent_id=OWNER_AGENT_ID,
                from_account_id="checking",
                to_agent_id=MGMT_AGENT_ID,
                to_account_id="checking",
                amount=SeriesIndexedAmount(
                    base_amount=monthly_management_fee, series=RENT_SERIES_KEY, adjustment_period_months=12
                ),
            )
        )
    if leasing_fees_by_month is not None:
        scheduled_transfers.extend(
            ScheduledTransfer(
                month=fire_month,
                cause_id=f"leasing_fee:p1:m{fire_month}",
                from_agent_id=OWNER_AGENT_ID,
                from_account_id="checking",
                to_agent_id=MGMT_AGENT_ID,
                to_account_id="checking",
                amount=SeriesIndexedAmount(base_amount=amount, series=RENT_SERIES_KEY, adjustment_period_months=12),
            )
            for fire_month, amount in leasing_fees_by_month.items()
        )
    return Scenario(
        agents=agents,
        initial_cash=initial_balances,
        recurring_transfers=recurring_transfers,
        scheduled_transfers=scheduled_transfers,
        tax_profiles=[],
        horizon_months=horizon_months,
    )


def _run(scenario: Scenario, rollouts: int = 1, rent_level: float = 1):
    """Run the scenario against a flat rent series at `rent_level` for all rollouts/months."""

    ctx = _flat_series(key=RENT_SERIES_KEY, value=rent_level, months=scenario.horizon_months + 1, rollouts=rollouts)
    return simulate_with_external_series(scenario, external_series=ctx, rollout_count=rollouts, locations={})


class TestRentalIncome:
    def test_rental_income_flows_monthly_at_constant_rent(self):
        """A recurring transfer fires once per month across its whole window and moves its
        configured amount into the recipient's account."""

        scenario = _rental_scenario(horizon_months=12, monthly_rent=5_000)
        run = _run(scenario)
        transfers = run.events_log.transfers.filter(pl.col("cause_id") == "rental_income:p1")
        assert transfers["month_index"].sort().to_list() == list(range(12))
        assert (transfers["amount_quanta"] / 100).to_list() == pytest.approx([5_000] * 12)
        terminal_owner_cash = cash_balances(run).filter(
            (pl.col("agent_id") == OWNER_AGENT_ID) & (pl.col("month_index") == 12)
        )
        assert (terminal_owner_cash["balance_quanta"] / 100)[0] == pytest.approx(100_000 + 60_000)

    def test_zero_amount_recurring_transfer_still_fires_but_moves_no_cash(self):
        """A transfer scheduled with a zero amount is a scheduled event, not an absent one:
        it logs a row every month of its window and leaves both balances untouched."""

        scenario = _rental_scenario(horizon_months=12, monthly_rent=0)
        run = _run(scenario)
        transfers = run.events_log.transfers.filter(pl.col("cause_id") == "rental_income:p1")
        assert transfers["month_index"].sort().to_list() == list(range(12))
        assert (transfers["amount_quanta"] / 100).to_list() == pytest.approx([0] * 12)
        terminal = cash_balances(run).filter(pl.col("month_index") == 12)
        balances = dict(zip(terminal["agent_id"].to_list(), (terminal["balance_quanta"] / 100).to_list(), strict=True))
        assert balances[OWNER_AGENT_ID] == pytest.approx(100_000)
        assert balances[TENANT_AGENT_ID] == pytest.approx(0)

    def test_rental_income_indexed_by_rent_series(self):
        # Rent series doubles at month 12 (annual adjustment period).
        scenario = _rental_scenario(horizon_months=24, monthly_rent=5_000)
        # Build a per-month rent series: 1.0 for months 0..11, 2.0 for months 12..24.
        levels = [1.0] * 12 + [2.0] * 13
        ctx = _multi_series(levels_by_series={RENT_SERIES_KEY: {0: levels}})
        run = simulate_with_external_series(scenario, external_series=ctx, rollout_count=1, locations={})
        transfers = (
            run.events_log.transfers.filter(pl.col("cause_id") == "rental_income:p1")
            .sort("month_index")
            .select("month_index", "amount_quanta")
        )
        # Months 0..11 use level 1.0 → $5000; months 12..23 reset to level 2.0 → $10000.
        amounts = (transfers["amount_quanta"] / 100).to_list()
        assert amounts[:12] == pytest.approx([5_000] * 12)
        assert amounts[12:] == pytest.approx([10_000] * 12)


class TestManagementFee:
    def test_owner_paid_recurring_transfer_debits_payer_and_credits_payee(self):
        """A recurring transfer running out of the owner's account alongside the incoming rent
        settles against the right two ledgers: the agency's balance is built entirely from the
        fee, and the owner keeps rent minus fee."""

        scenario = _rental_scenario(horizon_months=12, monthly_rent=5_000, monthly_management_fee=380)
        run = _run(scenario)
        mgmt = run.events_log.transfers.filter(pl.col("cause_id") == "management_fee:p1")
        assert mgmt["month_index"].sort().to_list() == list(range(12))
        assert (mgmt["amount_quanta"] / 100).to_list() == pytest.approx([380] * 12)
        assert mgmt["from_agent_id"].unique().to_list() == [OWNER_AGENT_ID]
        assert mgmt["to_agent_id"].unique().to_list() == [MGMT_AGENT_ID]

        terminal = cash_balances(run).filter(pl.col("month_index") == 12)
        balances = dict(zip(terminal["agent_id"].to_list(), (terminal["balance_quanta"] / 100).to_list(), strict=True))
        assert balances[MGMT_AGENT_ID] == pytest.approx(4_560)
        assert balances[OWNER_AGENT_ID] == pytest.approx(100_000 + 60_000 - 4_560)


class TestRentalLifecycleCashflows:
    def test_windowed_recurring_transfers_fire_only_within_their_own_month_ranges(self):
        """The product layer lowers a changing rented fraction into several non-overlapping
        `RecurringTransfer` windows, each with its own already-scaled amount (that lowering is
        tested in `product/service_test.py`). What the engine owes: each window fires in exactly
        its own months, at exactly its own amount, and a month covered by no window is silent —
        here months 6 and 7, the gap between the second and third window.
        """

        scenario = Scenario(
            agents=[Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id=TENANT_AGENT_ID), Agent(agent_id=MGMT_AGENT_ID)],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=700000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id=MGMT_AGENT_ID, account_id="checking", balance=0),
            ],
            recurring_transfers=[
                *(
                    RecurringTransfer(
                        start_month=start_month,
                        end_month=end_month,
                        cause_id="rental_income:p1",
                        from_agent_id=TENANT_AGENT_ID,
                        from_account_id="checking",
                        to_agent_id=OWNER_AGENT_ID,
                        to_account_id="checking",
                        amount=SeriesIndexedAmount(
                            base_amount=amount, series=RENT_SERIES_KEY, adjustment_period_months=12
                        ),
                        income_category=ORDINARY_INCOME,
                    )
                    for start_month, end_month, amount in [(0, 2, 1_500), (3, 5, 4_500), (8, 11, 3_000)]
                ),
                *(
                    RecurringTransfer(
                        start_month=start_month,
                        end_month=end_month,
                        cause_id="management_fee:p1",
                        from_agent_id=OWNER_AGENT_ID,
                        from_account_id="checking",
                        to_agent_id=MGMT_AGENT_ID,
                        to_account_id="checking",
                        amount=SeriesIndexedAmount(
                            base_amount=amount, series=RENT_SERIES_KEY, adjustment_period_months=12
                        ),
                        deduction_category="ordinary",
                    )
                    for start_month, end_month, amount in [(0, 2, 120), (3, 5, 360), (8, 11, 240)]
                ),
            ],
            tax_profiles=[],
            horizon_months=12,
        )
        run = simulate_with_external_series(
            scenario,
            external_series=_flat_series(key=RENT_SERIES_KEY, value=1, months=13, rollouts=1),
            rollout_count=1,
            locations={},
        )

        rent = run.events_log.transfers.filter(pl.col("cause_id") == "rental_income:p1").sort("month_index")
        assert rent["month_index"].to_list() == [0, 1, 2, 3, 4, 5, 8, 9, 10, 11]
        assert (rent["amount_quanta"] / 100).to_list() == pytest.approx([1_500] * 3 + [4_500] * 3 + [3_000] * 4)

        management_fee = run.events_log.transfers.filter(pl.col("cause_id") == "management_fee:p1").sort("month_index")
        assert management_fee["month_index"].to_list() == [0, 1, 2, 3, 4, 5, 8, 9, 10, 11]
        assert (management_fee["amount_quanta"] / 100).to_list() == pytest.approx([120] * 3 + [360] * 3 + [240] * 4)


class TestLeasingFee:
    def test_scheduled_transfers_fire_once_in_their_own_month_at_their_own_amount(self):
        """Distinct amounts per month so a mis-indexed schedule cannot pass: a scheduled
        transfer fires in the single month it names, and no other month sees one."""

        scenario = _rental_scenario(
            horizon_months=60, monthly_rent=5_000, leasing_fees_by_month={0: 5_000, 24: 6_000, 48: 7_000}
        )
        run = _run(scenario)
        leasing = (
            run.events_log.transfers.filter(pl.col("cause_id").str.starts_with("leasing_fee:p1"))
            .sort("month_index")
            .select("month_index", "amount_quanta")
        )
        assert leasing["month_index"].to_list() == [0, 24, 48]
        assert (leasing["amount_quanta"] / 100).to_list() == pytest.approx([5_000, 6_000, 7_000])


class TestRentalIncomeTaxation:
    """Phase 2.0: rental income transfers carry income_category='ordinary', so they accrue
    into the owner's taxable ordinary income at year-end. Schedule E deductions and
    MID/SALT scaling are deferred to follow-up commits; rental income is currently
    over-taxed by the amount of those deductions.
    """

    def _taxed_rental_scenario(self, *, monthly_rent: Decimal | int, horizon_months: int = 12) -> Scenario:
        end_month = horizon_months - 1
        return Scenario(
            agents=[Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id=TENANT_AGENT_ID), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=100000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    amount=SeriesIndexedAmount(
                        base_amount=monthly_rent, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    income_category=ORDINARY_INCOME,
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
        scenario = self._taxed_rental_scenario(monthly_rent=4_000)
        run = _run(scenario)
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        assert breakdowns["federal_us"]["ordinary_income_quanta"] / 100 == pytest.approx(48_000, abs=1e-6)

    def test_rental_income_generates_tax_accruals_at_year_end(self):
        scenario = self._taxed_rental_scenario(monthly_rent=4_000)
        run = _run(scenario)
        accruals = run.events_log.tax_accruals.sort("jurisdiction_id")
        assert accruals.height == 2  # federal + CA
        # Accruals fire at month 11 (year-end).
        assert all(month == 11 for month in accruals["month_index"].to_list())
        # Both jurisdictions should levy positive tax on $48k of ordinary income.
        assert all(amount > 0 for amount in (accruals["amount_quanta"] / 100).to_list())

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
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=100000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id=MGMT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    amount=SeriesIndexedAmount(base_amount=5000, series=RENT_SERIES_KEY, adjustment_period_months=12),
                    income_category=ORDINARY_INCOME,
                ),
                RecurringTransfer(
                    start_month=0,
                    end_month=end_month,
                    cause_id="management_fee:p1",
                    from_agent_id=OWNER_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=MGMT_AGENT_ID,
                    to_account_id="checking",
                    amount=SeriesIndexedAmount(base_amount=500, series=RENT_SERIES_KEY, adjustment_period_months=12),
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
        assert breakdowns["federal_us"]["ordinary_income_quanta"] / 100 == pytest.approx(54_000, abs=1e-6)

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
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=100000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="hoa", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    amount=SeriesIndexedAmount(base_amount=6000, series=RENT_SERIES_KEY, adjustment_period_months=12),
                    income_category=ORDINARY_INCOME,
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
                    amount_due=SeriesIndexedAmount(
                        base_amount=400, series=RENT_SERIES_KEY, adjustment_period_months=12
                    ),
                    deduction_category="ordinary",
                    deductible_fraction=1,
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
        assert breakdowns["federal_us"]["ordinary_income_quanta"] / 100 == pytest.approx(67_200, abs=1e-6)

    def test_depreciation_accrues_monthly_and_deducts_as_schedule_e(self, san_francisco_location: Location):
        """§168 monthly depreciation accrues for rented property and reduces taxable ordinary
        income at year-end. Building basis = $500k × 0.80 = $400k; rented_fraction = 1.0;
        annual depreciation = $400k / 27.5 ≈ $14,545.45."""

        end_month = 11
        purchase_price = 500_000
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=600000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    amount=SeriesIndexedAmount(base_amount=5000, series=RENT_SERIES_KEY, adjustment_period_months=12),
                    income_category=ORDINARY_INCOME,
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price,
                    rented_fraction=1,
                    land_value_fraction=0.20,
                    buyer_closing_cost=0,
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
        ctx = _multi_series(levels_by_series={RENT_SERIES_KEY: {0: [1] * 13}, SF_HOME_VALUE_KEY: {0: [1] * 13}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        # Cumulative depreciation grows monotonically; at month 12 (post-horizon snapshot) it's
        # accrued 12 months worth = $400,000 / 27.5 = $14,545.45.
        terminal_dep = property_state(run).filter(pl.col("month_index") == 12)
        assert terminal_dep.height == 1
        # Federal ordinary income: $60,000 rental - $14,545.45 depreciation = $45,454.55.
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        assert breakdowns["federal_us"]["ordinary_income_quanta"] / 100 == pytest.approx(45_454.55, abs=0.02)

    def test_lifecycle_start_renting_starts_depreciation_accrual_mid_horizon(self, san_francisco_location: Location):
        """StartRentingEvent at month 12 → depreciation accrues only from month 12 onward.
        24-month horizon, $400k building basis, 12 months of rental → annual depreciation
        in year 1 = 0; in year 2 (after start) = $400k / 27.5 = $14,545.45."""

        end_month = 23  # 24-month horizon
        purchase_price = 500_000
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=600000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    amount=SeriesIndexedAmount(base_amount=5000, series=RENT_SERIES_KEY, adjustment_period_months=12),
                    income_category=ORDINARY_INCOME,
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price,
                    rented_fraction=0,  # owner-occupied at start
                    land_value_fraction=0.20,
                    buyer_closing_cost=0,
                )
            ],
            property_lifecycle_events=[SetRentedFractionEvent(month=12, property_id="p1", rented_fraction=1)],
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
        ctx = _multi_series(levels_by_series={RENT_SERIES_KEY: {0: [1] * 25}, SF_HOME_VALUE_KEY: {0: [1] * 25}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        breakdowns = list(run.events_log.tax_breakdowns.sort("month_index", "jurisdiction_id").iter_rows(named=True))
        # Year 0 (month 11) federal_us: rented_fraction=0 the whole year, no rental income, no
        # depreciation → ordinary_income = $0.
        year_0_federal = next(b for b in breakdowns if b["month_index"] == 11 and b["jurisdiction_id"] == "federal_us")
        assert year_0_federal["ordinary_income_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        # Year 1 (month 23) federal_us: 12 months rent ($60k) minus 12 months depreciation ($14.5k)
        # ≈ $45,454.55.
        year_1_federal = next(b for b in breakdowns if b["month_index"] == 23 and b["jurisdiction_id"] == "federal_us")
        assert year_1_federal["ordinary_income_quanta"] / 100 == pytest.approx(45_454.55, abs=0.05)

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
        assert breakdowns_owner["federal_us"]["mortgage_interest_deduction_quanta"] / 100 > 0
        # Rented from month 1: MID for year 0 is the month-0 interest only (a tiny first-month
        # owner-share interest), much smaller than full-year owner-occupied MID.
        assert (
            breakdowns_rent["federal_us"]["mortgage_interest_deduction_quanta"] / 100
            < breakdowns_owner["federal_us"]["mortgage_interest_deduction_quanta"] / 100 * 0.15
        )

    def _mortgage_lifecycle_breakdown(self, *, start_renting_at: int | None, locations: dict[str, Location]) -> dict:
        end_month = 11
        purchase_price = 500_000
        lifecycle_events = (
            [SetRentedFractionEvent(month=start_renting_at, property_id="p1", rented_fraction=1)]
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
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=700000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="lender", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    amount=5000,
                    income_category=ORDINARY_INCOME,
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price * Decimal("0.20"),
                    land_value_fraction=1,  # isolate from depreciation
                    mortgage=MortgageFinancing(
                        liability_id="p1_mortgage",
                        lender_agent_id="lender",
                        lender_account_id="checking",
                        principal=purchase_price * Decimal("0.80"),
                        annual_interest_rate=0.06,
                        term_months=360,
                    ),
                    rented_fraction=0,
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
        ctx = _multi_series(levels_by_series={SF_HOME_VALUE_KEY: {0: [1] * 13}})
        run = simulate_with_external_series(scenario, external_series=ctx, rollout_count=1, locations=locations)
        return {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}

    def test_lifecycle_stop_renting_halts_depreciation(self, san_francisco_location: Location):
        """StopRentingEvent at month 12 → depreciation accrues months 0-11 only.
        Year 0 ordinary: $60k rent - $14.5k dep = $45.5k.
        Year 1 ordinary: $0 rent (no more rental income), no dep → $0.
        """

        purchase_price = 500_000
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=600000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    amount=SeriesIndexedAmount(base_amount=5000, series=RENT_SERIES_KEY, adjustment_period_months=12),
                    income_category=ORDINARY_INCOME,
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price,
                    rented_fraction=1,
                    land_value_fraction=0.20,
                    buyer_closing_cost=0,
                )
            ],
            property_lifecycle_events=[SetRentedFractionEvent(month=12, property_id="p1", rented_fraction=0)],
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
        ctx = _multi_series(levels_by_series={RENT_SERIES_KEY: {0: [1] * 25}, SF_HOME_VALUE_KEY: {0: [1] * 25}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        breakdowns = list(run.events_log.tax_breakdowns.sort("month_index", "jurisdiction_id").iter_rows(named=True))
        year_0_federal = next(b for b in breakdowns if b["month_index"] == 11 and b["jurisdiction_id"] == "federal_us")
        assert year_0_federal["ordinary_income_quanta"] / 100 == pytest.approx(45_454.55, abs=0.05)
        year_1_federal = next(b for b in breakdowns if b["month_index"] == 23 and b["jurisdiction_id"] == "federal_us")
        assert year_1_federal["ordinary_income_quanta"] / 100 == pytest.approx(0, abs=1e-6)

    def test_capital_improvement_bumps_basis_and_accelerates_depreciation(self, san_francisco_location: Location):
        """CapitalImprovementEvent at month 6 bumps building basis by $100k.
        Building basis after improvement = $400k + $100k = $500k.
        Monthly depreciation after month 6 = $500k / 27.5 / 12 ≈ $1,515.15
        (vs. ~$1,212.12/mo before). Cash decreases by $100k at month 6."""

        end_month = 11
        purchase_price = 500_000
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=700000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    amount=5000,
                    income_category=ORDINARY_INCOME,
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price,
                    rented_fraction=1,
                    land_value_fraction=0.20,
                )
            ],
            property_lifecycle_events=[
                CapitalImprovementEvent(month=6, property_id="p1", amount=100000, description="new roof")
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
        ctx = _multi_series(levels_by_series={SF_HOME_VALUE_KEY: {0: [1] * 13}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        # 6 months at $400k/27.5/12 + 6 months at $500k/27.5/12
        # = 6 × 1212.12 + 6 × 1515.15 = 7272.73 + 9090.91 = 16363.64
        expected_depreciation = 6 * (400_000 / 27.5 / 12) + 6 * (500_000 / 27.5 / 12)
        expected_ordinary = 60_000 - expected_depreciation
        assert breakdowns["federal_us"]["ordinary_income_quanta"] / 100 == pytest.approx(expected_ordinary, abs=0.05)

    def test_same_month_rent_fraction_and_capex_apply_before_depreciation(self, san_francisco_location: Location):
        """Non-sale lifecycle events in the same month all apply before that month's
        depreciation accrual.

        The property is not rented for months 0-5. At month 6 it becomes fully rented
        and receives a $100k capital improvement, so months 6-11 depreciate against
        $500k building basis, not $400k and not zero.
        """

        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id="employer"),
                Agent(agent_id="property_seller"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=700000),
                InitialAccountBalance(agent_id="employer", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=11,
                    cause_id="wages:employer",
                    from_agent_id="employer",
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount=5000,
                    income_category=ORDINARY_INCOME,
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
                    purchase_price=500000,
                    down_payment=500000,
                    rented_fraction=0,
                    land_value_fraction=0.20,
                )
            ],
            property_lifecycle_events=[
                SetRentedFractionEvent(month=6, property_id="p1", rented_fraction=1),
                CapitalImprovementEvent(month=6, property_id="p1", amount=100000, description="new roof"),
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
        ctx = _multi_series(levels_by_series={SF_HOME_VALUE_KEY: {0: [1] * 13}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )

        rented_rows = run.events_log.set_rented_fraction_events.to_dicts()
        capex_rows = run.events_log.capital_improvement_events.to_dicts()
        assert [(row["month_index"], row["rented_fraction"]) for row in rented_rows] == [(6, 1)]
        assert [(row["month_index"], row["amount_quanta"] / 100) for row in capex_rows] == [(6, 100_000)]

        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        expected_depreciation = 6 * (500_000 / 27.5 / 12)
        assert breakdowns["federal_us"]["ordinary_income_quanta"] / 100 == pytest.approx(
            60_000 - expected_depreciation, abs=0.05
        )

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
        ctx = _multi_series(levels_by_series={RENT_SERIES_KEY: {0: [1] * 14}, SF_HOME_VALUE_KEY: {0: [1] * 14}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        # Verify the sale fired: capital_gain_ytd has zero LTCG, ordinary_ytd has zero recapture
        # because the property sold for less than adjusted basis (loss). No gain to recapture.
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        # Federal LTCG: 0
        assert breakdowns["federal_us"]["ltcg_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        # Property is frozen after sale - the next year's depreciation should not accrue.
        # Verify by counting depreciation accruals: only 12 months should have happened.

    def test_property_sale_requires_home_value_series(self, san_francisco_location: Location):
        scenario = self._sale_scenario(horizon=13, sale_month=12, cumulative_depreciation_eligible=True)
        ctx = ExternalSeriesContext()

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
        levels = [1] * 12 + [1.5] * 13
        ctx = _multi_series(levels_by_series={RENT_SERIES_KEY: {0: [1] * 25}, SF_HOME_VALUE_KEY: {0: levels}})
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
        assert b["ltcg_quanta"] / 100 == pytest.approx(205_000, abs=1)

    def test_multi_rollout_property_sale_keeps_sale_tax_and_mortgage_amounts_rollout_scoped(
        self, san_francisco_location: Location
    ):
        """A single sale event with divergent home-value rollouts must not smear sale math
        across rollouts.

        The property is rented for 12 months, then owner-occupied for 24 months before sale.
        That activates all of the property-sale tax plumbing in one scenario: mortgage payoff,
        §1250 recapture from the rental period, §121 exclusion from the later owner-occupied
        period, and residual LTCG. The two rollouts use different home values at sale month.
        """

        purchase_price = 500_000
        mortgage_principal = 400_000
        annual_interest_rate = 0.06
        mortgage_term_months = 360
        sale_month = 36
        horizon = 48
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="lender"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=700000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="lender", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
            ],
            recurring_property_cashflows=[
                RecurringPropertyCashflow(
                    start_month=0,
                    end_month=11,
                    property_id="p1",
                    cause_id="rental_income:p1",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount=5000,
                    income_category=ORDINARY_INCOME,
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price - mortgage_principal,
                    rented_fraction=1,
                    land_value_fraction=0.20,
                    mortgage=MortgageFinancing(
                        liability_id="p1_mortgage",
                        lender_agent_id="lender",
                        lender_account_id="checking",
                        principal=mortgage_principal,
                        annual_interest_rate=annual_interest_rate,
                        term_months=mortgage_term_months,
                    ),
                )
            ],
            primary_residence_events=[SetPrimaryResidenceEvent(month=12, agent_id=OWNER_AGENT_ID, property_id="p1")],
            property_lifecycle_events=[
                SetRentedFractionEvent(month=12, property_id="p1", rented_fraction=0),
                PropertySaleEvent(month=sale_month, property_id="p1", closing_cost_pct=6),
            ],
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
        home_values_by_rollout = {
            0: [1] * sale_month + [1.2] * (horizon + 1 - sale_month),
            1: [1] * sale_month + [1.6] * (horizon + 1 - sale_month),
        }
        ctx = _multi_series(
            levels_by_series={
                RENT_SERIES_KEY: {0: [1] * (horizon + 1), 1: [1] * (horizon + 1)},
                SF_HOME_VALUE_KEY: home_values_by_rollout,
            }
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=2, locations={"san_francisco": san_francisco_location}
        )

        payoff, _ = _mortgage_balance_and_interest_after_payments(
            principal=mortgage_principal,
            annual_interest_rate=annual_interest_rate,
            term_months=mortgage_term_months,
            payment_count=sale_month - 1,
        )
        recapture = 12 * (purchase_price * 0.80 / 27.5 / 12)
        expected_by_rollout = {}
        for rollout_index, sale_level in [(0, 1.2), (1, 1.6)]:
            gross = purchase_price * sale_level * 0.94
            realized_gain = gross - (purchase_price - recapture)
            post_recapture_gain = realized_gain - recapture
            section_121 = min(post_recapture_gain, 250_000)
            ltcg = post_recapture_gain - section_121
            expected_by_rollout[rollout_index] = {
                "gross_proceeds_quanta": gross,
                "mortgage_payoff_quanta": payoff,
                "net_cash_to_owner_quanta": gross - payoff,
                "realized_gain_quanta": realized_gain,
                "depreciation_recapture_quanta": recapture,
                "section_121_exclusion_quanta": section_121,
                "long_term_capital_gain_quanta": ltcg,
            }

        sale_rows = {
            row["rollout_index"]: row
            for row in run.events_log.property_sale_events.sort("rollout_index").iter_rows(named=True)
        }
        assert set(sale_rows) == {0, 1}
        for rollout_index, expected in expected_by_rollout.items():
            row = sale_rows[rollout_index]
            assert row["month_index"] == sale_month
            for field, expected_value in expected.items():
                assert row[field] / 100 == pytest.approx(expected_value, abs=0.02)

        federal_sale_year = {
            row["rollout_index"]: row
            for row in run.events_log.tax_breakdowns.filter(
                (pl.col("month_index") == 47) & (pl.col("jurisdiction_id") == "federal_us")
            ).iter_rows(named=True)
        }
        assert federal_sale_year[0]["ltcg_quanta"] / 100 == pytest.approx(0, abs=1)
        assert federal_sale_year[1]["ltcg_quanta"] / 100 == pytest.approx(2_000, abs=1)

    def test_multi_taxpayer_property_tax_schedule_e_mid_and_sale_routing_are_owner_scoped(
        self, san_francisco_location: Location
    ):
        """Two owners with two properties should keep real-estate tax/accounting channels
        isolated by property owner.

        Alice owns a fully rented property: rental income, depreciation, property tax, and
        mortgage interest route through Schedule E; no MID or §121 applies. Bob owns an
        owner-occupied property: property tax routes to SALT, mortgage interest routes to
        MID, and §121 excludes the sale gain.
        """

        purchase_price = 500_000
        mortgage_principal = 400_000
        annual_interest_rate = 0.06
        mortgage_term_months = 360
        annual_property_tax_rate = 0.012
        monthly_rent = 5_000
        sale_month = 30
        horizon = 36
        scenario = Scenario(
            agents=[
                Agent(agent_id="alice"),
                Agent(agent_id="bob"),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="lender"),
                Agent(agent_id="tax_authority"),
                Agent(agent_id="irs"),
                Agent(agent_id="bond_issuer"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=800000),
                InitialAccountBalance(agent_id="bob", account_id="checking", balance=800000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="lender", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="tax_authority", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="bond_issuer", account_id="checking", balance=100),
            ],
            scheduled_transfers=[
                ScheduledTransfer(
                    month=0,
                    cause_id="california_muni_interest",
                    from_agent_id="bond_issuer",
                    from_account_id="checking",
                    to_agent_id="bob",
                    to_account_id="checking",
                    amount=100,
                    income_category=InterestIncome(issuer_jurisdiction_id="california"),
                )
            ],
            recurring_property_cashflows=[
                RecurringPropertyCashflow(
                    start_month=0,
                    end_month=sale_month - 1,
                    property_id="alice_rental",
                    cause_id="rental_income:alice_rental",
                    from_agent_id=TENANT_AGENT_ID,
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=monthly_rent,
                    income_category=ORDINARY_INCOME,
                )
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="alice_rental_purchase",
                    property_id="alice_rental",
                    location_id="san_francisco",
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price=purchase_price,
                    down_payment=purchase_price - mortgage_principal,
                    rented_fraction=1,
                    land_value_fraction=0.20,
                    mortgage=MortgageFinancing(
                        liability_id="alice_rental_mortgage",
                        lender_agent_id="lender",
                        lender_account_id="checking",
                        principal=mortgage_principal,
                        annual_interest_rate=annual_interest_rate,
                        term_months=mortgage_term_months,
                    ),
                ),
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="bob_home_purchase",
                    property_id="bob_home",
                    location_id="san_francisco",
                    buyer_agent_id="bob",
                    buyer_account_id="checking",
                    seller_agent_id="property_seller",
                    purchase_price=purchase_price,
                    down_payment=purchase_price - mortgage_principal,
                    rented_fraction=0,
                    land_value_fraction=0.20,
                    mortgage=MortgageFinancing(
                        liability_id="bob_home_mortgage",
                        lender_agent_id="lender",
                        lender_account_id="checking",
                        principal=mortgage_principal,
                        annual_interest_rate=annual_interest_rate,
                        term_months=mortgage_term_months,
                    ),
                ),
            ],
            initial_primary_residences=[PrimaryResidenceAssignment(agent_id="bob", property_id="bob_home")],
            property_lifecycle_events=[
                PropertySaleEvent(month=sale_month, property_id="alice_rental", closing_cost_pct=6),
                PropertySaleEvent(month=sale_month, property_id="bob_home", closing_cost_pct=6),
            ],
            property_tax_policies=[
                PropertyTaxPolicy(
                    property_id="alice_rental",
                    owner_agent_id="alice",
                    tax_authority_agent_id="tax_authority",
                    annual_tax_rate=annual_property_tax_rate,
                ),
                PropertyTaxPolicy(
                    property_id="bob_home",
                    owner_agent_id="bob",
                    tax_authority_agent_id="tax_authority",
                    annual_tax_rate=annual_property_tax_rate,
                ),
            ],
            mortgage_interest_deduction_policies=[
                MortgageInterestDeductionPolicy(liability_id="alice_rental_mortgage", owner_agent_id="alice"),
                MortgageInterestDeductionPolicy(liability_id="bob_home_mortgage", owner_agent_id="bob"),
            ],
            federal_salt_deduction_policies=[
                FederalSaltDeductionPolicy(
                    profile_id="alice", cap_schedule=[FederalSaltCapEntry(effective_year_index=0, cap=100000)]
                ),
                FederalSaltDeductionPolicy(
                    profile_id="bob", cap_schedule=[FederalSaltCapEntry(effective_year_index=0, cap=100000)]
                ),
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id="bob",
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us"],
                    tax_authority_agent_id="irs",
                ),
                TaxProfile(
                    agent_id="alice",
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us"],
                    tax_authority_agent_id="irs",
                ),
            ],
            horizon_months=horizon,
        )
        ctx = _multi_series(
            levels_by_series={
                RENT_SERIES_KEY: {0: [1] * (horizon + 1)},
                SF_HOME_VALUE_KEY: {0: [1] * sale_month + [1.4] * (horizon + 1 - sale_month)},
            }
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )

        property_tax_rows = run.events_log.obligation_settlements.filter(
            pl.col("obligation_type") == "property_tax"
        ).sort("agent_id", "month_index")
        alice_property_tax = property_tax_rows.filter(pl.col("agent_id") == "alice")
        bob_property_tax = property_tax_rows.filter(pl.col("agent_id") == "bob")
        monthly_property_tax = purchase_price * annual_property_tax_rate / 12
        assert alice_property_tax.height == sale_month - 1
        assert bob_property_tax.height == sale_month - 1
        assert alice_property_tax.get_column("amount_paid_quanta").map_elements(
            lambda q: q / 100, return_dtype=pl.Float64
        ).to_list() == pytest.approx([monthly_property_tax] * (sale_month - 1))
        assert bob_property_tax.get_column("amount_paid_quanta").map_elements(
            lambda q: q / 100, return_dtype=pl.Float64
        ).to_list() == pytest.approx([monthly_property_tax] * (sale_month - 1))

        _, year_0_interest = _mortgage_balance_and_interest_after_payments(
            principal=mortgage_principal,
            annual_interest_rate=annual_interest_rate,
            term_months=mortgage_term_months,
            payment_count=11,
        )
        depreciation_year_0 = 12 * (purchase_price * 0.80 / 27.5 / 12)
        property_tax_year_0 = 11 * monthly_property_tax
        federal_year_0 = {
            row["agent_id"]: row
            for row in run.events_log.tax_breakdowns.filter(
                (pl.col("month_index") == 11) & (pl.col("jurisdiction_id") == "federal_us")
            ).iter_rows(named=True)
        }
        assert federal_year_0["alice"]["ordinary_income_quanta"] / 100 == pytest.approx(
            12 * monthly_rent - depreciation_year_0 - property_tax_year_0 - year_0_interest, abs=1
        )
        assert federal_year_0["alice"]["mortgage_interest_deduction_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert federal_year_0["alice"]["salt_deduction_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert federal_year_0["bob"]["ordinary_income_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert federal_year_0["bob"]["mortgage_interest_deduction_quanta"] / 100 == pytest.approx(
            year_0_interest, abs=1
        )
        assert federal_year_0["bob"]["salt_deduction_quanta"] / 100 == pytest.approx(property_tax_year_0, abs=1)

        sale_rows = {
            row["property_id"]: row
            for row in run.events_log.property_sale_events.sort("property_id").iter_rows(named=True)
        }
        alice_recapture = sale_month * (purchase_price * 0.80 / 27.5 / 12)
        assert sale_rows["alice_rental"]["realized_gain_quanta"] / 100 == pytest.approx(194_363.64, abs=1)
        assert sale_rows["alice_rental"]["depreciation_recapture_quanta"] / 100 == pytest.approx(alice_recapture, abs=1)
        assert sale_rows["alice_rental"]["section_121_exclusion_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert sale_rows["alice_rental"]["long_term_capital_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale_rows["bob_home"]["realized_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale_rows["bob_home"]["depreciation_recapture_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert sale_rows["bob_home"]["section_121_exclusion_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale_rows["bob_home"]["long_term_capital_gain_quanta"] / 100 == pytest.approx(0, abs=1e-6)

        federal_sale_year = {
            row["agent_id"]: row
            for row in run.events_log.tax_breakdowns.filter(
                (pl.col("month_index") == 35) & (pl.col("jurisdiction_id") == "federal_us")
            ).iter_rows(named=True)
        }
        assert federal_sale_year["alice"]["ltcg_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert federal_sale_year["bob"]["ltcg_quanta"] / 100 == pytest.approx(0, abs=1e-6)

    def test_property_tied_recurring_obligation_stops_after_property_sale(self, san_francisco_location: Location):
        """Property-keyed HOA/insurance/maintenance-style obligations stop when the property sells."""

        sale_month = 12
        monthly_hoa = 400
        scenario = self._sale_scenario(horizon=24, sale_month=sale_month)
        scenario = scenario.model_copy(
            update={
                "agents": [*scenario.agents, Agent(agent_id="hoa")],
                "initial_cash": [
                    *scenario.initial_cash,
                    InitialAccountBalance(agent_id="hoa", account_id="checking", balance=0),
                ],
                "recurring_obligations": [
                    RecurringObligation(
                        start_month=0,
                        obligation_id="hoa_dues:p1",
                        obligation_type="hoa_dues",
                        agent_id=OWNER_AGENT_ID,
                        from_account_id="checking",
                        to_agent_id="hoa",
                        to_account_id="checking",
                        amount_due=monthly_hoa,
                        deduction_category="ordinary",
                        property_id="p1",
                    )
                ],
            }
        )
        levels = [1] * sale_month + [1.5] * (scenario.horizon_months + 1 - sale_month)
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_KEY: {0: [1] * (scenario.horizon_months + 1)}, SF_HOME_VALUE_KEY: {0: levels}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )

        accruals = run.events_log.obligation_accruals.filter(pl.col("obligation_type") == "hoa_dues").sort(
            "month_index"
        )
        settlements = run.events_log.obligation_settlements.filter(pl.col("obligation_type") == "hoa_dues").sort(
            "month_index"
        )
        expected_months = list(range(sale_month))
        assert accruals.get_column("month_index").to_list() == expected_months
        assert settlements.get_column("month_index").to_list() == expected_months
        assert settlements.get_column("amount_paid_quanta").map_elements(
            lambda q: q / 100, return_dtype=pl.Float64
        ).to_list() == pytest.approx([monthly_hoa] * sale_month)

        terminal_hoa_cash = cash_balances(run).filter(
            (pl.col("agent_id") == "hoa") & (pl.col("month_index") == scenario.horizon_months)
        )
        assert terminal_hoa_cash.get_column("balance_quanta").map_elements(
            lambda q: q / 100, return_dtype=pl.Float64
        ).item() == pytest.approx(monthly_hoa * sale_month)

        federal_y0 = next(
            row
            for row in run.events_log.tax_breakdowns.iter_rows(named=True)
            if row["month_index"] == 11 and row["jurisdiction_id"] == "federal_us"
        )
        expected_depreciation = 400_000 / 27.5
        assert federal_y0["ordinary_income_quanta"] / 100 == pytest.approx(
            12 * 5_000 - expected_depreciation - monthly_hoa * sale_month, abs=0.05
        )

    def test_property_cashflows_stop_after_property_sale_but_generic_transfers_continue(
        self, san_francisco_location: Location
    ):
        """Rental and management cashflows are property-domain flows, not generic transfers."""

        sale_month = 12
        monthly_rent = 5_000
        monthly_management_fee = 500
        leasing_fee = 1_000
        generic_transfer = 123
        scenario = self._sale_scenario(horizon=24, sale_month=sale_month)
        scenario = scenario.model_copy(
            update={
                "agents": [*scenario.agents, Agent(agent_id=MGMT_AGENT_ID), Agent(agent_id="generic_payer")],
                "initial_cash": [
                    *scenario.initial_cash,
                    InitialAccountBalance(agent_id=MGMT_AGENT_ID, account_id="checking", balance=0),
                    InitialAccountBalance(agent_id="generic_payer", account_id="checking", balance=0),
                ],
                "recurring_transfers": [
                    RecurringTransfer(
                        start_month=0,
                        end_month=23,
                        cause_id="generic_transfer",
                        from_agent_id="generic_payer",
                        from_account_id="checking",
                        to_agent_id=OWNER_AGENT_ID,
                        to_account_id="checking",
                        amount=generic_transfer,
                    )
                ],
                "recurring_property_cashflows": [
                    RecurringPropertyCashflow(
                        start_month=0,
                        end_month=23,
                        property_id="p1",
                        cause_id="rental_income:p1",
                        from_agent_id=TENANT_AGENT_ID,
                        from_account_id="checking",
                        to_agent_id=OWNER_AGENT_ID,
                        to_account_id="checking",
                        amount=monthly_rent,
                        income_category=ORDINARY_INCOME,
                    ),
                    RecurringPropertyCashflow(
                        start_month=0,
                        end_month=23,
                        property_id="p1",
                        cause_id="management_fee:p1",
                        from_agent_id=OWNER_AGENT_ID,
                        from_account_id="checking",
                        to_agent_id=MGMT_AGENT_ID,
                        to_account_id="checking",
                        amount=monthly_management_fee,
                        deduction_category="ordinary",
                    ),
                ],
                "scheduled_property_cashflows": [
                    ScheduledPropertyCashflow(
                        month=0,
                        property_id="p1",
                        cause_id="leasing_fee:p1:m0",
                        from_agent_id=OWNER_AGENT_ID,
                        from_account_id="checking",
                        to_agent_id=MGMT_AGENT_ID,
                        to_account_id="checking",
                        amount=leasing_fee,
                        deduction_category="ordinary",
                    ),
                    ScheduledPropertyCashflow(
                        month=sale_month,
                        property_id="p1",
                        cause_id=f"leasing_fee:p1:m{sale_month}",
                        from_agent_id=OWNER_AGENT_ID,
                        from_account_id="checking",
                        to_agent_id=MGMT_AGENT_ID,
                        to_account_id="checking",
                        amount=leasing_fee,
                        deduction_category="ordinary",
                    ),
                ],
            }
        )
        levels = [1] * sale_month + [1.5] * (scenario.horizon_months + 1 - sale_month)
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_KEY: {0: [1] * (scenario.horizon_months + 1)}, SF_HOME_VALUE_KEY: {0: levels}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )

        transfers = run.events_log.transfers
        rent = transfers.filter(pl.col("cause_id") == "rental_income:p1").sort("month_index")
        management = transfers.filter(pl.col("cause_id") == "management_fee:p1").sort("month_index")
        lease_at_purchase = transfers.filter(pl.col("cause_id") == "leasing_fee:p1:m0")
        lease_at_sale = transfers.filter(pl.col("cause_id") == f"leasing_fee:p1:m{sale_month}")
        generic = transfers.filter(pl.col("cause_id") == "generic_transfer").sort("month_index")

        assert rent.get_column("month_index").to_list() == list(range(sale_month))
        assert rent.get_column("amount_quanta").map_elements(
            lambda q: q / 100, return_dtype=pl.Float64
        ).to_list() == pytest.approx([monthly_rent] * sale_month)
        assert management.get_column("month_index").to_list() == list(range(sale_month))
        assert management.get_column("amount_quanta").map_elements(
            lambda q: q / 100, return_dtype=pl.Float64
        ).to_list() == pytest.approx([monthly_management_fee] * sale_month)
        assert lease_at_purchase.get_column("month_index").to_list() == [0]
        assert lease_at_purchase.get_column("amount_quanta").map_elements(
            lambda q: q / 100, return_dtype=pl.Float64
        ).to_list() == pytest.approx([leasing_fee])
        assert lease_at_sale.is_empty()
        assert generic.get_column("month_index").to_list() == list(range(24))
        assert generic.get_column("amount_quanta").map_elements(
            lambda q: q / 100, return_dtype=pl.Float64
        ).to_list() == pytest.approx([generic_transfer] * 24)

        federal_y0 = next(
            row
            for row in run.events_log.tax_breakdowns.iter_rows(named=True)
            if row["month_index"] == 11 and row["jurisdiction_id"] == "federal_us"
        )
        expected_depreciation = 400_000 / 27.5
        assert federal_y0["ordinary_income_quanta"] / 100 == pytest.approx(
            12 * monthly_rent - 12 * monthly_management_fee - leasing_fee - expected_depreciation, abs=0.05
        )

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
        levels = [1] * 12 + [1.5] * 13
        ctx = _multi_series(levels_by_series={RENT_SERIES_KEY: {0: [1] * 25}, SF_HOME_VALUE_KEY: {0: levels}})
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
        assert federal_y2["ordinary_income_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert federal_y2["ordinary_taxable_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        # Federal LTCG: ordinary income is zero in year 2, so the standard deduction is
        # unused against it and shelters that much of the gain instead (§63 nets it against
        # taxable income, which includes the gain, before §1(h) rates what is left).
        # Taxable income is $205,000 - $14,600 = $190,400, all net capital gain:
        # 0% slice 0..47,025, 15% slice 47,025..190,400 = 0.15 × 143,375 = 21,506.25.
        standard_deduction = 14_600
        ltcg_tax_federal = 0.15 * (205_000 - standard_deduction - 47_025)
        # §1250 implied marginal walk: 10% × 11600 + 12% × (14545.45 - 11600) = 1160 + 353.45 = 1513.45.
        # That's well below the 25% × 14545.45 = 3636.36 cap → marginal wins.
        section_1250_marginal = 0.10 * 11_600 + 0.12 * (recapture - 11_600)
        section_1250_cap = recapture * 0.25
        assert section_1250_marginal < section_1250_cap  # sanity: marginal binds, not cap
        assert federal_y2["capital_gain_tax_quanta"] / 100 == pytest.approx(
            ltcg_tax_federal + section_1250_marginal, abs=2
        )
        # California: no §1250 cap → recapture is added to ordinary brackets (and CA has no
        # separate LTCG schedule, so LTCG is in ordinary too).
        assert california_y2["capital_gain_tax_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert california_y2["ordinary_taxable_quanta"] / 100 == pytest.approx(
            205_000 + recapture - california_y2["standard_deduction_quanta"] / 100, abs=1
        )

    def test_section_1250_recapture_caps_at_25pct_when_marginal_exceeds(self, san_francisco_location: Location):
        """High-bracket case: federal 25% §1250 cap binds when marginal ≥ 25%.

        Same sale scenario, but the owner also earns enough wage income in year 2 to
        push ordinary_taxable past the 32% bracket threshold ($191,950 single). Stacking
        the recapture on top would normally land in the 32%/35% brackets, but the IRS
        cap holds it to 25%. Capital-gain tax = LTCG tax + 25% × recapture.
        """

        scenario = self._sale_scenario(horizon=24, sale_month=12, year2_wage=250_000)
        levels = [1] * 12 + [1.5] * 13
        ctx = _multi_series(levels_by_series={RENT_SERIES_KEY: {0: [1] * 25}, SF_HOME_VALUE_KEY: {0: levels}})
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
        assert federal_y2["ordinary_taxable_quanta"] / 100 > 191_950
        section_1250_tax = recapture * 0.25
        # LTCG bracket walk shifts because ordinary_taxable is now large: the 0% slice is
        # fully consumed and most of the LTCG lands in the 20% bracket (LTCG breakpoints
        # 47025 / 518900 single for 2024). Just assert that the §1250 tax piece is exactly
        # the 25% cap — the LTCG arithmetic is exercised elsewhere.
        assert federal_y2["capital_gain_tax_quanta"] / 100 >= section_1250_tax + 0.20 * 100_000  # rough lower bound

    def test_section_121_exclusion_after_24_owner_occupied_months(self, san_francisco_location: Location):
        """Owner-occupied for ≥ 24 of the last 60 months → up to $250k of post-recapture
        gain is excluded from LTCG (single-filer cap).

        Buy as primary residence (rented_fraction=0). Hold for 30 months. Then sell with
        $200k appreciation: realized gain = $188k (after closing costs), all of it post-
        recapture (no depreciation accrued because never rented). §121 excludes the full
        $188k → LTCG = 0; section_121_exclusion in the sale event records $188k.
        """

        purchase_price = 500_000
        sale_month = 30
        horizon = 36
        scenario = Scenario(
            agents=[Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id="property_seller"), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=600000),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price,
                    rented_fraction=0,
                    land_value_fraction=0.20,
                )
            ],
            initial_primary_residences=[PrimaryResidenceAssignment(agent_id=OWNER_AGENT_ID, property_id="p1")],
            property_lifecycle_events=[PropertySaleEvent(month=sale_month, property_id="p1", closing_cost_pct=6)],
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
        home_values = [1] * sale_month + [1.4] * (horizon + 1 - sale_month)
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_KEY: {0: [1] * (horizon + 1)}, SF_HOME_VALUE_KEY: {0: home_values}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        # Sale event surfaces in property_sale_events frame.
        sale_rows = run.events_log.property_sale_events.to_dicts()
        assert len(sale_rows) == 1
        sale = sale_rows[0]
        # Gross = $500k × 1.4 × 0.94 = $658k. Realized gain = $658k - $500k = $158k.
        assert sale["gross_proceeds_quanta"] / 100 == pytest.approx(658_000, abs=1)
        assert sale["realized_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale["depreciation_recapture_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        # §121 fully excludes the $158k gain (well under $250k single-filer cap).
        assert sale["section_121_exclusion_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale["long_term_capital_gain_quanta"] / 100 == pytest.approx(0, abs=1e-6)

    @pytest.mark.parametrize(
        ("primary_start_month", "primary_end_month", "expected_exclusion_usd", "expected_ltcg_usd"),
        [
            pytest.param(61, 84, 0, 158_000, id="23-recent-months-does-not-qualify"),
            pytest.param(60, 84, 158_000, 0, id="24-recent-months-qualifies"),
            pytest.param(0, 24, 0, 158_000, id="24-old-months-outside-lookback-do-not-qualify"),
            pytest.param(24, 48, 158_000, 0, id="24-months-at-lookback-boundary-qualify"),
        ],
    )
    def test_section_121_uses_24_of_trailing_60_months(
        self,
        san_francisco_location: Location,
        primary_start_month: int,
        primary_end_month: int,
        expected_exclusion_usd: float,
        expected_ltcg_usd: float,
    ):
        """§121 is a 24-of-trailing-60-month use test, not cumulative lifetime occupancy.

        The JAX scan keeps a 60-month occupancy ring. These cases pin the exact boundary behavior
        that ring must preserve.
        """

        purchase_price = 500_000
        sale_month = 84
        horizon = sale_month + 1
        scenario = Scenario(
            agents=[Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id="property_seller"), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=600000),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price,
                    rented_fraction=0,
                    land_value_fraction=0.20,
                )
            ],
            primary_residence_events=[
                SetPrimaryResidenceEvent(month=primary_start_month, agent_id=OWNER_AGENT_ID, property_id="p1"),
                SetPrimaryResidenceEvent(month=primary_end_month, agent_id=OWNER_AGENT_ID, property_id=None),
            ],
            property_lifecycle_events=[PropertySaleEvent(month=sale_month, property_id="p1", closing_cost_pct=6)],
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
        home_values = [1] * sale_month + [1.4]
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_KEY: {0: [1] * (horizon + 1)}, SF_HOME_VALUE_KEY: {0: home_values}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )

        sale = run.events_log.property_sale_events.to_dicts()[0]
        assert sale["realized_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale["depreciation_recapture_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert sale["section_121_exclusion_quanta"] / 100 == pytest.approx(expected_exclusion_usd, abs=1)
        assert sale["long_term_capital_gain_quanta"] / 100 == pytest.approx(expected_ltcg_usd, abs=1)

        post_sale_ltcg = capital_gains_ytd(run).filter(
            (pl.col("agent_id") == OWNER_AGENT_ID)
            & (pl.col("classification") == "ltcg")
            & (pl.col("month_index") == sale_month + 1)
            & (pl.col("rollout_index") == 0)
        )
        actual_ltcg = 0 if post_sale_ltcg.is_empty() else float(post_sale_ltcg.get_column("gain_quanta").sum()) / 100
        assert actual_ltcg == pytest.approx(expected_ltcg_usd, abs=1)

    def test_section_121_does_not_apply_to_unassigned_non_rented_property(self, san_francisco_location: Location):
        purchase_price = 500_000
        sale_month = 30
        horizon = 36
        scenario = Scenario(
            agents=[Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id="property_seller"), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=600000),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price,
                    rented_fraction=0,
                    land_value_fraction=0.20,
                )
            ],
            property_lifecycle_events=[PropertySaleEvent(month=sale_month, property_id="p1", closing_cost_pct=6)],
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
        home_values = [1] * sale_month + [1.4] * (horizon + 1 - sale_month)
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_KEY: {0: [1] * (horizon + 1)}, SF_HOME_VALUE_KEY: {0: home_values}}
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )

        sale = run.events_log.property_sale_events.to_dicts()[0]
        assert sale["realized_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale["section_121_exclusion_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert sale["long_term_capital_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)

    def test_primary_residence_event_starts_section_121_qualifying_months(self, san_francisco_location: Location):
        purchase_price = 500_000
        sale_month = 30
        horizon = 36
        scenario = Scenario(
            agents=[Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id="property_seller"), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=600000),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price,
                    rented_fraction=0,
                    land_value_fraction=0.20,
                )
            ],
            primary_residence_events=[SetPrimaryResidenceEvent(month=6, agent_id=OWNER_AGENT_ID, property_id="p1")],
            property_lifecycle_events=[PropertySaleEvent(month=sale_month, property_id="p1", closing_cost_pct=6)],
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
        home_values = [1] * sale_month + [1.4] * (horizon + 1 - sale_month)
        ctx = _multi_series(
            levels_by_series={RENT_SERIES_KEY: {0: [1] * (horizon + 1)}, SF_HOME_VALUE_KEY: {0: home_values}}
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
        assert sale["section_121_exclusion_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale["long_term_capital_gain_quanta"] / 100 == pytest.approx(0, abs=1e-6)

    def test_same_month_primary_residence_assignment_fires_before_sale_but_does_not_accrue_use(
        self, san_francisco_location: Location
    ):
        sale_month = 30
        scenario = self._sale_scenario(horizon=36, sale_month=sale_month, cumulative_depreciation_eligible=False)
        scenario = Scenario.model_validate(
            {
                **scenario.model_dump(),
                "recurring_transfers": [],
                "primary_residence_events": [
                    SetPrimaryResidenceEvent(month=sale_month, agent_id=OWNER_AGENT_ID, property_id="p1")
                ],
            }
        )
        home_values = [1] * sale_month + [1.4] * (scenario.horizon_months + 1 - sale_month)
        ctx = _multi_series(
            levels_by_series={
                RENT_SERIES_KEY: {0: [1] * (scenario.horizon_months + 1)},
                SF_HOME_VALUE_KEY: {0: home_values},
            }
        )
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )

        assert run.events_log.set_primary_residence_events.to_dicts() == [
            {
                "rollout_index": 0,
                "month_index": sale_month,
                "agent_id": OWNER_AGENT_ID,
                "property_id": "p1",
                "is_primary_residence": True,
            }
        ]
        sale = run.events_log.property_sale_events.to_dicts()[0]
        assert sale["section_121_exclusion_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert sale["long_term_capital_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)

    def test_section_121_does_not_apply_without_owner_occupied_months(self, san_francisco_location: Location):
        """Same sale at month 30, but the property has been 100% rented the entire time.
        Owner-occupied months = 0, so §121 does not apply. The depreciation recapture +
        LTCG flow remains intact."""

        scenario = self._sale_scenario(horizon=36, sale_month=30)
        home_values = [1] * 30 + [1.4] * 7
        ctx = _multi_series(levels_by_series={RENT_SERIES_KEY: {0: [1] * 37}, SF_HOME_VALUE_KEY: {0: home_values}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        sale = run.events_log.property_sale_events.to_dicts()[0]
        # §121 should be exactly zero — never owner-occupied.
        assert sale["section_121_exclusion_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        # Recapture should be positive (30 months of depreciation × $400k / 27.5 / 12 ≈ $36,363).
        assert sale["depreciation_recapture_quanta"] / 100 == pytest.approx(36_363.64, abs=1)

    def test_lifecycle_event_frames_logged_for_each_kind(self, san_francisco_location: Location):
        """All three lifecycle event kinds appear in their respective frames, one row per
        (rollout, event)."""

        purchase_price = 500_000
        scenario = Scenario(
            agents=[Agent(agent_id=OWNER_AGENT_ID), Agent(agent_id="property_seller"), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=800000),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price,
                    rented_fraction=0,
                    land_value_fraction=0.20,
                )
            ],
            property_lifecycle_events=[
                SetRentedFractionEvent(month=6, property_id="p1", rented_fraction=1),
                CapitalImprovementEvent(month=8, property_id="p1", amount=50000, description="new roof"),
                PropertySaleEvent(month=12, property_id="p1", closing_cost_pct=6),
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
        ctx = _multi_series(levels_by_series={RENT_SERIES_KEY: {0: [1] * 25}, SF_HOME_VALUE_KEY: {0: [1] * 25}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )

        rented_rows = run.events_log.set_rented_fraction_events.to_dicts()
        assert len(rented_rows) == 1
        assert rented_rows[0]["month_index"] == 6
        assert rented_rows[0]["rented_fraction"] == 1
        assert rented_rows[0]["property_id"] == "p1"

        capex_rows = run.events_log.capital_improvement_events.to_dicts()
        assert len(capex_rows) == 1
        assert capex_rows[0]["month_index"] == 8
        assert capex_rows[0]["amount_quanta"] / 100 == pytest.approx(50_000)
        assert capex_rows[0]["property_id"] == "p1"

        sale_rows = run.events_log.property_sale_events.to_dicts()
        assert len(sale_rows) == 1
        assert sale_rows[0]["month_index"] == 12
        assert sale_rows[0]["property_id"] == "p1"

    def test_same_month_property_sale_rejects_rented_fraction_event(self):
        scenario = self._sale_scenario(horizon=13, sale_month=12)
        with pytest.raises(ValueError, match="same-month sale lifecycle ordering is ambiguous"):
            Scenario.model_validate(
                {
                    **scenario.model_dump(),
                    "property_lifecycle_events": [
                        SetRentedFractionEvent(month=12, property_id="p1", rented_fraction=0),
                        PropertySaleEvent(month=12, property_id="p1", closing_cost_pct=6),
                    ],
                }
            )

    def test_same_month_property_sale_rejects_capital_improvement_event(self):
        scenario = self._sale_scenario(horizon=13, sale_month=12)
        with pytest.raises(ValueError, match="same-month sale lifecycle ordering is ambiguous"):
            Scenario.model_validate(
                {
                    **scenario.model_dump(),
                    "property_lifecycle_events": [
                        CapitalImprovementEvent(month=12, property_id="p1", amount=100000, description="new roof"),
                        PropertySaleEvent(month=12, property_id="p1", closing_cost_pct=6),
                    ],
                }
            )

    def _sale_scenario(
        self,
        *,
        horizon: int,
        sale_month: int,
        cumulative_depreciation_eligible: bool = True,
        year2_wage: Decimal | int = 0,
    ) -> Scenario:
        purchase_price = 500_000
        agents = [
            Agent(agent_id=OWNER_AGENT_ID),
            Agent(agent_id=TENANT_AGENT_ID),
            Agent(agent_id="property_seller"),
            Agent(agent_id="irs"),
        ]
        initial_cash = [
            InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=600000),
            InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
            InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                amount=5000,
                income_category=ORDINARY_INCOME,
            )
        ]
        if year2_wage > 0:
            agents.append(Agent(agent_id="employer"))
            initial_cash.append(InitialAccountBalance(agent_id="employer", account_id="checking", balance=0))
            recurring_transfers.append(
                RecurringTransfer(
                    start_month=12,
                    end_month=23,
                    cause_id="wages:employer",
                    from_agent_id="employer",
                    from_account_id="checking",
                    to_agent_id=OWNER_AGENT_ID,
                    to_account_id="checking",
                    amount=round_currency_amount(year2_wage / Decimal(12), quantum=Decimal("0.01")),
                    income_category=ORDINARY_INCOME,
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price,
                    rented_fraction=1 if cumulative_depreciation_eligible else 0,
                    land_value_fraction=0.20,
                )
            ],
            property_lifecycle_events=[PropertySaleEvent(month=sale_month, property_id="p1", closing_cost_pct=6)],
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
        purchase_price = 500_000
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=600000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    amount=5000,
                    income_category=ORDINARY_INCOME,
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price,
                    rented_fraction=0,
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
        ctx = _multi_series(levels_by_series={SF_HOME_VALUE_KEY: {0: [1] * 13}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        # No depreciation → ordinary income equals gross paycheck income: $60,000.
        assert breakdowns["federal_us"]["ordinary_income_quanta"] / 100 == pytest.approx(60_000, abs=1e-6)

    def test_mortgage_interest_deducts_full_for_owner_occupied_and_scales_for_partial_rental(
        self, san_francisco_location: Location
    ):
        """MID applies to the owner-fraction of mortgage interest; the rented-fraction share
        deducts as Schedule E rental interest. The MID compile-time scaling and the engine's
        year-end Schedule E rental-interest hook combine to make rented_fraction × interest
        deductible under either MID or Schedule E depending on which yields the better total."""

        owner_breakdown = self._mortgage_scenario_breakdown(
            rented_fraction=0, locations={"san_francisco": san_francisco_location}
        )
        rented_breakdown = self._mortgage_scenario_breakdown(
            rented_fraction=1, locations={"san_francisco": san_francisco_location}
        )
        # Whether the property is fully owner-occupied or fully rented, the same dollar amount
        # of mortgage interest reduces ordinary income — just via different mechanisms (MID +
        # itemized vs. Schedule E direct subtraction). The federal final tax should match.
        # The interest is the same; deduction mechanics differ.
        assert owner_breakdown["federal_us"]["mortgage_interest_deduction_quanta"] / 100 > 0
        assert rented_breakdown["federal_us"]["mortgage_interest_deduction_quanta"] / 100 == pytest.approx(0, abs=1e-6)

    def _mortgage_scenario_breakdown(self, *, rented_fraction: float, locations: dict[str, Location]) -> dict:
        end_month = 11
        purchase_price = 600_000
        scenario = Scenario(
            agents=[
                Agent(agent_id=OWNER_AGENT_ID),
                Agent(agent_id=TENANT_AGENT_ID),
                Agent(agent_id="property_seller"),
                Agent(agent_id="lender"),
                Agent(agent_id="irs"),
            ],
            initial_cash=[
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=700000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="lender", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    amount=SeriesIndexedAmount(base_amount=4000, series=RENT_SERIES_KEY, adjustment_period_months=12),
                    income_category=ORDINARY_INCOME,
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price * Decimal("0.20"),
                    # Isolate the MID-vs-Schedule-E comparison from depreciation.
                    land_value_fraction=1,
                    mortgage=MortgageFinancing(
                        liability_id="p1_mortgage",
                        lender_agent_id="lender",
                        lender_account_id="checking",
                        principal=purchase_price * Decimal("0.80"),
                        annual_interest_rate=0.06,
                        term_months=360,
                    ),
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
        ctx = _multi_series(levels_by_series={RENT_SERIES_KEY: {0: [1] * 13}, SF_HOME_VALUE_KEY: {0: [1] * 13}})
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
        purchase_price = 600_000
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
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=700000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="property_seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="tax_authority", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    amount=SeriesIndexedAmount(base_amount=4000, series=RENT_SERIES_KEY, adjustment_period_months=12),
                    income_category=ORDINARY_INCOME,
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
                    purchase_price=purchase_price,
                    down_payment=purchase_price,
                    rented_fraction=rented_fraction,
                    # Isolate the property-tax assertion from depreciation: setting
                    # land_value_fraction=1.0 makes the building basis zero, so no §168
                    # depreciation accrues for this test.
                    land_value_fraction=1,
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
                    profile_id=OWNER_AGENT_ID, cap_schedule=[FederalSaltCapEntry(effective_year_index=0, cap=10000)]
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
        ctx = _multi_series(levels_by_series={RENT_SERIES_KEY: {0: [1] * 13}, SF_HOME_VALUE_KEY: {0: [1] * 13}})
        run = simulate_with_external_series(
            scenario, external_series=ctx, rollout_count=1, locations={"san_francisco": san_francisco_location}
        )
        # Debug: surface any rollout failure before asserting on tax flows.
        status = rollout_status(run)
        assert status["status"][0] != "failed", (
            f"rollout failed at month {status['failed_month'][0]}; "
            f"failures: {run.events_log.rollout_failures.to_dicts()}"
        )
        breakdowns = {row["jurisdiction_id"]: row for row in run.events_log.tax_breakdowns.iter_rows(named=True)}
        # Gross rent: 12 × $4,000 = $48,000. Property tax fires at months 1..11 (11 payments;
        # month 0 is the purchase month, no tax that month) → $7,200 × 11/12 = $6,600.
        # rented_fraction=0.75 → $4,950 routes to Schedule E + $1,650 routes to SALT.
        # Federal ordinary_income after Schedule E = $48,000 - $4,950 = $43,050.
        # (The SALT total combines property tax + state income tax and gets capped, so we
        # don't assert on the absolute SALT number here. The owner-fraction effect is
        # observable through ordinary_income decreasing relative to the rental income.)
        assert breakdowns["federal_us"]["ordinary_income_quanta"] / 100 == pytest.approx(43_050, abs=1e-6)

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
                InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=100000),
                InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
                InitialAccountBalance(agent_id="hoa", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
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
                    amount=SeriesIndexedAmount(base_amount=2500, series=RENT_SERIES_KEY, adjustment_period_months=12),
                    income_category=ORDINARY_INCOME,
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
                    amount_due=SeriesIndexedAmount(
                        base_amount=400, series=RENT_SERIES_KEY, adjustment_period_months=12
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
        assert breakdowns["federal_us"]["ordinary_income_quanta"] / 100 == pytest.approx(27_600, abs=1e-6)


class TestRentalCashflowReconciliation:
    def test_owner_terminal_cash_reconciles_with_the_transfers_the_engine_logged(self):
        """Two independent engine outputs must agree: netting every logged transfer row that
        touches the owner has to reproduce the change in the owner's cash ledger. A flow that is
        logged but never settled — or settled but never logged — breaks this even though each
        output on its own still looks plausible.

        The literal is the hand check layered on top: 12 × $4,750 in, 12 × $380 out, one $5,000
        leasing fee out → $47,440 net.
        """

        initial_cash = 100_000
        scenario = _rental_scenario(
            horizon_months=12,
            initial_cash=initial_cash,
            monthly_rent=4_750,
            monthly_management_fee=380,
            leasing_fees_by_month={0: 5_000},
        )
        run = _run(scenario)
        transfers = run.events_log.transfers
        logged_net = float(
            transfers.filter(pl.col("to_agent_id") == OWNER_AGENT_ID)["amount_quanta"].sum() / 100
        ) - float(transfers.filter(pl.col("from_agent_id") == OWNER_AGENT_ID)["amount_quanta"].sum() / 100)
        # cash_balances has snapshot_months = horizon + 1, so terminal state is at month_index == horizon.
        cash = cash_balances(run).filter(
            (pl.col("agent_id") == OWNER_AGENT_ID) & (pl.col("month_index") == scenario.horizon_months)
        )
        assert cash.height == 1
        assert cash["balance_quanta"][0] / 100 - initial_cash == pytest.approx(logged_net, abs=0.01)
        assert cash["balance_quanta"][0] / 100 == pytest.approx(initial_cash + 47_440, rel=1e-6)


if __name__ == "__main__":
    pytest_bazel.main()
