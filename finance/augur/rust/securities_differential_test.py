"""Rust/JAX differential coverage for security distributions and held-to-maturity nominal bonds and TIPS.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pytest_bazel

from finance.augur.rust.fixture_adapter import run_legacy_fixture
from finance.augur.rust.testing.fixtures import (
    bond_fixture,
    distribution_fixture,
    distribution_tax_fixture,
    rust_cash_frame,
    rust_run,
)
from finance.augur.sim.engine.jax_engine import run_jax_product_metric_arrays
from finance.augur.sim.testing.state_helpers import cash_balances, ordinary_income_ytd


def test_rust_and_jax_match_monthly_security_distributions(tmp_path: Path) -> None:
    fixture = distribution_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()
    assert [[outcome["amount"] for outcome in rollout["distributions"]] for rollout in rust["rollouts"]] == [
        [200, 200, 200],
        [400, 600, 800],
    ]


def test_rust_and_jax_match_distribution_tax_character_slices(tmp_path: Path) -> None:
    fixture = distribution_tax_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    assert rust_cash_frame(rust).to_dicts() == (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
        .to_dicts()
    )

    legacy_tax = (
        legacy.events_log.tax_breakdowns.select(
            "rollout_index",
            "month_index",
            "agent_id",
            "jurisdiction_id",
            "ordinary_income_quanta",
            "ordinary_taxable_quanta",
            "ordinary_tax_quanta",
            "total_tax_quanta",
        )
        .sort("rollout_index", "jurisdiction_id")
        .to_dicts()
    )
    rust_tax = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": accrual["month"],
                "agent_id": accrual["agent_id"],
                "jurisdiction_id": accrual["jurisdiction_id"],
                "ordinary_income_quanta": accrual["ordinary_income"],
                "ordinary_taxable_quanta": accrual["ordinary_taxable"],
                "ordinary_tax_quanta": accrual["ordinary_tax"],
                "total_tax_quanta": accrual["total_tax"],
            }
            for rollout in rust["rollouts"]
            for accrual in rollout["tax_accruals"]
        ],
        key=lambda row: (row["rollout_index"], row["jurisdiction_id"]),
    )
    assert rust_tax == legacy_tax

    legacy_sources = (
        ordinary_income_ytd(legacy)
        .filter((pl.col("month_index") == 11) & (pl.col("ordinary_income_quanta") != 0))
        .select("rollout_index", "income_source", "ordinary_income_quanta")
        .sort("rollout_index", "income_source")
        .to_dicts()
    )
    rust_sources: list[dict[str, Any]] = []
    for rollout in rust["rollouts"]:
        by_source: dict[str, int] = {}
        for outcome in rollout["distributions"]:
            if outcome["month"] >= 11:
                continue
            issuer = outcome["issuer_jurisdiction_id"]
            source = f"interest:{issuer}" if issuer is not None else "interest:corporate"
            by_source[source] = by_source.get(source, 0) + outcome["amount"]
        rust_sources.extend(
            {"rollout_index": rollout["rollout_id"], "income_source": source, "ordinary_income_quanta": amount}
            for source, amount in sorted(by_source.items())
        )
    assert rust_sources == legacy_sources
    assert [outcome["amount"] for outcome in rust["rollouts"][0]["distributions"][:2]] == [80_000, 120_000]
    assert [outcome["amount"] for outcome in rust["rollouts"][1]["distributions"][:2]] == [120_000, 180_000]
    assert all(
        sum(posting["amount"] for posting in entry["postings"]) == 0
        for rollout in rust["rollouts"]
        for entry in rollout["journal"]
    )


def test_rust_and_jax_match_nominal_bonds_tips_and_issuer_tax_routing(tmp_path: Path) -> None:
    fixture = bond_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()

    legacy_bond_value = run_jax_product_metric_arrays(legacy.plan, primary_agent_id="alice").metric_arrays()[
        "bond_value_quanta"
    ]
    rust_bond_value = [
        [
            sum(bond["principal"] for bond in rollout["months"][month]["bonds"] if bond["agent_id"] == "alice")
            for rollout in rust["rollouts"]
        ]
        for month in range(len(rust["rollouts"][0]["months"]))
    ]
    assert rust_bond_value == legacy_bond_value.tolist()

    columns = [
        "rollout_index",
        "month_index",
        "agent_id",
        "jurisdiction_id",
        "ordinary_income_quanta",
        "ordinary_taxable_quanta",
        "ordinary_tax_quanta",
        "total_tax_quanta",
    ]
    legacy_tax = legacy.events_log.tax_breakdowns.select(columns).sort("rollout_index", "jurisdiction_id").to_dicts()
    rust_tax = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": accrual["month"],
                "agent_id": accrual["agent_id"],
                "jurisdiction_id": accrual["jurisdiction_id"],
                "ordinary_income_quanta": accrual["ordinary_income"],
                "ordinary_taxable_quanta": accrual["ordinary_taxable"],
                "ordinary_tax_quanta": accrual["ordinary_tax"],
                "total_tax_quanta": accrual["total_tax"],
            }
            for rollout in rust["rollouts"]
            for accrual in rollout["tax_accruals"]
        ],
        key=lambda row: (row["rollout_index"], row["jurisdiction_id"]),
    )
    assert rust_tax == legacy_tax

    first = {(flow["bond_id"], flow["month"]): flow for flow in rust["rollouts"][0]["bond_cashflows"]}
    assert first[("treasury", 5)]["coupon"] == 250_000
    assert first[("treasury", 5)]["issuer_jurisdiction_id"] == "federal_us"
    assert first[("california-muni", 5)]["coupon"] == 200_000
    assert first[("corporate", 5)]["coupon"] == 150_000
    assert first[("corporate", 5)]["issuer_jurisdiction_id"] is None
    assert first[("tips", 6)]["accretion"] == 10_000_000
    assert first[("tips", 6)]["issuer_jurisdiction_id"] == "federal_us"
    assert first[("tips", 11)]["coupon"] == 400_000
    assert first[("tips", 11)]["redemption"] == 20_000_000
    assert [first[("rounding-up", month)]["coupon"] for month in range(12)] == [1] * 12
    assert first[("rounding-down", 11)]["coupon"] == 0
    assert [first[("rounding-five-month", month)]["coupon"] for month in (1, 6, 11)] == [19_280] * 3
    second = {(flow["bond_id"], flow["month"]): flow for flow in rust["rollouts"][1]["bond_cashflows"]}
    assert second[("tips", 6)]["accretion"] == 5_000_000
    assert second[("tips", 11)]["redemption"] == 15_000_000
    third = {(flow["bond_id"], flow["month"]): flow for flow in rust["rollouts"][2]["bond_cashflows"]}
    assert third[("tips", 6)]["accretion"] == -2_000_000
    assert third[("tips", 11)]["coupon"] == 160_000
    assert third[("tips", 11)]["redemption"] == 10_000_000
    assert all(
        sum(posting["amount"] for posting in entry["postings"]) == 0
        for rollout in rust["rollouts"]
        for entry in rollout["journal"]
    )


if __name__ == "__main__":
    pytest_bazel.main()
