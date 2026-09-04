"""Rust/JAX differential coverage for the typed private-equity tender protocol.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest_bazel

from finance.augur.rust.output_adapter import decode_rust_event_log
from finance.augur.rust.testing.fixtures import (
    assert_private_equity_parity,
    private_equity_fixture,
    private_equity_tax_fixture,
    rust_cash_frame,
)


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
