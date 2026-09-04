"""Rust/JAX differential coverage for year-end accrual, estimated payments, capital-gain
netting, SALT, and depreciation recapture.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from typing import Any

import polars as pl
import pytest_bazel

from finance.augur.rust.differential.backend import assert_backends_agree
from finance.augur.rust.differential.fixtures import (
    financed_property_fixture,
    property_depreciation_fixture,
    tax_fixture,
)
from finance.augur.rust.fixture_spec import account_ref


def tax_payment_fixture(*, funded: bool = True) -> dict[str, Any]:
    fixture = tax_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 13
    scenario["tax_profiles"][0]["prior_year_tax"] = 400_000
    if not funded:
        scenario["tax_profiles"][0]["prior_year_tax"] = 0
        scenario["recurring_transfers"].append(
            {
                "start_month": 0,
                "end_month": 11,
                "cause_id": "alice-spends-paycheck",
                "from": account_ref("alice", "checking"),
                "to": account_ref("payroll", "checking"),
                "amount": 1_666_667,
            }
        )
    return fixture


def long_term_gain_tax_fixture() -> dict[str, Any]:
    fixture = tax_fixture()
    scenario = fixture["scenario"]
    scenario["recurring_transfers"][0]["amount"] = 416_667
    scenario["initial_lots"] = [
        {
            "lot_id": "alice-vti",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "purchase_month": -24,
            "quantity_scale": 1_000_000,
            "units": 100_000_000,
            "basis": 1_000_000,
        }
    ]
    scenario["scheduled_sales"] = [
        {
            "month": 6,
            "cause_id": "sell-vti",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "units": 100_000_000,
            "proceeds_account_id": "checking",
        }
    ]
    scenario["tax_profiles"][0]["jurisdictions"] = scenario["tax_profiles"][0]["jurisdictions"][:1]
    fixture["series"] = [{"series_id": "security:vti", "snapshots": 13, "values": [30_000] * 13}]
    return fixture


def capital_loss_carryforward_fixture() -> dict[str, Any]:
    fixture = tax_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 24
    scenario["accounts"] = [
        {"account": account_ref("alice", "checking"), "opening_balance": 0},
        {"account": account_ref("irs", "checking"), "opening_balance": 0},
    ]
    scenario["recurring_transfers"] = []
    scenario["initial_lots"] = [
        {
            "lot_id": "loss-lot",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "purchase_month": -24,
            "quantity_scale": 1_000_000,
            "units": 1_000_000,
            "basis": 1_000_000,
        },
        {
            "lot_id": "gain-lot",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "purchase_month": -12,
            "quantity_scale": 1_000_000,
            "units": 1_000_000,
            "basis": 100_000,
        },
    ]
    scenario["scheduled_sales"] = [
        {
            "month": 0,
            "cause_id": "harvest-loss",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "units": 1_000_000,
            "proceeds_account_id": "checking",
        },
        {
            "month": 12,
            "cause_id": "realize-gain",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "units": 1_000_000,
            "proceeds_account_id": "checking",
        },
    ]
    fixture["series"] = [{"series_id": "security:vti", "snapshots": 25, "values": [200_000] * 12 + [600_000] * 13}]
    return fixture


def salt_deduction_fixture() -> dict[str, Any]:
    fixture = financed_property_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 24
    scenario["accounts"].extend(
        [
            {"account": account_ref("payroll", "checking"), "opening_balance": 0},
            {"account": account_ref("irs", "checking"), "opening_balance": 0},
        ]
    )
    scenario["recurring_transfers"] = [
        {
            "start_month": 0,
            "end_month": 23,
            "cause_id": "alice-paycheck",
            "from": account_ref("payroll", "checking"),
            "to": account_ref("alice", "checking"),
            "amount": 2_000_000,
            "income_category": "ordinary",
        }
    ]
    scenario["tax_profiles"] = [tax_fixture()["scenario"]["tax_profiles"][0]]
    scenario["federal_salt_deduction_policies"] = [
        {
            "profile_id": "alice",
            "federal_jurisdiction_id": "federal_us",
            "cap_schedule": [
                {"effective_year_index": 0, "cap": 4_000_000},
                {"effective_year_index": 1, "cap": 1_000_000},
            ],
        }
    ]
    return fixture


def rust_tax_liability_frame(rust: dict[str, Any]) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for rollout in rust["rollouts"]:
        for snapshot in rollout["months"]:
            rows.extend(
                {
                    "rollout_index": rollout["rollout_id"],
                    "month_index": snapshot["month"],
                    "agent_id": liability["agent_id"],
                    "jurisdiction_id": liability["jurisdiction_id"],
                    "tax_year_end_month": liability["tax_year_end_month"],
                    "amount_owed_quanta": liability["amount_owed"],
                }
                for liability in snapshot["tax_liabilities"]
            )
    return pl.DataFrame(rows).sort("rollout_index", "month_index", "agent_id", "jurisdiction_id", "tax_year_end_month")


def test_backends_agree_on_federal_and_california_accruals() -> None:
    result = assert_backends_agree(tax_fixture())

    assert result.tax_accrual_details.get_column("total_tax_quanta").to_list() == [1_475_409, 3_753_851]
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_on_estimated_payments_true_up_and_settlement() -> None:
    """Quarterly estimates size off the safe harbour, and January trues up the rest."""

    result = assert_backends_agree(tax_payment_fixture())

    paid = result.events.obligation_settlements.filter(
        pl.col("obligation_type").is_in(["estimated_tax", "tax_true_up"])
    )
    assert not paid.is_empty()
    assert paid.get_column("shortfall_quanta").unique().to_list() == [0]
    assert not result.events.tax_settlements.is_empty()
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_that_an_unfunded_true_up_fails_the_rollout() -> None:
    result = assert_backends_agree(tax_payment_fixture(funded=False))

    assert result.rollout_status.get_column("failed_month").to_list() == [12]
    unfunded = result.events.obligation_settlements.filter(pl.col("obligation_type") == "tax_true_up")
    assert unfunded.get_column("amount_paid_quanta").to_list() == [0]
    # Nothing settles against the liability when the payment never funds.
    assert result.events.tax_settlements.is_empty()


def test_backends_agree_on_long_term_gain_stacking() -> None:
    """LTCG is bracketed on top of ordinary taxable income, per §1(h)."""

    result = assert_backends_agree(long_term_gain_tax_fixture())
    [row] = result.tax_accrual_details.to_dicts()

    assert row["long_term_gain_quanta"] == 2_000_000
    assert row["ordinary_taxable_quanta"] == 3_540_004
    assert row["long_term_capital_gain_taxable_quanta"] == 2_000_000
    assert row["ordinary_tax_quanta"] == 401_600
    assert row["capital_gain_tax_quanta"] == 125_626
    assert row["total_tax_quanta"] == 527_226


def test_backends_agree_on_a_capital_loss_carryforward_shared_across_jurisdictions() -> None:
    """One netting per taxpayer, so the same offset and carryforward feed every link."""

    result = assert_backends_agree(capital_loss_carryforward_fixture())
    accruals = result.tax_accrual_details

    first_year = accruals.filter(pl.col("month_index") == 11)
    second_year = accruals.filter(pl.col("month_index") == 23)
    assert set(first_year.get_column("capital_loss_carryforward_quanta")) == {500_000}
    assert set(second_year.get_column("capital_loss_carryforward_quanta")) == {0}
    # The year's ordinary income absorbs the capped ordinary offset.
    assert set(first_year.get_column("ordinary_income_quanta")) == {-300_000}
    assert set(second_year.get_column("long_term_gain_quanta")) == {0}


def test_backends_agree_on_federal_salt_from_property_and_state_tax() -> None:
    result = assert_backends_agree(salt_deduction_fixture())
    federal = {
        row["month_index"]: row
        for row in result.events.tax_breakdowns.filter(pl.col("jurisdiction_id") == "federal_us").to_dicts()
    }

    # Year one is under the cap; year two is capped at the schedule's 1,000,000 quanta.
    assert 1_000_000 < federal[11]["salt_deduction_quanta"] < 4_000_000
    assert federal[23]["salt_deduction_quanta"] == 1_000_000
    # SALT is the only itemized line in this fixture, so itemizing equals it.
    assert federal[11]["itemized_deduction_quanta"] == federal[11]["salt_deduction_quanta"]
    assert federal[23]["itemized_deduction_quanta"] == federal[23]["salt_deduction_quanta"]


def test_backends_agree_on_depreciation_recapture_by_jurisdiction() -> None:
    """Federal caps the §1250 rate; California runs recapture through ordinary brackets."""

    result = assert_backends_agree(property_depreciation_fixture(sale=True))

    assert result.property_sale_details.get_column("depreciation_recapture_quanta").item() > 0
    by_jurisdiction = {row["jurisdiction_id"]: row for row in result.tax_accrual_details.to_dicts()}
    assert by_jurisdiction["federal_us"]["section_1250_tax_quanta"] > 0
    assert by_jurisdiction["california"]["section_1250_tax_quanta"] == 0


if __name__ == "__main__":
    pytest_bazel.main()
