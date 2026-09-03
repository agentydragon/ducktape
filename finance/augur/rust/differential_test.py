"""Differential harness for the Rust and existing JAX simulators.

The canonical fixture is integer-only. Both engines consume the same scenario
and exogenous bytes; conversion to legacy floats happens only inside the
existing Python/JAX adapter because that engine predates this boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest
import pytest_bazel
from polars.testing import assert_frame_equal

from finance.augur.rust.benchmark_fixture import write_fixture
from finance.augur.rust.fixture_adapter import run_legacy_fixture
from finance.augur.rust.output_adapter import decode_rust_event_log
from finance.augur.sim.engine.jax_engine import run_jax_product_metric_arrays
from finance.augur.sim.events import EVENT_FRAME_SPECS
from finance.augur.sim.testing.state_helpers import (
    asset_lots,
    capital_gains_ytd,
    cash_balances,
    liabilities,
    ordinary_income_ytd,
    property_stakes,
    property_state,
    rollout_status,
    tax_liabilities,
)


def _binary() -> Path:
    runfiles = Path(os.environ["TEST_SRCDIR"])
    workspace = os.environ["TEST_WORKSPACE"]
    return runfiles / workspace / "finance/augur/rust/simulator_cli"


def _fixture() -> dict[str, Any]:
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


def _failure_fixture() -> dict[str, Any]:
    fixture = _fixture()
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


def _recurring_obligation_fixture() -> dict[str, Any]:
    fixture = _failure_fixture()
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


def _tax_fixture() -> dict[str, Any]:
    fixture = _failure_fixture()
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


def _tax_payment_fixture(*, funded: bool = True) -> dict[str, Any]:
    fixture = _tax_fixture()
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


def _long_term_gain_tax_fixture() -> dict[str, Any]:
    fixture = _tax_fixture()
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


def _capital_loss_carryforward_fixture() -> dict[str, Any]:
    fixture = _tax_fixture()
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


def _distribution_fixture() -> dict[str, Any]:
    fixture = _fixture()
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


def _distribution_tax_fixture() -> dict[str, Any]:
    fixture = _distribution_fixture()
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
    tax_profile = _tax_fixture()["scenario"]["tax_profiles"][0]
    federal, california = tax_profile["jurisdictions"]
    federal.update({"exempt_interest_from_levels": ["state"], "exempts_own_issue": False})
    california.update({"exempt_interest_from_levels": ["federal"], "exempts_own_issue": True})
    scenario["tax_profiles"] = [tax_profile]
    fixture["series"] = [
        {"series_id": "security:bnd", "snapshots": 13, "values": [7_300] * 26},
        {"series_id": "security_distribution:bnd", "snapshots": 13, "values": [20] * 13 + [30] * 13},
    ]
    return fixture


def _target_allocation_fixture() -> dict[str, Any]:
    tax_profile = _tax_fixture()["scenario"]["tax_profiles"][0]
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


def _target_allocation_failure_fixture() -> dict[str, Any]:
    fixture = _target_allocation_fixture()
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


def _target_allocation_purchase_fixture(*, purchase_slots: int = 1) -> dict[str, Any]:
    fixture = _target_allocation_fixture()
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


def _target_allocation_purchase_then_sale_fixture() -> dict[str, Any]:
    fixture = _target_allocation_purchase_fixture()
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


def _target_allocation_purchase_distribution_fixture() -> dict[str, Any]:
    fixture = _target_allocation_purchase_fixture()
    scenario = fixture["scenario"]
    scenario["initial_lots"][1]["account_id"] = "brokerage-b"
    scenario["distributions"] = [
        {"agent_id": "alice", "holding_account_id": "brokerage-a", "asset_id": "vti", "to_account_id": "checking"}
    ]
    fixture["series"].append({"series_id": "security_distribution:vti", "snapshots": 3, "values": [100, 100, 100]})
    return fixture


def _target_allocation_rebalance_fixture(*, tolerance_ppb: int = 250_000_000) -> dict[str, Any]:
    fixture = _target_allocation_purchase_fixture()
    scenario = fixture["scenario"]
    scenario["accounts"][0]["opening_balance"] = 5_000_000
    policy = scenario["target_allocation_policies"][0]
    policy["cash_floor"] = 1_000_000
    policy["cash_ceiling"] = 9_000_000
    policy["rebalance_tolerance_ppb"] = tolerance_ppb
    return fixture


def _bond_fixture() -> dict[str, Any]:
    fixture = _tax_fixture()
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


def _financed_property_fixture() -> dict[str, Any]:
    fixture = _failure_fixture()
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


def _property_cashflow_fixture() -> dict[str, Any]:
    fixture = _financed_property_fixture()
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
    federal_profile = _tax_fixture()["scenario"]["tax_profiles"][0]
    federal_profile["jurisdictions"] = federal_profile["jurisdictions"][:1]
    scenario["tax_profiles"] = [federal_profile]
    return fixture


def _property_cashflow_gating_fixture() -> dict[str, Any]:
    fixture = _failure_fixture()
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


def _series_indexed_amount_fixture() -> dict[str, Any]:
    fixture = _failure_fixture()
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


def _property_sale_fixture() -> dict[str, Any]:
    fixture = _financed_property_fixture()
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


def _section_121_fixture() -> dict[str, Any]:
    fixture = _financed_property_fixture()
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
    federal_profile = _tax_fixture()["scenario"]["tax_profiles"][0]
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


def _property_depreciation_fixture(*, sale: bool) -> dict[str, Any]:
    fixture = _property_cashflow_fixture()
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
        tax_profile = _tax_fixture()["scenario"]["tax_profiles"][0]
        tax_profile["jurisdictions"][0]["section_1250_rate_ppb"] = 250_000_000
        tax_profile["jurisdictions"][1]["section_1250_rate_ppb"] = 0
        scenario["tax_profiles"] = [tax_profile]
        fixture["series"] = [
            {"series_id": "home_value:sf", "snapshots": 25, "values": [50_000_000] * 12 + [75_000_000] * 13}
        ]
    return fixture


def _uncapped_mortgage_interest_fixture() -> dict[str, Any]:
    fixture = _property_cashflow_fixture()
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


def _mortgage_interest_policy_fixture() -> dict[str, Any]:
    fixture = _financed_property_fixture()
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
    base_profile = _tax_fixture()["scenario"]["tax_profiles"][0]
    scenario["tax_profiles"] = []
    for agent_id in ("alice", "bob"):
        profile = json.loads(json.dumps(base_profile))
        profile["agent_id"] = agent_id
        scenario["tax_profiles"].append(profile)
    fixture["series"] = []
    return fixture


def _salt_deduction_fixture() -> dict[str, Any]:
    fixture = _financed_property_fixture()
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
    scenario["tax_profiles"] = [_tax_fixture()["scenario"]["tax_profiles"][0]]
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


def _private_equity_fixture() -> dict[str, Any]:
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


def _private_equity_tax_fixture() -> dict[str, Any]:
    fixture = _private_equity_fixture()
    fixture["rollout_count"] = 1
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 12
    scenario["accounts"].append({"account": {"agent_id": "irs", "account_id": "checking"}, "opening_balance": 0})
    scenario["private_equity_tender_policies"][0]["liquid_net_worth_floor"] = 10_000_000
    scenario["tax_profiles"] = [
        {
            "agent_id": "alice",
            "tax_authority_agent_id": "irs",
            "jurisdictions": [_tax_fixture()["scenario"]["tax_profiles"][0]["jurisdictions"][0]],
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


def _tlh_fixture(
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
                    "jurisdictions": [_tax_fixture()["scenario"]["tax_profiles"][0]["jurisdictions"][0]],
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


def _rust_run(fixture: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    fixture_path = tmp_path / "fixture.json"
    output_path = tmp_path / "rust-output.json"
    fixture_path.write_text(json.dumps(fixture, separators=(",", ":")))
    subprocess.run([_binary(), fixture_path, output_path], check=True)
    return cast(dict[str, Any], json.loads(output_path.read_text()))


def _rust_cash(rust: dict[str, Any]) -> pl.DataFrame:
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


def _rust_lots(rust: dict[str, Any]) -> pl.DataFrame:
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


def _rust_tax_liabilities(rust: dict[str, Any]) -> pl.DataFrame:
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


def _rust_capital_gains(rust: dict[str, Any]) -> pl.DataFrame:
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


def test_rust_and_jax_match_on_shared_integer_fixture(tmp_path: Path) -> None:
    fixture = _fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()

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

    # The existing lot read model must also agree on the post-sale quantity.
    legacy_final_lots = asset_lots(legacy).filter(pl.col("month_index") == 3)
    assert legacy_final_lots.get_column("remaining_quantity_quanta").to_list() == [1_000_000, 1_000_000]

    for rollout in rust["rollouts"]:
        for entry in rollout["journal"]:
            assert sum(posting["amount"] for posting in entry["postings"]) == 0

    rust_events = decode_rust_event_log(rust)
    legacy_events = legacy.events_log
    comparisons = (
        (
            legacy_events.transfers,
            rust_events.transfers,
            [
                "rollout_index",
                "month_index",
                "cause_id",
                "from_agent_id",
                "from_account_id",
                "to_agent_id",
                "to_account_id",
            ],
        ),
        (
            legacy_events.lot_dispositions,
            rust_events.lot_dispositions,
            ["rollout_index", "month_index", "cause_id", "lot_id"],
        ),
        (
            legacy_events.obligation_accruals,
            rust_events.obligation_accruals,
            ["rollout_index", "month_index", "cause_id", "obligation_id"],
        ),
        (
            legacy_events.obligation_settlements,
            rust_events.obligation_settlements,
            ["rollout_index", "month_index", "cause_id", "obligation_id"],
        ),
    )
    for legacy_frame, rust_frame, sort_columns in comparisons:
        assert rust_frame.schema == legacy_frame.schema
        assert rust_frame.sort(sort_columns).to_dicts() == legacy_frame.sort(sort_columns).to_dicts()


def test_rust_and_jax_match_failure_freeze_semantics(tmp_path: Path) -> None:
    fixture = _failure_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()

    assert rollout_status(legacy).to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 0},
        {"rollout_index": 1, "status": "failed_insufficient_cash", "failed_month": 0},
    ]
    assert [rollout["failed_month"] for rollout in rust["rollouts"]] == [0, 0]
    assert all(
        snapshot["failed"] and all(balance["balance"] == 0 for balance in snapshot["balances"])
        for rollout in rust["rollouts"]
        for snapshot in rollout["months"][1:]
    )
    rust_failures = decode_rust_event_log(rust).rollout_failures
    legacy_failures = legacy.events_log.rollout_failures
    sort_columns = ["rollout_index", "month_index", "cause_id", "obligation_id"]
    assert rust_failures.schema == legacy_failures.schema
    assert rust_failures.sort(sort_columns).to_dicts() == legacy_failures.sort(sort_columns).to_dicts()


def test_fixture_rejects_inexact_per_unit_lot_basis_in_both_engines(tmp_path: Path) -> None:
    fixture = _fixture()
    lot = fixture["scenario"]["initial_lots"][0]
    lot["units"] = 3
    lot["basis"] = 1

    with pytest.raises(ValueError, match="does not encode an exact integer-quantum per-unit basis"):
        run_legacy_fixture(fixture)

    fixture_path = tmp_path / "inexact-lot-basis.json"
    output_path = tmp_path / "unused.json"
    fixture_path.write_text(json.dumps(fixture, separators=(",", ":")))
    completed = subprocess.run([_binary(), fixture_path, output_path], check=False, capture_output=True, text=True)
    assert completed.returncode == 1
    assert "total basis does not encode an exact per-unit basis" in completed.stderr


def test_fixture_rejects_noncanonical_target_allocation_quantity_scale(tmp_path: Path) -> None:
    fixture = _target_allocation_purchase_fixture()
    fixture["scenario"]["target_allocation_policies"][0]["sleeves"][0]["quantity_scale"] = 1

    with pytest.raises(ValueError, match="canonical Python asset scale is 1000000"):
        run_legacy_fixture(fixture)

    fixture_path = tmp_path / "wrong-target-allocation-scale.json"
    output_path = tmp_path / "unused.json"
    fixture_path.write_text(json.dumps(fixture, separators=(",", ":")))
    completed = subprocess.run([_binary(), fixture_path, output_path], check=False, capture_output=True, text=True)
    assert completed.returncode == 1
    assert "mixes quantity scales 1000000 and 1" in completed.stderr


def test_rust_and_jax_match_liquidity_sales_before_obligation_funding(tmp_path: Path) -> None:
    fixture = _target_allocation_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
        .to_dicts()
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash

    legacy_dispositions = (
        legacy.events_log.lot_dispositions.with_columns(
            (pl.col("units_sold") * 1_000_000).round().cast(pl.Int64).alias("units_sold_quanta"),
            (pl.col("proceeds_quanta") - pl.col("cost_basis_consumed_quanta")).alias("realized_gain_quanta"),
        )
        .select(
            "rollout_index",
            "month_index",
            "cause_id",
            "agent_id",
            "source_account_id",
            "asset_id",
            "lot_id",
            "purchase_month_index",
            "units_sold_quanta",
            "cost_basis_consumed_quanta",
            "proceeds_quanta",
            "proceeds_account_id",
            "realized_gain_quanta",
        )
        .sort("rollout_index", "month_index", "source_account_id")
        .to_dicts()
    )
    rust_dispositions_in_execution_order = [
        {
            "rollout_index": rollout["rollout_id"],
            "month_index": disposition["month"],
            "cause_id": disposition["cause_id"],
            "agent_id": disposition["agent_id"],
            "source_account_id": disposition["source_account_id"],
            "asset_id": disposition["asset_id"],
            "lot_id": disposition["lot_id"],
            "purchase_month_index": disposition["purchase_month"],
            "units_sold_quanta": disposition["units"],
            "cost_basis_consumed_quanta": disposition["basis"],
            "proceeds_quanta": disposition["proceeds"],
            "proceeds_account_id": disposition["proceeds_account_id"],
            "realized_gain_quanta": disposition["realized_gain"],
        }
        for rollout in rust["rollouts"]
        for disposition in rollout["dispositions"]
    ]
    assert [row["source_account_id"] for row in rust_dispositions_in_execution_order] == ["brokerage-a", "brokerage-b"]
    rust_dispositions = sorted(
        rust_dispositions_in_execution_order,
        key=lambda row: (row["rollout_index"], row["month_index"], row["source_account_id"]),
    )
    assert rust_dispositions == legacy_dispositions
    assert rust_dispositions == [
        {
            "rollout_index": 0,
            "month_index": 1,
            "cause_id": "allocation_sale_m1_security:vti",
            "agent_id": "alice",
            "source_account_id": "brokerage-a",
            "asset_id": "security:vti",
            "lot_id": "z-source-first",
            "purchase_month_index": -24,
            "units_sold_quanta": 100_000_000,
            "cost_basis_consumed_quanta": 500_000,
            "proceeds_quanta": 1_000_000,
            "proceeds_account_id": "checking",
            "realized_gain_quanta": 500_000,
        },
        {
            "rollout_index": 0,
            "month_index": 1,
            "cause_id": "allocation_sale_m1_security:vti",
            "agent_id": "alice",
            "source_account_id": "brokerage-b",
            "asset_id": "security:vti",
            "lot_id": "a-source-second",
            "purchase_month_index": 0,
            "units_sold_quanta": 130_000_000,
            "cost_basis_consumed_quanta": 1_040_000,
            "proceeds_quanta": 1_300_000,
            "proceeds_account_id": "checking",
            "realized_gain_quanta": 260_000,
        },
    ]

    obligation_columns = [
        "rollout_index",
        "month_index",
        "cause_id",
        "obligation_id",
        "amount_due_quanta",
        "amount_paid_quanta",
        "shortfall_quanta",
        "attempted_funding_sources",
    ]
    legacy_obligations = legacy.events_log.obligation_settlements.select(obligation_columns).sort(
        ["month_index", "obligation_id"]
    )
    rust_obligations = pl.DataFrame(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": outcome["month"],
                "cause_id": outcome["cause_id"],
                "obligation_id": outcome["obligation_id"],
                "amount_due_quanta": outcome["amount_due"],
                "amount_paid_quanta": outcome["amount_paid"],
                "shortfall_quanta": outcome["shortfall"],
                "attempted_funding_sources": outcome["attempted_funding_sources"],
            }
            for rollout in rust["rollouts"]
            for outcome in rollout["obligations"]
        ]
    ).sort(["month_index", "obligation_id"])
    assert rust_obligations.to_dicts() == legacy_obligations.to_dicts()
    assert rust_obligations.get_column("attempted_funding_sources").unique().to_list() == ["security:vti,security:bnd"]

    legacy_tax = (
        legacy.events_log.tax_breakdowns.select(
            "rollout_index", "month_index", "jurisdiction_id", "stcg_quanta", "ltcg_quanta"
        )
        .sort("jurisdiction_id")
        .to_dicts()
    )
    rust_tax = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": accrual["month"],
                "jurisdiction_id": accrual["jurisdiction_id"],
                "stcg_quanta": accrual["short_term_gain"],
                "ltcg_quanta": accrual["long_term_gain"],
            }
            for rollout in rust["rollouts"]
            for accrual in rollout["tax_accruals"]
        ],
        key=lambda row: row["jurisdiction_id"],
    )
    assert rust_tax == legacy_tax
    assert {(row["stcg_quanta"], row["ltcg_quanta"]) for row in rust_tax} == {(260_000, 500_000)}
    assert rollout_status(legacy).to_dicts() == [{"rollout_index": 0, "status": "active", "failed_month": None}]
    assert rust["rollouts"][0]["failed_month"] is None
    assert all(sum(posting["amount"] for posting in entry["postings"]) == 0 for entry in rust["rollouts"][0]["journal"])


def test_rust_and_jax_match_post_settlement_target_allocation_purchases(tmp_path: Path) -> None:
    fixture = _target_allocation_purchase_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
        .to_dicts()
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash

    columns = [
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
    ]
    legacy_lots = (
        asset_lots(legacy)
        .filter((pl.col("month_index") == 1) & pl.col("lot_id").str.starts_with("allocation_sale_buy"))
        .select(columns)
        .sort("lot_id")
        .to_dicts()
    )
    rust_lots = (
        _rust_lots(rust)
        .filter((pl.col("month_index") == 1) & pl.col("lot_id").str.starts_with("allocation_sale_buy"))
        .select(columns)
        .sort("lot_id")
        .to_dicts()
    )
    assert rust_lots == legacy_lots
    assert rust_lots == [
        {
            "rollout_index": 0,
            "month_index": 1,
            "lot_id": "allocation_sale_buy_p0_s0_0",
            "agent_id": "alice",
            "account_id": "brokerage-a",
            "asset_id": "security:vti",
            "purchase_month_index": 0,
            "cost_basis_per_unit_quanta": 10_000,
            "remaining_quantity_quanta": 50_000_000,
            "quantity_scale": 1_000_000,
        },
        {
            "rollout_index": 0,
            "month_index": 1,
            "lot_id": "allocation_sale_buy_p0_s1_0",
            "agent_id": "alice",
            "account_id": "brokerage-a",
            "asset_id": "security:bnd",
            "purchase_month_index": 0,
            "cost_basis_per_unit_quanta": 10_000,
            "remaining_quantity_quanta": 850_000_000,
            "quantity_scale": 1_000_000,
        },
    ]
    alice_cash = next(
        account["balance"]
        for account in rust["rollouts"][0]["months"][1]["balances"]
        if account["account"]["agent_id"] == "alice" and account["account"]["account_id"] == "checking"
    )
    assert alice_cash == 1_000_000
    buy_entries = [
        entry
        for entry in rust["rollouts"][0]["journal"]
        if entry["cause_id"].startswith("allocation_sale_buy_m0_security:")
    ]
    assert {
        entry["cause_id"]: {
            (posting["account"]["agent_id"], posting["account"]["account_id"]): posting["amount"]
            for posting in entry["postings"]
        }
        for entry in buy_entries
    } == {
        "allocation_sale_buy_m0_security:vti": {
            ("alice", "checking"): -500_000,
            ("alice", "asset-basis:brokerage-a:vti"): 500_000,
        },
        "allocation_sale_buy_m0_security:bnd": {
            ("alice", "checking"): -8_500_000,
            ("alice", "asset-basis:brokerage-a:bnd"): 8_500_000,
        },
    }
    assert all(sum(posting["amount"] for posting in entry["postings"]) == 0 for entry in rust["rollouts"][0]["journal"])


def test_rust_and_jax_match_quiet_band_drift_rebalancing(tmp_path: Path) -> None:
    fixture = _target_allocation_rebalance_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    assert _rust_cash(rust).to_dicts() == (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
        .to_dicts()
    )
    columns = [
        "lot_id",
        "account_id",
        "purchase_month_index",
        "cost_basis_per_unit_quanta",
        "remaining_quantity_quanta",
    ]
    legacy_lots = asset_lots(legacy).filter(pl.col("month_index") == 1).select(columns).sort("lot_id").to_dicts()
    rust_lots = _rust_lots(rust).filter(pl.col("month_index") == 1).select(columns).sort("lot_id").to_dicts()
    assert rust_lots == legacy_lots
    remaining = {row["lot_id"]: row["remaining_quantity_quanta"] for row in rust_lots}
    assert remaining == {
        "a-source-second": 500_000_000,
        "allocation_sale_buy_p0_s0_0": 0,
        "allocation_sale_buy_p0_s1_0": 400_000_000,
        "bond": 100_000_000,
        "z-source-first": 0,
    }
    assert (
        next(
            account["balance"]
            for account in rust["rollouts"][0]["months"][1]["balances"]
            if account["account"]["agent_id"] == "alice" and account["account"]["account_id"] == "checking"
        )
        == 5_000_000
    )
    assert [(row["lot_id"], row["units"]) for row in rust["rollouts"][0]["dispositions"]] == [
        ("z-source-first", 100_000_000),
        ("a-source-second", 300_000_000),
    ]


def test_purchased_lots_join_the_source_pool_after_real_lots_in_fifo_order(tmp_path: Path) -> None:
    fixture = _target_allocation_purchase_then_sale_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_dispositions = (
        legacy.events_log.lot_dispositions.with_columns(
            (pl.col("units_sold") * 1_000_000).round().cast(pl.Int64).alias("units_sold_quanta")
        )
        .filter((pl.col("month_index") == 1) & (pl.col("asset_id") == "security:vti"))
        .select("lot_id", "units_sold_quanta", "cost_basis_consumed_quanta", "proceeds_quanta")
        .sort("lot_id")
        .to_dicts()
    )
    rust_dispositions = sorted(
        [
            {
                "lot_id": disposition["lot_id"],
                "units_sold_quanta": disposition["units"],
                "cost_basis_consumed_quanta": disposition["basis"],
                "proceeds_quanta": disposition["proceeds"],
            }
            for disposition in rust["rollouts"][0]["dispositions"]
            if disposition["month"] == 1 and disposition["asset_id"] == "security:vti"
        ],
        key=lambda row: row["lot_id"],
    )
    assert rust_dispositions == legacy_dispositions
    assert {row["lot_id"]: row["units_sold_quanta"] for row in rust_dispositions} == {
        "allocation_sale_buy_p0_s0_0": 25_000_000,
        "z-source-first": 100_000_000,
        "zz-real-same-month": 800_000_000,
    }

    columns = [
        "lot_id",
        "account_id",
        "purchase_month_index",
        "cost_basis_per_unit_quanta",
        "remaining_quantity_quanta",
    ]
    legacy_slots = (
        asset_lots(legacy)
        .filter((pl.col("month_index") == 2) & pl.col("lot_id").str.starts_with("allocation_sale_buy"))
        .select(columns)
        .sort("lot_id")
        .to_dicts()
    )
    rust_slots = (
        _rust_lots(rust)
        .filter((pl.col("month_index") == 2) & pl.col("lot_id").str.starts_with("allocation_sale_buy"))
        .select(columns)
        .sort("lot_id")
        .to_dicts()
    )
    assert rust_slots == legacy_slots


def test_purchase_slot_only_pool_receives_later_distributions(tmp_path: Path) -> None:
    fixture = _target_allocation_purchase_distribution_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    assert _rust_cash(rust).to_dicts() == (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
        .to_dicts()
    )
    vti_slot = next(
        lot for lot in rust["rollouts"][0]["months"][1]["lots"] if lot["lot_id"] == "allocation_sale_buy_p0_s0_0"
    )
    assert vti_slot["account_id"] == "brokerage-a"
    assert vti_slot["units_remaining"] == 50_000_000
    assert [outcome["amount"] for outcome in rust["rollouts"][0]["distributions"]] == [0, 5_000]
    assert (
        next(
            account["balance"]
            for account in rust["rollouts"][0]["months"][2]["balances"]
            if account["account"]["agent_id"] == "alice" and account["account"]["account_id"] == "checking"
        )
        == 1_005_000
    )


def test_failed_settlement_suppresses_previously_decided_purchases(tmp_path: Path) -> None:
    fixture = _target_allocation_purchase_fixture()
    scenario = fixture["scenario"]
    scenario["accounts"][1]["opening_balance"] = 0
    scenario["recurring_obligations"] = [
        {
            "start_month": 0,
            "end_month": 0,
            "obligation_id": "unfunded-income",
            "obligation_type": "cash_spend",
            "from": {"agent_id": "landlord", "account_id": "checking"},
            "to": {"agent_id": "alice", "account_id": "checking"},
            "amount_due": 1_000_000,
        }
    ]
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    assert rollout_status(legacy).to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 0}
    ]
    rollout = rust["rollouts"][0]
    assert rollout["failed_month"] == 0
    assert not any(entry["cause_id"].startswith("allocation_sale_buy_") for entry in rollout["journal"])
    assert all(lot["units_remaining"] == 0 and lot["basis_remaining"] == 0 for lot in rollout["months"][1]["lots"])


def test_successive_purchases_keep_distinct_months_and_rollout_prices(tmp_path: Path) -> None:
    fixture = _target_allocation_purchase_fixture(purchase_slots=2)
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
        series["snapshots"] = 4
        series["values"] = [10_000] * 4 if series["series_id"] == "security:vti" else [10_000, 20_000, 30_000, 30_000]
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    columns = ["lot_id", "purchase_month_index", "cost_basis_per_unit_quanta", "remaining_quantity_quanta"]
    legacy_slots = (
        asset_lots(legacy)
        .filter((pl.col("month_index") == 3) & pl.col("lot_id").str.starts_with("allocation_sale_buy"))
        .select(columns)
        .sort("lot_id")
        .to_dicts()
    )
    rust_slots = (
        _rust_lots(rust)
        .filter((pl.col("month_index") == 3) & pl.col("lot_id").str.starts_with("allocation_sale_buy"))
        .select(columns)
        .sort("lot_id")
        .to_dicts()
    )
    assert rust_slots == legacy_slots
    bnd_slots = {row["lot_id"]: row for row in rust_slots if "_s1_" in row["lot_id"]}
    assert (
        bnd_slots["allocation_sale_buy_p0_s1_0"]["purchase_month_index"],
        bnd_slots["allocation_sale_buy_p0_s1_0"]["cost_basis_per_unit_quanta"],
    ) == (1, 20_000)
    assert (
        bnd_slots["allocation_sale_buy_p0_s1_1"]["purchase_month_index"],
        bnd_slots["allocation_sale_buy_p0_s1_1"]["cost_basis_per_unit_quanta"],
    ) == (2, 30_000)


def test_rust_and_jax_abort_when_target_allocation_purchase_slots_are_exhausted(tmp_path: Path) -> None:
    fixture = _target_allocation_purchase_fixture(purchase_slots=1)
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
        series["snapshots"] = 4
        series["values"] = [10_000] * 4

    with pytest.raises(ValueError, match="ran out of purchase slots: 1 configured, 2 needed"):
        run_legacy_fixture(fixture)

    fixture_path = tmp_path / "exhausted-purchase-slots.json"
    output_path = tmp_path / "unused.json"
    fixture_path.write_text(json.dumps(fixture, separators=(",", ":")))
    completed = subprocess.run([_binary(), fixture_path, output_path], check=False, capture_output=True, text=True)
    assert completed.returncode == 1
    assert "ran out of purchase slots: 1 configured, 2 needed" in completed.stderr


def test_rust_and_jax_match_insufficient_target_allocation_failure_metadata(tmp_path: Path) -> None:
    fixture = _target_allocation_failure_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    columns = [
        "rollout_index",
        "month_index",
        "cause_id",
        "agent_id",
        "deficit_quanta",
        "obligation_id",
        "obligation_type",
        "amount_due_quanta",
        "amount_paid_quanta",
        "shortfall_quanta",
        "attempted_funding_sources",
    ]
    legacy_failures = legacy.events_log.rollout_failures.select(columns).to_dicts()
    rust_failures = [
        {
            "rollout_index": rollout["rollout_id"],
            "month_index": failure["month"],
            "cause_id": failure["cause_id"],
            "agent_id": failure["agent_id"],
            "deficit_quanta": failure["deficit"],
            "obligation_id": failure["obligation_id"],
            "obligation_type": failure["obligation_type"],
            "amount_due_quanta": failure["amount_due"],
            "amount_paid_quanta": failure["amount_paid"],
            "shortfall_quanta": failure["shortfall"],
            "attempted_funding_sources": failure["attempted_funding_sources"],
        }
        for rollout in rust["rollouts"]
        for failure in rollout["rollout_failures"]
    ]
    assert rust_failures == legacy_failures
    assert rust_failures == [
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
    assert rollout_status(legacy).to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 1}
    ]
    assert rust["rollouts"][0]["failed_month"] == 1
    assert all(sum(posting["amount"] for posting in entry["postings"]) == 0 for entry in rust["rollouts"][0]["journal"])


def test_rebalancing_without_purchase_slots_is_rejected(tmp_path: Path) -> None:
    fixture = _target_allocation_fixture()
    fixture["scenario"]["target_allocation_policies"][0]["rebalance_tolerance_ppb"] = 250_000_000

    with pytest.raises(ValueError, match="no purchase slots"):
        run_legacy_fixture(fixture)

    fixture_path = tmp_path / "rebalance-without-purchase-slots.json"
    output_path = tmp_path / "unused.json"
    fixture_path.write_text(json.dumps(fixture, separators=(",", ":")))
    completed = subprocess.run([_binary(), fixture_path, output_path], check=False, capture_output=True, text=True)
    assert completed.returncode == 1
    assert "invalid configuration" in completed.stderr


def test_rust_and_jax_match_grouped_recurring_obligations(tmp_path: Path) -> None:
    fixture = _recurring_obligation_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()
    assert rollout_status(legacy).to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 1},
        {"rollout_index": 1, "status": "failed_insufficient_cash", "failed_month": 1},
    ]

    columns = [
        "rollout_index",
        "month_index",
        "obligation_id",
        "obligation_type",
        "agent_id",
        "from_account_id",
        "amount_due_quanta",
        "amount_paid_quanta",
        "shortfall_quanta",
    ]
    legacy_outcomes = legacy.events_log.obligation_settlements.select(columns).sort(columns[:3]).to_dicts()
    rust_outcomes = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": outcome["month"],
                "obligation_id": outcome["obligation_id"],
                "obligation_type": outcome["obligation_type"],
                "agent_id": outcome["from"]["agent_id"],
                "from_account_id": outcome["from"]["account_id"],
                "amount_due_quanta": outcome["amount_due"],
                "amount_paid_quanta": outcome["amount_paid"],
                "shortfall_quanta": outcome["shortfall"],
            }
            for rollout in rust["rollouts"]
            for outcome in rollout["obligations"]
        ],
        key=lambda row: (row["rollout_index"], row["month_index"], row["obligation_id"]),
    )
    assert rust_outcomes == legacy_outcomes


def test_rust_and_jax_match_fixed_and_series_indexed_amounts(tmp_path: Path) -> None:
    fixture = _series_indexed_amount_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()

    transfer_causes = {
        "indexed-gift",
        "tagged-fixed-gift",
        "zero-gift",
        "annual-indexed-paycheck",
        "indexed-repair",
        "indexed-property-rent",
    }
    legacy_transfers = (
        legacy.events_log.transfers.filter(pl.col("cause_id").is_in(transfer_causes))
        .select("rollout_index", "month_index", "cause_id", "amount_quanta")
        .sort("rollout_index", "month_index", "cause_id")
        .to_dicts()
    )
    rust_transfers = (
        decode_rust_event_log(rust)
        .transfers.filter(pl.col("cause_id").is_in(transfer_causes))
        .select("rollout_index", "month_index", "cause_id", "amount_quanta")
        .sort("rollout_index", "month_index", "cause_id")
        .to_dicts()
    )
    assert rust_transfers == legacy_transfers

    columns = [
        "rollout_index",
        "month_index",
        "obligation_id",
        "amount_due_quanta",
        "amount_paid_quanta",
        "shortfall_quanta",
    ]
    legacy_outcomes = legacy.events_log.obligation_settlements.select(columns).sort(columns[:3]).to_dicts()
    rust_outcomes = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": outcome["month"],
                "obligation_id": outcome["obligation_id"],
                "amount_due_quanta": outcome["amount_due"],
                "amount_paid_quanta": outcome["amount_paid"],
                "shortfall_quanta": outcome["shortfall"],
            }
            for rollout in rust["rollouts"]
            for outcome in rollout["obligations"]
        ],
        key=lambda row: (row["rollout_index"], row["month_index"], row["obligation_id"]),
    )
    assert rust_outcomes == legacy_outcomes
    assert [row["amount_due_quanta"] for row in rust_outcomes if row["obligation_id"] == "indexed-bill_m2"] == [
        152,
        126,
    ]
    assert [row["amount_due_quanta"] for row in rust_outcomes if row["obligation_id"] == "indexed-rent_m12"] == [
        1_101,
        1_251,
    ]
    assert all(rollout["failed_month"] is None for rollout in rust["rollouts"])
    for rollout in rust["rollouts"]:
        for entry in rollout["journal"]:
            assert sum(posting["amount"] for posting in entry["postings"]) == 0


def test_generated_feature_rich_benchmark_fixture_matches_at_four_rollouts(tmp_path: Path) -> None:
    fixture_path = tmp_path / "benchmark-fixture.json"
    write_fixture(fixture_path, rollout_count=4, horizon_months=60)
    fixture = cast(dict[str, Any], json.loads(fixture_path.read_text()))
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .filter(pl.col("account_id") == "checking")
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()

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


def test_rust_and_jax_match_federal_and_california_tax_accruals(tmp_path: Path) -> None:
    fixture = _tax_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()

    columns = [
        "rollout_index",
        "month_index",
        "agent_id",
        "jurisdiction_id",
        "ordinary_income_quanta",
        "ordinary_taxable_quanta",
        "capital_gain_taxable_quanta",
        "ordinary_tax_quanta",
        "capital_gain_tax_quanta",
        "total_tax_quanta",
    ]
    legacy_accruals = legacy.events_log.tax_breakdowns.select(columns).sort("jurisdiction_id").to_dicts()
    rust_accruals = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": accrual["month"],
                "agent_id": accrual["agent_id"],
                "jurisdiction_id": accrual["jurisdiction_id"],
                "ordinary_income_quanta": accrual["ordinary_income"],
                "ordinary_taxable_quanta": accrual["ordinary_taxable"],
                "capital_gain_taxable_quanta": accrual["long_term_capital_gain_taxable"],
                "ordinary_tax_quanta": accrual["ordinary_tax"],
                "capital_gain_tax_quanta": accrual["capital_gain_tax"],
                "total_tax_quanta": accrual["total_tax"],
            }
            for rollout in rust["rollouts"]
            for accrual in rollout["tax_accruals"]
        ],
        key=lambda row: row["jurisdiction_id"],
    )
    assert rust_accruals == legacy_accruals
    assert [row["total_tax_quanta"] for row in rust_accruals] == [1_475_409, 3_753_851]

    legacy_events = legacy.events_log
    rust_events = decode_rust_event_log(rust)
    for legacy_frame, rust_frame in (
        (legacy_events.tax_accruals, rust_events.tax_accruals),
        (legacy_events.tax_breakdowns, rust_events.tax_breakdowns),
    ):
        sort_columns = ["rollout_index", "month_index", "cause_id", "jurisdiction_id"]
        assert rust_frame.schema == legacy_frame.schema
        assert rust_frame.sort(sort_columns).to_dicts() == legacy_frame.sort(sort_columns).to_dicts()
    for entry in rust["rollouts"][0]["journal"]:
        assert sum(posting["amount"] for posting in entry["postings"]) == 0


def test_rust_and_jax_match_estimated_tax_true_up_and_liability_settlement(tmp_path: Path) -> None:
    fixture = _tax_payment_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()

    tax_types = ["estimated_tax", "tax_true_up"]
    legacy_payments = (
        legacy.events_log.obligation_settlements.filter(pl.col("obligation_type").is_in(tax_types))
        .select(
            "month_index",
            pl.col("obligation_id").alias("cause_id"),
            "obligation_type",
            "amount_due_quanta",
            "amount_paid_quanta",
            "shortfall_quanta",
        )
        .sort("month_index", "cause_id")
        .to_dicts()
    )
    rust_payments = sorted(
        [
            {
                "month_index": payment["month"],
                "cause_id": payment["cause_id"],
                "obligation_type": payment["obligation_type"],
                "amount_due_quanta": payment["amount_due"],
                "amount_paid_quanta": payment["amount_paid"],
                "shortfall_quanta": payment["shortfall"],
            }
            for payment in rust["rollouts"][0]["tax_payments"]
        ],
        key=lambda row: (row["month_index"], row["cause_id"]),
    )
    assert rust_payments == legacy_payments

    legacy_liabilities = (
        tax_liabilities(legacy)
        .filter(pl.col("month_index").is_in([12, 13]))
        .sort("rollout_index", "month_index", "agent_id", "jurisdiction_id", "tax_year_end_month")
    )
    rust_liabilities = _rust_tax_liabilities(rust).filter(pl.col("month_index").is_in([12, 13]))
    assert rust_liabilities.to_dicts() == legacy_liabilities.to_dicts()

    legacy_settlements = (
        legacy.events_log.tax_settlements.select(
            "month_index", "cause_id", "agent_id", "tax_year_end_month", "amount_quanta"
        )
        .sort("month_index", "cause_id")
        .to_dicts()
    )
    rust_settlements = sorted(
        [
            {
                "month_index": settlement["month"],
                "cause_id": settlement["cause_id"],
                "agent_id": settlement["agent_id"],
                "tax_year_end_month": settlement["tax_year_end_month"],
                "amount_quanta": settlement["amount"],
            }
            for settlement in rust["rollouts"][0]["tax_settlements"]
        ],
        key=lambda row: (row["month_index"], row["cause_id"]),
    )
    assert rust_settlements == legacy_settlements

    rust_events = decode_rust_event_log(rust)
    legacy_events = legacy.events_log
    settlement_sort = ["rollout_index", "month_index", "cause_id", "tax_year_end_month"]
    assert rust_events.tax_settlements.schema == legacy_events.tax_settlements.schema
    assert (
        rust_events.tax_settlements.sort(settlement_sort).to_dicts()
        == legacy_events.tax_settlements.sort(settlement_sort).to_dicts()
    )

    tax_causes = {payment["cause_id"] for payment in rust["rollouts"][0]["tax_payments"]}
    transfer_sort = ["rollout_index", "month_index", "cause_id"]
    rust_tax_transfers = rust_events.transfers.filter(pl.col("cause_id").is_in(tax_causes)).sort(transfer_sort)
    legacy_tax_transfers = legacy_events.transfers.filter(pl.col("cause_id").is_in(tax_causes)).sort(transfer_sort)
    assert rust_tax_transfers.schema == legacy_tax_transfers.schema
    assert rust_tax_transfers.to_dicts() == legacy_tax_transfers.to_dicts()
    for entry in rust["rollouts"][0]["journal"]:
        assert sum(posting["amount"] for posting in entry["postings"]) == 0


def test_rust_and_jax_match_unfunded_tax_true_up_failure(tmp_path: Path) -> None:
    fixture = _tax_payment_fixture(funded=False)
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    assert rollout_status(legacy).row(0, named=True)["failed_month"] == 12
    assert rust["rollouts"][0]["failed_month"] == 12
    payment = next(
        payment for payment in rust["rollouts"][0]["tax_payments"] if payment["obligation_type"] == "tax_true_up"
    )
    assert payment["cause_id"] == "alice_tax_true_up_y0"
    assert payment["obligation_type"] == "tax_true_up"
    assert payment["amount_paid"] == 0
    assert payment["shortfall"] == payment["amount_due"]
    assert rust["rollouts"][0]["tax_settlements"] == []
    assert _rust_cash(rust).to_dicts() == (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
        .to_dicts()
    )


def test_rust_and_jax_match_long_term_gain_tax(tmp_path: Path) -> None:
    fixture = _long_term_gain_tax_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_row = legacy.events_log.tax_breakdowns.row(0, named=True)
    rust_row = rust["rollouts"][0]["tax_accruals"][0]
    assert rust_row["ordinary_income"] == legacy_row["ordinary_income_quanta"]
    assert rust_row["long_term_gain"] == legacy_row["ltcg_quanta"] == 2_000_000
    assert rust_row["ordinary_taxable"] == legacy_row["ordinary_taxable_quanta"] == 3_540_004
    assert rust_row["long_term_capital_gain_taxable"] == legacy_row["capital_gain_taxable_quanta"] == 2_000_000
    assert rust_row["ordinary_tax"] == legacy_row["ordinary_tax_quanta"] == 401_600
    assert rust_row["capital_gain_tax"] == legacy_row["capital_gain_tax_quanta"] == 125_626
    assert rust_row["total_tax"] == legacy_row["total_tax_quanta"] == 527_226


def test_rust_and_jax_match_monthly_security_distributions(tmp_path: Path) -> None:
    fixture = _distribution_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()
    assert [[outcome["amount"] for outcome in rollout["distributions"]] for rollout in rust["rollouts"]] == [
        [200, 200, 200],
        [400, 600, 800],
    ]


def test_rust_and_jax_match_distribution_tax_character_slices(tmp_path: Path) -> None:
    fixture = _distribution_tax_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    assert _rust_cash(rust).to_dicts() == (
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
    fixture = _bond_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()

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


def test_rust_and_jax_match_financed_property_purchase_and_first_carry_month(tmp_path: Path) -> None:
    fixture = _financed_property_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()

    legacy_property = property_state(legacy).filter(pl.col("month_index") == 2).row(0, named=True)
    legacy_stake = property_stakes(legacy).filter(pl.col("month_index") == 2).row(0, named=True)
    legacy_mortgage = liabilities(legacy).filter(pl.col("month_index") == 2).row(0, named=True)
    rust_property = rust["rollouts"][0]["months"][2]["properties"][0]
    rust_mortgage = rust["rollouts"][0]["months"][2]["mortgages"][0]
    assert rust_property["property_id"] == legacy_property["property_id"] == "home"
    assert rust_property["location_id"] == legacy_property["location_id"] == "sf"
    assert rust_property["adjusted_basis"] == legacy_property["adjusted_basis_quanta"] == 51_000_000
    assert rust_property["contribution_used"] == legacy_stake["contribution_used_quanta"] == 11_000_000
    assert rust_property["equity_ledger"] == legacy_stake["equity_ledger_quanta"] == 10_000_000
    assert rust_mortgage["liability_id"] == legacy_mortgage["liability_id"] == "home-mortgage"
    assert rust_mortgage["monthly_payment"] == legacy_mortgage["monthly_payment_quanta"] == 239_820
    assert rust_mortgage["principal"] == legacy_mortgage["principal_quanta"] == 39_960_180
    assert rust_mortgage["interest_paid_ytd"] == legacy_mortgage["interest_paid_ytd_quanta"] == 200_000
    assert rust_mortgage["principal_paid_ytd"] == legacy_mortgage["principal_paid_ytd_quanta"] == 39_820

    rust_payment = rust["rollouts"][0]["mortgage_payments"][0]
    legacy_payment = legacy.events_log.mortgage_payments.row(0, named=True)
    assert rust_payment["cause_id"] == legacy_payment["cause_id"] == "home-mortgage_payment_m1"
    assert rust_payment["interest"] == legacy_payment["interest_quanta"] == 200_000
    assert rust_payment["principal"] == legacy_payment["principal_quanta"] == 39_820
    assert rust_payment["total_payment"] == legacy_payment["total_payment_quanta"] == 239_820


def test_rust_and_jax_match_property_cashflows_and_tax_tagging(tmp_path: Path) -> None:
    fixture = _property_cashflow_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()

    legacy_transfers = (
        legacy.events_log.transfers.filter(pl.col("cause_id").is_in(["leasing-fee", "rent", "management-fee"]))
        .group_by("cause_id")
        .agg(pl.len().alias("count"), pl.col("amount_quanta").sum().alias("amount_quanta"))
        .sort("cause_id")
        .to_dicts()
    )
    assert legacy_transfers == [
        {"cause_id": "leasing-fee", "count": 1, "amount_quanta": 100_000},
        {"cause_id": "management-fee", "count": 12, "amount_quanta": 600_000},
        {"cause_id": "rent", "count": 12, "amount_quanta": 6_000_000},
    ]
    rust_causes = [entry["cause_id"] for entry in rust["rollouts"][0]["journal"]]
    assert rust_causes.count("leasing-fee") == 1
    assert rust_causes.count("management-fee") == 12
    assert rust_causes.count("rent") == 12

    legacy_tax = legacy.events_log.tax_breakdowns.row(0, named=True)
    rust_tax = rust["rollouts"][0]["tax_accruals"][0]
    assert rust_tax["ordinary_income"] == legacy_tax["ordinary_income_quanta"] == 5_300_000
    assert rust_tax["total_tax"] == legacy_tax["total_tax_quanta"] == 437_600


def test_rust_and_jax_match_property_cashflow_purchase_and_failure_gates(tmp_path: Path) -> None:
    fixture = _property_cashflow_gating_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()
    assert rollout_status(legacy).to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 2}
    ]
    assert rust["rollouts"][0]["failed_month"] == 2

    legacy_property_cashflows = (
        legacy.events_log.transfers.filter(
            pl.col("cause_id").is_in(["before-purchase", "purchase-month", "property-carry"])
        )
        .select("month_index", "cause_id")
        .sort("month_index", "cause_id")
        .to_dicts()
    )
    assert legacy_property_cashflows == [
        {"month_index": 1, "cause_id": "property-carry"},
        {"month_index": 1, "cause_id": "purchase-month"},
        {"month_index": 2, "cause_id": "property-carry"},
    ]
    rust_property_cashflows = sorted(
        [
            {"month_index": entry["month"], "cause_id": entry["cause_id"]}
            for entry in rust["rollouts"][0]["journal"]
            if entry["cause_id"] in {"before-purchase", "purchase-month", "property-carry"}
        ],
        key=lambda row: (row["month_index"], row["cause_id"]),
    )
    assert rust_property_cashflows == legacy_property_cashflows


def test_rust_and_jax_match_property_sale_lifecycle_and_rollout_values(tmp_path: Path) -> None:
    fixture = _property_sale_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()

    legacy_sales = (
        legacy.events_log.property_sale_events.select(
            "rollout_index",
            "month_index",
            "property_id",
            "gross_proceeds_quanta",
            "mortgage_payoff_quanta",
            "net_cash_to_owner_quanta",
            "realized_gain_quanta",
            "depreciation_recapture_quanta",
            "section_121_exclusion_quanta",
            "long_term_capital_gain_quanta",
        )
        .sort("rollout_index")
        .to_dicts()
    )
    rust_sales = sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": sale["month"],
                "property_id": sale["property_id"],
                "gross_proceeds_quanta": sale["gross_proceeds"],
                "mortgage_payoff_quanta": sale["mortgage_payoff"],
                "net_cash_to_owner_quanta": sale["net_cash_to_owner"],
                "realized_gain_quanta": sale["realized_gain"],
                "depreciation_recapture_quanta": sale["depreciation_recapture"],
                "section_121_exclusion_quanta": sale["section_121_exclusion"],
                "long_term_capital_gain_quanta": sale["long_term_capital_gain"],
            }
            for rollout in rust["rollouts"]
            for sale in rollout["property_sales"]
        ],
        key=lambda row: row["rollout_index"],
    )
    assert rust_sales == legacy_sales
    assert [row["gross_proceeds_quanta"] for row in rust_sales] == [56_400_000, 51_700_000]

    assert property_state(legacy).filter(pl.col("month_index") >= 3).is_empty()
    assert liabilities(legacy).filter(pl.col("month_index") >= 3).is_empty()
    for rollout in rust["rollouts"]:
        post_sale = rollout["months"][3]
        assert post_sale["properties"][0]["active"] is False
        assert post_sale["mortgages"][0]["active"] is False
        assert post_sale["mortgages"][0]["principal"] == 0
        sale_month_causes = [entry["cause_id"] for entry in rollout["journal"] if entry["month"] == 2]
        assert "property-sale:home" in sale_month_causes
        assert "sale-month-generic-transfer" in sale_month_causes
        assert "rent" not in sale_month_causes
        assert "home-mortgage_payment_m2" not in sale_month_causes
        assert "home_property_tax_m2" not in sale_month_causes
        for entry in rollout["journal"]:
            assert sum(posting["amount"] for posting in entry["postings"]) == 0


def test_rust_and_jax_match_primary_residence_events_and_section_121_boundaries(tmp_path: Path) -> None:
    fixture = _section_121_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    primary_sort = ["rollout_index", "month_index", "agent_id"]
    assert rust_events.set_primary_residence_events.schema == legacy.events_log.set_primary_residence_events.schema
    assert (
        rust_events.set_primary_residence_events.sort(primary_sort).to_dicts()
        == legacy.events_log.set_primary_residence_events.sort(primary_sort).to_dicts()
    )

    sale_sort = ["rollout_index", "month_index", "property_id"]
    assert rust_events.property_sale_events.schema == legacy.events_log.property_sale_events.schema
    assert (
        rust_events.property_sale_events.sort(sale_sort).to_dicts()
        == legacy.events_log.property_sale_events.sort(sale_sort).to_dicts()
    )
    assert {
        row["property_id"]: row["section_121_exclusion_quanta"] for row in rust_events.property_sale_events.to_dicts()
    } == {"alice-home": 25_000_000, "bob-home": 0, "carol-home": 0, "dave-home": 0}

    # Snapshot 30 is after month 29 and immediately before the month-30 sales.
    assert legacy.output.state.property_owner_occupied_months[30, :, 0].tolist() == [30, 23, 0, 24]
    rust_month_30 = sorted(rust["rollouts"][0]["months"][30]["properties"], key=lambda row: row["property_id"])
    assert [row["owner_occupied_months"] for row in rust_month_30] == [30, 23, 0, 24]
    assert all(sum(posting["amount"] for posting in entry["postings"]) == 0 for entry in rust["rollouts"][0]["journal"])


def test_rust_and_jax_match_rental_transition_capex_depreciation_and_interest(tmp_path: Path) -> None:
    fixture = _property_depreciation_fixture(sale=False)
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()
    assert legacy.events_log.set_rented_fraction_events.select(
        "month_index", "property_id", "rented_fraction"
    ).to_dicts() == [{"month_index": 6, "property_id": "home", "rented_fraction": 0.5}]
    assert legacy.events_log.capital_improvement_events.select(
        "month_index", "property_id", "amount_quanta", "description"
    ).to_dicts() == [{"month_index": 6, "property_id": "home", "amount_quanta": 1_000_000, "description": ""}]
    rollout = rust["rollouts"][0]
    assert rollout["property_rented_fraction_events"] == [
        {"month": 6, "property_id": "home", "rented_fraction_ppb": 500_000_000}
    ]
    assert rollout["capital_improvements"] == [
        {"month": 6, "property_id": "home", "amount": 1_000_000, "description": ""}
    ]
    # Lifecycle changes apply before this month's depreciation and mortgage split.
    first_depreciation = rollout["months"][7]["properties"][0]["cumulative_depreciation"]
    assert first_depreciation > 0
    assert rollout["months"][6]["properties"][0]["cumulative_depreciation"] == 0

    legacy_tax = legacy.events_log.tax_breakdowns.row(0, named=True)
    rust_tax = rollout["tax_accruals"][0]
    assert rust_tax["ordinary_income"] == legacy_tax["ordinary_income_quanta"]
    assert rust_tax["mortgage_interest_deduction"] == legacy_tax["mortgage_interest_deduction_quanta"]
    assert rust_tax["itemized_deduction"] == legacy_tax["itemized_deduction_quanta"]
    assert rust_tax["ordinary_taxable"] == legacy_tax["ordinary_taxable_quanta"]
    assert rust_tax["total_tax"] == legacy_tax["total_tax_quanta"]
    assert rust_tax["rental_interest_deduction"] > 0
    assert rust_tax["depreciation_deduction"] > 0


def test_rust_and_jax_match_uncapped_acquisition_mortgage_interest(tmp_path: Path) -> None:
    fixture = _uncapped_mortgage_interest_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_tax = legacy.events_log.tax_breakdowns.row(0, named=True)
    rollout = rust["rollouts"][0]
    rust_tax = rollout["tax_accruals"][0]
    total_interest = sum(payment["interest"] for payment in rollout["mortgage_payments"])
    assert rust_tax["mortgage_interest_deduction"] == total_interest
    assert rust_tax["mortgage_interest_deduction"] == legacy_tax["mortgage_interest_deduction_quanta"]
    assert rust_tax["itemized_deduction"] == legacy_tax["itemized_deduction_quanta"]
    assert rust_tax["total_tax"] == legacy_tax["total_tax_quanta"]


def test_rust_and_jax_match_mid_principal_caps_and_home_equity_exclusion(tmp_path: Path) -> None:
    fixture = _mortgage_interest_policy_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    sort_columns = ["rollout_index", "month_index", "agent_id", "jurisdiction_id"]
    assert rust_events.tax_breakdowns.schema == legacy.events_log.tax_breakdowns.schema
    assert (
        rust_events.tax_breakdowns.sort(sort_columns).to_dicts()
        == legacy.events_log.tax_breakdowns.sort(sort_columns).to_dicts()
    )

    breakdowns = {(row["agent_id"], row["jurisdiction_id"]): row for row in rust_events.tax_breakdowns.to_dicts()}
    alice_federal = breakdowns[("alice", "federal_us")]["mortgage_interest_deduction_quanta"]
    alice_california = breakdowns[("alice", "california")]["mortgage_interest_deduction_quanta"]
    assert 0 < alice_federal < alice_california
    assert alice_federal == round(alice_california * 75 / 80)
    assert breakdowns[("bob", "federal_us")]["mortgage_interest_deduction_quanta"] == 0
    assert breakdowns[("bob", "california")]["mortgage_interest_deduction_quanta"] == 0


def test_rust_and_jax_match_federal_salt_property_and_state_tax_caps(tmp_path: Path) -> None:
    fixture = _salt_deduction_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    sort_columns = ["rollout_index", "month_index", "agent_id", "jurisdiction_id"]
    assert rust_events.tax_breakdowns.schema == legacy.events_log.tax_breakdowns.schema
    assert (
        rust_events.tax_breakdowns.sort(sort_columns).to_dicts()
        == legacy.events_log.tax_breakdowns.sort(sort_columns).to_dicts()
    )
    federal = {
        row["month_index"]: row
        for row in rust_events.tax_breakdowns.filter(pl.col("jurisdiction_id") == "federal_us").to_dicts()
    }
    assert 1_000_000 < federal[11]["salt_deduction_quanta"] < 4_000_000
    assert federal[23]["salt_deduction_quanta"] == 1_000_000
    assert federal[11]["itemized_deduction_quanta"] == federal[11]["salt_deduction_quanta"]
    assert federal[23]["itemized_deduction_quanta"] == federal[23]["salt_deduction_quanta"]


def test_rust_and_jax_match_depreciation_recapture_and_jurisdiction_tax(tmp_path: Path) -> None:
    fixture = _property_depreciation_fixture(sale=True)
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)

    legacy_events = legacy.events_log
    rust_events = decode_rust_event_log(rust)
    event_comparisons = (
        (
            legacy_events.transfers,
            rust_events.transfers,
            [
                "rollout_index",
                "month_index",
                "cause_id",
                "from_agent_id",
                "from_account_id",
                "to_agent_id",
                "to_account_id",
            ],
        ),
        (
            legacy_events.property_purchases,
            rust_events.property_purchases,
            ["rollout_index", "month_index", "cause_id", "property_id"],
        ),
        (
            legacy_events.mortgage_originations,
            rust_events.mortgage_originations,
            ["rollout_index", "month_index", "cause_id", "liability_id"],
        ),
        (
            legacy_events.mortgage_payments,
            rust_events.mortgage_payments,
            ["rollout_index", "month_index", "cause_id", "liability_id"],
        ),
        (
            legacy_events.set_rented_fraction_events,
            rust_events.set_rented_fraction_events,
            ["rollout_index", "month_index", "property_id"],
        ),
        (
            legacy_events.capital_improvement_events,
            rust_events.capital_improvement_events,
            ["rollout_index", "month_index", "property_id"],
        ),
        (
            legacy_events.property_sale_events,
            rust_events.property_sale_events,
            ["rollout_index", "month_index", "property_id"],
        ),
    )
    for legacy_frame, rust_frame, sort_columns in event_comparisons:
        assert rust_frame.schema == legacy_frame.schema
        assert rust_frame.sort(sort_columns).to_dicts() == legacy_frame.sort(sort_columns).to_dicts()

    legacy_sales = legacy.events_log.property_sale_events.select(
        "month_index",
        "property_id",
        "gross_proceeds_quanta",
        "mortgage_payoff_quanta",
        "net_cash_to_owner_quanta",
        "realized_gain_quanta",
        "depreciation_recapture_quanta",
        "section_121_exclusion_quanta",
        "long_term_capital_gain_quanta",
    ).to_dicts()
    rust_sale = rust["rollouts"][0]["property_sales"][0]
    assert [
        {
            "month_index": rust_sale["month"],
            "property_id": rust_sale["property_id"],
            "gross_proceeds_quanta": rust_sale["gross_proceeds"],
            "mortgage_payoff_quanta": rust_sale["mortgage_payoff"],
            "net_cash_to_owner_quanta": rust_sale["net_cash_to_owner"],
            "realized_gain_quanta": rust_sale["realized_gain"],
            "depreciation_recapture_quanta": rust_sale["depreciation_recapture"],
            "section_121_exclusion_quanta": rust_sale["section_121_exclusion"],
            "long_term_capital_gain_quanta": rust_sale["long_term_capital_gain"],
        }
    ] == legacy_sales
    assert rust_sale["depreciation_recapture"] > 0

    legacy_tax = legacy.events_log.tax_breakdowns.filter(pl.col("month_index") == 23).sort("jurisdiction_id").to_dicts()
    rust_tax = sorted(
        [row for row in rust["rollouts"][0]["tax_accruals"] if row["month"] == 23],
        key=lambda row: row["jurisdiction_id"],
    )
    assert [row["jurisdiction_id"] for row in rust_tax] == [row["jurisdiction_id"] for row in legacy_tax]
    for rust_row, legacy_row in zip(rust_tax, legacy_tax, strict=True):
        assert rust_row["ordinary_income"] == legacy_row["ordinary_income_quanta"]
        assert rust_row["long_term_gain"] == legacy_row["ltcg_quanta"]
        assert rust_row["ordinary_taxable"] == legacy_row["ordinary_taxable_quanta"]
        assert rust_row["long_term_capital_gain_taxable"] == legacy_row["capital_gain_taxable_quanta"]
        assert rust_row["ordinary_tax"] == legacy_row["ordinary_tax_quanta"]
        assert rust_row["capital_gain_tax"] == legacy_row["capital_gain_tax_quanta"]
        assert rust_row["total_tax"] == legacy_row["total_tax_quanta"]
        assert rust_row["section_1250_recapture"] == rust_sale["depreciation_recapture"]
    rust_tax_by_jurisdiction = {row["jurisdiction_id"]: row for row in rust_tax}
    assert rust_tax_by_jurisdiction["federal_us"]["section_1250_tax"] > 0
    assert rust_tax_by_jurisdiction["california"]["section_1250_tax"] == 0


def test_rust_and_jax_match_shared_capital_loss_carryforward_across_tax_links(tmp_path: Path) -> None:
    fixture = _capital_loss_carryforward_fixture()
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    sort_columns = ["rollout_index", "month_index", "agent_id", "jurisdiction_id"]
    assert rust_events.tax_breakdowns.schema == legacy.events_log.tax_breakdowns.schema
    assert (
        rust_events.tax_breakdowns.sort(sort_columns).to_dicts()
        == legacy.events_log.tax_breakdowns.sort(sort_columns).to_dicts()
    )
    rust_accruals = sorted(rust["rollouts"][0]["tax_accruals"], key=lambda row: (row["month"], row["jurisdiction_id"]))
    assert {row["capital_loss_carryforward"] for row in rust_accruals if row["month"] == 11} == {500_000}
    assert {row["capital_loss_carryforward"] for row in rust_accruals if row["month"] == 23} == {0}
    assert {row["ordinary_income"] for row in rust_accruals if row["month"] == 11} == {-300_000}
    assert {row["long_term_gain"] for row in rust_accruals if row["month"] == 23} == {0}


def _assert_private_equity_parity(fixture: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    legacy_cash = (
        cash_balances(legacy)
        .select("rollout_index", "month_index", "agent_id", "account_id", "balance_quanta")
        .sort("rollout_index", "month_index", "agent_id", "account_id")
    )
    assert _rust_cash(rust).to_dicts() == legacy_cash.to_dicts()

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
    assert _rust_lots(rust).to_dicts() == legacy_lots.to_dicts()

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


def test_rust_and_jax_match_private_equity_protocol_sales_and_opportunities(tmp_path: Path) -> None:
    rust = _assert_private_equity_parity(_private_equity_fixture(), tmp_path)

    final_cash = {
        row["rollout_index"]: row["balance_quanta"]
        for row in _rust_cash(rust).filter(pl.col("month_index") == 3).to_dicts()
    }
    assert final_cash == {0: 250_000, 1: 500_000, 2: 300_000, 3: 10_000}


def test_rust_and_jax_match_private_equity_protocol_without_owner_policy(tmp_path: Path) -> None:
    fixture = _private_equity_fixture()
    fixture["scenario"]["private_equity_tender_policies"] = []
    rust = _assert_private_equity_parity(fixture, tmp_path)
    rust_events = decode_rust_event_log(rust)

    assert rust_events.lot_dispositions.is_empty()
    assert set(rust_events.private_equity_opportunities.get_column("outcome")) == {"no_policy"}
    assert {
        row["rollout_index"]: row["balance_quanta"]
        for row in _rust_cash(rust).filter(pl.col("month_index") == 3).to_dicts()
    } == dict.fromkeys(range(4), 0)


def test_rust_and_jax_match_private_equity_floor_satisfied_before_voluntary_sales(tmp_path: Path) -> None:
    fixture = _private_equity_fixture()
    fixture["scenario"]["accounts"][0]["opening_balance"] = 600_000
    rust = _assert_private_equity_parity(fixture, tmp_path)
    dispositions = decode_rust_event_log(rust).lot_dispositions

    assert not any(
        cause.startswith(("pe_tender_", "pe_public_market_")) for cause in dispositions.get_column("cause_id")
    )
    assert {
        row["rollout_index"]: row["balance_quanta"]
        for row in _rust_cash(rust).filter(pl.col("month_index") == 3).to_dicts()
    } == {0: 600_000, 1: 600_000, 2: 900_000, 3: 610_000}


def test_rust_and_jax_match_private_equity_disposition_tax_facts(tmp_path: Path) -> None:
    rust = _assert_private_equity_parity(_private_equity_tax_fixture(), tmp_path)
    [accrual] = rust["rollouts"][0]["tax_accruals"]

    assert accrual["short_term_gain"] == 0
    assert accrual["long_term_gain"] == 9_840_000
    assert accrual["capital_gain_tax"] == 770_625


def _assert_tlh_parity(fixture: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    legacy = run_legacy_fixture(fixture)
    rust = _rust_run(fixture, tmp_path)
    legacy_lookup = {
        (row["rollout_index"], row["month_index"], row["agent_id"], row["classification"]): row["gain_quanta"]
        for row in capital_gains_ytd(legacy).to_dicts()
    }
    rust_gains = _rust_capital_gains(rust)
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


def test_rust_and_jax_match_tlh_harvest_paths_and_year_end_tax(tmp_path: Path) -> None:
    rust = _assert_tlh_parity(_tlh_fixture(), tmp_path)

    drawdown = rust["rollouts"][0]["months"][3]["tlh_cumulative_harvest"][0]
    flat = rust["rollouts"][1]["months"][3]["tlh_cumulative_harvest"][0]
    assert drawdown > flat > 0


def test_rust_and_jax_match_tlh_partial_sale_give_back(tmp_path: Path) -> None:
    rust = _assert_tlh_parity(_tlh_fixture(partial_sales=True), tmp_path)

    assert rust["rollouts"][0]["months"][8]["tlh_cumulative_harvest"] == [0]


def test_rust_and_jax_match_tlh_same_month_sales_share_pre_sale_ledger(tmp_path: Path) -> None:
    rust = _assert_tlh_parity(_tlh_fixture(same_month_sales=True), tmp_path)

    assert rust["rollouts"][0]["months"][8]["tlh_cumulative_harvest"] == [0]


def test_rust_and_jax_match_tlh_target_allocation_sale_give_back(tmp_path: Path) -> None:
    rust = _assert_tlh_parity(_tlh_fixture(target_allocation_sale=True), tmp_path)

    dispositions = decode_rust_event_log(rust).lot_dispositions
    assert dispositions.filter(pl.col("cause_id").str.starts_with("allocation_sale_m1_security:sp500")).height == 1
    assert rust["rollouts"][0]["months"][2]["tlh_cumulative_harvest"][0] > 0


def test_rust_and_jax_match_tlh_failure_suppression(tmp_path: Path) -> None:
    rust = _assert_tlh_parity(_tlh_fixture(failure_after_first_harvest=True), tmp_path)

    for rollout in rust["rollouts"]:
        assert rollout["failed_month"] == 1
        assert rollout["months"][1]["tlh_cumulative_harvest"][0] > 0
        assert all(snapshot["tlh_cumulative_harvest"] == [0] for snapshot in rollout["months"][2:])


@pytest.mark.parametrize("rollout_count", [1, 17])
def test_fixture_contains_no_floating_point_numbers(rollout_count: int) -> None:
    fixture = _fixture()
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
