"""Rust/JAX differential coverage for the generated feature-rich scenario, compared frame by frame across every canonical event channel.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest_bazel
from polars.testing import assert_frame_equal

from finance.augur.rust.benchmark_fixture import write_fixture
from finance.augur.rust.fixture_adapter import run_legacy_fixture
from finance.augur.rust.output_adapter import decode_rust_event_log
from finance.augur.rust.testing.fixtures import rust_cash_frame, rust_run
from finance.augur.sim.events import EVENT_FRAME_SPECS
from finance.augur.sim.testing.state_helpers import cash_balances, rollout_status


def test_generated_feature_rich_benchmark_fixture_matches_at_four_rollouts(tmp_path: Path) -> None:
    fixture_path = tmp_path / "benchmark-fixture.json"
    write_fixture(fixture_path, rollout_count=4, horizon_months=60)
    fixture = cast(dict[str, Any], json.loads(fixture_path.read_text()))
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .filter(pl.col("account_id") == "checking")
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

    rust_events = decode_rust_event_log(rust)
    for spec in EVENT_FRAME_SPECS:
        rust_frame = rust_events.frame(spec)
        legacy_frame = legacy.events_log.frame(spec)
        try:
            assert_frame_equal(rust_frame, legacy_frame, check_row_order=False)
        except AssertionError as error:
            columns = rust_frame.columns
            rust_only = rust_frame.join(legacy_frame, on=columns, how="anti")
            legacy_only = legacy_frame.join(rust_frame, on=columns, how="anti")
            raise AssertionError(
                f"canonical event frame {spec.name!r} differs:\n"
                f"Rust-only rows:\n{rust_only}\nJAX-only rows:\n{legacy_only}"
            ) from error

    assert rollout_status(legacy).get_column("status").unique().to_list() == ["active"]
    assert all(rollout["failed_month"] is None for rollout in rust["rollouts"])
    assert all(
        sum(len(rollout[field]) for rollout in rust["rollouts"]) > 0
        for field in (
            "dispositions",
            "private_equity_events",
            "private_equity_opportunities",
            "obligations",
            "tax_accruals",
            "tax_payments",
            "tax_settlements",
            "bond_cashflows",
            "distributions",
            "property_purchases",
            "primary_residence_events",
            "property_rented_fraction_events",
            "capital_improvements",
            "property_sales",
            "mortgage_originations",
            "mortgage_payments",
        )
    )
    disposition_causes = set(rust_events.lot_dispositions.get_column("cause_id"))
    assert {"tlh-half-sale", "tlh-final-sale"} <= disposition_causes
    assert any(cause.startswith("benchmark-allocation_") for cause in disposition_causes)
    assert any(cause.startswith(("pe_forced_sale_", "pe_forced_recovery_")) for cause in disposition_causes)
    assert any(cause.startswith(("pe_tender_", "pe_public_market_")) for cause in disposition_causes)
    assert any(
        lot["lot_id"].startswith("benchmark-allocation_buy_")
        and lot["purchase_month"] >= 0
        and lot["units_remaining"] > 0
        for rollout in rust["rollouts"]
        for snapshot in rollout["months"]
        for lot in snapshot["lots"]
    )


if __name__ == "__main__":
    pytest_bazel.main()
