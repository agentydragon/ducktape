"""Shared fixtures and readers for the Rust/JAX differential suites.

Every fixture here is an exact-integer document both engines consume unchanged; the
conversion to the legacy float surface happens inside `fixture_adapter`, not here.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import polars as pl

from finance.augur.rust.benchmark_fixture import write_fixture
from finance.augur.rust.differential.fixture_adapter import build_legacy_fixture
from finance.augur.sim.compiler.plan import CompiledSimulation, compile_simulation
from finance.augur.sim.runtime import load_jurisdictions_for


def simulator_binary() -> Path:
    runfiles = Path(os.environ["TEST_SRCDIR"])
    workspace = os.environ["TEST_WORKSPACE"]
    return runfiles / workspace / "finance/augur/rust/simulator_cli"


def shared_integer_fixture() -> dict[str, Any]:
    # Prices are cents per whole unit and quantities are millionths of a unit.
    # The two rollouts deliberately use different paths before the scheduled
    # sale, while sharing the sale-month value supported by the legacy fixed
    # sale-price surface.
    return {
        "schema_version": 8,
        "currency_code": "USD",
        "currency_quantum": "0.01",
        "rollout_count": 2,
        "scenario": {
            "horizon_months": 3,
            "accounts": [
                {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 1_000},
                {"account": {"agent_id": "bob", "account_id": "checking"}, "opening_balance": 2_000},
            ],
            "scheduled_transfers": [
                {
                    "month": 0,
                    "cause_id": "bob_gives_alice_5",
                    "from": {"agent_id": "bob", "account_id": "checking"},
                    "to": {"agent_id": "alice", "account_id": "checking"},
                    "amount": 500,
                }
            ],
            "recurring_transfers": [
                {
                    "start_month": 1,
                    "end_month": 2,
                    "cause_id": "paycheck",
                    "from": {"agent_id": "bob", "account_id": "checking"},
                    "to": {"agent_id": "alice", "account_id": "checking"},
                    "amount": 100,
                    "income_category": "ordinary",
                }
            ],
            "obligations": [
                {
                    "month": 2,
                    "obligation_id": "required-payment",
                    "from": {"agent_id": "alice", "account_id": "checking"},
                    "to": {"agent_id": "bob", "account_id": "checking"},
                    "amount_due": 50,
                }
            ],
            "initial_lots": [
                {
                    "lot_id": "alice-vti",
                    "agent_id": "alice",
                    "account_id": "checking",
                    "asset_id": "vti",
                    "purchase_month": -12,
                    "quantity_scale": 1_000_000,
                    "units": 2_000_000,
                    "basis": 20_000,
                }
            ],
            "scheduled_sales": [
                {
                    "month": 1,
                    "cause_id": "sell-vti",
                    "agent_id": "alice",
                    "account_id": "checking",
                    "asset_id": "vti",
                    "units": 1_000_000,
                    "proceeds_account_id": "checking",
                }
            ],
        },
        "series": [
            {
                "series_id": "security:vti",
                "snapshots": 4,
                "values": [10_000, 15_000, 15_000, 15_000, 20_000, 15_000, 15_000, 15_000],
            }
        ],
    }


def failure_fixture() -> dict[str, Any]:
    fixture = shared_integer_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 2
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 100},
        {"account": {"agent_id": "bob", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["scheduled_transfers"] = [
        {
            "month": 1,
            "cause_id": "must-not-run",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "bob", "account_id": "checking"},
            "amount": 1,
        }
    ]
    scenario["recurring_transfers"] = []
    scenario["obligations"] = [
        {
            "month": 0,
            "obligation_id": "too-large",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "bob", "account_id": "checking"},
            "amount_due": 101,
        }
    ]
    scenario["initial_lots"] = []
    scenario["scheduled_sales"] = []
    fixture["series"] = []
    return fixture


def tax_fixture() -> dict[str, Any]:
    fixture = failure_fixture()
    fixture["rollout_count"] = 1
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 12
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "payroll", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["scheduled_transfers"] = []
    scenario["recurring_transfers"] = [
        {
            "start_month": 0,
            "end_month": 11,
            "cause_id": "alice-paycheck",
            "from": {"agent_id": "payroll", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount": 1_666_667,
            "income_category": "ordinary",
        }
    ]
    scenario["obligations"] = []
    scenario["recurring_obligations"] = []
    scenario["tax_profiles"] = [
        {
            "agent_id": "alice",
            "tax_authority_agent_id": "irs",
            "jurisdictions": [
                {
                    "jurisdiction_id": "federal_us",
                    "ordinary_brackets": [
                        {"upper": 1_160_000, "rate_ppb": 100_000_000},
                        {"upper": 4_715_000, "rate_ppb": 120_000_000},
                        {"upper": 10_052_500, "rate_ppb": 220_000_000},
                        {"upper": 19_195_000, "rate_ppb": 240_000_000},
                        {"upper": None, "rate_ppb": 320_000_000},
                    ],
                    "long_term_capital_gain_brackets": [
                        {"upper": 4_702_500, "rate_ppb": 0},
                        {"upper": None, "rate_ppb": 150_000_000},
                    ],
                    "standard_deduction": 1_460_000,
                    "max_capital_loss_ordinary_offset": 300_000,
                },
                {
                    "jurisdiction_id": "california",
                    "ordinary_brackets": [
                        {"upper": 1_041_200, "rate_ppb": 10_000_000},
                        {"upper": 2_468_400, "rate_ppb": 20_000_000},
                        {"upper": 3_895_900, "rate_ppb": 40_000_000},
                        {"upper": 5_408_100, "rate_ppb": 60_000_000},
                        {"upper": 6_835_000, "rate_ppb": 80_000_000},
                        {"upper": None, "rate_ppb": 93_000_000},
                    ],
                    "long_term_capital_gain_brackets": [],
                    "standard_deduction": 536_300,
                    "max_capital_loss_ordinary_offset": 300_000,
                },
            ],
        }
    ]
    return fixture


def target_allocation_fixture() -> dict[str, Any]:
    tax_profile = tax_fixture()["scenario"]["tax_profiles"][0]
    return {
        "schema_version": 8,
        "currency_code": "USD",
        "currency_quantum": "0.01",
        "rollout_count": 1,
        "scenario": {
            "horizon_months": 12,
            "accounts": [
                {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 1_200_000},
                {"account": {"agent_id": "landlord", "account_id": "checking"}, "opening_balance": 0},
                {"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0},
            ],
            "scheduled_transfers": [],
            "recurring_transfers": [],
            "obligations": [],
            "recurring_obligations": [
                {
                    "start_month": 1,
                    "end_month": 3,
                    "obligation_id": "rent",
                    "obligation_type": "rent",
                    "from": {"agent_id": "alice", "account_id": "checking"},
                    "to": {"agent_id": "landlord", "account_id": "checking"},
                    "amount_due": 500_000,
                }
            ],
            "initial_lots": [
                {
                    "lot_id": "a-source-second",
                    "agent_id": "alice",
                    "account_id": "brokerage-b",
                    "asset_id": "vti",
                    "purchase_month": 0,
                    "quantity_scale": 1_000_000,
                    "units": 800_000_000,
                    "basis": 6_400_000,
                },
                {
                    "lot_id": "z-source-first",
                    "agent_id": "alice",
                    "account_id": "brokerage-a",
                    "asset_id": "vti",
                    "purchase_month": -24,
                    "quantity_scale": 1_000_000,
                    "units": 100_000_000,
                    "basis": 500_000,
                },
                {
                    "lot_id": "bond",
                    "agent_id": "alice",
                    "account_id": "brokerage-b",
                    "asset_id": "bnd",
                    "purchase_month": -24,
                    "quantity_scale": 1_000_000,
                    "units": 100_000_000,
                    "basis": 1_000_000,
                },
            ],
            "scheduled_sales": [],
            "tax_profiles": [tax_profile],
            "distributions": [],
            "target_allocation_policies": [
                {
                    "agent_id": "alice",
                    "account_id": "checking",
                    "source_account_ids": ["brokerage-a", "brokerage-b"],
                    "sleeves": [
                        {"asset_id": "vti", "weight": 1, "quantity_scale": 1_000_000},
                        {"asset_id": "bnd", "weight": 1, "quantity_scale": 1_000_000},
                    ],
                    "cash_floor": 1_000_000,
                    "cash_ceiling": 3_000_000,
                }
            ],
        },
        "series": [
            {"series_id": "security:vti", "snapshots": 13, "values": [10_000] * 13},
            {"series_id": "security:bnd", "snapshots": 13, "values": [10_000] * 13},
        ],
    }


def target_allocation_purchase_fixture(*, purchase_slots: int = 1) -> dict[str, Any]:
    fixture = target_allocation_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 2
    scenario["accounts"][0]["opening_balance"] = 10_000_000
    scenario["recurring_obligations"] = []
    policy = scenario["target_allocation_policies"][0]
    policy["cash_floor"] = 1_000_000
    policy["cash_ceiling"] = 2_000_000
    policy["purchase_slots_per_sleeve"] = purchase_slots
    for series in fixture["series"]:
        series["snapshots"] = 3
        series["values"] = series["values"][:3]
    return fixture


def financed_property_fixture() -> dict[str, Any]:
    fixture = failure_fixture()
    fixture["rollout_count"] = 1
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 2
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 12_000_000},
        {"account": {"agent_id": "seller", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "bank", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "county", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["scheduled_transfers"] = []
    scenario["recurring_transfers"] = []
    scenario["obligations"] = []
    scenario["recurring_obligations"] = []
    scenario["initial_lots"] = []
    scenario["scheduled_sales"] = []
    scenario["locations"] = [
        {
            "location_id": "sf",
            "display_name": "San Francisco",
            "jurisdiction_ids": [],
            "annual_property_tax_rate_ppb": 11_800_000,
            "annual_special_assessment": 0,
        }
    ]
    scenario["scheduled_property_purchases"] = [
        {
            "month": 0,
            "cause_id": "alice-buys-home",
            "property_id": "home",
            "location_id": "sf",
            "buyer_agent_id": "alice",
            "buyer_account_id": "checking",
            "seller_agent_id": "seller",
            "seller_account_id": "checking",
            "purchase_price": 50_000_000,
            "down_payment": 10_000_000,
            "buyer_closing_cost": 1_000_000,
            "mortgage": {
                "liability_id": "home-mortgage",
                "lender_agent_id": "bank",
                "lender_account_id": "checking",
                "principal": 40_000_000,
                "annual_interest_rate_ppb": 60_000_000,
                "term_months": 360,
            },
        }
    ]
    scenario["property_tax_policies"] = [
        {
            "property_id": "home",
            "owner_agent_id": "alice",
            "from_account_id": "checking",
            "tax_authority_agent_id": "county",
            "tax_authority_account_id": "checking",
            "annual_tax_rate_ppb": 12_000_000,
            "start_month": 0,
            "end_month": None,
        }
    ]
    fixture["series"] = []
    return fixture


def property_cashflow_fixture() -> dict[str, Any]:
    fixture = financed_property_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 12
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 30_000_000},
        {"account": {"agent_id": "seller", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "bank", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "county", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "tenant", "account_id": "checking"}, "opening_balance": 6_000_000},
        {"account": {"agent_id": "manager", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["scheduled_property_cashflows"] = [
        {
            "month": 0,
            "property_id": "home",
            "cause_id": "leasing-fee",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "manager", "account_id": "checking"},
            "amount": 100_000,
            "deduction_category": "ordinary",
        }
    ]
    scenario["recurring_property_cashflows"] = [
        {
            "start_month": 0,
            "end_month": 11,
            "property_id": "home",
            "cause_id": "rent",
            "from": {"agent_id": "tenant", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount": 500_000,
            "income_category": "ordinary",
        },
        {
            "start_month": 0,
            "end_month": 11,
            "property_id": "home",
            "cause_id": "management-fee",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "manager", "account_id": "checking"},
            "amount": 50_000,
            "deduction_category": "ordinary",
        },
    ]
    federal_profile = tax_fixture()["scenario"]["tax_profiles"][0]
    federal_profile["jurisdictions"] = federal_profile["jurisdictions"][:1]
    scenario["tax_profiles"] = [federal_profile]
    return fixture


def property_depreciation_fixture(*, sale: bool) -> dict[str, Any]:
    fixture = property_cashflow_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 24 if sale else 12
    purchase = scenario["scheduled_property_purchases"][0]
    purchase["rented_fraction_ppb"] = 0
    purchase["land_value_fraction_ppb"] = 200_000_000
    purchase["mortgage"]["annual_interest_rate_ppb"] = 120_000_000
    scenario["property_tax_policies"] = []
    scenario["property_rented_fraction_events"] = [
        {"month": 6, "property_id": "home", "rented_fraction_ppb": 500_000_000}
    ]
    scenario["capital_improvement_events"] = [
        {"month": 6, "property_id": "home", "amount": 1_000_000, "description": "new roof"}
    ]
    scenario["mortgage_interest_deduction_policies"] = [{"liability_id": "home-mortgage", "owner_agent_id": "alice"}]
    if sale:
        scenario["recurring_property_cashflows"][0]["end_month"] = 23
        scenario["recurring_property_cashflows"][1]["end_month"] = 23
        scenario["property_sales"] = [{"month": 12, "property_id": "home", "closing_cost_bps": 600}]
        tax_profile = tax_fixture()["scenario"]["tax_profiles"][0]
        tax_profile["jurisdictions"][0]["section_1250_rate_ppb"] = 250_000_000
        tax_profile["jurisdictions"][1]["section_1250_rate_ppb"] = 0
        scenario["tax_profiles"] = [tax_profile]
        fixture["series"] = [
            {"series_id": "home_value:sf", "snapshots": 25, "values": [50_000_000] * 12 + [75_000_000] * 13}
        ]
    return fixture


def rust_run(fixture: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    fixture_path = tmp_path / "fixture.json"
    output_path = tmp_path / "rust-output.json"
    fixture_path.write_text(json.dumps(fixture, separators=(",", ":")))
    subprocess.run([simulator_binary(), fixture_path, output_path], check=True)
    return cast(dict[str, Any], json.loads(output_path.read_text()))


def rust_cash_frame(rust: dict[str, Any]) -> pl.DataFrame:
    rows = []
    for rollout in rust["rollouts"]:
        for snapshot in rollout["months"]:
            for balance in snapshot["balances"]:
                account = balance["account"]
                if account["account_id"] == "checking":
                    rows.append(
                        {
                            "rollout_index": rollout["rollout_id"],
                            "month_index": snapshot["month"],
                            "agent_id": account["agent_id"],
                            "account_id": account["account_id"],
                            "balance_quanta": balance["balance"],
                        }
                    )
    return pl.DataFrame(rows).sort("rollout_index", "month_index", "agent_id", "account_id")


def rust_lot_frame(rust: dict[str, Any]) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for rollout in rust["rollouts"]:
        for snapshot in rollout["months"]:
            rows.extend(
                {
                    "rollout_index": rollout["rollout_id"],
                    "month_index": snapshot["month"],
                    "lot_id": lot["lot_id"],
                    "agent_id": lot["agent_id"],
                    "account_id": lot["account_id"],
                    "asset_id": lot["asset_id"],
                    "purchase_month_index": lot["purchase_month"],
                    "cost_basis_per_unit_quanta": lot["cost_basis_per_unit"],
                    "remaining_quantity_quanta": lot["units_remaining"],
                    "quantity_scale": lot["quantity_scale"],
                }
                for lot in snapshot["lots"]
            )
    return pl.DataFrame(rows).sort("rollout_index", "month_index", "lot_id")


MIN_FEATURE_HORIZON_MONTHS = 60


def feature_rich_fixture(tmp_path: Path, *, rollout_count: int = 4) -> dict[str, Any]:
    fixture_path = tmp_path / f"benchmark-fixture-{rollout_count}.json"
    write_fixture(fixture_path, rollout_count=rollout_count, horizon_months=MIN_FEATURE_HORIZON_MONTHS)
    return cast(dict[str, Any], json.loads(fixture_path.read_text()))


def legacy_plan(fixture: dict[str, Any]) -> CompiledSimulation:
    """Compile the same fixture the Rust binding consumes, for the JAX product entry points."""

    scenario, external, locations = build_legacy_fixture(fixture)
    return compile_simulation(
        scenario,
        rollout_count=cast(int, fixture["rollout_count"]),
        external_series=external,
        jurisdictions=load_jurisdictions_for(scenario),
        locations=locations,
    )
