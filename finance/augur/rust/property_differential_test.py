"""Rust/JAX differential coverage for the financed-property lifecycle: purchase, carrying costs, rental transitions, sale, and mortgage interest.

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
    financed_property_fixture,
    mortgage_interest_policy_fixture,
    property_cashflow_fixture,
    property_cashflow_gating_fixture,
    property_depreciation_fixture,
    property_sale_fixture,
    rust_cash_frame,
    rust_run,
    section_121_fixture,
    uncapped_mortgage_interest_fixture,
)
from finance.augur.sim.testing.state_helpers import (
    cash_balances,
    liabilities,
    property_stakes,
    property_state,
    rollout_status,
)


def test_rust_and_jax_match_financed_property_purchase_and_first_carry_month(tmp_path: Path) -> None:
    fixture = financed_property_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()

    legacy_property = property_state(legacy).filter(pl.col("month_index") == 2).row(0, named=True)
    legacy_stake = property_stakes(legacy).filter(pl.col("month_index") == 2).row(0, named=True)
    legacy_mortgage = liabilities(legacy).filter(pl.col("month_index") == 2).row(0, named=True)
    rust_property = rust["rollouts"][0]["months"][2]["properties"][0]
    rust_mortgage = rust["rollouts"][0]["months"][2]["mortgages"][0]
    assert rust_property["property_id"] == legacy_property["property_id"] == "home"
    assert rust_property["location_id"] == legacy_property["location_id"] == "sf"
    assert rust_property["adjusted_basis"] == legacy_property["adjusted_basis_quanta"] == 51_000_000
    assert rust_property["contribution_used"] == legacy_stake["contribution_used_quanta"] == 11_000_000
    assert rust_property["equity_ledger"] == legacy_stake["equity_ledger_quanta"] == 10_000_000
    assert rust_mortgage["liability_id"] == legacy_mortgage["liability_id"] == "home-mortgage"
    assert rust_mortgage["monthly_payment"] == legacy_mortgage["monthly_payment_quanta"] == 239_820
    assert rust_mortgage["principal"] == legacy_mortgage["principal_quanta"] == 39_960_180
    assert rust_mortgage["interest_paid_ytd"] == legacy_mortgage["interest_paid_ytd_quanta"] == 200_000
    assert rust_mortgage["principal_paid_ytd"] == legacy_mortgage["principal_paid_ytd_quanta"] == 39_820

    rust_payment = rust["rollouts"][0]["mortgage_payments"][0]
    legacy_payment = legacy.events_log.mortgage_payments.row(0, named=True)
    assert rust_payment["cause_id"] == legacy_payment["cause_id"] == "home-mortgage_payment_m1"
    assert rust_payment["interest"] == legacy_payment["interest_quanta"] == 200_000
    assert rust_payment["principal"] == legacy_payment["principal_quanta"] == 39_820
    assert rust_payment["total_payment"] == legacy_payment["total_payment_quanta"] == 239_820


def test_rust_and_jax_match_property_cashflows_and_tax_tagging(tmp_path: Path) -> None:
    fixture = property_cashflow_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()

    legacy_transfers = (
        legacy.events_log.transfers.filter(pl.col("cause_id").is_in(["leasing-fee", "rent", "management-fee"]))
        .group_by("cause_id")
        .agg(pl.len().alias("count"), pl.col("amount_quanta").sum().alias("amount_quanta"))
        .sort("cause_id")
        .to_dicts()
    )
    assert legacy_transfers == [
        {"cause_id": "leasing-fee", "count": 1, "amount_quanta": 100_000},
        {"cause_id": "management-fee", "count": 12, "amount_quanta": 600_000},
        {"cause_id": "rent", "count": 12, "amount_quanta": 6_000_000},
    ]
    rust_causes = [entry["cause_id"] for entry in rust["rollouts"][0]["journal"]]
    assert rust_causes.count("leasing-fee") == 1
    assert rust_causes.count("management-fee") == 12
    assert rust_causes.count("rent") == 12

    legacy_tax = legacy.events_log.tax_breakdowns.row(0, named=True)
    rust_tax = rust["rollouts"][0]["tax_accruals"][0]
    assert rust_tax["ordinary_income"] == legacy_tax["ordinary_income_quanta"] == 5_300_000
    assert rust_tax["total_tax"] == legacy_tax["total_tax_quanta"] == 437_600


def test_rust_and_jax_match_property_cashflow_purchase_and_failure_gates(tmp_path: Path) -> None:
    fixture = property_cashflow_gating_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()
    assert rollout_status(legacy).to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 2}
    ]
    assert rust["rollouts"][0]["failed_month"] == 2

    legacy_property_cashflows = (
        legacy.events_log.transfers.filter(
            pl.col("cause_id").is_in(["before-purchase", "purchase-month", "property-carry"])
        )
        .select("month_index", "cause_id")
        .sort("month_index", "cause_id")
        .to_dicts()
    )
    assert legacy_property_cashflows == [
        {"month_index": 1, "cause_id": "property-carry"},
        {"month_index": 1, "cause_id": "purchase-month"},
        {"month_index": 2, "cause_id": "property-carry"},
    ]
    rust_property_cashflows = sorted(
        [
            {"month_index": entry["month"], "cause_id": entry["cause_id"]}
            for entry in rust["rollouts"][0]["journal"]
            if entry["cause_id"] in {"before-purchase", "purchase-month", "property-carry"}
        ],
        key=lambda row: (row["month_index"], row["cause_id"]),
    )
    assert rust_property_cashflows == legacy_property_cashflows


def test_rust_and_jax_match_property_sale_lifecycle_and_rollout_values(tmp_path: Path) -> None:
    fixture = property_sale_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()

    legacy_sales = (
        legacy.events_log.property_sale_events.select(
            "rollout_index",
            "month_index",
            "property_id",
            "gross_proceeds_quanta",
            "mortgage_payoff_quanta",
            "net_cash_to_owner_quanta",
            "realized_gain_quanta",
            "depreciation_recapture_quanta",
            "section_121_exclusion_quanta",
            "long_term_capital_gain_quanta",
        )
        .sort("rollout_index")
        .to_dicts()
    )
    rust_sales = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": sale["month"],
                "property_id": sale["property_id"],
                "gross_proceeds_quanta": sale["gross_proceeds"],
                "mortgage_payoff_quanta": sale["mortgage_payoff"],
                "net_cash_to_owner_quanta": sale["net_cash_to_owner"],
                "realized_gain_quanta": sale["realized_gain"],
                "depreciation_recapture_quanta": sale["depreciation_recapture"],
                "section_121_exclusion_quanta": sale["section_121_exclusion"],
                "long_term_capital_gain_quanta": sale["long_term_capital_gain"],
            }
            for rollout in rust["rollouts"]
            for sale in rollout["property_sales"]
        ],
        key=lambda row: row["rollout_index"],
    )
    assert rust_sales == legacy_sales
    assert [row["gross_proceeds_quanta"] for row in rust_sales] == [56_400_000, 51_700_000]

    assert property_state(legacy).filter(pl.col("month_index") >= 3).is_empty()
    assert liabilities(legacy).filter(pl.col("month_index") >= 3).is_empty()
    for rollout in rust["rollouts"]:
        post_sale = rollout["months"][3]
        assert post_sale["properties"][0]["active"] is False
        assert post_sale["mortgages"][0]["active"] is False
        assert post_sale["mortgages"][0]["principal"] == 0
        sale_month_causes = [entry["cause_id"] for entry in rollout["journal"] if entry["month"] == 2]
        assert "property-sale:home" in sale_month_causes
        assert "sale-month-generic-transfer" in sale_month_causes
        assert "rent" not in sale_month_causes
        assert "home-mortgage_payment_m2" not in sale_month_causes
        assert "home_property_tax_m2" not in sale_month_causes
        for entry in rollout["journal"]:
            assert sum(posting["amount"] for posting in entry["postings"]) == 0


def test_rust_and_jax_match_primary_residence_events_and_section_121_boundaries(tmp_path: Path) -> None:
    fixture = section_121_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    primary_sort = ["rollout_index", "month_index", "agent_id"]
    assert rust_events.set_primary_residence_events.schema == legacy.events_log.set_primary_residence_events.schema
    assert (
        rust_events.set_primary_residence_events.sort(primary_sort).to_dicts()
        == legacy.events_log.set_primary_residence_events.sort(primary_sort).to_dicts()
    )

    sale_sort = ["rollout_index", "month_index", "property_id"]
    assert rust_events.property_sale_events.schema == legacy.events_log.property_sale_events.schema
    assert (
        rust_events.property_sale_events.sort(sale_sort).to_dicts()
        == legacy.events_log.property_sale_events.sort(sale_sort).to_dicts()
    )
    assert {
        row["property_id"]: row["section_121_exclusion_quanta"] for row in rust_events.property_sale_events.to_dicts()
    } == {"alice-home": 25_000_000, "bob-home": 0, "carol-home": 0, "dave-home": 0}

    # Snapshot 30 is after month 29 and immediately before the month-30 sales.
    assert legacy.output.state.property_owner_occupied_months[30, :, 0].tolist() == [30, 23, 0, 24]
    rust_month_30 = sorted(rust["rollouts"][0]["months"][30]["properties"], key=lambda row: row["property_id"])
    assert [row["owner_occupied_months"] for row in rust_month_30] == [30, 23, 0, 24]
    assert all(sum(posting["amount"] for posting in entry["postings"]) == 0 for entry in rust["rollouts"][0]["journal"])


def test_rust_and_jax_match_rental_transition_capex_depreciation_and_interest(tmp_path: Path) -> None:
    fixture = property_depreciation_fixture(sale=False)
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()
    assert legacy.events_log.set_rented_fraction_events.select(
        "month_index", "property_id", "rented_fraction"
    ).to_dicts() == [{"month_index": 6, "property_id": "home", "rented_fraction": 0.5}]
    assert legacy.events_log.capital_improvement_events.select(
        "month_index", "property_id", "amount_quanta", "description"
    ).to_dicts() == [{"month_index": 6, "property_id": "home", "amount_quanta": 1_000_000, "description": ""}]
    rollout = rust["rollouts"][0]
    assert rollout["property_rented_fraction_events"] == [
        {"month": 6, "property_id": "home", "rented_fraction_ppb": 500_000_000}
    ]
    assert rollout["capital_improvements"] == [
        {"month": 6, "property_id": "home", "amount": 1_000_000, "description": ""}
    ]
    # Lifecycle changes apply before this month's depreciation and mortgage split.
    first_depreciation = rollout["months"][7]["properties"][0]["cumulative_depreciation"]
    assert first_depreciation > 0
    assert rollout["months"][6]["properties"][0]["cumulative_depreciation"] == 0

    legacy_tax = legacy.events_log.tax_breakdowns.row(0, named=True)
    rust_tax = rollout["tax_accruals"][0]
    assert rust_tax["ordinary_income"] == legacy_tax["ordinary_income_quanta"]
    assert rust_tax["mortgage_interest_deduction"] == legacy_tax["mortgage_interest_deduction_quanta"]
    assert rust_tax["itemized_deduction"] == legacy_tax["itemized_deduction_quanta"]
    assert rust_tax["ordinary_taxable"] == legacy_tax["ordinary_taxable_quanta"]
    assert rust_tax["total_tax"] == legacy_tax["total_tax_quanta"]
    assert rust_tax["rental_interest_deduction"] > 0
    assert rust_tax["depreciation_deduction"] > 0


def test_rust_and_jax_match_uncapped_acquisition_mortgage_interest(tmp_path: Path) -> None:
    fixture = uncapped_mortgage_interest_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_tax = legacy.events_log.tax_breakdowns.row(0, named=True)
    rollout = rust["rollouts"][0]
    rust_tax = rollout["tax_accruals"][0]
    total_interest = sum(payment["interest"] for payment in rollout["mortgage_payments"])
    assert rust_tax["mortgage_interest_deduction"] == total_interest
    assert rust_tax["mortgage_interest_deduction"] == legacy_tax["mortgage_interest_deduction_quanta"]
    assert rust_tax["itemized_deduction"] == legacy_tax["itemized_deduction_quanta"]
    assert rust_tax["total_tax"] == legacy_tax["total_tax_quanta"]


def test_rust_and_jax_match_mid_principal_caps_and_home_equity_exclusion(tmp_path: Path) -> None:
    fixture = mortgage_interest_policy_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    sort_columns = ["rollout_index", "month_index", "agent_id", "jurisdiction_id"]
    assert rust_events.tax_breakdowns.schema == legacy.events_log.tax_breakdowns.schema
    assert (
        rust_events.tax_breakdowns.sort(sort_columns).to_dicts()
        == legacy.events_log.tax_breakdowns.sort(sort_columns).to_dicts()
    )

    breakdowns = {(row["agent_id"], row["jurisdiction_id"]): row for row in rust_events.tax_breakdowns.to_dicts()}
    alice_federal = breakdowns[("alice", "federal_us")]["mortgage_interest_deduction_quanta"]
    alice_california = breakdowns[("alice", "california")]["mortgage_interest_deduction_quanta"]
    assert 0 < alice_federal < alice_california
    assert alice_federal == round(alice_california * 75 / 80)
    assert breakdowns[("bob", "federal_us")]["mortgage_interest_deduction_quanta"] == 0
    assert breakdowns[("bob", "california")]["mortgage_interest_deduction_quanta"] == 0


if __name__ == "__main__":
    pytest_bazel.main()
