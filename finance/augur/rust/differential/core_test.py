"""Rust/JAX differential coverage for opening balances, transfers, amount schedules,
grouped obligations, failure freezing, and fixture validation.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from typing import Any

import polars as pl
import pytest
import pytest_bazel

from finance.augur.rust.differential.backend import BACKENDS, Backend, assert_backends_agree
from finance.augur.rust.differential.fixtures import (
    failure_fixture,
    shared_integer_fixture,
    target_allocation_purchase_fixture,
)
from finance.augur.rust.fixture_spec import account_ref


def recurring_obligation_fixture() -> dict[str, Any]:
    fixture = failure_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 3
    scenario["accounts"] = [
        {"account": account_ref("alice", "checking"), "opening_balance": 100_000},
        {"account": account_ref("landlord", "checking"), "opening_balance": 0},
        {"account": account_ref("utility", "checking"), "opening_balance": 0},
    ]
    scenario["scheduled_transfers"] = []
    scenario["obligations"] = []
    scenario["recurring_obligations"] = [
        {
            "start_month": 0,
            "end_month": 2,
            "obligation_id": "rent",
            "obligation_type": "cash_spend",
            "from": account_ref("alice", "checking"),
            "to": account_ref("landlord", "checking"),
            "amount_due": 60_000,
        },
        {
            "start_month": 1,
            "end_month": 2,
            "obligation_id": "utility",
            "obligation_type": "cash_spend",
            "from": account_ref("alice", "checking"),
            "to": account_ref("utility", "checking"),
            "amount_due": 1,
        },
    ]
    return fixture


def series_indexed_amount_fixture() -> dict[str, Any]:
    fixture = failure_fixture()
    fixture["rollout_count"] = 2
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 14
    scenario["accounts"] = [
        {"account": account_ref("alice", "checking"), "opening_balance": 20_000_000},
        {"account": account_ref("bob", "checking"), "opening_balance": 20_000_000},
        {"account": account_ref("seller", "checking"), "opening_balance": 0},
        {"account": account_ref("tenant", "checking"), "opening_balance": 20_000_000},
        {"account": account_ref("landlord", "checking"), "opening_balance": 0},
        {"account": account_ref("manager", "checking"), "opening_balance": 0},
    ]
    indexed_inflation = {
        "kind": "series_indexed",
        "base_amount": 101,
        "series_id": "inflation",
        "base_month_index": 0,
        "adjustment_period_months": 1,
    }
    indexed_annual_rent = {
        "kind": "series_indexed",
        "base_amount": 1_001,
        "series_id": "rent:test",
        "base_month_index": 0,
        "adjustment_period_months": 12,
    }
    scenario["scheduled_transfers"] = [
        {
            "month": 2,
            "cause_id": "indexed-gift",
            "from": account_ref("bob", "checking"),
            "to": account_ref("alice", "checking"),
            "amount": indexed_inflation,
        },
        {
            "month": 3,
            "cause_id": "tagged-fixed-gift",
            "from": account_ref("bob", "checking"),
            "to": account_ref("alice", "checking"),
            "amount": {"kind": "fixed", "amount": -17},
        },
        {
            "month": 4,
            "cause_id": "zero-gift",
            "from": account_ref("bob", "checking"),
            "to": account_ref("alice", "checking"),
            "amount": 0,
        },
    ]
    scenario["recurring_transfers"] = [
        {
            "start_month": 0,
            "end_month": 13,
            "cause_id": "annual-indexed-paycheck",
            "from": account_ref("bob", "checking"),
            "to": account_ref("alice", "checking"),
            "amount": indexed_annual_rent,
        }
    ]
    scenario["obligations"] = [
        {
            "month": 2,
            "obligation_id": "indexed-bill",
            "from": account_ref("alice", "checking"),
            "to": account_ref("landlord", "checking"),
            "amount_due": indexed_inflation,
        }
    ]
    scenario["recurring_obligations"] = [
        {
            "start_month": 0,
            "end_month": 13,
            "obligation_id": "indexed-rent",
            "obligation_type": "cash_spend",
            "from": account_ref("alice", "checking"),
            "to": account_ref("landlord", "checking"),
            "amount_due": indexed_annual_rent,
        }
    ]
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
            "month": 0,
            "cause_id": "buy-test-home",
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
    scenario["scheduled_property_cashflows"] = [
        {
            "month": 2,
            "property_id": "home",
            "cause_id": "indexed-repair",
            "from": account_ref("alice", "checking"),
            "to": account_ref("manager", "checking"),
            "amount": indexed_inflation,
        }
    ]
    scenario["recurring_property_cashflows"] = [
        {
            "start_month": 0,
            "end_month": 13,
            "property_id": "home",
            "cause_id": "indexed-property-rent",
            "from": account_ref("tenant", "checking"),
            "to": account_ref("alice", "checking"),
            "amount": indexed_annual_rent,
        }
    ]
    scenario["initial_lots"] = []
    scenario["scheduled_sales"] = []
    scenario["tax_profiles"] = []
    scenario["distributions"] = []
    scenario["property_tax_policies"] = []
    fixture["series"] = [
        {
            "series_id": "inflation",
            "snapshots": 15,
            "values": [
                1_000_000_000,
                1_250_000_000,
                1_500_000_000,
                *([1_500_000_000] * 12),
                1_000_000_000,
                1_500_000_000,
                1_250_000_000,
                *([1_250_000_000] * 12),
            ],
        },
        {
            "series_id": "rent:test",
            "snapshots": 15,
            "values": [
                *([1_000_000_000] * 12),
                1_100_000_000,
                1_100_000_000,
                1_100_000_000,
                *([1_000_000_000] * 12),
                1_250_000_000,
                1_250_000_000,
                1_250_000_000,
            ],
        },
    ]
    return fixture


def test_backends_agree_on_the_shared_fixture() -> None:
    """Opening balances, transfers, a FIFO sale, and the events each produces."""

    result = assert_backends_agree(shared_integer_fixture())

    # The sale consumes one of the two units the lot opened with.
    assert result.lots.filter(pl.col("month_index") == 3).get_column("remaining_quantity_quanta").to_list() == [
        1_000_000,
        1_000_000,
    ]
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_on_failure_freeze_semantics() -> None:
    """A rollout that cannot fund an obligation stops and reports zero value thereafter."""

    result = assert_backends_agree(failure_fixture())

    assert result.rollout_status.to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 0},
        {"rollout_index": 1, "status": "failed_insufficient_cash", "failed_month": 0},
    ]
    frozen = result.cash.filter(pl.col("month_index") > 0)
    assert frozen.get_column("balance_quanta").unique().to_list() == [0]


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_every_backend_rejects_an_inexact_per_unit_lot_basis(backend: Backend) -> None:
    fixture = shared_integer_fixture()
    lot = fixture["scenario"]["initial_lots"][0]
    lot["units"] = 3
    lot["basis"] = 1

    with pytest.raises(ValueError, match="does not encode an exact"):
        backend(fixture)


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_every_backend_rejects_a_noncanonical_target_allocation_quantity_scale(backend: Backend) -> None:
    fixture = target_allocation_purchase_fixture()
    fixture["scenario"]["target_allocation_policies"][0]["sleeves"][0]["quantity_scale"] = 1

    with pytest.raises(ValueError, match="1000000"):
        backend(fixture)


def test_backends_agree_on_grouped_recurring_obligations() -> None:
    """Obligations sharing a payer and source account settle all-or-none."""

    result = assert_backends_agree(recurring_obligation_fixture())

    assert result.rollout_status.to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 1},
        {"rollout_index": 1, "status": "failed_insufficient_cash", "failed_month": 1},
    ]


def test_backends_agree_on_fixed_and_series_indexed_amounts() -> None:
    """Scalar, tagged-fixed and index-scaled amounts, including periodic reset boundaries."""

    result = assert_backends_agree(series_indexed_amount_fixture())

    due = result.events.obligation_settlements.sort("rollout_index")
    assert due.filter(pl.col("obligation_id") == "indexed-bill_m2").get_column("amount_due_quanta").to_list() == [
        152,
        126,
    ]
    # A 12-month adjustment period holds the rent flat through month 11 and resets at 12.
    assert due.filter(pl.col("obligation_id") == "indexed-rent_m12").get_column("amount_due_quanta").to_list() == [
        1_101,
        1_251,
    ]
    assert result.rollout_status.get_column("status").unique().to_list() == ["active"]
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


@pytest.mark.parametrize("rollout_count", [1, 17])
def test_fixture_contains_no_floating_point_numbers(rollout_count: int) -> None:
    """The fixture is the engines' shared input, so a float in it is a lost integer."""

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
