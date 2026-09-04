"""Rust/JAX differential coverage for year-end accrual, estimated payments, capital-gain netting, SALT, and depreciation recapture.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest_bazel

from finance.augur.rust.fixture_adapter import run_legacy_fixture
from finance.augur.rust.output_adapter import decode_rust_event_log
from finance.augur.rust.testing.fixtures import (
    capital_loss_carryforward_fixture,
    long_term_gain_tax_fixture,
    property_depreciation_fixture,
    rust_cash_frame,
    rust_run,
    rust_tax_liability_frame,
    salt_deduction_fixture,
    tax_fixture,
    tax_payment_fixture,
)
from finance.augur.sim.testing.state_helpers import cash_balances, rollout_status, tax_liabilities


def test_rust_and_jax_match_federal_and_california_tax_accruals(tmp_path: Path) -> None:
    fixture = tax_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()

    columns = [
        "rollout_index",
        "month_index",
        "agent_id",
        "jurisdiction_id",
        "ordinary_income_quanta",
        "ordinary_taxable_quanta",
        "capital_gain_taxable_quanta",
        "ordinary_tax_quanta",
        "capital_gain_tax_quanta",
        "total_tax_quanta",
    ]
    legacy_accruals = legacy.events_log.tax_breakdowns.select(columns).sort("jurisdiction_id").to_dicts()
    rust_accruals = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": accrual["month"],
                "agent_id": accrual["agent_id"],
                "jurisdiction_id": accrual["jurisdiction_id"],
                "ordinary_income_quanta": accrual["ordinary_income"],
                "ordinary_taxable_quanta": accrual["ordinary_taxable"],
                "capital_gain_taxable_quanta": accrual["long_term_capital_gain_taxable"],
                "ordinary_tax_quanta": accrual["ordinary_tax"],
                "capital_gain_tax_quanta": accrual["capital_gain_tax"],
                "total_tax_quanta": accrual["total_tax"],
            }
            for rollout in rust["rollouts"]
            for accrual in rollout["tax_accruals"]
        ],
        key=lambda row: row["jurisdiction_id"],
    )
    assert rust_accruals == legacy_accruals
    assert [row["total_tax_quanta"] for row in rust_accruals] == [1_475_409, 3_753_851]

    legacy_events = legacy.events_log
    rust_events = decode_rust_event_log(rust)
    for legacy_frame, rust_frame in (
        (legacy_events.tax_accruals, rust_events.tax_accruals),
        (legacy_events.tax_breakdowns, rust_events.tax_breakdowns),
    ):
        sort_columns = ["rollout_index", "month_index", "cause_id", "jurisdiction_id"]
        assert rust_frame.schema == legacy_frame.schema
        assert rust_frame.sort(sort_columns).to_dicts() == legacy_frame.sort(sort_columns).to_dicts()
    for entry in rust["rollouts"][0]["journal"]:
        assert sum(posting["amount"] for posting in entry["postings"]) == 0


def test_rust_and_jax_match_estimated_tax_true_up_and_liability_settlement(tmp_path: Path) -> None:
    fixture = tax_payment_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()

    tax_types = ["estimated_tax", "tax_true_up"]
    legacy_payments = (
        legacy.events_log.obligation_settlements.filter(pl.col("obligation_type").is_in(tax_types))
        .select(
            "month_index",
            pl.col("obligation_id").alias("cause_id"),
            "obligation_type",
            "amount_due_quanta",
            "amount_paid_quanta",
            "shortfall_quanta",
        )
        .sort("month_index", "cause_id")
        .to_dicts()
    )
    rust_payments = sorted(
        [
            {
                "month_index": payment["month"],
                "cause_id": payment["cause_id"],
                "obligation_type": payment["obligation_type"],
                "amount_due_quanta": payment["amount_due"],
                "amount_paid_quanta": payment["amount_paid"],
                "shortfall_quanta": payment["shortfall"],
            }
            for payment in rust["rollouts"][0]["tax_payments"]
        ],
        key=lambda row: (row["month_index"], row["cause_id"]),
    )
    assert rust_payments == legacy_payments

    legacy_liabilities = (
        tax_liabilities(legacy)
        .filter(pl.col("month_index").is_in([12, 13]))
        .sort("rollout_index", "month_index", "agent_id", "jurisdiction_id", "tax_year_end_month")
    )
    rust_liabilities = rust_tax_liability_frame(rust).filter(pl.col("month_index").is_in([12, 13]))
    assert rust_liabilities.to_dicts() == legacy_liabilities.to_dicts()

    legacy_settlements = (
        legacy.events_log.tax_settlements.select(
            "month_index", "cause_id", "agent_id", "tax_year_end_month", "amount_quanta"
        )
        .sort("month_index", "cause_id")
        .to_dicts()
    )
    rust_settlements = sorted(
        [
            {
                "month_index": settlement["month"],
                "cause_id": settlement["cause_id"],
                "agent_id": settlement["agent_id"],
                "tax_year_end_month": settlement["tax_year_end_month"],
                "amount_quanta": settlement["amount"],
            }
            for settlement in rust["rollouts"][0]["tax_settlements"]
        ],
        key=lambda row: (row["month_index"], row["cause_id"]),
    )
    assert rust_settlements == legacy_settlements

    rust_events = decode_rust_event_log(rust)
    legacy_events = legacy.events_log
    settlement_sort = ["rollout_index", "month_index", "cause_id", "tax_year_end_month"]
    assert rust_events.tax_settlements.schema == legacy_events.tax_settlements.schema
    assert (
        rust_events.tax_settlements.sort(settlement_sort).to_dicts()
        == legacy_events.tax_settlements.sort(settlement_sort).to_dicts()
    )

    tax_causes = {payment["cause_id"] for payment in rust["rollouts"][0]["tax_payments"]}
    transfer_sort = ["rollout_index", "month_index", "cause_id"]
    rust_tax_transfers = rust_events.transfers.filter(pl.col("cause_id").is_in(tax_causes)).sort(transfer_sort)
    legacy_tax_transfers = legacy_events.transfers.filter(pl.col("cause_id").is_in(tax_causes)).sort(transfer_sort)
    assert rust_tax_transfers.schema == legacy_tax_transfers.schema
    assert rust_tax_transfers.to_dicts() == legacy_tax_transfers.to_dicts()
    for entry in rust["rollouts"][0]["journal"]:
        assert sum(posting["amount"] for posting in entry["postings"]) == 0


def test_rust_and_jax_match_unfunded_tax_true_up_failure(tmp_path: Path) -> None:
    fixture = tax_payment_fixture(funded=False)
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    assert rollout_status(legacy).row(0, named=True)["failed_month"] == 12
    assert rust["rollouts"][0]["failed_month"] == 12
    payment = next(
        payment for payment in rust["rollouts"][0]["tax_payments"] if payment["obligation_type"] == "tax_true_up"
    )
    assert payment["cause_id"] == "alice_tax_true_up_y0"
    assert payment["obligation_type"] == "tax_true_up"
    assert payment["amount_paid"] == 0
    assert payment["shortfall"] == payment["amount_due"]
    assert rust["rollouts"][0]["tax_settlements"] == []
    assert rust_cash_frame(rust).to_dicts() == (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
        .to_dicts()
    )


def test_rust_and_jax_match_long_term_gain_tax(tmp_path: Path) -> None:
    fixture = long_term_gain_tax_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_row = legacy.events_log.tax_breakdowns.row(0, named=True)
    rust_row = rust["rollouts"][0]["tax_accruals"][0]
    assert rust_row["ordinary_income"] == legacy_row["ordinary_income_quanta"]
    assert rust_row["long_term_gain"] == legacy_row["ltcg_quanta"] == 2_000_000
    assert rust_row["ordinary_taxable"] == legacy_row["ordinary_taxable_quanta"] == 3_540_004
    assert rust_row["long_term_capital_gain_taxable"] == legacy_row["capital_gain_taxable_quanta"] == 2_000_000
    assert rust_row["ordinary_tax"] == legacy_row["ordinary_tax_quanta"] == 401_600
    assert rust_row["capital_gain_tax"] == legacy_row["capital_gain_tax_quanta"] == 125_626
    assert rust_row["total_tax"] == legacy_row["total_tax_quanta"] == 527_226


def test_rust_and_jax_match_shared_capital_loss_carryforward_across_tax_links(tmp_path: Path) -> None:
    fixture = capital_loss_carryforward_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    sort_columns = ["rollout_index", "month_index", "agent_id", "jurisdiction_id"]
    assert rust_events.tax_breakdowns.schema == legacy.events_log.tax_breakdowns.schema
    assert (
        rust_events.tax_breakdowns.sort(sort_columns).to_dicts()
        == legacy.events_log.tax_breakdowns.sort(sort_columns).to_dicts()
    )
    rust_accruals = sorted(rust["rollouts"][0]["tax_accruals"], key=lambda row: (row["month"], row["jurisdiction_id"]))
    assert {row["capital_loss_carryforward"] for row in rust_accruals if row["month"] == 11} == {500_000}
    assert {row["capital_loss_carryforward"] for row in rust_accruals if row["month"] == 23} == {0}
    assert {row["ordinary_income"] for row in rust_accruals if row["month"] == 11} == {-300_000}
    assert {row["long_term_gain"] for row in rust_accruals if row["month"] == 23} == {0}


def test_rust_and_jax_match_federal_salt_property_and_state_tax_caps(tmp_path: Path) -> None:
    fixture = salt_deduction_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    sort_columns = ["rollout_index", "month_index", "agent_id", "jurisdiction_id"]
    assert rust_events.tax_breakdowns.schema == legacy.events_log.tax_breakdowns.schema
    assert (
        rust_events.tax_breakdowns.sort(sort_columns).to_dicts()
        == legacy.events_log.tax_breakdowns.sort(sort_columns).to_dicts()
    )
    federal = {
        row["month_index"]: row
        for row in rust_events.tax_breakdowns.filter(pl.col("jurisdiction_id") == "federal_us").to_dicts()
    }
    assert 1_000_000 < federal[11]["salt_deduction_quanta"] < 4_000_000
    assert federal[23]["salt_deduction_quanta"] == 1_000_000
    assert federal[11]["itemized_deduction_quanta"] == federal[11]["salt_deduction_quanta"]
    assert federal[23]["itemized_deduction_quanta"] == federal[23]["salt_deduction_quanta"]


def test_rust_and_jax_match_depreciation_recapture_and_jurisdiction_tax(tmp_path: Path) -> None:
    fixture = property_depreciation_fixture(sale=True)
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_events = legacy.events_log
    rust_events = decode_rust_event_log(rust)
    event_comparisons = (
        (
            legacy_events.transfers,
            rust_events.transfers,
            [
                "rollout_index",
                "month_index",
                "cause_id",
                "from_agent_id",
                "from_account_id",
                "to_agent_id",
                "to_account_id",
            ],
        ),
        (
            legacy_events.property_purchases,
            rust_events.property_purchases,
            ["rollout_index", "month_index", "cause_id", "property_id"],
        ),
        (
            legacy_events.mortgage_originations,
            rust_events.mortgage_originations,
            ["rollout_index", "month_index", "cause_id", "liability_id"],
        ),
        (
            legacy_events.mortgage_payments,
            rust_events.mortgage_payments,
            ["rollout_index", "month_index", "cause_id", "liability_id"],
        ),
        (
            legacy_events.set_rented_fraction_events,
            rust_events.set_rented_fraction_events,
            ["rollout_index", "month_index", "property_id"],
        ),
        (
            legacy_events.capital_improvement_events,
            rust_events.capital_improvement_events,
            ["rollout_index", "month_index", "property_id"],
        ),
        (
            legacy_events.property_sale_events,
            rust_events.property_sale_events,
            ["rollout_index", "month_index", "property_id"],
        ),
    )
    for legacy_frame, rust_frame, sort_columns in event_comparisons:
        assert rust_frame.schema == legacy_frame.schema
        assert rust_frame.sort(sort_columns).to_dicts() == legacy_frame.sort(sort_columns).to_dicts()

    legacy_sales = legacy.events_log.property_sale_events.select(
        "month_index",
        "property_id",
        "gross_proceeds_quanta",
        "mortgage_payoff_quanta",
        "net_cash_to_owner_quanta",
        "realized_gain_quanta",
        "depreciation_recapture_quanta",
        "section_121_exclusion_quanta",
        "long_term_capital_gain_quanta",
    ).to_dicts()
    rust_sale = rust["rollouts"][0]["property_sales"][0]
    assert [
        {
            "month_index": rust_sale["month"],
            "property_id": rust_sale["property_id"],
            "gross_proceeds_quanta": rust_sale["gross_proceeds"],
            "mortgage_payoff_quanta": rust_sale["mortgage_payoff"],
            "net_cash_to_owner_quanta": rust_sale["net_cash_to_owner"],
            "realized_gain_quanta": rust_sale["realized_gain"],
            "depreciation_recapture_quanta": rust_sale["depreciation_recapture"],
            "section_121_exclusion_quanta": rust_sale["section_121_exclusion"],
            "long_term_capital_gain_quanta": rust_sale["long_term_capital_gain"],
        }
    ] == legacy_sales
    assert rust_sale["depreciation_recapture"] > 0

    legacy_tax = legacy.events_log.tax_breakdowns.filter(pl.col("month_index") == 23).sort("jurisdiction_id").to_dicts()
    rust_tax = sorted(
        [row for row in rust["rollouts"][0]["tax_accruals"] if row["month"] == 23],
        key=lambda row: row["jurisdiction_id"],
    )
    assert [row["jurisdiction_id"] for row in rust_tax] == [row["jurisdiction_id"] for row in legacy_tax]
    for rust_row, legacy_row in zip(rust_tax, legacy_tax, strict=True):
        assert rust_row["ordinary_income"] == legacy_row["ordinary_income_quanta"]
        assert rust_row["long_term_gain"] == legacy_row["ltcg_quanta"]
        assert rust_row["ordinary_taxable"] == legacy_row["ordinary_taxable_quanta"]
        assert rust_row["long_term_capital_gain_taxable"] == legacy_row["capital_gain_taxable_quanta"]
        assert rust_row["ordinary_tax"] == legacy_row["ordinary_tax_quanta"]
        assert rust_row["capital_gain_tax"] == legacy_row["capital_gain_tax_quanta"]
        assert rust_row["total_tax"] == legacy_row["total_tax_quanta"]
        assert rust_row["section_1250_recapture"] == rust_sale["depreciation_recapture"]
    rust_tax_by_jurisdiction = {row["jurisdiction_id"]: row for row in rust_tax}
    assert rust_tax_by_jurisdiction["federal_us"]["section_1250_tax"] > 0
    assert rust_tax_by_jurisdiction["california"]["section_1250_tax"] == 0


if __name__ == "__main__":
    pytest_bazel.main()
