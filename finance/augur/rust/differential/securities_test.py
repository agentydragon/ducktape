"""Rust/JAX differential coverage for security distributions and held-to-maturity nominal
bonds and TIPS.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from typing import Any

import polars as pl
import pytest_bazel

from finance.augur.rust.differential.backend import assert_backends_agree
from finance.augur.rust.differential.fixtures import shared_integer_fixture, tax_fixture


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


def test_backends_agree_on_monthly_security_distributions() -> None:
    """A distribution pays on the units held that month, so a growing position grows it."""

    result = assert_backends_agree(distribution_fixture())

    by_rollout = result.distributions.sort("rollout_index", "month_index")
    assert by_rollout.filter(pl.col("rollout_index") == 0).get_column("amount_quanta").to_list() == [200, 200, 200]
    assert by_rollout.filter(pl.col("rollout_index") == 1).get_column("amount_quanta").to_list() == [400, 600, 800]


def test_backends_agree_on_distribution_tax_character_slices() -> None:
    """Each issuer slice is routed through its jurisdiction's interest-exemption policy."""

    assert_backends_agree(distribution_tax_fixture())


def test_backends_agree_on_nominal_bonds_tips_and_issuer_tax_routing() -> None:
    result = assert_backends_agree(bond_fixture())
    flows = result.bond_cashflows

    def flow(rollout: int, bond_id: str, month: int) -> dict[str, Any]:
        return flows.filter(
            (pl.col("rollout_index") == rollout) & (pl.col("bond_id") == bond_id) & (pl.col("month_index") == month)
        ).to_dicts()[0]

    # Treasury and muni coupons carry their issuer; a corporate bond has none.
    assert flow(0, "treasury", 5)["coupon_quanta"] == 250_000
    assert flow(0, "treasury", 5)["issuer_jurisdiction_id"] == "federal_us"
    assert flow(0, "california-muni", 5)["coupon_quanta"] == 200_000
    assert flow(0, "corporate", 5)["coupon_quanta"] == 150_000
    assert flow(0, "corporate", 5)["issuer_jurisdiction_id"] is None

    # TIPS: phantom accretion while CPI rises, par redemption at maturity.
    assert flow(0, "tips", 6)["accretion_quanta"] == 10_000_000
    assert flow(0, "tips", 6)["issuer_jurisdiction_id"] == "federal_us"
    assert flow(0, "tips", 11)["coupon_quanta"] == 400_000
    assert flow(0, "tips", 11)["redemption_quanta"] == 20_000_000
    assert flow(1, "tips", 6)["accretion_quanta"] == 5_000_000
    assert flow(1, "tips", 11)["redemption_quanta"] == 15_000_000
    # A deflating path accretes negatively but the deflation floor holds redemption at par.
    assert flow(2, "tips", 6)["accretion_quanta"] == -2_000_000
    assert flow(2, "tips", 11)["coupon_quanta"] == 160_000
    assert flow(2, "tips", 11)["redemption_quanta"] == 10_000_000

    # Rounding edges of the once-per-period rational coupon.
    rounding_up = flows.filter((pl.col("rollout_index") == 0) & (pl.col("bond_id") == "rounding-up"))
    assert rounding_up.get_column("coupon_quanta").to_list() == [1] * 12
    assert flow(0, "rounding-down", 11)["coupon_quanta"] == 0
    assert [flow(0, "rounding-five-month", month)["coupon_quanta"] for month in (1, 6, 11)] == [19_280] * 3

    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


if __name__ == "__main__":
    pytest_bazel.main()
