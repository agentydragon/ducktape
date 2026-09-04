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
from finance.augur.rust.fixture_adapter import build_legacy_fixture, run_legacy_fixture
from finance.augur.rust.output_adapter import decode_rust_event_log
from finance.augur.sim.compiler.plan import CompiledSimulation, compile_simulation
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.testing.state_helpers import asset_lots, capital_gains_ytd, cash_balances


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


def recurring_obligation_fixture() -> dict[str, Any]:
    fixture = failure_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 3
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 100_000},
        {"account": {"agent_id": "landlord", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "utility", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["scheduled_transfers"] = []
    scenario["obligations"] = []
    scenario["recurring_obligations"] = [
        {
            "start_month": 0,
            "end_month": 2,
            "obligation_id": "rent",
            "obligation_type": "cash_spend",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "landlord", "account_id": "checking"},
            "amount_due": 60_000,
        },
        {
            "start_month": 1,
            "end_month": 2,
            "obligation_id": "utility",
            "obligation_type": "cash_spend",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "utility", "account_id": "checking"},
            "amount_due": 1,
        },
    ]
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


def tax_payment_fixture(*, funded: bool = True) -> dict[str, Any]:
    fixture = tax_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 13
    scenario["tax_profiles"][0]["prior_year_tax"] = 400_000
    if not funded:
        scenario["tax_profiles"][0]["prior_year_tax"] = 0
        scenario["recurring_transfers"].append(
            {
                "start_month": 0,
                "end_month": 11,
                "cause_id": "alice-spends-paycheck",
                "from": {"agent_id": "alice", "account_id": "checking"},
                "to": {"agent_id": "payroll", "account_id": "checking"},
                "amount": 1_666_667,
            }
        )
    return fixture


def long_term_gain_tax_fixture() -> dict[str, Any]:
    fixture = tax_fixture()
    scenario = fixture["scenario"]
    scenario["recurring_transfers"][0]["amount"] = 416_667
    scenario["initial_lots"] = [
        {
            "lot_id": "alice-vti",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "purchase_month": -24,
            "quantity_scale": 1_000_000,
            "units": 100_000_000,
            "basis": 1_000_000,
        }
    ]
    scenario["scheduled_sales"] = [
        {
            "month": 6,
            "cause_id": "sell-vti",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "units": 100_000_000,
            "proceeds_account_id": "checking",
        }
    ]
    scenario["tax_profiles"][0]["jurisdictions"] = scenario["tax_profiles"][0]["jurisdictions"][:1]
    fixture["series"] = [{"series_id": "security:vti", "snapshots": 13, "values": [30_000] * 13}]
    return fixture


def capital_loss_carryforward_fixture() -> dict[str, Any]:
    fixture = tax_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 24
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["recurring_transfers"] = []
    scenario["initial_lots"] = [
        {
            "lot_id": "loss-lot",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "purchase_month": -24,
            "quantity_scale": 1_000_000,
            "units": 1_000_000,
            "basis": 1_000_000,
        },
        {
            "lot_id": "gain-lot",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "purchase_month": -12,
            "quantity_scale": 1_000_000,
            "units": 1_000_000,
            "basis": 100_000,
        },
    ]
    scenario["scheduled_sales"] = [
        {
            "month": 0,
            "cause_id": "harvest-loss",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "units": 1_000_000,
            "proceeds_account_id": "checking",
        },
        {
            "month": 12,
            "cause_id": "realize-gain",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "units": 1_000_000,
            "proceeds_account_id": "checking",
        },
    ]
    fixture["series"] = [{"series_id": "security:vti", "snapshots": 25, "values": [200_000] * 12 + [600_000] * 13}]
    return fixture


def distribution_fixture() -> dict[str, Any]:
    fixture = shared_integer_fixture()
    scenario = fixture["scenario"]
    scenario["accounts"] = [{"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 0}]
    scenario["scheduled_transfers"] = []
    scenario["recurring_transfers"] = []
    scenario["obligations"] = []
    scenario["recurring_obligations"] = []
    scenario["initial_lots"] = [
        {
            "lot_id": "alice-vti",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "vti",
            "purchase_month": -12,
            "quantity_scale": 1_000_000,
            "units": 2_000_000,
            "basis": 20_000,
        }
    ]
    scenario["scheduled_sales"] = []
    scenario["distributions"] = [
        {"agent_id": "alice", "holding_account_id": "brokerage", "asset_id": "vti", "to_account_id": "checking"}
    ]
    fixture["series"] = [
        {"series_id": "security:vti", "snapshots": 4, "values": [10_000] * 8},
        {"series_id": "security_distribution:vti", "snapshots": 4, "values": [100, 100, 100, 100, 200, 300, 400, 500]},
    ]
    return fixture


def distribution_tax_fixture() -> dict[str, Any]:
    fixture = distribution_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 12
    scenario["jurisdictions"] = [
        {"jurisdiction_id": "federal_us", "level": "federal"},
        {"jurisdiction_id": "california", "level": "state"},
    ]
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["initial_lots"] = [
        {
            "lot_id": "alice-bnd",
            "agent_id": "alice",
            "account_id": "brokerage",
            "asset_id": "bnd",
            "purchase_month": -24,
            "quantity_scale": 1_000_000,
            "units": 10_000_000_000,
            "basis": 73_000_000,
        }
    ]
    scenario["distributions"] = [
        {
            "agent_id": "alice",
            "holding_account_id": "brokerage",
            "asset_id": "bnd",
            "to_account_id": "checking",
            "tax_character": [
                {"fraction_ppb": 400_000_000, "issuer_jurisdiction_id": "federal_us"},
                {"fraction_ppb": 600_000_000},
            ],
        }
    ]
    tax_profile = tax_fixture()["scenario"]["tax_profiles"][0]
    federal, california = tax_profile["jurisdictions"]
    federal.update({"exempt_interest_from_levels": ["state"], "exempts_own_issue": False})
    california.update({"exempt_interest_from_levels": ["federal"], "exempts_own_issue": True})
    scenario["tax_profiles"] = [tax_profile]
    fixture["series"] = [
        {"series_id": "security:bnd", "snapshots": 13, "values": [7_300] * 26},
        {"series_id": "security_distribution:bnd", "snapshots": 13, "values": [20] * 13 + [30] * 13},
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


def bond_fixture() -> dict[str, Any]:
    fixture = tax_fixture()
    fixture["rollout_count"] = 3
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 12
    scenario["jurisdictions"] = [
        {"jurisdiction_id": "federal_us", "level": "federal"},
        {"jurisdiction_id": "california", "level": "state"},
    ]
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["scheduled_transfers"] = []
    scenario["recurring_transfers"] = []
    scenario["obligations"] = []
    scenario["recurring_obligations"] = []
    scenario["initial_lots"] = []
    scenario["scheduled_sales"] = []
    scenario["initial_bonds"] = [
        {
            "bond_id": "treasury",
            "agent_id": "alice",
            "account_id": "checking",
            "issuer_jurisdiction_id": "federal_us",
            "face_value": 10_000_000,
            "purchase_price": 10_000_000,
            "annual_coupon_rate_ppb": 50_000_000,
            "coupon_period_months": 6,
            "purchase_month_index": -1,
            "maturity_month_index": 11,
        },
        {
            "bond_id": "california-muni",
            "agent_id": "alice",
            "account_id": "checking",
            "issuer_jurisdiction_id": "california",
            "face_value": 10_000_000,
            "purchase_price": 10_000_000,
            "annual_coupon_rate_ppb": 40_000_000,
            "coupon_period_months": 6,
            "purchase_month_index": -1,
            "maturity_month_index": 11,
        },
        {
            "bond_id": "corporate",
            "agent_id": "alice",
            "account_id": "checking",
            "face_value": 10_000_000,
            "purchase_price": 10_000_000,
            "annual_coupon_rate_ppb": 30_000_000,
            "coupon_period_months": 6,
            "purchase_month_index": -1,
            "maturity_month_index": 11,
        },
        {
            "bond_id": "tips",
            "agent_id": "alice",
            "account_id": "checking",
            "issuer_jurisdiction_id": "federal_us",
            "face_value": 10_000_000,
            "purchase_price": 10_000_000,
            "annual_coupon_rate_ppb": 40_000_000,
            "coupon_period_months": 6,
            "inflation_indexed": True,
            "purchase_month_index": -1,
            "maturity_month_index": 11,
        },
        {
            "bond_id": "rounding-up",
            "agent_id": "alice",
            "account_id": "checking",
            "face_value": 600,
            "purchase_price": 600,
            "annual_coupon_rate_ppb": 10_000_000,
            "coupon_period_months": 1,
            "purchase_month_index": -1,
            "maturity_month_index": 11,
        },
        {
            "bond_id": "rounding-down",
            "agent_id": "alice",
            "account_id": "checking",
            "face_value": 180,
            "purchase_price": 180,
            "annual_coupon_rate_ppb": 33_333_333,
            "coupon_period_months": 1,
            "purchase_month_index": -1,
            "maturity_month_index": 11,
        },
        {
            "bond_id": "rounding-five-month",
            "agent_id": "alice",
            "account_id": "checking",
            "face_value": 1_250_627,
            "purchase_price": 1_250_627,
            "annual_coupon_rate_ppb": 37_000_000,
            "coupon_period_months": 5,
            "purchase_month_index": -4,
            "maturity_month_index": 11,
        },
    ]
    federal, california = scenario["tax_profiles"][0]["jurisdictions"]
    federal.update({"exempt_interest_from_levels": ["state"], "exempts_own_issue": False})
    california.update({"exempt_interest_from_levels": ["federal"], "exempts_own_issue": True})
    fixture["series"] = [
        {
            "series_id": "inflation",
            "snapshots": 13,
            "values": [
                *([1_000_000_000] * 6),
                *([2_000_000_000] * 7),
                *([1_000_000_000] * 6),
                *([1_500_000_000] * 7),
                *([1_000_000_000] * 6),
                *([800_000_000] * 7),
            ],
        }
    ]
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


def property_cashflow_gating_fixture() -> dict[str, Any]:
    fixture = failure_fixture()
    fixture["rollout_count"] = 1
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 4
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 1_000},
        {"account": {"agent_id": "seller", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "vendor", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "creditor", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["scheduled_transfers"] = []
    scenario["recurring_transfers"] = []
    scenario["obligations"] = [
        {
            "month": 2,
            "obligation_id": "unaffordable",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "creditor", "account_id": "checking"},
            "amount_due": 876,
        }
    ]
    scenario["recurring_obligations"] = []
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
            "month": 1,
            "cause_id": "buy-home",
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
    scenario["property_tax_policies"] = []
    scenario["scheduled_property_cashflows"] = [
        {
            "month": 0,
            "property_id": "home",
            "cause_id": "before-purchase",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "vendor", "account_id": "checking"},
            "amount": 3,
        },
        {
            "month": 1,
            "property_id": "home",
            "cause_id": "purchase-month",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "vendor", "account_id": "checking"},
            "amount": 5,
        },
    ]
    scenario["recurring_property_cashflows"] = [
        {
            "start_month": 0,
            "end_month": 3,
            "property_id": "home",
            "cause_id": "property-carry",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "vendor", "account_id": "checking"},
            "amount": 10,
        }
    ]
    scenario["initial_lots"] = []
    scenario["scheduled_sales"] = []
    scenario["tax_profiles"] = []
    scenario["distributions"] = []
    fixture["series"] = []
    return fixture


def series_indexed_amount_fixture() -> dict[str, Any]:
    fixture = failure_fixture()
    fixture["rollout_count"] = 2
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 14
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 20_000_000},
        {"account": {"agent_id": "bob", "account_id": "checking"}, "opening_balance": 20_000_000},
        {"account": {"agent_id": "seller", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "tenant", "account_id": "checking"}, "opening_balance": 20_000_000},
        {"account": {"agent_id": "landlord", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "manager", "account_id": "checking"}, "opening_balance": 0},
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
            "from": {"agent_id": "bob", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount": indexed_inflation,
        },
        {
            "month": 3,
            "cause_id": "tagged-fixed-gift",
            "from": {"agent_id": "bob", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount": {"kind": "fixed", "amount": -17},
        },
        {
            "month": 4,
            "cause_id": "zero-gift",
            "from": {"agent_id": "bob", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount": 0,
        },
    ]
    scenario["recurring_transfers"] = [
        {
            "start_month": 0,
            "end_month": 13,
            "cause_id": "annual-indexed-paycheck",
            "from": {"agent_id": "bob", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount": indexed_annual_rent,
        }
    ]
    scenario["obligations"] = [
        {
            "month": 2,
            "obligation_id": "indexed-bill",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "landlord", "account_id": "checking"},
            "amount_due": indexed_inflation,
        }
    ]
    scenario["recurring_obligations"] = [
        {
            "start_month": 0,
            "end_month": 13,
            "obligation_id": "indexed-rent",
            "obligation_type": "cash_spend",
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "landlord", "account_id": "checking"},
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
            "from": {"agent_id": "alice", "account_id": "checking"},
            "to": {"agent_id": "manager", "account_id": "checking"},
            "amount": indexed_inflation,
        }
    ]
    scenario["recurring_property_cashflows"] = [
        {
            "start_month": 0,
            "end_month": 13,
            "property_id": "home",
            "cause_id": "indexed-property-rent",
            "from": {"agent_id": "tenant", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
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


def property_sale_fixture() -> dict[str, Any]:
    fixture = financed_property_fixture()
    fixture["rollout_count"] = 2
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 4
    scenario["accounts"].extend(
        [
            {"account": {"agent_id": "tenant", "account_id": "checking"}, "opening_balance": 10_000},
            {"account": {"agent_id": "gift", "account_id": "checking"}, "opening_balance": 1_000},
        ]
    )
    scenario["scheduled_transfers"] = [
        {
            "month": 2,
            "cause_id": "sale-month-generic-transfer",
            "from": {"agent_id": "gift", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount": 7,
        }
    ]
    scenario["recurring_property_cashflows"] = [
        {
            "start_month": 0,
            "end_month": 3,
            "property_id": "home",
            "cause_id": "rent",
            "from": {"agent_id": "tenant", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount": 1_000,
        }
    ]
    scenario["property_sales"] = [{"month": 2, "property_id": "home", "closing_cost_bps": 600}]
    fixture["series"] = [
        {
            "series_id": "home_value:sf",
            "snapshots": 5,
            "values": [
                50_000_000,
                50_000_000,
                60_000_000,
                60_000_000,
                60_000_000,
                50_000_000,
                50_000_000,
                55_000_000,
                55_000_000,
                55_000_000,
            ],
        }
    ]
    return fixture


def section_121_fixture() -> dict[str, Any]:
    fixture = financed_property_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 86
    scenario["accounts"] = [
        *[
            {"account": {"agent_id": agent_id, "account_id": "checking"}, "opening_balance": 60_000_000}
            for agent_id in ("alice", "bob", "carol", "dave")
        ],
        *[
            {"account": {"agent_id": seller_id, "account_id": "checking"}, "opening_balance": 0}
            for seller_id in ("seller-a", "seller-b", "seller-c", "seller-d")
        ],
        {"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["scheduled_property_purchases"] = [
        {
            "month": 0,
            "cause_id": f"{agent_id}-buys-home",
            "property_id": property_id,
            "location_id": "sf",
            "buyer_agent_id": agent_id,
            "buyer_account_id": "checking",
            "seller_agent_id": seller_id,
            "seller_account_id": "checking",
            "purchase_price": 50_000_000,
            "down_payment": 50_000_000,
            "buyer_closing_cost": 0,
            "rented_fraction_ppb": 0,
            "mortgage": None,
        }
        for agent_id, property_id, seller_id in (
            ("alice", "alice-home", "seller-a"),
            ("bob", "bob-home", "seller-b"),
            ("carol", "carol-home", "seller-c"),
            ("dave", "dave-home", "seller-d"),
        )
    ]
    scenario["initial_primary_residences"] = [
        {"agent_id": "alice", "property_id": "alice-home"},
        {"agent_id": "dave", "property_id": "dave-home"},
    ]
    scenario["primary_residence_events"] = [
        {"month": 7, "agent_id": "bob", "property_id": "bob-home"},
        {"month": 24, "agent_id": "dave", "property_id": None},
        {"month": 30, "agent_id": "carol", "property_id": "carol-home"},
    ]
    scenario["property_sales"] = [
        {"month": 30, "property_id": property_id, "closing_cost_bps": 0}
        for property_id in ("alice-home", "bob-home", "carol-home")
    ] + [{"month": 84, "property_id": "dave-home", "closing_cost_bps": 0}]
    scenario["property_tax_policies"] = []
    federal_profile = tax_fixture()["scenario"]["tax_profiles"][0]
    federal_profile["jurisdictions"] = federal_profile["jurisdictions"][:1]
    scenario["tax_profiles"] = []
    for agent_id in ("alice", "bob", "carol", "dave"):
        profile = json.loads(json.dumps(federal_profile))
        profile["agent_id"] = agent_id
        profile["section_121_exclusion"] = 25_000_000
        scenario["tax_profiles"].append(profile)
    fixture["series"] = [
        {"series_id": "home_value:sf", "snapshots": 87, "values": [50_000_000] * 30 + [75_000_000] * 57}
    ]
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


def uncapped_mortgage_interest_fixture() -> dict[str, Any]:
    fixture = property_cashflow_fixture()
    scenario = fixture["scenario"]
    purchase = scenario["scheduled_property_purchases"][0]
    purchase["purchase_price"] = 100_000_000
    purchase["down_payment"] = 20_000_000
    purchase["buyer_closing_cost"] = 1_000_000
    purchase["mortgage"]["principal"] = 80_000_000
    purchase["rented_fraction_ppb"] = 0
    scenario["property_tax_policies"] = []
    scenario["mortgage_interest_deduction_policies"] = [{"liability_id": "home-mortgage", "owner_agent_id": "alice"}]
    return fixture


def mortgage_interest_policy_fixture() -> dict[str, Any]:
    fixture = financed_property_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 12
    scenario["accounts"] = [
        {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 30_000_000},
        {"account": {"agent_id": "bob", "account_id": "checking"}, "opening_balance": 30_000_000},
        {"account": {"agent_id": "seller-a", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "seller-b", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "bank-a", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "bank-b", "account_id": "checking"}, "opening_balance": 0},
        {"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0},
    ]
    scenario["scheduled_property_purchases"] = [
        {
            "month": 0,
            "cause_id": f"{agent_id}-buys-home",
            "property_id": f"{agent_id}-home",
            "location_id": "sf",
            "buyer_agent_id": agent_id,
            "buyer_account_id": "checking",
            "seller_agent_id": seller_id,
            "seller_account_id": "checking",
            "purchase_price": 100_000_000,
            "down_payment": 20_000_000,
            "buyer_closing_cost": 0,
            "rented_fraction_ppb": 0,
            "land_value_fraction_ppb": 1_000_000_000,
            "mortgage": {
                "liability_id": f"{agent_id}-mortgage",
                "lender_agent_id": bank_id,
                "lender_account_id": "checking",
                "principal": 80_000_000,
                "annual_interest_rate_ppb": 60_000_000,
                "term_months": 360,
            },
        }
        for agent_id, seller_id, bank_id in (("alice", "seller-a", "bank-a"), ("bob", "seller-b", "bank-b"))
    ]
    scenario["mortgage_interest_deduction_policies"] = [
        {
            "liability_id": "alice-mortgage",
            "owner_agent_id": "alice",
            "debt_class": "acquisition",
            "per_jurisdiction_principal_cap": {"federal_us": 75_000_000, "california": 100_000_000},
        },
        {
            "liability_id": "bob-mortgage",
            "owner_agent_id": "bob",
            "debt_class": "home_equity",
            "per_jurisdiction_principal_cap": {"federal_us": 75_000_000, "california": 100_000_000},
        },
    ]
    scenario["property_tax_policies"] = []
    scenario["scheduled_property_cashflows"] = []
    scenario["recurring_property_cashflows"] = []
    base_profile = tax_fixture()["scenario"]["tax_profiles"][0]
    scenario["tax_profiles"] = []
    for agent_id in ("alice", "bob"):
        profile = json.loads(json.dumps(base_profile))
        profile["agent_id"] = agent_id
        scenario["tax_profiles"].append(profile)
    fixture["series"] = []
    return fixture


def salt_deduction_fixture() -> dict[str, Any]:
    fixture = financed_property_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 24
    scenario["accounts"].extend(
        [
            {"account": {"agent_id": "payroll", "account_id": "checking"}, "opening_balance": 0},
            {"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0},
        ]
    )
    scenario["recurring_transfers"] = [
        {
            "start_month": 0,
            "end_month": 23,
            "cause_id": "alice-paycheck",
            "from": {"agent_id": "payroll", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount": 2_000_000,
            "income_category": "ordinary",
        }
    ]
    scenario["tax_profiles"] = [tax_fixture()["scenario"]["tax_profiles"][0]]
    scenario["federal_salt_deduction_policies"] = [
        {
            "profile_id": "alice",
            "federal_jurisdiction_id": "federal_us",
            "cap_schedule": [
                {"effective_year_index": 0, "cap": 4_000_000},
                {"effective_year_index": 1, "cap": 1_000_000},
            ],
        }
    ]
    return fixture


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
            "accounts": [{"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 0}],
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
    scenario["accounts"].append({"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0})
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


def rust_tax_liability_frame(rust: dict[str, Any]) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for rollout in rust["rollouts"]:
        for snapshot in rollout["months"]:
            rows.extend(
                {
                    "rollout_index": rollout["rollout_id"],
                    "month_index": snapshot["month"],
                    "agent_id": liability["agent_id"],
                    "jurisdiction_id": liability["jurisdiction_id"],
                    "tax_year_end_month": liability["tax_year_end_month"],
                    "amount_owed_quanta": liability["amount_owed"],
                }
                for liability in snapshot["tax_liabilities"]
            )
    return pl.DataFrame(rows).sort("rollout_index", "month_index", "agent_id", "jurisdiction_id", "tax_year_end_month")


def rust_capital_gain_frame(rust: dict[str, Any]) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for rollout in rust["rollouts"]:
        for snapshot in rollout["months"]:
            for state in snapshot["capital_gains"]:
                rows.extend(
                    [
                        {
                            "rollout_index": rollout["rollout_id"],
                            "month_index": snapshot["month"],
                            "agent_id": state["agent_id"],
                            "classification": "stcg",
                            "gain_quanta": state["short_term_gain"],
                        },
                        {
                            "rollout_index": rollout["rollout_id"],
                            "month_index": snapshot["month"],
                            "agent_id": state["agent_id"],
                            "classification": "ltcg",
                            "gain_quanta": state["long_term_gain"],
                        },
                    ]
                )
    return pl.DataFrame(rows).sort("rollout_index", "month_index", "agent_id", "classification")


def assert_private_equity_parity(fixture: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert rust_cash_frame(rust).to_dicts() == legacy_cash.to_dicts()

    legacy_lots = (
        asset_lots(legacy)
        .select(
            "rollout_index",
            "month_index",
            "lot_id",
            "agent_id",
            "account_id",
            "asset_id",
            "purchase_month_index",
            "cost_basis_per_unit_quanta",
            "remaining_quantity_quanta",
            "quantity_scale",
        )
        .sort("rollout_index", "month_index", "lot_id")
    )
    assert rust_lot_frame(rust).to_dicts() == legacy_lots.to_dicts()

    comparisons = (
        (
            legacy.events_log.lot_dispositions,
            rust_events.lot_dispositions,
            ["rollout_index", "month_index", "cause_id", "lot_id"],
        ),
        (
            legacy.events_log.private_equity_events,
            rust_events.private_equity_events,
            ["rollout_index", "month_index", "issuer_id"],
        ),
        (
            legacy.events_log.private_equity_opportunities,
            rust_events.private_equity_opportunities,
            ["rollout_index", "month_index", "cause_id"],
        ),
        (
            legacy.events_log.tax_accruals,
            rust_events.tax_accruals,
            ["rollout_index", "month_index", "cause_id", "jurisdiction_id"],
        ),
        (
            legacy.events_log.tax_breakdowns,
            rust_events.tax_breakdowns,
            ["rollout_index", "month_index", "cause_id", "jurisdiction_id"],
        ),
    )
    for legacy_frame, rust_frame, sort_columns in comparisons:
        assert rust_frame.schema == legacy_frame.schema
        assert rust_frame.sort(sort_columns).to_dicts() == legacy_frame.sort(sort_columns).to_dicts()

    assert all(
        sum(posting["amount"] for posting in entry["postings"]) == 0
        for rollout in rust["rollouts"]
        for entry in rollout["journal"]
    )
    return rust


def assert_tlh_parity(fixture: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    legacy = run_legacy_fixture(fixture)
    rust = rust_run(fixture, tmp_path)
    legacy_lookup = {
        (row["rollout_index"], row["month_index"], row["agent_id"], row["classification"]): row["gain_quanta"]
        for row in capital_gains_ytd(legacy).to_dicts()
    }
    rust_gains = rust_capital_gain_frame(rust)
    for row in rust_gains.to_dicts():
        key = (row["rollout_index"], row["month_index"], row["agent_id"], row["classification"])
        assert row["gain_quanta"] == legacy_lookup.get(key, 0)

    rust_events = decode_rust_event_log(rust)
    for legacy_frame, rust_frame, sort_columns in (
        (
            legacy.events_log.lot_dispositions,
            rust_events.lot_dispositions,
            ["rollout_index", "month_index", "cause_id", "lot_id"],
        ),
        (
            legacy.events_log.tax_accruals,
            rust_events.tax_accruals,
            ["rollout_index", "month_index", "cause_id", "jurisdiction_id"],
        ),
        (
            legacy.events_log.tax_breakdowns,
            rust_events.tax_breakdowns,
            ["rollout_index", "month_index", "cause_id", "jurisdiction_id"],
        ),
    ):
        assert rust_frame.schema == legacy_frame.schema
        assert rust_frame.sort(sort_columns).to_dicts() == legacy_frame.sort(sort_columns).to_dicts()

    for rollout in rust["rollouts"]:
        gains_by_month = {
            row["month_index"]: row["gain_quanta"]
            for row in rust_gains.filter(
                (pl.col("rollout_index") == rollout["rollout_id"]) & (pl.col("classification") == "stcg")
            ).to_dicts()
        }
        for snapshot in rollout["months"]:
            if snapshot["month"] < 12:
                assert snapshot["tlh_cumulative_harvest"][0] == -gains_by_month[snapshot["month"]]
    return rust


MIN_FEATURE_HORIZON_MONTHS = 60


def feature_rich_fixture(tmp_path: Path, *, rollout_count: int = 4) -> dict[str, Any]:
    fixture_path = tmp_path / f"benchmark-fixture-{rollout_count}.json"
    write_fixture(fixture_path, rollout_count=rollout_count, horizon_months=MIN_FEATURE_HORIZON_MONTHS)
    return cast(dict[str, Any], json.loads(fixture_path.read_text()))


def feature_rich_failure_fixture(tmp_path: Path) -> dict[str, Any]:
    """The feature-rich fixture with one obligation nobody can fund.

    Building the failure case from the full fixture rather than a bare one keeps the
    exogenous series every metric reads, so the comparison still covers holdings, property
    and bonds on the frozen side of the failure.
    """

    fixture = feature_rich_fixture(tmp_path)
    fixture["scenario"]["obligations"].append(
        {
            "month": 30,
            "obligation_id": "unfundable-differential-probe",
            "from": {"agent_id": "cashflow", "account_id": "checking"},
            "to": {"agent_id": "vendor", "account_id": "checking"},
            "amount_due": 10_000_000_000,
        }
    )
    return fixture


# One agent per policy family the benchmark fixture separates. JAX bakes the selected agent
# into the compiled program, so each name here costs a full compile of the 60-month
# scenario — hence a covering set rather than every account holder. The metric-coverage
# assertion below is what keeps the set honest: drop an agent that uniquely carries a
# metric and the test fails rather than quietly narrowing.
PRODUCT_METRIC_AGENTS = ("allocator", "bondholder", "cashflow", "homeowner", "pe_owner")


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
