"""Rust/JAX differential coverage for the typed private-equity tender protocol.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from typing import Any

import polars as pl
import pytest_bazel

from finance.augur.rust.differential.backend import assert_backends_agree
from finance.augur.rust.differential.fixtures import tax_fixture
from finance.augur.rust.fixture_spec import account_ref


def private_equity_fixture() -> dict[str, Any]:
    rollout_count = 4
    horizon = 3
    snapshots = horizon + 1

    def channel(default: int) -> list[int]:
        return [default] * (rollout_count * snapshots)

    def set_value(values: list[int], rollout: int, month: int, value: int) -> None:
        values[rollout * snapshots + month] = value

    mark = channel(10_000)
    regime = channel(1)
    event_kind = channel(0)
    opportunity = channel(0)
    capacity = channel(1_000_000_000)
    eligible = channel(1_000_000_000)
    forced_sale = channel(0)
    blocked = channel(0)
    recovery = channel(0)
    valuation = channel(0)

    set_value(event_kind, 0, 1, 1)
    set_value(opportunity, 0, 1, 1)
    set_value(capacity, 0, 1, 250_000_000)
    set_value(event_kind, 0, 2, 1)
    set_value(opportunity, 0, 2, 1)
    set_value(blocked, 0, 2, 1)

    set_value(regime, 1, 1, 2)
    set_value(event_kind, 1, 1, 3)

    set_value(event_kind, 2, 1, 5)
    set_value(forced_sale, 2, 1, 300_000_000)

    set_value(event_kind, 3, 1, 6)
    set_value(recovery, 3, 1, 10_000)

    series = []
    for name, values in {
        "mark": mark,
        "regime": regime,
        "event_kind": event_kind,
        "sale_opportunity": opportunity,
        "sale_capacity": capacity,
        "eligible": eligible,
        "forced_sale": forced_sale,
        "liquidity_blocked": blocked,
        "forced_recovery": recovery,
        "company_valuation": valuation,
    }.items():
        series.append({"series_id": f"private_equity_{name}:acme", "snapshots": snapshots, "values": values})
    return {
        "schema_version": 8,
        "currency_code": "USD",
        "currency_quantum": "0.01",
        "rollout_count": rollout_count,
        "scenario": {
            "horizon_months": horizon,
            "accounts": [{"account": account_ref("alice", "checking"), "opening_balance": 0}],
            "scheduled_transfers": [],
            "recurring_transfers": [],
            "obligations": [],
            "recurring_obligations": [],
            "initial_lots": [
                {
                    "lot_id": "acme_lot_a",
                    "agent_id": "alice",
                    "account_id": "checking",
                    "asset_id": "private_equity:acme",
                    "purchase_month": -36,
                    "quantity_scale": 1_000_000,
                    "units": 40_000_000,
                    "basis": 40_000,
                },
                {
                    "lot_id": "acme_lot_b",
                    "agent_id": "alice",
                    "account_id": "checking",
                    "asset_id": "private_equity:acme",
                    "purchase_month": -12,
                    "quantity_scale": 1_000_000,
                    "units": 60_000_000,
                    "basis": 120_000,
                },
            ],
            "initial_bonds": [],
            "scheduled_sales": [],
            "tax_profiles": [],
            "distributions": [],
            "target_allocation_policies": [],
            "private_equity_tender_policies": [
                {"owner_agent_id": "alice", "proceeds_account_id": "checking", "liquid_net_worth_floor": 500_000}
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
        "series": series,
    }


def private_equity_tax_fixture() -> dict[str, Any]:
    fixture = private_equity_fixture()
    fixture["rollout_count"] = 1
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 12
    scenario["accounts"].append({"account": account_ref("irs", "checking"), "opening_balance": 0})
    scenario["private_equity_tender_policies"][0]["liquid_net_worth_floor"] = 10_000_000
    scenario["tax_profiles"] = [
        {
            "agent_id": "alice",
            "tax_authority_agent_id": "irs",
            "jurisdictions": [tax_fixture()["scenario"]["tax_profiles"][0]["jurisdictions"][0]],
        }
    ]
    defaults = {
        "mark": 100_000,
        "regime": 1,
        "event_kind": 0,
        "sale_opportunity": 0,
        "sale_capacity": 1_000_000_000,
        "eligible": 1_000_000_000,
        "forced_sale": 0,
        "liquidity_blocked": 0,
        "forced_recovery": 0,
        "company_valuation": 0,
    }
    for series in fixture["series"]:
        channel = series["series_id"].removeprefix("private_equity_").partition(":")[0]
        series["snapshots"] = 13
        series["values"] = [defaults[channel]] * 13
        if channel in {"event_kind", "sale_opportunity"}:
            series["values"][1] = 1
    return fixture


def _final_cash(result) -> dict[int, int]:
    return {
        row["rollout_index"]: row["balance_quanta"] for row in result.cash.filter(pl.col("month_index") == 3).to_dicts()
    }


def test_backends_agree_on_tender_sales_and_opportunities() -> None:
    result = assert_backends_agree(private_equity_fixture())

    assert _final_cash(result) == {0: 250_000, 1: 500_000, 2: 300_000, 3: 10_000}
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_that_an_issuer_without_an_owner_policy_never_tenders() -> None:
    fixture = private_equity_fixture()
    fixture["scenario"]["private_equity_tender_policies"] = []
    result = assert_backends_agree(fixture)

    assert result.events.lot_dispositions.is_empty()
    assert set(result.events.private_equity_opportunities.get_column("outcome")) == {"no_policy"}
    assert _final_cash(result) == dict.fromkeys(range(4), 0)


def test_backends_agree_that_a_satisfied_floor_suppresses_voluntary_sales() -> None:
    fixture = private_equity_fixture()
    fixture["scenario"]["accounts"][0]["opening_balance"] = 600_000
    result = assert_backends_agree(fixture)

    causes = result.events.lot_dispositions.get_column("cause_id")
    assert not any(cause.startswith(("pe_tender_", "pe_public_market_")) for cause in causes)
    assert _final_cash(result) == {0: 600_000, 1: 600_000, 2: 900_000, 3: 610_000}


def test_backends_agree_on_the_tax_facts_a_tender_disposition_produces() -> None:
    result = assert_backends_agree(private_equity_tax_fixture())
    breakdown = result.events.tax_breakdowns.filter(pl.col("rollout_index") == 0)

    # A tender sale of a lot held past a year is long-term, and nothing else realizes.
    assert breakdown.get_column("stcg_quanta").to_list() == [0]
    assert breakdown.get_column("ltcg_quanta").to_list() == [9_840_000]
    # There is no ordinary income here, so the standard deduction goes unused against it and
    # shelters that much of the gain instead: taxable income is 9_840_000 - 1_460_000, of
    # which 4_702_500 sits in the 0% slice and the rest is rated at 15%.
    assert breakdown.get_column("capital_gain_tax_quanta").to_list() == [551_625]


if __name__ == "__main__":
    pytest_bazel.main()
