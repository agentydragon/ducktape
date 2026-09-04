"""Rust/JAX differential coverage for opening balances, transfers, amount schedules, grouped obligations, failure freezing, and fixture validation.

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

from finance.augur.rust.fixture_adapter import run_legacy_fixture
from finance.augur.rust.output_adapter import decode_rust_event_log
from finance.augur.rust.testing.fixtures import (
    failure_fixture,
    recurring_obligation_fixture,
    rust_cash_frame,
    rust_run,
    series_indexed_amount_fixture,
    shared_integer_fixture,
    simulator_binary,
    target_allocation_purchase_fixture,
)
from finance.augur.sim.testing.state_helpers import asset_lots, cash_balances, rollout_status


def test_rust_and_jax_match_on_shared_integer_fixture(tmp_path: Path) -> None:
    fixture = shared_integer_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()

    legacy_dispositions = (
        legacy.events_log.lot_dispositions.with_columns(
            (pl.col("units_sold") * 1_000_000).round().cast(pl.Int64).alias("units_sold_quanta")
        )
        .select(
            "rollout_index",
            "month_index",
            "lot_id",
            "units_sold_quanta",
            "cost_basis_consumed_quanta",
            "proceeds_quanta",
        )
        .sort("rollout_index", "month_index", "lot_id")
        .to_dicts()
    )
    rust_dispositions = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": disposition["month"],
                "lot_id": disposition["lot_id"],
                "units_sold_quanta": disposition["units"],
                "cost_basis_consumed_quanta": disposition["basis"],
                "proceeds_quanta": disposition["proceeds"],
            }
            for rollout in rust["rollouts"]
            for disposition in rollout["dispositions"]
        ],
        key=lambda row: (row["rollout_index"], row["month_index"], row["lot_id"]),
    )
    assert rust_dispositions == legacy_dispositions

    # The existing lot read model must also agree on the post-sale quantity.
    legacy_final_lots = asset_lots(legacy).filter(pl.col("month_index") == 3)
    assert legacy_final_lots.get_column("remaining_quantity_quanta").to_list() == [1_000_000, 1_000_000]

    for rollout in rust["rollouts"]:
        for entry in rollout["journal"]:
            assert sum(posting["amount"] for posting in entry["postings"]) == 0

    rust_events = decode_rust_event_log(rust)
    legacy_events = legacy.events_log
    comparisons = (
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
            legacy_events.lot_dispositions,
            rust_events.lot_dispositions,
            ["rollout_index", "month_index", "cause_id", "lot_id"],
        ),
        (
            legacy_events.obligation_accruals,
            rust_events.obligation_accruals,
            ["rollout_index", "month_index", "cause_id", "obligation_id"],
        ),
        (
            legacy_events.obligation_settlements,
            rust_events.obligation_settlements,
            ["rollout_index", "month_index", "cause_id", "obligation_id"],
        ),
    )
    for legacy_frame, rust_frame, sort_columns in comparisons:
        assert rust_frame.schema == legacy_frame.schema
        assert rust_frame.sort(sort_columns).to_dicts() == legacy_frame.sort(sort_columns).to_dicts()


def test_rust_and_jax_match_failure_freeze_semantics(tmp_path: Path) -> None:
    fixture = failure_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()

    assert rollout_status(legacy).to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 0},
        {"rollout_index": 1, "status": "failed_insufficient_cash", "failed_month": 0},
    ]
    assert [rollout["failed_month"] for rollout in rust["rollouts"]] == [0, 0]
    assert all(
        snapshot["failed"] and all(balance["balance"] == 0 for balance in snapshot["balances"])
        for rollout in rust["rollouts"]
        for snapshot in rollout["months"][1:]
    )
    rust_failures = decode_rust_event_log(rust).rollout_failures
    legacy_failures = legacy.events_log.rollout_failures
    sort_columns = ["rollout_index", "month_index", "cause_id", "obligation_id"]
    assert rust_failures.schema == legacy_failures.schema
    assert rust_failures.sort(sort_columns).to_dicts() == legacy_failures.sort(sort_columns).to_dicts()


def test_fixture_rejects_inexact_per_unit_lot_basis_in_both_engines(tmp_path: Path) -> None:
    fixture = shared_integer_fixture()
    lot = fixture["scenario"]["initial_lots"][0]
    lot["units"] = 3
    lot["basis"] = 1

    with pytest.raises(ValueError, match="does not encode an exact integer-quantum per-unit basis"):
        run_legacy_fixture(fixture)

    fixture_path = tmp_path / "inexact-lot-basis.json"
    output_path = tmp_path / "unused.json"
    fixture_path.write_text(json.dumps(fixture, separators=(",", ":")))
    completed = subprocess.run(
        [simulator_binary(), fixture_path, output_path], check=False, capture_output=True, text=True
    )
    assert completed.returncode == 1
    assert "total basis does not encode an exact per-unit basis" in completed.stderr


def test_fixture_rejects_noncanonical_target_allocation_quantity_scale(tmp_path: Path) -> None:
    fixture = target_allocation_purchase_fixture()
    fixture["scenario"]["target_allocation_policies"][0]["sleeves"][0]["quantity_scale"] = 1

    with pytest.raises(ValueError, match="canonical Python asset scale is 1000000"):
        run_legacy_fixture(fixture)

    fixture_path = tmp_path / "wrong-target-allocation-scale.json"
    output_path = tmp_path / "unused.json"
    fixture_path.write_text(json.dumps(fixture, separators=(",", ":")))
    completed = subprocess.run(
        [simulator_binary(), fixture_path, output_path], check=False, capture_output=True, text=True
    )
    assert completed.returncode == 1
    assert "mixes quantity scales 1000000 and 1" in completed.stderr


def test_rust_and_jax_match_grouped_recurring_obligations(tmp_path: Path) -> None:
    fixture = recurring_obligation_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()
    assert rollout_status(legacy).to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 1},
        {"rollout_index": 1, "status": "failed_insufficient_cash", "failed_month": 1},
    ]

    columns = [
        "rollout_index",
        "month_index",
        "obligation_id",
        "obligation_type",
        "agent_id",
        "from_account_id",
        "amount_due_quanta",
        "amount_paid_quanta",
        "shortfall_quanta",
    ]
    legacy_outcomes = legacy.events_log.obligation_settlements.select(columns).sort(columns[:3]).to_dicts()
    rust_outcomes = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": outcome["month"],
                "obligation_id": outcome["obligation_id"],
                "obligation_type": outcome["obligation_type"],
                "agent_id": outcome["from"]["agent_id"],
                "from_account_id": outcome["from"]["account_id"],
                "amount_due_quanta": outcome["amount_due"],
                "amount_paid_quanta": outcome["amount_paid"],
                "shortfall_quanta": outcome["shortfall"],
            }
            for rollout in rust["rollouts"]
            for outcome in rollout["obligations"]
        ],
        key=lambda row: (row["rollout_index"], row["month_index"], row["obligation_id"]),
    )
    assert rust_outcomes == legacy_outcomes


def test_rust_and_jax_match_fixed_and_series_indexed_amounts(tmp_path: Path) -> None:
    fixture = series_indexed_amount_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()

    transfer_causes = {
        "indexed-gift",
        "tagged-fixed-gift",
        "zero-gift",
        "annual-indexed-paycheck",
        "indexed-repair",
        "indexed-property-rent",
    }
    legacy_transfers = (
        legacy.events_log.transfers.filter(pl.col("cause_id").is_in(transfer_causes))
        .select("rollout_index", "month_index", "cause_id", "amount_quanta")
        .sort("rollout_index", "month_index", "cause_id")
        .to_dicts()
    )
    rust_transfers = (
        decode_rust_event_log(rust)
        .transfers.filter(pl.col("cause_id").is_in(transfer_causes))
        .select("rollout_index", "month_index", "cause_id", "amount_quanta")
        .sort("rollout_index", "month_index", "cause_id")
        .to_dicts()
    )
    assert rust_transfers == legacy_transfers

    columns = [
        "rollout_index",
        "month_index",
        "obligation_id",
        "amount_due_quanta",
        "amount_paid_quanta",
        "shortfall_quanta",
    ]
    legacy_outcomes = legacy.events_log.obligation_settlements.select(columns).sort(columns[:3]).to_dicts()
    rust_outcomes = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": outcome["month"],
                "obligation_id": outcome["obligation_id"],
                "amount_due_quanta": outcome["amount_due"],
                "amount_paid_quanta": outcome["amount_paid"],
                "shortfall_quanta": outcome["shortfall"],
            }
            for rollout in rust["rollouts"]
            for outcome in rollout["obligations"]
        ],
        key=lambda row: (row["rollout_index"], row["month_index"], row["obligation_id"]),
    )
    assert rust_outcomes == legacy_outcomes
    assert [row["amount_due_quanta"] for row in rust_outcomes if row["obligation_id"] == "indexed-bill_m2"] == [
        152,
        126,
    ]
    assert [row["amount_due_quanta"] for row in rust_outcomes if row["obligation_id"] == "indexed-rent_m12"] == [
        1_101,
        1_251,
    ]
    assert all(rollout["failed_month"] is None for rollout in rust["rollouts"])
    for rollout in rust["rollouts"]:
        for entry in rollout["journal"]:
            assert sum(posting["amount"] for posting in entry["postings"]) == 0


@pytest.mark.parametrize("rollout_count", [1, 17])
def test_fixture_contains_no_floating_point_numbers(rollout_count: int) -> None:
    fixture = shared_integer_fixture()
    fixture["rollout_count"] = rollout_count
    fixture["series"][0]["values"] = fixture["series"][0]["values"][:4] * rollout_count

    def walk(value: Any) -> None:
        assert not isinstance(value, float)
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(fixture)


if __name__ == "__main__":
    pytest_bazel.main()
