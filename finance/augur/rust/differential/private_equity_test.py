"""Rust/JAX differential coverage for the typed private-equity tender protocol.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pytest_bazel

from finance.augur.rust.differential.fixture_adapter import run_legacy_fixture
from finance.augur.rust.differential.fixtures import rust_cash_frame, rust_lot_frame, rust_run, tax_fixture
from finance.augur.rust.differential.output_adapter import decode_rust_event_log
from finance.augur.sim.testing.state_helpers import asset_lots, cash_balances


def private_equity_fixture() -> dict[str, Any]:
    rollout_count = 4
    horizon = 3
    snapshots = horizon + 1

    def channel(default: int) -> list[int]:
        return [default] * (rollout_count * snapshots)

    def set_value(values: list[int], rollout: int, month: int, value: int) -> None:
        values[rollout * snapshots + month] = value

    mark = channel(10_000)
    regime = channel(1)
    event_kind = channel(0)
    opportunity = channel(0)
    capacity = channel(1_000_000_000)
    eligible = channel(1_000_000_000)
    forced_sale = channel(0)
    blocked = channel(0)
    recovery = channel(0)
    valuation = channel(0)

    set_value(event_kind, 0, 1, 1)
    set_value(opportunity, 0, 1, 1)
    set_value(capacity, 0, 1, 250_000_000)
    set_value(event_kind, 0, 2, 1)
    set_value(opportunity, 0, 2, 1)
    set_value(blocked, 0, 2, 1)

    set_value(regime, 1, 1, 2)
    set_value(event_kind, 1, 1, 3)

    set_value(event_kind, 2, 1, 5)
    set_value(forced_sale, 2, 1, 300_000_000)

    set_value(event_kind, 3, 1, 6)
    set_value(recovery, 3, 1, 10_000)

    series = []
    for name, values in {
        "mark": mark,
        "regime": regime,
        "event_kind": event_kind,
        "sale_opportunity": opportunity,
        "sale_capacity": capacity,
        "eligible": eligible,
        "forced_sale": forced_sale,
        "liquidity_blocked": blocked,
        "forced_recovery": recovery,
        "company_valuation": valuation,
    }.items():
        series.append({"series_id": f"private_equity_{name}:acme", "snapshots": snapshots, "values": values})
    return {
        "schema_version": 8,
        "currency_code": "USD",
        "currency_quantum": "0.01",
        "rollout_count": rollout_count,
        "scenario": {
            "horizon_months": horizon,
            "accounts": [{"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 0}],
            "scheduled_transfers": [],
            "recurring_transfers": [],
            "obligations": [],
            "recurring_obligations": [],
            "initial_lots": [
                {
                    "lot_id": "acme_lot_a",
                    "agent_id": "alice",
                    "account_id": "checking",
                    "asset_id": "private_equity:acme",
                    "purchase_month": -36,
                    "quantity_scale": 1_000_000,
                    "units": 40_000_000,
                    "basis": 40_000,
                },
                {
                    "lot_id": "acme_lot_b",
                    "agent_id": "alice",
                    "account_id": "checking",
                    "asset_id": "private_equity:acme",
                    "purchase_month": -12,
                    "quantity_scale": 1_000_000,
                    "units": 60_000_000,
                    "basis": 120_000,
                },
            ],
            "initial_bonds": [],
            "scheduled_sales": [],
            "tax_profiles": [],
            "distributions": [],
            "target_allocation_policies": [],
            "private_equity_tender_policies": [
                {"owner_agent_id": "alice", "proceeds_account_id": "checking", "liquid_net_worth_floor": 500_000}
            ],
            "scheduled_property_purchases": [],
            "initial_primary_residences": [],
            "primary_residence_events": [],
            "property_rented_fraction_events": [],
            "capital_improvement_events": [],
            "property_sales": [],
            "mortgage_interest_deduction_policies": [],
            "property_tax_policies": [],
            "federal_salt_deduction_policies": [],
        },
        "series": series,
    }


def private_equity_tax_fixture() -> dict[str, Any]:
    fixture = private_equity_fixture()
    fixture["rollout_count"] = 1
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 12
    scenario["accounts"].append({"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0})
    scenario["private_equity_tender_policies"][0]["liquid_net_worth_floor"] = 10_000_000
    scenario["tax_profiles"] = [
        {
            "agent_id": "alice",
            "tax_authority_agent_id": "irs",
            "jurisdictions": [tax_fixture()["scenario"]["tax_profiles"][0]["jurisdictions"][0]],
        }
    ]
    defaults = {
        "mark": 100_000,
        "regime": 1,
        "event_kind": 0,
        "sale_opportunity": 0,
        "sale_capacity": 1_000_000_000,
        "eligible": 1_000_000_000,
        "forced_sale": 0,
        "liquidity_blocked": 0,
        "forced_recovery": 0,
        "company_valuation": 0,
    }
    for series in fixture["series"]:
        channel = series["series_id"].removeprefix("private_equity_").partition(":")[0]
        series["snapshots"] = 13
        series["values"] = [defaults[channel]] * 13
        if channel in {"event_kind", "sale_opportunity"}:
            series["values"][1] = 1
    return fixture


def assert_private_equity_parity(fixture: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()

    legacy_lots = (
        asset_lots(legacy)
        .select(
            "rollout_index",
            "month_index",
            "lot_id",
            "agent_id",
            "account_id",
            "asset_id",
            "purchase_month_index",
            "cost_basis_per_unit_quanta",
            "remaining_quantity_quanta",
            "quantity_scale",
        )
        .sort("rollout_index", "month_index", "lot_id")
    )
    assert rust_lot_frame(rust).to_dicts() == legacy_lots.to_dicts()

    comparisons = (
        (
            legacy.events_log.lot_dispositions,
            rust_events.lot_dispositions,
            ["rollout_index", "month_index", "cause_id", "lot_id"],
        ),
        (
            legacy.events_log.private_equity_events,
            rust_events.private_equity_events,
            ["rollout_index", "month_index", "issuer_id"],
        ),
        (
            legacy.events_log.private_equity_opportunities,
            rust_events.private_equity_opportunities,
            ["rollout_index", "month_index", "cause_id"],
        ),
        (
            legacy.events_log.tax_accruals,
            rust_events.tax_accruals,
            ["rollout_index", "month_index", "cause_id", "jurisdiction_id"],
        ),
        (
            legacy.events_log.tax_breakdowns,
            rust_events.tax_breakdowns,
            ["rollout_index", "month_index", "cause_id", "jurisdiction_id"],
        ),
    )
    for legacy_frame, rust_frame, sort_columns in comparisons:
        assert rust_frame.schema == legacy_frame.schema
        assert rust_frame.sort(sort_columns).to_dicts() == legacy_frame.sort(sort_columns).to_dicts()

    assert all(
        sum(posting["amount"] for posting in entry["postings"]) == 0
        for rollout in rust["rollouts"]
        for entry in rollout["journal"]
    )
    return rust


def test_rust_and_jax_match_private_equity_protocol_sales_and_opportunities(tmp_path: Path) -> None:
    rust = assert_private_equity_parity(private_equity_fixture(), tmp_path)

    final_cash = {
        row["rollout_index"]: row["balance_quanta"]
        for row in rust_cash_frame(rust).filter(pl.col("month_index") == 3).to_dicts()
    }
    assert final_cash == {0: 250_000, 1: 500_000, 2: 300_000, 3: 10_000}


def test_rust_and_jax_match_private_equity_protocol_without_owner_policy(tmp_path: Path) -> None:
    fixture = private_equity_fixture()
    fixture["scenario"]["private_equity_tender_policies"] = []
    rust = assert_private_equity_parity(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    assert rust_events.lot_dispositions.is_empty()
    assert set(rust_events.private_equity_opportunities.get_column("outcome")) == {"no_policy"}
    assert {
        row["rollout_index"]: row["balance_quanta"]
        for row in rust_cash_frame(rust).filter(pl.col("month_index") == 3).to_dicts()
    } == dict.fromkeys(range(4), 0)


def test_rust_and_jax_match_private_equity_floor_satisfied_before_voluntary_sales(tmp_path: Path) -> None:
    fixture = private_equity_fixture()
    fixture["scenario"]["accounts"][0]["opening_balance"] = 600_000
    rust = assert_private_equity_parity(fixture, tmp_path)
    dispositions = decode_rust_event_log(rust).lot_dispositions

    assert not any(
        cause.startswith(("pe_tender_", "pe_public_market_")) for cause in dispositions.get_column("cause_id")
    )
    assert {
        row["rollout_index"]: row["balance_quanta"]
        for row in rust_cash_frame(rust).filter(pl.col("month_index") == 3).to_dicts()
    } == {0: 600_000, 1: 600_000, 2: 900_000, 3: 610_000}


def test_rust_and_jax_match_private_equity_disposition_tax_facts(tmp_path: Path) -> None:
    rust = assert_private_equity_parity(private_equity_tax_fixture(), tmp_path)
    [accrual] = rust["rollouts"][0]["tax_accruals"]

    assert accrual["short_term_gain"] == 0
    assert accrual["long_term_gain"] == 9_840_000
    assert accrual["capital_gain_tax"] == 770_625


if __name__ == "__main__":
    pytest_bazel.main()
