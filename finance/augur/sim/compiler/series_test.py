"""What declarations demand of the exogenous model, before any world runs them."""

from __future__ import annotations

from decimal import Decimal

import pytest_bazel

from finance.augur.model.series import InflationKey, LevelSeriesKey
from finance.augur.sim.compiler.series import level_series_demand
from finance.augur.sim.ids import AccountId, AgentId, BondId
from finance.augur.sim.scenario import BondHolding


def demand(*, bonds: tuple[BondHolding, ...]) -> tuple[LevelSeriesKey, ...]:
    return level_series_demand(
        lots=(), tlh_portfolios=(), bonds=bonds, distributions=(), amounts=(), tender_policies=(), purchases=()
    )


def bond(*, indexed: bool) -> BondHolding:
    """One dated bond, with nothing priced beside it."""
    return BondHolding(
        bond_id=BondId("test-bond"),
        agent_id=AgentId("test-investor"),
        account_id=AccountId("cash"),
        face_value=Decimal(100),
        purchase_price=Decimal(100),
        annual_coupon_rate=0.04,
        coupon_period_months=6,
        purchase_month_index=0,
        maturity_month_index=12,
        inflation_indexed=indexed,
    )


def test_an_indexed_bond_demands_an_inflation_path() -> None:
    """The demand a TIPS makes that nothing else beside it need make.

    Every other level-series demand comes from something PRICED — a lot, a sleeve, a home. A
    bond has no price series at all, so an indexed one is the only instrument whose exogenous
    demand is invisible from the thing that carries it. Without it, the engine rejects a
    missing inflation path for any caller that derives its sampling request from the
    declarations, which is what the product surface does, unless it happens to want CPI anyway.

    Asserted on the demand function rather than through a run: a run supplies its own bundle
    and would pass either way, which is how the gap stayed invisible from `sim/`.
    """

    assert InflationKey() in demand(bonds=(bond(indexed=True),))
    # And not otherwise: a nominal bond's cashflows are fixed by its terms, so demanding a
    # series it never reads would fail an unmodeled-inflation deployment for no reason.
    assert InflationKey() not in demand(bonds=(bond(indexed=False),))


if __name__ == "__main__":
    pytest_bazel.main()
