"""Rust/JAX differential coverage for stateful reduced-form tax-loss harvesting and its
sale-time give-back.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from typing import Any

import polars as pl
import pytest_bazel

from finance.augur.rust.differential.backend import RustResult, assert_backends_agree
from finance.augur.rust.differential.fixtures import tax_fixture


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


def _ledger(result: RustResult, rollout: int, month: int) -> int:
    """The single harvest policy's cumulative deferral at one snapshot."""

    row = result.tlh_ledger.filter(
        (pl.col("rollout_index") == rollout) & (pl.col("month_index") == month) & (pl.col("policy_index") == 0)
    )
    return int(row.get_column("cumulative_harvest_quanta").item())


def _assert_ledger_mirrors_short_term_gains(result: RustResult) -> None:
    """Before the first year-end, the deferral ledger is exactly the short-term loss booked.

    This is the invariant tying the two representations together: what the harvest policy
    accumulates is what the taxpayer's short-term gain reflects, with the sign flipped.
    """

    short_term = {
        (row["rollout_index"], row["month_index"]): row["gain_quanta"]
        for row in result.capital_gains.filter(pl.col("classification") == "stcg").to_dicts()
    }
    pre_year_end = result.tlh_ledger.filter(pl.col("month_index") < 12)
    assert not pre_year_end.is_empty()
    for row in pre_year_end.to_dicts():
        gain = short_term.get((row["rollout_index"], row["month_index"]), 0)
        assert row["cumulative_harvest_quanta"] == -gain


def test_backends_agree_on_harvest_paths_and_year_end_tax() -> None:
    result = assert_backends_agree(tlh_fixture())
    _assert_ledger_mirrors_short_term_gains(result)

    # The drawdown path harvests more than the flat one, and both harvest something.
    assert _ledger(result, 0, 3) > _ledger(result, 1, 3) > 0


def test_backends_agree_that_a_partial_sale_gives_basis_back() -> None:
    result = assert_backends_agree(tlh_fixture(partial_sales=True))
    _assert_ledger_mirrors_short_term_gains(result)

    assert _ledger(result, 0, 8) == 0


def test_backends_agree_that_same_month_sales_share_the_pre_sale_ledger() -> None:
    result = assert_backends_agree(tlh_fixture(same_month_sales=True))
    _assert_ledger_mirrors_short_term_gains(result)

    assert _ledger(result, 0, 8) == 0


def test_backends_agree_on_give_back_through_a_target_allocation_sale() -> None:
    result = assert_backends_agree(tlh_fixture(target_allocation_sale=True))

    dispositions = result.events.lot_dispositions
    assert dispositions.filter(pl.col("cause_id").str.starts_with("allocation_sale_m1_security:sp500")).height == 1
    assert _ledger(result, 0, 2) > 0


def test_backends_agree_that_failure_suppresses_the_harvest_ledger() -> None:
    result = assert_backends_agree(tlh_fixture(failure_after_first_harvest=True))

    assert result.rollout_status.get_column("failed_month").unique().to_list() == [1]
    for rollout in result.tlh_ledger.get_column("rollout_index").unique():
        assert _ledger(result, rollout, 1) > 0
        frozen = result.tlh_ledger.filter((pl.col("rollout_index") == rollout) & (pl.col("month_index") > 1))
        assert frozen.get_column("cumulative_harvest_quanta").unique().to_list() == [0]


if __name__ == "__main__":
    pytest_bazel.main()
