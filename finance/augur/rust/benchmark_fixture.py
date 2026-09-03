"""Generate the deterministic shared Rust/JAX feature-rich benchmark fixture.

The fixture is generated outside timed regions. It intentionally combines
independent agents so the benchmark exercises the supported financial policy
surface without one policy family consuming another's liquidity. Large series
are streamed directly to JSON to avoid constructing a second Python copy.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

RATE_SCALE = 1_000_000_000
MIN_FEATURE_HORIZON_MONTHS = 60


@dataclass(frozen=True)
class _Series:
    series_id: str
    value_at: Callable[[int, int], int]


def _account(agent_id: str, opening_balance: int) -> dict[str, Any]:
    return {"account": {"agent_id": agent_id, "account_id": "checking"}, "opening_balance": opening_balance}


def _federal_rules() -> dict[str, Any]:
    return {
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
        "exempt_interest_from_levels": ["state"],
        "exempts_own_issue": False,
        "section_1250_rate_ppb": 250_000_000,
    }


def _california_rules() -> dict[str, Any]:
    return {
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
        "exempt_interest_from_levels": ["federal"],
        "exempts_own_issue": True,
        "section_1250_rate_ppb": 0,
    }


def _tax_profile(
    agent_id: str, *, federal_only: bool = False, prior_year_tax: int = 0, section_121_exclusion: int = 0
) -> dict[str, Any]:
    jurisdictions = [_federal_rules()]
    if not federal_only:
        jurisdictions.append(_california_rules())
    return {
        "agent_id": agent_id,
        "tax_authority_agent_id": "irs",
        "prior_year_tax": prior_year_tax,
        "section_121_exclusion": section_121_exclusion,
        "jurisdictions": jurisdictions,
    }


def _series_indexed(base_amount: int, series_id: str, *, adjustment_period_months: int = 1) -> dict[str, Any]:
    return {
        "kind": "series_indexed",
        "base_amount": base_amount,
        "series_id": series_id,
        "base_month_index": 0,
        "adjustment_period_months": adjustment_period_months,
    }


def _scenario(horizon_months: int) -> dict[str, Any]:
    if horizon_months < MIN_FEATURE_HORIZON_MONTHS:
        raise ValueError(f"feature-rich benchmark requires at least {MIN_FEATURE_HORIZON_MONTHS} months")
    return {
        "horizon_months": horizon_months,
        "jurisdictions": [
            {"jurisdiction_id": "federal_us", "level": "federal"},
            {"jurisdiction_id": "california", "level": "state"},
        ],
        "locations": [
            {
                "location_id": "sf",
                "display_name": "San Francisco",
                "jurisdiction_ids": ["federal_us", "california"],
                "annual_property_tax_rate_ppb": 11_800_000,
                "annual_special_assessment": 25_000,
            }
        ],
        "accounts": [
            _account("payroll", 0),
            _account("cashflow", 5_000_000),
            _account("allocator", 15_000_000),
            _account("bondholder", 5_000_000),
            _account("homeowner", 60_000_000),
            _account("pe_owner", 100_000),
            _account("tlh_owner", 1_000_000),
            {"account": {"agent_id": "tlh_owner", "account_id": "brokerage"}, "opening_balance": 0},
            _account("landlord", 0),
            _account("vendor", 0),
            _account("seller", 0),
            _account("bank", 0),
            _account("county", 0),
            _account("tenant", 50_000_000),
            _account("manager", 0),
            _account("irs", 0),
        ],
        "scheduled_transfers": [
            {
                "month": 2,
                "cause_id": "indexed-bonus",
                "from": {"agent_id": "payroll", "account_id": "checking"},
                "to": {"agent_id": "cashflow", "account_id": "checking"},
                "amount": _series_indexed(100_000, "inflation"),
                "income_category": "ordinary",
            },
            {
                "month": 18,
                "cause_id": "allocator-windfall",
                "from": {"agent_id": "payroll", "account_id": "checking"},
                "to": {"agent_id": "allocator", "account_id": "checking"},
                "amount": 12_000_000,
                "income_category": "ordinary",
            },
            {
                "month": 10,
                "cause_id": "cashflow-charity",
                "from": {"agent_id": "cashflow", "account_id": "checking"},
                "to": {"agent_id": "vendor", "account_id": "checking"},
                "amount": 200_000,
                "deduction_category": "ordinary",
            },
        ],
        "recurring_transfers": [
            {
                "start_month": 0,
                "end_month": horizon_months - 1,
                "cause_id": "cashflow-paycheck",
                "from": {"agent_id": "payroll", "account_id": "checking"},
                "to": {"agent_id": "cashflow", "account_id": "checking"},
                "amount": _series_indexed(800_000, "rent:sf", adjustment_period_months=12),
                "income_category": "ordinary",
            },
            {
                "start_month": 0,
                "end_month": horizon_months - 1,
                "cause_id": "allocator-contribution",
                "from": {"agent_id": "payroll", "account_id": "checking"},
                "to": {"agent_id": "allocator", "account_id": "checking"},
                "amount": 500_000,
                "income_category": "ordinary",
            },
            {
                "start_month": 0,
                "end_month": horizon_months - 1,
                "cause_id": "homeowner-paycheck",
                "from": {"agent_id": "payroll", "account_id": "checking"},
                "to": {"agent_id": "homeowner", "account_id": "checking"},
                "amount": 1_500_000,
                "income_category": "ordinary",
            },
        ],
        "obligations": [
            {
                "month": 6,
                "obligation_id": "allocator-large-expense",
                "obligation_type": "cash_spend",
                "from": {"agent_id": "allocator", "account_id": "checking"},
                "to": {"agent_id": "vendor", "account_id": "checking"},
                "amount_due": 12_000_000,
            },
            {
                "month": 20,
                "obligation_id": "indexed-repair-bill",
                "obligation_type": "cash_spend",
                "from": {"agent_id": "cashflow", "account_id": "checking"},
                "to": {"agent_id": "vendor", "account_id": "checking"},
                "amount_due": _series_indexed(250_000, "inflation"),
            },
        ],
        "recurring_obligations": [
            {
                "start_month": 0,
                "end_month": horizon_months - 1,
                "obligation_id": "living-cost",
                "obligation_type": "cash_spend",
                "from": {"agent_id": "cashflow", "account_id": "checking"},
                "to": {"agent_id": "landlord", "account_id": "checking"},
                "amount_due": _series_indexed(300_000, "rent:sf", adjustment_period_months=12),
            }
        ],
        "initial_lots": [
            {
                "lot_id": "allocator-vti-old",
                "agent_id": "allocator",
                "account_id": "brokerage-a",
                "asset_id": "vti",
                "purchase_month": -24,
                "quantity_scale": 1_000_000,
                "units": 2_000_000_000,
                "basis": 16_000_000,
            },
            {
                "lot_id": "allocator-bnd-old",
                "agent_id": "allocator",
                "account_id": "brokerage-b",
                "asset_id": "bnd",
                "purchase_month": -18,
                "quantity_scale": 1_000_000,
                "units": 1_000_000_000,
                "basis": 8_000_000,
            },
            {
                "lot_id": "bondholder-bnd-fund",
                "agent_id": "bondholder",
                "account_id": "brokerage",
                "asset_id": "bnd",
                "purchase_month": -24,
                "quantity_scale": 1_000_000,
                "units": 1_000_000_000,
                "basis": 8_000_000,
            },
            {
                "lot_id": "pe-acme-old",
                "agent_id": "pe_owner",
                "account_id": "private",
                "asset_id": "private_equity:acme",
                "purchase_month": -36,
                "quantity_scale": 1_000_000,
                "units": 40_000_000,
                "basis": 40_000,
            },
            {
                "lot_id": "pe-acme-new",
                "agent_id": "pe_owner",
                "account_id": "private",
                "asset_id": "private_equity:acme",
                "purchase_month": -12,
                "quantity_scale": 1_000_000,
                "units": 60_000_000,
                "basis": 120_000,
            },
            {
                "lot_id": "tlh-sp500",
                "agent_id": "tlh_owner",
                "account_id": "brokerage",
                "asset_id": "sp500",
                "purchase_month": 0,
                "quantity_scale": 1_000_000,
                "units": 1_000_000_000,
                "basis": 100_000,
            },
        ],
        "initial_bonds": [
            {
                "bond_id": "treasury",
                "agent_id": "bondholder",
                "account_id": "checking",
                "issuer_jurisdiction_id": "federal_us",
                "face_value": 10_000_000,
                "purchase_price": 10_000_000,
                "annual_coupon_rate_ppb": 50_000_000,
                "coupon_period_months": 6,
                "purchase_month_index": -1,
                "maturity_month_index": 35,
            },
            {
                "bond_id": "california-muni",
                "agent_id": "bondholder",
                "account_id": "checking",
                "issuer_jurisdiction_id": "california",
                "face_value": 10_000_000,
                "purchase_price": 10_000_000,
                "annual_coupon_rate_ppb": 40_000_000,
                "coupon_period_months": 6,
                "purchase_month_index": -1,
                "maturity_month_index": 47,
            },
            {
                "bond_id": "corporate",
                "agent_id": "bondholder",
                "account_id": "checking",
                "face_value": 10_000_000,
                "purchase_price": 10_000_000,
                "annual_coupon_rate_ppb": 30_000_000,
                "coupon_period_months": 6,
                "purchase_month_index": -1,
                "maturity_month_index": horizon_months - 1,
            },
            {
                "bond_id": "tips",
                "agent_id": "bondholder",
                "account_id": "checking",
                "issuer_jurisdiction_id": "federal_us",
                "face_value": 10_000_000,
                "purchase_price": 10_000_000,
                "annual_coupon_rate_ppb": 40_000_000,
                "coupon_period_months": 6,
                "inflation_indexed": True,
                "purchase_month_index": -1,
                "maturity_month_index": horizon_months - 1,
            },
        ],
        "scheduled_sales": [
            {
                "month": 24,
                "cause_id": "tlh-half-sale",
                "agent_id": "tlh_owner",
                "account_id": "brokerage",
                "asset_id": "sp500",
                "units": 500_000_000,
                "proceeds_account_id": "checking",
            },
            {
                "month": 36,
                "cause_id": "tlh-final-sale",
                "agent_id": "tlh_owner",
                "account_id": "brokerage",
                "asset_id": "sp500",
                "units": 500_000_000,
                "proceeds_account_id": "checking",
            },
            {
                "month": 42,
                "cause_id": "allocator-explicit-sale",
                "agent_id": "allocator",
                "account_id": "brokerage-a",
                "asset_id": "vti",
                "units": 100_000_000,
                "proceeds_account_id": "checking",
            },
        ],
        "tax_profiles": [
            _tax_profile("bondholder"),
            _tax_profile("homeowner", prior_year_tax=1_000_000, section_121_exclusion=25_000_000),
            _tax_profile("pe_owner", federal_only=True),
            _tax_profile("tlh_owner", federal_only=True),
        ],
        "distributions": [
            {
                "agent_id": "allocator",
                "holding_account_id": "brokerage-b",
                "asset_id": "bnd",
                "to_account_id": "checking",
                "tax_character": [
                    {"fraction_ppb": 400_000_000, "issuer_jurisdiction_id": "federal_us"},
                    {"fraction_ppb": 600_000_000},
                ],
            },
            {
                "agent_id": "bondholder",
                "holding_account_id": "brokerage",
                "asset_id": "bnd",
                "to_account_id": "checking",
                "tax_character": [
                    {"fraction_ppb": 600_000_000, "issuer_jurisdiction_id": "california"},
                    {"fraction_ppb": 400_000_000},
                ],
            },
        ],
        "target_allocation_policies": [
            {
                "agent_id": "allocator",
                "account_id": "checking",
                "source_account_ids": ["brokerage-a", "brokerage-b"],
                "sleeves": [
                    {"asset_id": "vti", "weight": 3, "quantity_scale": 1_000_000},
                    {"asset_id": "bnd", "weight": 2, "quantity_scale": 1_000_000},
                ],
                "cash_floor": _series_indexed(2_000_000, "inflation"),
                "cash_ceiling": 4_000_000,
                "cause_id_prefix": "benchmark-allocation",
                "purchase_slots_per_sleeve": 128,
                "rebalance_tolerance_ppb": 100_000_000,
            }
        ],
        "private_equity_tender_policies": [
            {
                "owner_agent_id": "pe_owner",
                "proceeds_account_id": "checking",
                "liquid_net_worth_floor": _series_indexed(5_000_000, "inflation"),
            }
        ],
        "harvest_policies": [
            {
                "owner_agent_id": "tlh_owner",
                "account_id": "brokerage",
                "asset_id": "sp500",
                "peak_annual_yield_ppb": 120_000_000,
                "floor_annual_yield_ppb": 4_000_000,
                "maturity_decay_exponent_ppb": 1_500_000_000,
                "drawdown_sensitivity_ppb": 6_000_000_000,
                "short_term_fraction_ppb": RATE_SCALE,
            }
        ],
        "scheduled_property_purchases": [
            {
                "month": 0,
                "cause_id": "homeowner-buys-home",
                "property_id": "home",
                "location_id": "sf",
                "buyer_agent_id": "homeowner",
                "buyer_account_id": "checking",
                "seller_agent_id": "seller",
                "seller_account_id": "checking",
                "purchase_price": 50_000_000,
                "down_payment": 10_000_000,
                "buyer_closing_cost": 1_000_000,
                "rented_fraction_ppb": 0,
                "land_value_fraction_ppb": 200_000_000,
                "mortgage": {
                    "liability_id": "home-mortgage",
                    "lender_agent_id": "bank",
                    "lender_account_id": "checking",
                    "principal": 40_000_000,
                    "annual_interest_rate_ppb": 60_000_000,
                    "term_months": 360,
                },
            }
        ],
        "initial_primary_residences": [{"agent_id": "homeowner", "property_id": "home"}],
        "primary_residence_events": [{"month": 36, "agent_id": "homeowner", "property_id": None}],
        "property_rented_fraction_events": [{"month": 24, "property_id": "home", "rented_fraction_ppb": 500_000_000}],
        "capital_improvement_events": [
            {"month": 24, "property_id": "home", "amount": 1_000_000, "description": "new roof"}
        ],
        "property_sales": [{"month": 48, "property_id": "home", "closing_cost_bps": 600}],
        "scheduled_property_cashflows": [
            {
                "month": 0,
                "property_id": "home",
                "cause_id": "leasing-fee",
                "from": {"agent_id": "homeowner", "account_id": "checking"},
                "to": {"agent_id": "manager", "account_id": "checking"},
                "amount": 100_000,
                "deduction_category": "ordinary",
            },
            {
                "month": 24,
                "property_id": "home",
                "cause_id": "indexed-property-repair",
                "from": {"agent_id": "homeowner", "account_id": "checking"},
                "to": {"agent_id": "manager", "account_id": "checking"},
                "amount": _series_indexed(250_000, "inflation"),
                "deduction_category": "ordinary",
            },
        ],
        "recurring_property_cashflows": [
            {
                "start_month": 0,
                "end_month": 47,
                "property_id": "home",
                "cause_id": "property-rent",
                "from": {"agent_id": "tenant", "account_id": "checking"},
                "to": {"agent_id": "homeowner", "account_id": "checking"},
                "amount": _series_indexed(500_000, "rent:sf", adjustment_period_months=12),
                "income_category": "ordinary",
            },
            {
                "start_month": 0,
                "end_month": 47,
                "property_id": "home",
                "cause_id": "management-fee",
                "from": {"agent_id": "homeowner", "account_id": "checking"},
                "to": {"agent_id": "manager", "account_id": "checking"},
                "amount": 50_000,
                "deduction_category": "ordinary",
            },
        ],
        "mortgage_interest_deduction_policies": [
            {
                "liability_id": "home-mortgage",
                "owner_agent_id": "homeowner",
                "debt_class": "acquisition",
                "per_jurisdiction_principal_cap": {"federal_us": 75_000_000, "california": 100_000_000},
            }
        ],
        "property_tax_policies": [
            {
                "property_id": "home",
                "owner_agent_id": "homeowner",
                "from_account_id": "checking",
                "tax_authority_agent_id": "county",
                "tax_authority_account_id": "checking",
                "annual_tax_rate_ppb": 12_000_000,
                "start_month": 0,
                "end_month": 48,
            }
        ],
        "federal_salt_deduction_policies": [
            {
                "profile_id": "homeowner",
                "federal_jurisdiction_id": "federal_us",
                "cap_schedule": [
                    {"effective_year_index": 0, "cap": 4_000_000},
                    {"effective_year_index": 1, "cap": 1_000_000},
                ],
            }
        ],
    }


def _inflation_value(_rollout: int, month: int) -> int:
    return 1_000_000_000 + 20_000_000 * min(month // 12, 4) + 1_000_000 * (month % 12)


def _rent_value(rollout: int, month: int) -> int:
    return 1_000_000_000 + 35_000_000 * min(month // 12, 4) + 2_000_000 * (rollout % 3)


def _vti_value(_rollout: int, month: int) -> int:
    base = 10_000
    if month >= 12:
        base = 14_000
    if month >= 24:
        base = 9_000
    if month >= 36:
        base = 16_000
    return base


def _bnd_value(rollout: int, month: int) -> int:
    return 8_000 + 25 * ((rollout + month // 12) % 4)


def _bnd_distribution_value(rollout: int, month: int) -> int:
    return 20 + (rollout % 3) + month // 24


def _sp500_value(_rollout: int, month: int) -> int:
    if month < 6:
        base = 100
    elif month < 18:
        base = 80
    elif month < 30:
        base = 95
    elif month < 42:
        base = 120
    else:
        base = 130
    return base


def _home_value(rollout: int, month: int) -> int:
    if month < 24:
        base = 50_000_000
    elif month < 48:
        base = 60_000_000
    else:
        base = 72_000_000
    return base + 1_000_000 * (rollout % 4)


def _pe_regime(rollout: int, month: int) -> int:
    path = rollout % 4
    if path == 1 and month >= 12:
        return 2
    if path == 0 and month >= 30:
        return 3
    if path == 1 and month >= 30:
        return 4
    return 1


def _pe_event_kind(rollout: int, month: int) -> int:
    path = rollout % 4
    if path == 0:
        return {6: 1, 18: 1, 30: 4}.get(month, 0)
    if path == 1:
        return {12: 3, 30: 7}.get(month, 0)
    if path == 2:
        return {18: 5, 24: 2}.get(month, 0)
    return 6 if month == 24 else 0


def _pe_sale_opportunity(rollout: int, month: int) -> int:
    return int(rollout % 4 == 0 and month in {6, 18})


def _pe_sale_capacity(rollout: int, month: int) -> int:
    if rollout % 4 == 0 and month == 6:
        return 250_000_000
    return RATE_SCALE


def _pe_forced_sale(rollout: int, month: int) -> int:
    return 300_000_000 if rollout % 4 == 2 and month == 18 else 0


def _pe_liquidity_blocked(rollout: int, month: int) -> int:
    return int(rollout % 4 == 0 and month == 18)


def _pe_forced_recovery(rollout: int, month: int) -> int:
    return 1_000_000 if rollout % 4 == 3 and month == 24 else 0


def _series() -> list[_Series]:
    return [
        _Series("inflation", _inflation_value),
        _Series("rent:sf", _rent_value),
        _Series("security:vti", _vti_value),
        _Series("security:bnd", _bnd_value),
        _Series("security_distribution:bnd", _bnd_distribution_value),
        _Series("security:sp500", _sp500_value),
        _Series("home_value:sf", _home_value),
        _Series("private_equity_mark:acme", lambda _rollout, _month: 10_000),
        _Series("private_equity_regime:acme", _pe_regime),
        _Series("private_equity_event_kind:acme", _pe_event_kind),
        _Series("private_equity_sale_opportunity:acme", _pe_sale_opportunity),
        _Series("private_equity_sale_capacity:acme", _pe_sale_capacity),
        _Series("private_equity_eligible:acme", lambda _rollout, _month: RATE_SCALE),
        _Series("private_equity_forced_sale:acme", _pe_forced_sale),
        _Series("private_equity_liquidity_blocked:acme", _pe_liquidity_blocked),
        _Series("private_equity_forced_recovery:acme", _pe_forced_recovery),
        _Series("private_equity_company_valuation:acme", lambda _rollout, _month: 0),
    ]


def _write_values(file: TextIO, *, rollout_count: int, snapshots: int, value_at: Callable[[int, int], int]) -> None:
    first = True
    chunk: list[str] = []
    for rollout in range(rollout_count):
        for snapshot in range(snapshots):
            chunk.append(str(value_at(rollout, snapshot)))
            if len(chunk) == 4_096:
                if not first:
                    file.write(",")
                file.write(",".join(chunk))
                first = False
                chunk.clear()
    if chunk:
        if not first:
            file.write(",")
        file.write(",".join(chunk))


def write_fixture(path: Path, *, rollout_count: int, horizon_months: int) -> None:
    if rollout_count <= 0:
        raise ValueError("rollout_count must be positive")
    scenario = _scenario(horizon_months)
    snapshots = horizon_months + 1
    header = {
        "schema_version": 8,
        "currency_code": "USD",
        "currency_quantum": "0.01",
        "rollout_count": rollout_count,
        "scenario": scenario,
    }
    with path.open("w") as file:
        file.write(json.dumps(header, separators=(",", ":"))[:-1])
        file.write(',"series":[')
        for index, series in enumerate(_series()):
            if index:
                file.write(",")
            file.write(json.dumps({"series_id": series.series_id, "snapshots": snapshots}, separators=(",", ":"))[:-1])
            file.write(',"values":[')
            _write_values(file, rollout_count=rollout_count, snapshots=snapshots, value_at=series.value_at)
            file.write("]}")
        file.write("]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rollouts", type=int, default=2_000)
    parser.add_argument("--horizon-months", type=int, default=60)
    args = parser.parse_args()
    write_fixture(args.output, rollout_count=args.rollouts, horizon_months=args.horizon_months)


if __name__ == "__main__":
    main()
