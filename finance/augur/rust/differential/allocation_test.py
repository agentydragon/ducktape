"""Rust/JAX differential coverage for target-allocation liquidity sales, post-settlement purchases, and quiet-band drift rebalancing.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import pytest_bazel

from finance.augur.rust.differential.fixture_adapter import run_legacy_fixture
from finance.augur.rust.differential.fixtures import (
    rust_cash_frame,
    rust_lot_frame,
    rust_run,
    simulator_binary,
    target_allocation_fixture,
    target_allocation_purchase_fixture,
)
from finance.augur.sim.testing.state_helpers import asset_lots, cash_balances, rollout_status


def target_allocation_failure_fixture() -> dict[str, Any]:
    fixture = target_allocation_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 2
    scenario["accounts"][0]["opening_balance"] = 0
    scenario["recurring_obligations"][0]["end_month"] = 1
    scenario["recurring_obligations"][0]["amount_due"] = 5_000_000
    scenario["initial_lots"] = [
        {
            "lot_id": "vti",
            "agent_id": "alice",
            "account_id": "brokerage-a",
            "asset_id": "vti",
            "purchase_month": -24,
            "quantity_scale": 1_000_000,
            "units": 100_000_000,
            "basis": 500_000,
        },
        {
            "lot_id": "bnd",
            "agent_id": "alice",
            "account_id": "brokerage-b",
            "asset_id": "bnd",
            "purchase_month": -24,
            "quantity_scale": 1_000_000,
            "units": 100_000_000,
            "basis": 1_000_000,
        },
    ]
    scenario["tax_profiles"] = []
    fixture["series"] = [
        {"series_id": "security:vti", "snapshots": 3, "values": [10_000] * 3},
        {"series_id": "security:bnd", "snapshots": 3, "values": [10_000] * 3},
    ]
    return fixture


def target_allocation_purchase_then_sale_fixture() -> dict[str, Any]:
    fixture = target_allocation_purchase_fixture()
    scenario = fixture["scenario"]
    policy = scenario["target_allocation_policies"][0]
    policy["source_account_ids"] = ["brokerage-b"]
    scenario["initial_lots"][0]["lot_id"] = "zz-real-same-month"
    scenario["initial_lots"][1]["account_id"] = "brokerage-b"
    scenario["recurring_obligations"] = [
        {
            "start_month": 1,
            "end_month": 1,
            "obligation_id": "rent",
            "obligation_type": "rent",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "landlord", "account_id": "checking"},
            "amount_due": 17_500_000,
        }
    ]
    return fixture


def target_allocation_purchase_distribution_fixture() -> dict[str, Any]:
    fixture = target_allocation_purchase_fixture()
    scenario = fixture["scenario"]
    scenario["initial_lots"][1]["account_id"] = "brokerage-b"
    scenario["distributions"] = [
        {"agent_id": "alice", "holding_account_id": "brokerage-a", "asset_id": "vti", "to_account_id": "checking"}
    ]
    fixture["series"].append({"series_id": "security_distribution:vti", "snapshots": 3, "values": [100, 100, 100]})
    return fixture


def target_allocation_rebalance_fixture(*, tolerance_ppb: int = 250_000_000) -> dict[str, Any]:
    fixture = target_allocation_purchase_fixture()
    scenario = fixture["scenario"]
    scenario["accounts"][0]["opening_balance"] = 5_000_000
    policy = scenario["target_allocation_policies"][0]
    policy["cash_floor"] = 1_000_000
    policy["cash_ceiling"] = 9_000_000
    policy["rebalance_tolerance_ppb"] = tolerance_ppb
    return fixture


def test_rust_and_jax_match_liquidity_sales_before_obligation_funding(tmp_path: Path) -> None:
    fixture = target_allocation_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
        .to_dicts()
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash

    legacy_dispositions = (
        legacy.events_log.lot_dispositions.with_columns(
            (pl.col("units_sold") * 1_000_000).round().cast(pl.Int64).alias("units_sold_quanta"),
            (pl.col("proceeds_quanta") - pl.col("cost_basis_consumed_quanta")).alias("realized_gain_quanta"),
        )
        .select(
            "rollout_index",
            "month_index",
            "cause_id",
            "agent_id",
            "source_account_id",
            "asset_id",
            "lot_id",
            "purchase_month_index",
            "units_sold_quanta",
            "cost_basis_consumed_quanta",
            "proceeds_quanta",
            "proceeds_account_id",
            "realized_gain_quanta",
        )
        .sort("rollout_index", "month_index", "source_account_id")
        .to_dicts()
    )
    rust_dispositions_in_execution_order = [
        {
            "rollout_index": rollout["rollout_id"],
            "month_index": disposition["month"],
            "cause_id": disposition["cause_id"],
            "agent_id": disposition["agent_id"],
            "source_account_id": disposition["source_account_id"],
            "asset_id": disposition["asset_id"],
            "lot_id": disposition["lot_id"],
            "purchase_month_index": disposition["purchase_month"],
            "units_sold_quanta": disposition["units"],
            "cost_basis_consumed_quanta": disposition["basis"],
            "proceeds_quanta": disposition["proceeds"],
            "proceeds_account_id": disposition["proceeds_account_id"],
            "realized_gain_quanta": disposition["realized_gain"],
        }
        for rollout in rust["rollouts"]
        for disposition in rollout["dispositions"]
    ]
    assert [row["source_account_id"] for row in rust_dispositions_in_execution_order] == ["brokerage-a", "brokerage-b"]
    rust_dispositions = sorted(
        rust_dispositions_in_execution_order,
        key=lambda row: (row["rollout_index"], row["month_index"], row["source_account_id"]),
    )
    assert rust_dispositions == legacy_dispositions
    assert rust_dispositions == [
        {
            "rollout_index": 0,
            "month_index": 1,
            "cause_id": "allocation_sale_m1_security:vti",
            "agent_id": "alice",
            "source_account_id": "brokerage-a",
            "asset_id": "security:vti",
            "lot_id": "z-source-first",
            "purchase_month_index": -24,
            "units_sold_quanta": 100_000_000,
            "cost_basis_consumed_quanta": 500_000,
            "proceeds_quanta": 1_000_000,
            "proceeds_account_id": "checking",
            "realized_gain_quanta": 500_000,
        },
        {
            "rollout_index": 0,
            "month_index": 1,
            "cause_id": "allocation_sale_m1_security:vti",
            "agent_id": "alice",
            "source_account_id": "brokerage-b",
            "asset_id": "security:vti",
            "lot_id": "a-source-second",
            "purchase_month_index": 0,
            "units_sold_quanta": 130_000_000,
            "cost_basis_consumed_quanta": 1_040_000,
            "proceeds_quanta": 1_300_000,
            "proceeds_account_id": "checking",
            "realized_gain_quanta": 260_000,
        },
    ]

    obligation_columns = [
        "rollout_index",
        "month_index",
        "cause_id",
        "obligation_id",
        "amount_due_quanta",
        "amount_paid_quanta",
        "shortfall_quanta",
        "attempted_funding_sources",
    ]
    legacy_obligations = legacy.events_log.obligation_settlements.select(obligation_columns).sort(
        ["month_index", "obligation_id"]
    )
    rust_obligations = pl.DataFrame(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": outcome["month"],
                "cause_id": outcome["cause_id"],
                "obligation_id": outcome["obligation_id"],
                "amount_due_quanta": outcome["amount_due"],
                "amount_paid_quanta": outcome["amount_paid"],
                "shortfall_quanta": outcome["shortfall"],
                "attempted_funding_sources": outcome["attempted_funding_sources"],
            }
            for rollout in rust["rollouts"]
            for outcome in rollout["obligations"]
        ]
    ).sort(["month_index", "obligation_id"])
    assert rust_obligations.to_dicts() == legacy_obligations.to_dicts()
    assert rust_obligations.get_column("attempted_funding_sources").unique().to_list() == ["security:vti,security:bnd"]

    legacy_tax = (
        legacy.events_log.tax_breakdowns.select(
            "rollout_index", "month_index", "jurisdiction_id", "stcg_quanta", "ltcg_quanta"
        )
        .sort("jurisdiction_id")
        .to_dicts()
    )
    rust_tax = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": accrual["month"],
                "jurisdiction_id": accrual["jurisdiction_id"],
                "stcg_quanta": accrual["short_term_gain"],
                "ltcg_quanta": accrual["long_term_gain"],
            }
            for rollout in rust["rollouts"]
            for accrual in rollout["tax_accruals"]
        ],
        key=lambda row: row["jurisdiction_id"],
    )
    assert rust_tax == legacy_tax
    assert {(row["stcg_quanta"], row["ltcg_quanta"]) for row in rust_tax} == {(260_000, 500_000)}
    assert rollout_status(legacy).to_dicts() == [{"rollout_index": 0, "status": "active", "failed_month": None}]
    assert rust["rollouts"][0]["failed_month"] is None
    assert all(sum(posting["amount"] for posting in entry["postings"]) == 0 for entry in rust["rollouts"][0]["journal"])


def test_rust_and_jax_match_post_settlement_target_allocation_purchases(tmp_path: Path) -> None:
    fixture = target_allocation_purchase_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
        .to_dicts()
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash

    columns = [
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
    ]
    legacy_lots = (
        asset_lots(legacy)
        .filter((pl.col("month_index") == 1) & pl.col("lot_id").str.starts_with("allocation_sale_buy"))
        .select(columns)
        .sort("lot_id")
        .to_dicts()
    )
    rust_lots = (
        rust_lot_frame(rust)
        .filter((pl.col("month_index") == 1) & pl.col("lot_id").str.starts_with("allocation_sale_buy"))
        .select(columns)
        .sort("lot_id")
        .to_dicts()
    )
    assert rust_lots == legacy_lots
    assert rust_lots == [
        {
            "rollout_index": 0,
            "month_index": 1,
            "lot_id": "allocation_sale_buy_p0_s0_0",
            "agent_id": "alice",
            "account_id": "brokerage-a",
            "asset_id": "security:vti",
            "purchase_month_index": 0,
            "cost_basis_per_unit_quanta": 10_000,
            "remaining_quantity_quanta": 50_000_000,
            "quantity_scale": 1_000_000,
        },
        {
            "rollout_index": 0,
            "month_index": 1,
            "lot_id": "allocation_sale_buy_p0_s1_0",
            "agent_id": "alice",
            "account_id": "brokerage-a",
            "asset_id": "security:bnd",
            "purchase_month_index": 0,
            "cost_basis_per_unit_quanta": 10_000,
            "remaining_quantity_quanta": 850_000_000,
            "quantity_scale": 1_000_000,
        },
    ]
    alice_cash = next(
        account["balance"]
        for account in rust["rollouts"][0]["months"][1]["balances"]
        if account["account"]["agent_id"] == "alice" and account["account"]["account_id"] == "checking"
    )
    assert alice_cash == 1_000_000
    buy_entries = [
        entry
        for entry in rust["rollouts"][0]["journal"]
        if entry["cause_id"].startswith("allocation_sale_buy_m0_security:")
    ]
    assert {
        entry["cause_id"]: {
            (posting["account"]["agent_id"], posting["account"]["account_id"]): posting["amount"]
            for posting in entry["postings"]
        }
        for entry in buy_entries
    } == {
        "allocation_sale_buy_m0_security:vti": {
            ("alice", "checking"): -500_000,
            ("alice", "asset-basis:brokerage-a:vti"): 500_000,
        },
        "allocation_sale_buy_m0_security:bnd": {
            ("alice", "checking"): -8_500_000,
            ("alice", "asset-basis:brokerage-a:bnd"): 8_500_000,
        },
    }
    assert all(sum(posting["amount"] for posting in entry["postings"]) == 0 for entry in rust["rollouts"][0]["journal"])


def test_rust_and_jax_match_quiet_band_drift_rebalancing(tmp_path: Path) -> None:
    fixture = target_allocation_rebalance_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    assert rust_cash_frame(rust).to_dicts() == (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
        .to_dicts()
    )
    columns = [
        "lot_id",
        "account_id",
        "purchase_month_index",
        "cost_basis_per_unit_quanta",
        "remaining_quantity_quanta",
    ]
    legacy_lots = asset_lots(legacy).filter(pl.col("month_index") == 1).select(columns).sort("lot_id").to_dicts()
    rust_lots = rust_lot_frame(rust).filter(pl.col("month_index") == 1).select(columns).sort("lot_id").to_dicts()
    assert rust_lots == legacy_lots
    remaining = {row["lot_id"]: row["remaining_quantity_quanta"] for row in rust_lots}
    assert remaining == {
        "a-source-second": 500_000_000,
        "allocation_sale_buy_p0_s0_0": 0,
        "allocation_sale_buy_p0_s1_0": 400_000_000,
        "bond": 100_000_000,
        "z-source-first": 0,
    }
    assert (
        next(
            account["balance"]
            for account in rust["rollouts"][0]["months"][1]["balances"]
            if account["account"]["agent_id"] == "alice" and account["account"]["account_id"] == "checking"
        )
        == 5_000_000
    )
    assert [(row["lot_id"], row["units"]) for row in rust["rollouts"][0]["dispositions"]] == [
        ("z-source-first", 100_000_000),
        ("a-source-second", 300_000_000),
    ]


def test_purchased_lots_join_the_source_pool_after_real_lots_in_fifo_order(tmp_path: Path) -> None:
    fixture = target_allocation_purchase_then_sale_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_dispositions = (
        legacy.events_log.lot_dispositions.with_columns(
            (pl.col("units_sold") * 1_000_000).round().cast(pl.Int64).alias("units_sold_quanta")
        )
        .filter((pl.col("month_index") == 1) & (pl.col("asset_id") == "security:vti"))
        .select("lot_id", "units_sold_quanta", "cost_basis_consumed_quanta", "proceeds_quanta")
        .sort("lot_id")
        .to_dicts()
    )
    rust_dispositions = sorted(
        [
            {
                "lot_id": disposition["lot_id"],
                "units_sold_quanta": disposition["units"],
                "cost_basis_consumed_quanta": disposition["basis"],
                "proceeds_quanta": disposition["proceeds"],
            }
            for disposition in rust["rollouts"][0]["dispositions"]
            if disposition["month"] == 1 and disposition["asset_id"] == "security:vti"
        ],
        key=lambda row: row["lot_id"],
    )
    assert rust_dispositions == legacy_dispositions
    assert {row["lot_id"]: row["units_sold_quanta"] for row in rust_dispositions} == {
        "allocation_sale_buy_p0_s0_0": 25_000_000,
        "z-source-first": 100_000_000,
        "zz-real-same-month": 800_000_000,
    }

    columns = [
        "lot_id",
        "account_id",
        "purchase_month_index",
        "cost_basis_per_unit_quanta",
        "remaining_quantity_quanta",
    ]
    legacy_slots = (
        asset_lots(legacy)
        .filter((pl.col("month_index") == 2) & pl.col("lot_id").str.starts_with("allocation_sale_buy"))
        .select(columns)
        .sort("lot_id")
        .to_dicts()
    )
    rust_slots = (
        rust_lot_frame(rust)
        .filter((pl.col("month_index") == 2) & pl.col("lot_id").str.starts_with("allocation_sale_buy"))
        .select(columns)
        .sort("lot_id")
        .to_dicts()
    )
    assert rust_slots == legacy_slots


def test_purchase_slot_only_pool_receives_later_distributions(tmp_path: Path) -> None:
    fixture = target_allocation_purchase_distribution_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    assert rust_cash_frame(rust).to_dicts() == (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
        .to_dicts()
    )
    vti_slot = next(
        lot for lot in rust["rollouts"][0]["months"][1]["lots"] if lot["lot_id"] == "allocation_sale_buy_p0_s0_0"
    )
    assert vti_slot["account_id"] == "brokerage-a"
    assert vti_slot["units_remaining"] == 50_000_000
    assert [outcome["amount"] for outcome in rust["rollouts"][0]["distributions"]] == [0, 5_000]
    assert (
        next(
            account["balance"]
            for account in rust["rollouts"][0]["months"][2]["balances"]
            if account["account"]["agent_id"] == "alice" and account["account"]["account_id"] == "checking"
        )
        == 1_005_000
    )


def test_failed_settlement_suppresses_previously_decided_purchases(tmp_path: Path) -> None:
    fixture = target_allocation_purchase_fixture()
    scenario = fixture["scenario"]
    scenario["accounts"][1]["opening_balance"] = 0
    scenario["recurring_obligations"] = [
        {
            "start_month": 0,
            "end_month": 0,
            "obligation_id": "unfunded-income",
            "obligation_type": "cash_spend",
            "from": {"agent_id": "landlord", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount_due": 1_000_000,
        }
    ]
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    assert rollout_status(legacy).to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 0}
    ]
    rollout = rust["rollouts"][0]
    assert rollout["failed_month"] == 0
    assert not any(entry["cause_id"].startswith("allocation_sale_buy_") for entry in rollout["journal"])
    assert all(lot["units_remaining"] == 0 and lot["basis_remaining"] == 0 for lot in rollout["months"][1]["lots"])


def test_successive_purchases_keep_distinct_months_and_rollout_prices(tmp_path: Path) -> None:
    fixture = target_allocation_purchase_fixture(purchase_slots=2)
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 3
    scenario["accounts"][0]["opening_balance"] = 0
    scenario["accounts"][1]["opening_balance"] = 9_000_000
    scenario["target_allocation_policies"][0]["cash_floor"] = 0
    scenario["target_allocation_policies"][0]["cash_ceiling"] = 1_000_000
    scenario["recurring_obligations"] = [
        {
            "start_month": 0,
            "end_month": 2,
            "obligation_id": "income",
            "obligation_type": "cash_spend",
            "from": {"agent_id": "landlord", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount_due": 3_000_000,
        }
    ]
    for series in fixture["series"]:
        series["snapshots"] = 4
        series["values"] = [10_000] * 4 if series["series_id"] == "security:vti" else [10_000, 20_000, 30_000, 30_000]
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    columns = ["lot_id", "purchase_month_index", "cost_basis_per_unit_quanta", "remaining_quantity_quanta"]
    legacy_slots = (
        asset_lots(legacy)
        .filter((pl.col("month_index") == 3) & pl.col("lot_id").str.starts_with("allocation_sale_buy"))
        .select(columns)
        .sort("lot_id")
        .to_dicts()
    )
    rust_slots = (
        rust_lot_frame(rust)
        .filter((pl.col("month_index") == 3) & pl.col("lot_id").str.starts_with("allocation_sale_buy"))
        .select(columns)
        .sort("lot_id")
        .to_dicts()
    )
    assert rust_slots == legacy_slots
    bnd_slots = {row["lot_id"]: row for row in rust_slots if "_s1_" in row["lot_id"]}
    assert (
        bnd_slots["allocation_sale_buy_p0_s1_0"]["purchase_month_index"],
        bnd_slots["allocation_sale_buy_p0_s1_0"]["cost_basis_per_unit_quanta"],
    ) == (1, 20_000)
    assert (
        bnd_slots["allocation_sale_buy_p0_s1_1"]["purchase_month_index"],
        bnd_slots["allocation_sale_buy_p0_s1_1"]["cost_basis_per_unit_quanta"],
    ) == (2, 30_000)


def test_rust_and_jax_abort_when_target_allocation_purchase_slots_are_exhausted(tmp_path: Path) -> None:
    fixture = target_allocation_purchase_fixture(purchase_slots=1)
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 3
    scenario["accounts"][0]["opening_balance"] = 0
    scenario["accounts"][1]["opening_balance"] = 9_000_000
    scenario["target_allocation_policies"][0]["cash_floor"] = 0
    scenario["target_allocation_policies"][0]["cash_ceiling"] = 1_000_000
    scenario["recurring_obligations"] = [
        {
            "start_month": 0,
            "end_month": 2,
            "obligation_id": "income",
            "obligation_type": "cash_spend",
            "from": {"agent_id": "landlord", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount_due": 3_000_000,
        }
    ]
    for series in fixture["series"]:
        series["snapshots"] = 4
        series["values"] = [10_000] * 4

    with pytest.raises(ValueError, match="ran out of purchase slots: 1 configured, 2 needed"):
        run_legacy_fixture(fixture)

    fixture_path = tmp_path / "exhausted-purchase-slots.json"
    output_path = tmp_path / "unused.json"
    fixture_path.write_text(json.dumps(fixture, separators=(",", ":")))
    completed = subprocess.run(
        [simulator_binary(), fixture_path, output_path], check=False, capture_output=True, text=True
    )
    assert completed.returncode == 1
    assert "ran out of purchase slots: 1 configured, 2 needed" in completed.stderr


def test_rust_and_jax_match_insufficient_target_allocation_failure_metadata(tmp_path: Path) -> None:
    fixture = target_allocation_failure_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    columns = [
        "rollout_index",
        "month_index",
        "cause_id",
        "agent_id",
        "deficit_quanta",
        "obligation_id",
        "obligation_type",
        "amount_due_quanta",
        "amount_paid_quanta",
        "shortfall_quanta",
        "attempted_funding_sources",
    ]
    legacy_failures = legacy.events_log.rollout_failures.select(columns).to_dicts()
    rust_failures = [
        {
            "rollout_index": rollout["rollout_id"],
            "month_index": failure["month"],
            "cause_id": failure["cause_id"],
            "agent_id": failure["agent_id"],
            "deficit_quanta": failure["deficit"],
            "obligation_id": failure["obligation_id"],
            "obligation_type": failure["obligation_type"],
            "amount_due_quanta": failure["amount_due"],
            "amount_paid_quanta": failure["amount_paid"],
            "shortfall_quanta": failure["shortfall"],
            "attempted_funding_sources": failure["attempted_funding_sources"],
        }
        for rollout in rust["rollouts"]
        for failure in rollout["rollout_failures"]
    ]
    assert rust_failures == legacy_failures
    assert rust_failures == [
        {
            "rollout_index": 0,
            "month_index": 1,
            "cause_id": "rent_m1_failure",
            "agent_id": "alice",
            "deficit_quanta": 5_000_000,
            "obligation_id": "rent_m1",
            "obligation_type": "rent",
            "amount_due_quanta": 5_000_000,
            "amount_paid_quanta": 0,
            "shortfall_quanta": 5_000_000,
            "attempted_funding_sources": "security:vti,security:bnd",
        }
    ]
    assert rollout_status(legacy).to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 1}
    ]
    assert rust["rollouts"][0]["failed_month"] == 1
    assert all(sum(posting["amount"] for posting in entry["postings"]) == 0 for entry in rust["rollouts"][0]["journal"])


def test_rebalancing_without_purchase_slots_is_rejected(tmp_path: Path) -> None:
    fixture = target_allocation_fixture()
    fixture["scenario"]["target_allocation_policies"][0]["rebalance_tolerance_ppb"] = 250_000_000

    with pytest.raises(ValueError, match="no purchase slots"):
        run_legacy_fixture(fixture)

    fixture_path = tmp_path / "rebalance-without-purchase-slots.json"
    output_path = tmp_path / "unused.json"
    fixture_path.write_text(json.dumps(fixture, separators=(",", ":")))
    completed = subprocess.run(
        [simulator_binary(), fixture_path, output_path], check=False, capture_output=True, text=True
    )
    assert completed.returncode == 1
    assert "invalid configuration" in completed.stderr


if __name__ == "__main__":
    pytest_bazel.main()
