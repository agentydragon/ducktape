"""Coupon, index accretion and redemption have distinct cash, income and observation clocks."""

from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim.accounting import Accounting
from finance.augur.sim.held_bonds import Bond, HeldBonds
from finance.augur.sim.ids import AccountId, BondId
from finance.augur.sim.income import InterestIncome, Taxable
from finance.augur.sim.market_path import MarketPath, Series
from finance.augur.sim.observations import FixedCoupon, IndexedCoupon
from finance.augur.sim.testing.accounting import CASH, HOUSEHOLD, accounting


@pytest.fixture
def nominal() -> Bond:
    return Bond(
        bond_id=BondId("test_bond"),
        agent_id=HOUSEHOLD,
        account_id=AccountId("checking"),
        character=Taxable(),
        face_value=1000,
        purchase_price=1000,
        coupon=FixedCoupon(amount=10),
        coupon_period_months=1,
        purchase_month_index=0,
        maturity_month_index=2,
    )


def bond_books(bond: Bond, levels: tuple[int, ...]) -> tuple[Accounting, HeldBonds]:
    market = MarketPath((Series(series_id="inflation", snapshots=len(levels), values=levels),), 0, rollout_count=1)
    return accounting(), HeldBonds((bond,), market)


def test_tips_deflation_changes_income_but_redemption_has_a_face_floor(nominal: Bond) -> None:
    bond = replace(nominal, coupon=IndexedCoupon(annual_rate_ppb=120_000_000))
    accounting, bonds = bond_books(bond, (100, 90, 80, 80))
    bonds.advance(accounting, 0)
    bonds.advance(accounting, 1)
    assert (bonds.cashflows[-1].principal, bonds.cashflows[-1].coupon, bonds.cashflows[-1].accretion) == (900, 9, -100)
    assert accounting.tax.income.by_source[HOUSEHOLD, InterestIncome(character=Taxable())] == -91
    bonds.advance(accounting, 2)
    assert (bonds.cashflows[-1].principal, bonds.cashflows[-1].coupon, bonds.cashflows[-1].redemption) == (800, 8, 1000)
    # Current held-bond contract accrues index changes only before maturity.
    assert bonds.cashflows[-1].accretion == 0
    assert accounting.tax.income.by_source[HOUSEHOLD, InterestIncome(character=Taxable())] == -83
    assert accounting.ledger.balance(CASH) == 1117


def test_stopped_bond_snapshot_uses_the_last_observed_index(nominal: Bond) -> None:
    bond = replace(nominal, coupon=IndexedCoupon(annual_rate_ppb=0), maturity_month_index=3)
    _, bonds = bond_books(bond, (100, 110, 200, 300))
    assert bonds.snapshots(2, 1)[0].principal == 1100
    assert bonds.snapshots(2, 2)[0].principal == 2000


if __name__ == "__main__":
    pytest_bazel.main()
