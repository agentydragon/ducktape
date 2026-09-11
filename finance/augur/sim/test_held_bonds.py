"""Coupon, index accretion and redemption have distinct cash, income and observation clocks."""

from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim.accounting import Accounting
from finance.augur.sim.held_bonds import HeldBonds
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    CompiledRun,
    PreparedBond,
    PreparedFixedAmount,
    PreparedIndexedCoupon,
    PreparedSeries,
)
from finance.augur.sim.scenario import InterestIncome
from finance.augur.sim.testing.accounting import CASH, HOUSEHOLD, prepared_scenario


@pytest.fixture
def nominal() -> PreparedBond:
    return PreparedBond(
        bond_id="test_bond",
        agent_id=HOUSEHOLD,
        account_id="checking",
        issuer_jurisdiction_id=None,
        face_value=1000,
        purchase_price=1000,
        coupon=PreparedFixedAmount(amount=10),
        coupon_period_months=1,
        purchase_month_index=0,
        maturity_month_index=2,
    )


def bond_books(bond: PreparedBond, levels: tuple[int, ...]) -> tuple[Accounting, HeldBonds]:
    scenario = replace(prepared_scenario(), horizon_months=len(levels) - 1, initial_bonds=(bond,))
    accounting = Accounting(scenario.accounts, scenario.tax_profiles, scenario.income_sources, capture="forensic")
    run = CompiledRun(
        currency_code="USD",
        currency_quantum="0.01",
        rollout_count=1,
        scenario=scenario,
        series=(PreparedSeries(series_id="inflation", snapshots=len(levels), values=levels),),
    )
    return accounting, HeldBonds((bond,), MarketPath(run, 0))


def test_no_month_zero_coupon_and_redemption_keeps_the_maturity_coupon(nominal: PreparedBond) -> None:
    accounting, bonds = bond_books(nominal, (100, 100, 100, 100))
    assert bonds.snapshots(0, 0)[0].principal == 1000
    bonds.advance(accounting, 0)
    assert not bonds.cashflows
    bonds.advance(accounting, 1)
    assert accounting.ledger.balance(CASH) == 110
    assert bonds.snapshots(2, 2)[0].active
    bonds.advance(accounting, 2)
    assert accounting.ledger.balance(CASH) == 1120
    assert bonds.cashflows[-1].coupon == 10
    assert bonds.cashflows[-1].redemption == 1000
    assert not bonds.snapshots(3, 2)[0].active
    assert accounting.tax.income.by_source[HOUSEHOLD, InterestIncome()] == 20
    assert accounting.ledger.trial_balance() == 0


def test_tips_deflation_changes_income_but_redemption_has_a_face_floor(nominal: PreparedBond) -> None:
    bond = replace(nominal, coupon=PreparedIndexedCoupon(annual_rate_ppb=120_000_000))
    accounting, bonds = bond_books(bond, (100, 90, 80, 80))
    bonds.advance(accounting, 0)
    bonds.advance(accounting, 1)
    assert (bonds.cashflows[-1].principal, bonds.cashflows[-1].coupon, bonds.cashflows[-1].accretion) == (900, 9, -100)
    assert accounting.tax.income.by_source[HOUSEHOLD, InterestIncome()] == -91
    bonds.advance(accounting, 2)
    assert (bonds.cashflows[-1].principal, bonds.cashflows[-1].coupon, bonds.cashflows[-1].redemption) == (800, 8, 1000)
    # Current held-bond contract accrues index changes only before maturity.
    assert bonds.cashflows[-1].accretion == 0
    assert accounting.tax.income.by_source[HOUSEHOLD, InterestIncome()] == -83
    assert accounting.ledger.balance(CASH) == 1117


def test_stopped_bond_snapshot_uses_the_last_observed_index(nominal: PreparedBond) -> None:
    bond = replace(nominal, coupon=PreparedIndexedCoupon(annual_rate_ppb=0), maturity_month_index=3)
    _, bonds = bond_books(bond, (100, 110, 200, 300))
    assert bonds.snapshots(2, 1)[0].principal == 1100
    assert bonds.snapshots(2, 2)[0].principal == 2000


if __name__ == "__main__":
    pytest_bazel.main()
