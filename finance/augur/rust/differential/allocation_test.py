"""Rust/JAX differential coverage for target-allocation liquidity sales, post-settlement
purchases, and quiet-band drift rebalancing.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from typing import Any

import polars as pl
import pytest
import pytest_bazel

from finance.augur.rust.differential.backend import BACKENDS, Backend, assert_backends_agree, run_jax, run_rust
from finance.augur.rust.differential.fixtures import target_allocation_fixture, target_allocation_purchase_fixture


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


def _monthly_income_scenario(purchase_slots: int, *, rising_bond_price: bool) -> dict[str, Any]:
    """Three months of income into a policy with a tight ceiling, so it buys every month."""

    fixture = target_allocation_purchase_fixture(purchase_slots=purchase_slots)
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
        rising = series["series_id"] != "security:vti" and rising_bond_price
        series["snapshots"] = 4
        series["values"] = [10_000, 20_000, 30_000, 30_000] if rising else [10_000] * 4
    return fixture


def _cash(result, agent_id: str, month: int) -> int:
    row = result.cash.filter(
        (pl.col("agent_id") == agent_id) & (pl.col("account_id") == "checking") & (pl.col("month_index") == month)
    )
    return int(row.get_column("balance_quanta").item())


def test_backends_agree_on_liquidity_sales_before_obligation_funding() -> None:
    """The band raises cash before the grouped funding check, in source-account order."""

    result = assert_backends_agree(target_allocation_fixture())
    dispositions = result.events.lot_dispositions.sort("source_account_id")

    # Source-account order decides which lots are reached, not lot id.
    assert dispositions.get_column("source_account_id").to_list() == ["brokerage-a", "brokerage-b"]
    assert dispositions.get_column("lot_id").to_list() == ["z-source-first", "a-source-second"]
    assert dispositions.get_column("units_sold").to_list() == [100.0, 130.0]
    assert result.events.obligation_settlements.get_column("attempted_funding_sources").unique().to_list() == [
        "security:vti,security:bnd"
    ]
    assert result.rollout_status.get_column("status").to_list() == ["active"]
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_on_post_settlement_purchases() -> None:
    """Buys are decided pre-settlement and execute after, clamped to then-current cash."""

    result = assert_backends_agree(target_allocation_purchase_fixture())
    bought = result.lots.filter(
        (pl.col("month_index") == 1) & pl.col("lot_id").str.starts_with("allocation_sale_buy")
    ).sort("lot_id")

    assert bought.get_column("asset_id").to_list() == ["security:vti", "security:bnd"]
    assert bought.get_column("remaining_quantity_quanta").to_list() == [50_000_000, 850_000_000]
    assert bought.get_column("cost_basis_per_unit_quanta").unique().to_list() == [10_000]
    assert bought.get_column("account_id").unique().to_list() == ["brokerage-a"]
    assert _cash(result, "alice", 1) == 1_000_000


def test_backends_agree_on_quiet_band_drift_rebalancing() -> None:
    """Rebalancing is all-or-nothing and returns every sleeve to its floored target."""

    result = assert_backends_agree(target_allocation_rebalance_fixture())
    remaining = {
        row["lot_id"]: row["remaining_quantity_quanta"]
        for row in result.lots.filter(pl.col("month_index") == 1).to_dicts()
    }

    assert remaining == {
        "a-source-second": 500_000_000,
        "allocation_sale_buy_p0_s0_0": 0,
        "allocation_sale_buy_p0_s1_0": 400_000_000,
        "bond": 100_000_000,
        "z-source-first": 0,
    }
    assert _cash(result, "alice", 1) == 5_000_000
    sold = result.events.lot_dispositions.sort("lot_id")
    assert sold.get_column("lot_id").to_list() == ["a-source-second", "z-source-first"]
    assert sold.get_column("units_sold").to_list() == [300.0, 100.0]


def test_backends_agree_that_purchased_lots_join_the_pool_after_real_lots() -> None:
    """A bought lot sells only once the real lots ahead of it in FIFO rank are exhausted."""

    result = assert_backends_agree(target_allocation_purchase_then_sale_fixture())
    sold = result.events.lot_dispositions.filter((pl.col("month_index") == 1) & (pl.col("asset_id") == "security:vti"))

    assert {row["lot_id"]: row["units_sold"] for row in sold.to_dicts()} == {
        "allocation_sale_buy_p0_s0_0": 25.0,
        "z-source-first": 100.0,
        "zz-real-same-month": 800.0,
    }


def test_backends_agree_that_a_purchase_slot_pool_receives_later_distributions() -> None:
    result = assert_backends_agree(target_allocation_purchase_distribution_fixture())
    slot = result.lots.filter(
        (pl.col("month_index") == 1) & (pl.col("lot_id") == "allocation_sale_buy_p0_s0_0")
    ).to_dicts()[0]

    assert slot["account_id"] == "brokerage-a"
    assert slot["remaining_quantity_quanta"] == 50_000_000
    # Nothing is held at the first distribution; the bought units earn the second.
    assert result.distributions.sort("month_index").get_column("amount_quanta").to_list() == [0, 5_000]


def test_backends_agree_that_a_failed_settlement_suppresses_decided_purchases() -> None:
    """Buys decided before settlement must not execute once the settlement fails."""

    fixture = target_allocation_purchase_fixture()
    fixture["scenario"]["obligations"] = [
        {
            "month": 0,
            "obligation_id": "unfundable",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "landlord", "account_id": "checking"},
            "amount_due": 10_000_000_000,
        }
    ]
    result = assert_backends_agree(fixture)

    assert result.rollout_status.get_column("failed_month").to_list() == [0]
    assert not any(cause.startswith("allocation_sale_buy_") for cause in result.journal.get_column("cause_id"))
    frozen = result.lots.filter(pl.col("month_index") == 1)
    assert frozen.get_column("remaining_quantity_quanta").unique().to_list() == [0]


def test_backends_agree_that_successive_purchases_keep_distinct_months_and_prices() -> None:
    """Each month's buy fills its own slot at the price that month's rollout observed."""

    result = assert_backends_agree(_monthly_income_scenario(2, rising_bond_price=True))
    slots = {
        row["lot_id"]: row
        for row in result.lots.filter((pl.col("month_index") == 3) & pl.col("lot_id").str.contains("_s1_")).to_dicts()
    }

    first, second = slots["allocation_sale_buy_p0_s1_0"], slots["allocation_sale_buy_p0_s1_1"]
    assert (first["purchase_month_index"], first["cost_basis_per_unit_quanta"]) == (1, 20_000)
    assert (second["purchase_month_index"], second["cost_basis_per_unit_quanta"]) == (2, 30_000)


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_every_backend_aborts_when_purchase_slots_are_exhausted(backend: Backend) -> None:
    """A dropped purchase would be a silent wrong answer, so both engines refuse instead."""

    with pytest.raises(ValueError, match="ran out of purchase slots: 1 configured, 2 needed"):
        backend(_monthly_income_scenario(1, rising_bond_price=False))


# Both engines refuse a rebalancing policy with nowhere to buy back into, but each says so
# in its own words, so the pattern is per backend rather than one shared substring.
REBALANCE_REJECTIONS = ((run_jax, "no purchase slots"), (run_rust, "invalid configuration"))


@pytest.mark.parametrize(("backend", "message"), REBALANCE_REJECTIONS, ids=lambda item: getattr(item, "__name__", ""))
def test_every_backend_rejects_rebalancing_without_purchase_slots(backend: Backend, message: str) -> None:
    fixture = target_allocation_fixture()
    fixture["scenario"]["target_allocation_policies"][0]["rebalance_tolerance_ppb"] = 250_000_000

    with pytest.raises(ValueError, match=message):
        backend(fixture)


def test_backends_agree_on_insufficient_funding_failure_metadata() -> None:
    result = assert_backends_agree(target_allocation_failure_fixture())

    assert result.events.rollout_failures.to_dicts() == [
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
    assert result.rollout_status.get_column("failed_month").to_list() == [1]
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


if __name__ == "__main__":
    pytest_bazel.main()
