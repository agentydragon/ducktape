"""Rust/JAX differential coverage for the financed-property lifecycle: purchase, carrying costs, rental transitions, sale, and mortgage interest.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest_bazel

from finance.augur.rust.differential.fixture_adapter import run_legacy_fixture
from finance.augur.rust.differential.fixtures import (
    failure_fixture,
    financed_property_fixture,
    property_cashflow_fixture,
    property_depreciation_fixture,
    rust_cash_frame,
    rust_run,
    tax_fixture,
)
from finance.augur.rust.differential.output_adapter import decode_rust_event_log
from finance.augur.sim.testing.state_helpers import (
    cash_balances,
    liabilities,
    property_stakes,
    property_state,
    rollout_status,
)


def property_cashflow_gating_fixture() -> dict[str, Any]:
    fixture = failure_fixture()
    fixture["rollout_count"] = 1
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 4
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 1_000},
        {"account": {"agent_id": "seller", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "vendor", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "creditor", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["scheduled_transfers"] = []
    scenario["recurring_transfers"] = []
    scenario["obligations"] = [
        {
            "month": 2,
            "obligation_id": "unaffordable",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "creditor", "account_id": "checking"},
            "amount_due": 876,
        }
    ]
    scenario["recurring_obligations"] = []
    scenario["locations"] = [
        {
            "location_id": "test",
            "display_name": "Test",
            "jurisdiction_ids": [],
            "annual_property_tax_rate_ppb": 0,
            "annual_special_assessment": 0,
        }
    ]
    scenario["scheduled_property_purchases"] = [
        {
            "month": 1,
            "cause_id": "buy-home",
            "property_id": "home",
            "location_id": "test",
            "buyer_agent_id": "alice",
            "buyer_account_id": "checking",
            "seller_agent_id": "seller",
            "seller_account_id": "checking",
            "purchase_price": 100,
            "down_payment": 100,
            "buyer_closing_cost": 0,
            "mortgage": None,
        }
    ]
    scenario["property_tax_policies"] = []
    scenario["scheduled_property_cashflows"] = [
        {
            "month": 0,
            "property_id": "home",
            "cause_id": "before-purchase",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "vendor", "account_id": "checking"},
            "amount": 3,
        },
        {
            "month": 1,
            "property_id": "home",
            "cause_id": "purchase-month",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "vendor", "account_id": "checking"},
            "amount": 5,
        },
    ]
    scenario["recurring_property_cashflows"] = [
        {
            "start_month": 0,
            "end_month": 3,
            "property_id": "home",
            "cause_id": "property-carry",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "vendor", "account_id": "checking"},
            "amount": 10,
        }
    ]
    scenario["initial_lots"] = []
    scenario["scheduled_sales"] = []
    scenario["tax_profiles"] = []
    scenario["distributions"] = []
    fixture["series"] = []
    return fixture


def property_sale_fixture() -> dict[str, Any]:
    fixture = financed_property_fixture()
    fixture["rollout_count"] = 2
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 4
    scenario["accounts"].extend(
        [
            {"account": {"agent_id": "tenant", "account_id": "checking"}, "opening_balance": 10_000},
            {"account": {"agent_id": "gift", "account_id": "checking"}, "opening_balance": 1_000},
        ]
    )
    scenario["scheduled_transfers"] = [
        {
            "month": 2,
            "cause_id": "sale-month-generic-transfer",
            "from": {"agent_id": "gift", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount": 7,
        }
    ]
    scenario["recurring_property_cashflows"] = [
        {
            "start_month": 0,
            "end_month": 3,
            "property_id": "home",
            "cause_id": "rent",
            "from": {"agent_id": "tenant", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount": 1_000,
        }
    ]
    scenario["property_sales"] = [{"month": 2, "property_id": "home", "closing_cost_bps": 600}]
    fixture["series"] = [
        {
            "series_id": "home_value:sf",
            "snapshots": 5,
            "values": [
                50_000_000,
                50_000_000,
                60_000_000,
                60_000_000,
                60_000_000,
                50_000_000,
                50_000_000,
                55_000_000,
                55_000_000,
                55_000_000,
            ],
        }
    ]
    return fixture


def section_121_fixture() -> dict[str, Any]:
    fixture = financed_property_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 86
    scenario["accounts"] = [
        *[
            {"account": {"agent_id": agent_id, "account_id": "checking"}, "opening_balance": 60_000_000}
            for agent_id in ("alice", "bob", "carol", "dave")
        ],
        *[
            {"account": {"agent_id": seller_id, "account_id": "checking"}, "opening_balance": 0}
            for seller_id in ("seller-a", "seller-b", "seller-c", "seller-d")
        ],
        {"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["scheduled_property_purchases"] = [
        {
            "month": 0,
            "cause_id": f"{agent_id}-buys-home",
            "property_id": property_id,
            "location_id": "sf",
            "buyer_agent_id": agent_id,
            "buyer_account_id": "checking",
            "seller_agent_id": seller_id,
            "seller_account_id": "checking",
            "purchase_price": 50_000_000,
            "down_payment": 50_000_000,
            "buyer_closing_cost": 0,
            "rented_fraction_ppb": 0,
            "mortgage": None,
        }
        for agent_id, property_id, seller_id in (
            ("alice", "alice-home", "seller-a"),
            ("bob", "bob-home", "seller-b"),
            ("carol", "carol-home", "seller-c"),
            ("dave", "dave-home", "seller-d"),
        )
    ]
    scenario["initial_primary_residences"] = [
        {"agent_id": "alice", "property_id": "alice-home"},
        {"agent_id": "dave", "property_id": "dave-home"},
    ]
    scenario["primary_residence_events"] = [
        {"month": 7, "agent_id": "bob", "property_id": "bob-home"},
        {"month": 24, "agent_id": "dave", "property_id": None},
        {"month": 30, "agent_id": "carol", "property_id": "carol-home"},
    ]
    scenario["property_sales"] = [
        {"month": 30, "property_id": property_id, "closing_cost_bps": 0}
        for property_id in ("alice-home", "bob-home", "carol-home")
    ] + [{"month": 84, "property_id": "dave-home", "closing_cost_bps": 0}]
    scenario["property_tax_policies"] = []
    federal_profile = tax_fixture()["scenario"]["tax_profiles"][0]
    federal_profile["jurisdictions"] = federal_profile["jurisdictions"][:1]
    scenario["tax_profiles"] = []
    for agent_id in ("alice", "bob", "carol", "dave"):
        profile = json.loads(json.dumps(federal_profile))
        profile["agent_id"] = agent_id
        profile["section_121_exclusion"] = 25_000_000
        scenario["tax_profiles"].append(profile)
    fixture["series"] = [
        {"series_id": "home_value:sf", "snapshots": 87, "values": [50_000_000] * 30 + [75_000_000] * 57}
    ]
    return fixture


def uncapped_mortgage_interest_fixture() -> dict[str, Any]:
    fixture = property_cashflow_fixture()
    scenario = fixture["scenario"]
    purchase = scenario["scheduled_property_purchases"][0]
    purchase["purchase_price"] = 100_000_000
    purchase["down_payment"] = 20_000_000
    purchase["buyer_closing_cost"] = 1_000_000
    purchase["mortgage"]["principal"] = 80_000_000
    purchase["rented_fraction_ppb"] = 0
    scenario["property_tax_policies"] = []
    scenario["mortgage_interest_deduction_policies"] = [{"liability_id": "home-mortgage", "owner_agent_id": "alice"}]
    return fixture


def mortgage_interest_policy_fixture() -> dict[str, Any]:
    fixture = financed_property_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 12
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 30_000_000},
        {"account": {"agent_id": "bob", "account_id": "checking"}, "opening_balance": 30_000_000},
        {"account": {"agent_id": "seller-a", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "seller-b", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "bank-a", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "bank-b", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["scheduled_property_purchases"] = [
        {
            "month": 0,
            "cause_id": f"{agent_id}-buys-home",
            "property_id": f"{agent_id}-home",
            "location_id": "sf",
            "buyer_agent_id": agent_id,
            "buyer_account_id": "checking",
            "seller_agent_id": seller_id,
            "seller_account_id": "checking",
            "purchase_price": 100_000_000,
            "down_payment": 20_000_000,
            "buyer_closing_cost": 0,
            "rented_fraction_ppb": 0,
            "land_value_fraction_ppb": 1_000_000_000,
            "mortgage": {
                "liability_id": f"{agent_id}-mortgage",
                "lender_agent_id": bank_id,
                "lender_account_id": "checking",
                "principal": 80_000_000,
                "annual_interest_rate_ppb": 60_000_000,
                "term_months": 360,
            },
        }
        for agent_id, seller_id, bank_id in (("alice", "seller-a", "bank-a"), ("bob", "seller-b", "bank-b"))
    ]
    scenario["mortgage_interest_deduction_policies"] = [
        {
            "liability_id": "alice-mortgage",
            "owner_agent_id": "alice",
            "debt_class": "acquisition",
            "per_jurisdiction_principal_cap": {"federal_us": 75_000_000, "california": 100_000_000},
        },
        {
            "liability_id": "bob-mortgage",
            "owner_agent_id": "bob",
            "debt_class": "home_equity",
            "per_jurisdiction_principal_cap": {"federal_us": 75_000_000, "california": 100_000_000},
        },
    ]
    scenario["property_tax_policies"] = []
    scenario["scheduled_property_cashflows"] = []
    scenario["recurring_property_cashflows"] = []
    base_profile = tax_fixture()["scenario"]["tax_profiles"][0]
    scenario["tax_profiles"] = []
    for agent_id in ("alice", "bob"):
        profile = json.loads(json.dumps(base_profile))
        profile["agent_id"] = agent_id
        scenario["tax_profiles"].append(profile)
    fixture["series"] = []
    return fixture


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
