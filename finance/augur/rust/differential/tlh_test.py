"""Rust/JAX differential coverage for stateful reduced-form tax-loss harvesting and its sale-time give-back.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pytest_bazel

from finance.augur.rust.differential.fixture_adapter import run_legacy_fixture
from finance.augur.rust.differential.fixtures import rust_run, tax_fixture
from finance.augur.rust.differential.output_adapter import decode_rust_event_log
from finance.augur.sim.testing.state_helpers import capital_gains_ytd


def tlh_fixture(
    *,
    partial_sales: bool = False,
    same_month_sales: bool = False,
    target_allocation_sale: bool = False,
    failure_after_first_harvest: bool = False,
) -> dict[str, Any]:
    horizon = 12
    scheduled_sales = []
    if partial_sales:
        scheduled_sales = [
            {
                "month": 4,
                "cause_id": "sp500_half",
                "agent_id": "alice",
                "account_id": "brokerage",
                "asset_id": "sp500",
                "units": 500_000_000,
                "proceeds_account_id": "checking",
            },
            {
                "month": 7,
                "cause_id": "sp500_rest",
                "agent_id": "alice",
                "account_id": "brokerage",
                "asset_id": "sp500",
                "units": 500_000_000,
                "proceeds_account_id": "checking",
            },
        ]
    if same_month_sales:
        scheduled_sales = [
            {
                "month": 4,
                "cause_id": "sp500_quarter_a",
                "agent_id": "alice",
                "account_id": "brokerage",
                "asset_id": "sp500",
                "units": 250_000_000,
                "proceeds_account_id": "checking",
            },
            {
                "month": 4,
                "cause_id": "sp500_quarter_b",
                "agent_id": "alice",
                "account_id": "brokerage",
                "asset_id": "sp500",
                "units": 250_000_000,
                "proceeds_account_id": "checking",
            },
            {
                "month": 7,
                "cause_id": "sp500_final_half",
                "agent_id": "alice",
                "account_id": "brokerage",
                "asset_id": "sp500",
                "units": 500_000_000,
                "proceeds_account_id": "checking",
            },
        ]
    accounts = [
        {
            "account": {"agent_id": "alice", "account_id": "brokerage"},
            "opening_balance": 500 if target_allocation_sale else 0,
        },
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0},
    ]
    obligations = []
    target_allocation_policies = []
    if target_allocation_sale:
        target_allocation_policies = [
            {
                "agent_id": "alice",
                "account_id": "brokerage",
                "source_account_ids": ["brokerage"],
                "cash_floor": {
                    "kind": "series_indexed",
                    "base_amount": 500,
                    "series_id": "inflation",
                    "base_month_index": 0,
                    "adjustment_period_months": 1,
                },
                "cash_ceiling": 2_000,
                "cause_id_prefix": "allocation_sale",
                "purchase_slots_per_sleeve": 0,
                "sleeves": [{"asset_id": "sp500", "weight": 1, "quantity_scale": 1_000_000}],
            }
        ]
    if failure_after_first_harvest:
        accounts.append({"account": {"agent_id": "sink", "account_id": "checking"}, "opening_balance": 0})
        obligations = [
            {
                "month": 1,
                "obligation_id": "unfunded_after_harvest",
                "obligation_type": "cash_spend",
                "from": {"agent_id": "alice", "account_id": "brokerage"},
                "to": {"agent_id": "sink", "account_id": "checking"},
                "amount_due": 1,
            }
        ]
    rollout_count = 1 if partial_sales or same_month_sales or target_allocation_sale else 2
    levels = [[100] * (horizon + 1)]
    if rollout_count == 2:
        levels = [[100, 100, 80, 80, 90, 90, 90, 95, 95, 95, 95, 95, 95], [100] * (horizon + 1)]
    return {
        "schema_version": 8,
        "currency_code": "USD",
        "currency_quantum": "0.01",
        "rollout_count": rollout_count,
        "scenario": {
            "horizon_months": horizon,
            "accounts": accounts,
            "scheduled_transfers": [],
            "recurring_transfers": [],
            "obligations": obligations,
            "recurring_obligations": [],
            "initial_lots": [
                {
                    "lot_id": "alice_sp500",
                    "agent_id": "alice",
                    "account_id": "brokerage",
                    "asset_id": "sp500",
                    "purchase_month": 0,
                    "quantity_scale": 1_000_000,
                    "units": 1_000_000_000,
                    "basis": 100_000,
                }
            ],
            "initial_bonds": [],
            "scheduled_sales": scheduled_sales,
            "tax_profiles": [
                {
                    "agent_id": "alice",
                    "tax_authority_agent_id": "irs",
                    "jurisdictions": [tax_fixture()["scenario"]["tax_profiles"][0]["jurisdictions"][0]],
                }
            ],
            "distributions": [],
            "target_allocation_policies": target_allocation_policies,
            "private_equity_tender_policies": [],
            "harvest_policies": [
                {
                    "owner_agent_id": "alice",
                    "account_id": "brokerage",
                    "asset_id": "sp500",
                    "peak_annual_yield_ppb": 120_000_000,
                    "floor_annual_yield_ppb": 4_000_000,
                    "maturity_decay_exponent_ppb": 1_500_000_000,
                    "drawdown_sensitivity_ppb": 6_000_000_000,
                    "short_term_fraction_ppb": 1_000_000_000,
                }
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
        "series": [
            {
                "series_id": "security:sp500",
                "snapshots": horizon + 1,
                "values": [value for rollout in levels for value in rollout],
            },
            *(
                [
                    {
                        "series_id": "inflation",
                        "snapshots": horizon + 1,
                        "values": [1_000_000_000] + [2_000_000_000] * horizon,
                    }
                ]
                if target_allocation_sale
                else []
            ),
        ],
    }


def rust_capital_gain_frame(rust: dict[str, Any]) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for rollout in rust["rollouts"]:
        for snapshot in rollout["months"]:
            for state in snapshot["capital_gains"]:
                rows.extend(
                    [
                        {
                            "rollout_index": rollout["rollout_id"],
                            "month_index": snapshot["month"],
                            "agent_id": state["agent_id"],
                            "classification": "stcg",
                            "gain_quanta": state["short_term_gain"],
                        },
                        {
                            "rollout_index": rollout["rollout_id"],
                            "month_index": snapshot["month"],
                            "agent_id": state["agent_id"],
                            "classification": "ltcg",
                            "gain_quanta": state["long_term_gain"],
                        },
                    ]
                )
    return pl.DataFrame(rows).sort("rollout_index", "month_index", "agent_id", "classification")


def assert_tlh_parity(fixture: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)
    legacy_lookup = {
        (row["rollout_index"], row["month_index"], row["agent_id"], row["classification"]): row["gain_quanta"]
        for row in capital_gains_ytd(legacy).to_dicts()
    }
    rust_gains = rust_capital_gain_frame(rust)
    for row in rust_gains.to_dicts():
        key = (row["rollout_index"], row["month_index"], row["agent_id"], row["classification"])
        assert row["gain_quanta"] == legacy_lookup.get(key, 0)

    rust_events = decode_rust_event_log(rust)
    for legacy_frame, rust_frame, sort_columns in (
        (
            legacy.events_log.lot_dispositions,
            rust_events.lot_dispositions,
            ["rollout_index", "month_index", "cause_id", "lot_id"],
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
    ):
        assert rust_frame.schema == legacy_frame.schema
        assert rust_frame.sort(sort_columns).to_dicts() == legacy_frame.sort(sort_columns).to_dicts()

    for rollout in rust["rollouts"]:
        gains_by_month = {
            row["month_index"]: row["gain_quanta"]
            for row in rust_gains.filter(
                (pl.col("rollout_index") == rollout["rollout_id"]) & (pl.col("classification") == "stcg")
            ).to_dicts()
        }
        for snapshot in rollout["months"]:
            if snapshot["month"] < 12:
                assert snapshot["tlh_cumulative_harvest"][0] == -gains_by_month[snapshot["month"]]
    return rust


def test_rust_and_jax_match_tlh_harvest_paths_and_year_end_tax(tmp_path: Path) -> None:
    rust = assert_tlh_parity(tlh_fixture(), tmp_path)

    drawdown = rust["rollouts"][0]["months"][3]["tlh_cumulative_harvest"][0]
    flat = rust["rollouts"][1]["months"][3]["tlh_cumulative_harvest"][0]
    assert drawdown > flat > 0


def test_rust_and_jax_match_tlh_partial_sale_give_back(tmp_path: Path) -> None:
    rust = assert_tlh_parity(tlh_fixture(partial_sales=True), tmp_path)

    assert rust["rollouts"][0]["months"][8]["tlh_cumulative_harvest"] == [0]


def test_rust_and_jax_match_tlh_same_month_sales_share_pre_sale_ledger(tmp_path: Path) -> None:
    rust = assert_tlh_parity(tlh_fixture(same_month_sales=True), tmp_path)

    assert rust["rollouts"][0]["months"][8]["tlh_cumulative_harvest"] == [0]


def test_rust_and_jax_match_tlh_target_allocation_sale_give_back(tmp_path: Path) -> None:
    rust = assert_tlh_parity(tlh_fixture(target_allocation_sale=True), tmp_path)

    dispositions = decode_rust_event_log(rust).lot_dispositions
    assert dispositions.filter(pl.col("cause_id").str.starts_with("allocation_sale_m1_security:sp500")).height == 1
    assert rust["rollouts"][0]["months"][2]["tlh_cumulative_harvest"][0] > 0


def test_rust_and_jax_match_tlh_failure_suppression(tmp_path: Path) -> None:
    rust = assert_tlh_parity(tlh_fixture(failure_after_first_harvest=True), tmp_path)

    for rollout in rust["rollouts"]:
        assert rollout["failed_month"] == 1
        assert rollout["months"][1]["tlh_cumulative_harvest"][0] > 0
        assert all(snapshot["tlh_cumulative_harvest"] == [0] for snapshot in rollout["months"][2:])


if __name__ == "__main__":
    pytest_bazel.main()
