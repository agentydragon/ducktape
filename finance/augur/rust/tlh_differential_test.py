"""Rust/JAX differential coverage for stateful reduced-form tax-loss harvesting and its sale-time give-back.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest_bazel

from finance.augur.rust.output_adapter import decode_rust_event_log
from finance.augur.rust.testing.fixtures import assert_tlh_parity, tlh_fixture


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
