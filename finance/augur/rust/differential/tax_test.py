"""Rust/JAX differential coverage for year-end accrual, estimated payments, capital-gain
netting, SALT, and depreciation recapture.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from decimal import Decimal

import polars as pl
import pytest_bazel

from finance.augur.rust.differential.backend import assert_backends_agree
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    FederalSaltCapEntry,
    FederalSaltDeductionPolicy,
    InitialLot,
    RecurringTransfer,
    ScheduledAssetSale,
)
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.testing.fixtures import (
    FINANCED_PROPERTY_ACCOUNTS,
    MONTHLY_SALARY,
    SF,
    VTI,
    checking,
    county_property_tax,
    home_mortgage,
    home_purchase,
    property_depreciation_case,
    salary,
    salary_case,
    taxed,
)


def tax_payment_case(*, funded: bool = True) -> Case:
    """A year of salary with a January true-up, sized off the prior year's safe harbour.

    Unfunded, the safe harbour is gone and every dollar of the salary leaves again, so the
    true-up in month 12 has nothing to draw on.
    """

    if funded:
        return salary_case(horizon_months=13, prior_year_tax=Decimal(4_000))
    return salary_case(
        horizon_months=13,
        recurring_transfers=[
            salary(),
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="alice-spends-paycheck",
                from_agent_id="alice",
                from_account_id="checking",
                to_agent_id="payroll",
                to_account_id="checking",
                amount=MONTHLY_SALARY,
            ),
        ],
    )


def long_term_gain_case() -> Case:
    """A long-held lot sold at a gain on top of a year of ordinary income."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("payroll", Decimal(0)), ("irs", Decimal(0))),
            horizon_months=12,
            recurring_transfers=[salary(amount=Decimal("4166.67"))],
            initial_lots=[
                InitialLot(
                    lot_id="alice-vti",
                    agent_id="alice",
                    account_id="brokerage",
                    asset=VTI,
                    purchase_month_index=-24,
                    quantity=100.0,
                    cost_basis_per_unit=Decimal(100),
                )
            ],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=6,
                    cause_id="sell-vti",
                    agent_id="alice",
                    source_account_id="brokerage",
                    asset=VTI,
                    quantity=100.0,
                    proceeds_account_id="checking",
                )
            ],
            tax_profiles=[taxed("alice", "federal_us")],
        ),
        rollout_count=1,
        series={VTI: levels([[Decimal(300)] * 13])},
    )


def capital_loss_carryforward_case() -> Case:
    """A loss harvested in year one and a gain realized in year two, with no other income."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("irs", Decimal(0))),
            horizon_months=24,
            initial_lots=[
                InitialLot(
                    lot_id="loss-lot",
                    agent_id="alice",
                    account_id="brokerage",
                    asset=VTI,
                    purchase_month_index=-24,
                    quantity=1.0,
                    cost_basis_per_unit=Decimal(10_000),
                ),
                InitialLot(
                    lot_id="gain-lot",
                    agent_id="alice",
                    account_id="brokerage",
                    asset=VTI,
                    purchase_month_index=-12,
                    quantity=1.0,
                    cost_basis_per_unit=Decimal(1_000),
                ),
            ],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=0,
                    cause_id="harvest-loss",
                    agent_id="alice",
                    source_account_id="brokerage",
                    asset=VTI,
                    quantity=1.0,
                    proceeds_account_id="checking",
                ),
                ScheduledAssetSale(
                    month=12,
                    cause_id="realize-gain",
                    agent_id="alice",
                    source_account_id="brokerage",
                    asset=VTI,
                    quantity=1.0,
                    proceeds_account_id="checking",
                ),
            ],
            tax_profiles=[taxed("alice", "federal_us", "california")],
        ),
        rollout_count=1,
        series={VTI: levels([[Decimal(2_000)] * 12 + [Decimal(6_000)] * 13])},
    )


def salt_deduction_case() -> Case:
    """Property tax and state income tax deducted federally, against a shrinking cap."""

    return Case(
        scenario=scenario(
            [*checking(*FINANCED_PROPERTY_ACCOUNTS), *checking(("payroll", Decimal(0)), ("irs", Decimal(0)))],
            horizon_months=24,
            scheduled_property_purchases=[home_purchase(mortgage=home_mortgage())],
            property_tax_policies=[county_property_tax()],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=23,
                    cause_id="alice-paycheck",
                    from_agent_id="payroll",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=Decimal(20_000),
                    income_category=ORDINARY_INCOME,
                )
            ],
            tax_profiles=[taxed("alice", "federal_us", "california")],
            federal_salt_deduction_policies=[
                FederalSaltDeductionPolicy(
                    profile_id="alice",
                    federal_jurisdiction_id="federal_us",
                    cap_schedule=[
                        FederalSaltCapEntry(effective_year_index=0, cap=Decimal(40_000)),
                        FederalSaltCapEntry(effective_year_index=1, cap=Decimal(10_000)),
                    ],
                )
            ],
        ),
        rollout_count=1,
        locations={"sf": SF},
    )


def test_backends_agree_on_federal_and_california_accruals() -> None:
    result = assert_backends_agree(salary_case())

    assert result.tax_accrual_details.get_column("total_tax_quanta").to_list() == [1_475_409, 3_753_851]
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_on_estimated_payments_true_up_and_settlement() -> None:
    """Quarterly estimates size off the safe harbour, and January trues up the rest."""

    result = assert_backends_agree(tax_payment_case())

    paid = result.events.obligation_settlements.filter(
        pl.col("obligation_type").is_in(["estimated_tax", "tax_true_up"])
    )
    assert not paid.is_empty()
    assert paid.get_column("shortfall_quanta").unique().to_list() == [0]
    assert not result.events.tax_settlements.is_empty()
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_that_an_unfunded_true_up_fails_the_rollout() -> None:
    result = assert_backends_agree(tax_payment_case(funded=False))

    assert result.rollout_status.get_column("failed_month").to_list() == [12]
    unfunded = result.events.obligation_settlements.filter(pl.col("obligation_type") == "tax_true_up")
    assert unfunded.get_column("amount_paid_quanta").to_list() == [0]
    # Nothing settles against the liability when the payment never funds.
    assert result.events.tax_settlements.is_empty()


def test_backends_agree_on_long_term_gain_stacking() -> None:
    """LTCG is bracketed on top of ordinary taxable income, per §1(h)."""

    result = assert_backends_agree(long_term_gain_case())
    [row] = result.tax_accrual_details.to_dicts()

    assert row["long_term_gain_quanta"] == 2_000_000
    assert row["ordinary_taxable_quanta"] == 3_540_004
    assert row["long_term_capital_gain_taxable_quanta"] == 2_000_000
    assert row["ordinary_tax_quanta"] == 401_600
    assert row["capital_gain_tax_quanta"] == 125_626
    assert row["total_tax_quanta"] == 527_226


def test_backends_agree_on_a_capital_loss_carryforward_shared_across_jurisdictions() -> None:
    """One netting per taxpayer, so the same offset and carryforward feed every link."""

    result = assert_backends_agree(capital_loss_carryforward_case())
    accruals = result.tax_accrual_details

    first_year = accruals.filter(pl.col("month_index") == 11)
    second_year = accruals.filter(pl.col("month_index") == 23)
    assert set(first_year.get_column("capital_loss_carryforward_quanta")) == {500_000}
    assert set(second_year.get_column("capital_loss_carryforward_quanta")) == {0}
    # The year's ordinary income absorbs the capped ordinary offset.
    assert set(first_year.get_column("ordinary_income_quanta")) == {-300_000}
    assert set(second_year.get_column("long_term_gain_quanta")) == {0}


def test_backends_agree_on_federal_salt_from_property_and_state_tax() -> None:
    result = assert_backends_agree(salt_deduction_case())
    federal = {
        row["month_index"]: row
        for row in result.events.tax_breakdowns.filter(pl.col("jurisdiction_id") == "federal_us").to_dicts()
    }

    # Year one is under the cap; year two is capped at the schedule's 1,000,000 quanta.
    assert 1_000_000 < federal[11]["salt_deduction_quanta"] < 4_000_000
    assert federal[23]["salt_deduction_quanta"] == 1_000_000
    # SALT is the only itemized line in this case, so itemizing equals it.
    assert federal[11]["itemized_deduction_quanta"] == federal[11]["salt_deduction_quanta"]
    assert federal[23]["itemized_deduction_quanta"] == federal[23]["salt_deduction_quanta"]


def test_backends_agree_on_depreciation_recapture_by_jurisdiction() -> None:
    """Federal caps the §1250 rate; California runs recapture through ordinary brackets."""

    result = assert_backends_agree(property_depreciation_case(sale=True))

    assert result.property_sale_details.get_column("depreciation_recapture_quanta").item() > 0
    by_jurisdiction = {row["jurisdiction_id"]: row for row in result.tax_accrual_details.to_dicts()}
    assert by_jurisdiction["federal_us"]["section_1250_tax_quanta"] > 0
    assert by_jurisdiction["california"]["section_1250_tax_quanta"] == 0


if __name__ == "__main__":
    pytest_bazel.main()
