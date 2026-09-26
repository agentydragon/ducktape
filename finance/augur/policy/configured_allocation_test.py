"""Configured proposals preserve exact budgets, source order, and opaque managed value."""

from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.policy import sleeves
from finance.augur.policy.configured_allocation import (
    PendingBuy,
    PendingContribution,
    materialize_buy,
    materialize_contribution,
    plan,
)
from finance.augur.sim.actions import Buy, Contribute, Liquidate, Sell, Withdraw
from finance.augur.sim.books import AccountRef
from finance.augur.sim.observations import Claim, Observation, PublicPosition, TlhPortfolioObservation
from finance.augur.sim.prepared import _AllocationPolicy, _ManagedSleeveTarget, _SecuritySleeveTarget, _SleeveTarget


def _policy(
    *, weights: tuple[int, ...] = (1,), scale: int = 1, drift: int | None = None, managed: bool = False
) -> _AllocationPolicy:
    """Sleeve `i` holds `asset-i`; with `managed`, sleeve 0 is the managed portfolio instead."""
    targets: list[_SleeveTarget] = [
        _SecuritySleeveTarget(asset_id=f"asset-{index}", weight=weight, quantity_scale=scale)
        for index, weight in enumerate(weights)
    ]
    if managed:
        targets[0] = _ManagedSleeveTarget(portfolio_id="managed", weight=weights[0])
    return _AllocationPolicy(
        agent_id="owner",
        account_id="cash",
        source_account_ids=("first", "second"),
        sleeves=tuple(targets),
        cash_floor=0,
        cash_ceiling=0,
        cause_id_prefix="fund",
        allow_purchases=True,
        rebalance_tolerance_ppb=drift,
    )


def _observation(
    *,
    cash: int = 0,
    lots: tuple[PublicPosition, ...] = (),
    portfolios: tuple[TlhPortfolioObservation, ...] = (),
    due: int = 0,
) -> Observation:
    return Observation(
        agent_id="owner",
        month=0,
        cpi=None,
        cash=cash,
        public_holdings=sum(lot.value for lot in lots),
        accounts=(("cash", cash),),
        holding_pools=(),
        public_positions=lots,
        held_bonds=(),
        tlh_portfolios=portfolios,
        claims=(
            Claim(
                month=0,
                index=0,
                cause_id="bill",
                obligation_type="spending",
                from_account=AccountRef(agent_id="owner", account_id="cash"),
                to_account=AccountRef(agent_id="world", account_id="cash"),
                amount_due=due,
            ),
        )
        if due
        else (),
        tax_records=None,
    )


def _lot(
    *,
    account: str = "first",
    asset: str = "asset-0",
    lot: str = "opening",
    units: int = 5,
    scale: int = 1,
    price: int = 3,
    month: int = -1,
) -> PublicPosition:
    return PublicPosition(
        account_id=account,
        asset_id=asset,
        lot_id=lot,
        purchase_month=month,
        units=units,
        quantity_scale=scale,
        book_basis=7,
        price=price,
        value=sleeves._quoted_value(units, price, scale),
    )


def _managed(value: int, *, accepts_contributions: bool = True) -> TlhPortfolioObservation:
    """A portfolio in the first source account, pegged to the index `asset-0` also names."""
    return TlhPortfolioObservation(
        portfolio_id="managed",
        owner_agent_id="owner",
        account_id="first",
        asset_id="asset-0",
        value=value,
        reported_tax_basis=80,
        accepts_contributions=accepts_contributions,
    )


def test_post_claim_purchase_clamp_and_lot_identity() -> None:
    original = _observation(cash=100, due=70)
    proposal = plan(original, _policy(scale=10), policy_index=2, floor=0, ceiling=0, prices={"asset-0": 3})
    assert proposal.sales == []
    [pending] = proposal.buys
    assert isinstance(pending, PendingBuy)
    assert pending.wanted_units == 100  # Only the unreserved 30 quanta are planned.
    purchase = materialize_buy(_observation(cash=7), pending, lot_sequence=4)
    assert isinstance(purchase, Buy)
    assert (purchase.units, purchase.quantity_scale, purchase.lot_id) == (23, 10, "fund_buy_p2_s0_4")
    assert sleeves._quoted_value(purchase.units, pending.price, purchase.quantity_scale) == 7
    assert materialize_buy(_observation(), pending, lot_sequence=4) is None


def test_a_managed_sleeve_plans_money_not_units_and_clamps_to_cash() -> None:
    managed = (_managed(100),)
    proposal = plan(
        _observation(cash=100, due=70, portfolios=managed),
        _policy(managed=True),
        policy_index=2,
        floor=0,
        ceiling=0,
        prices={},
    )
    [pending] = proposal.buys
    assert isinstance(pending, PendingContribution)
    # All 30 unreserved quanta, with no quote to round them to units against.
    assert pending.wanted_amount == 30
    contribution = materialize_contribution(_observation(cash=7, portfolios=managed), pending)
    assert isinstance(contribution, Contribute)
    assert (contribution.portfolio_id, contribution.amount) == ("managed", 7)
    assert materialize_contribution(_observation(portfolios=managed), pending) is None


def test_funding_ceil_and_drift_floor_are_distinct_quantity_controls() -> None:
    observation = _observation(lots=(_lot(),))
    funding = plan(observation, _policy(), policy_index=0, floor=8, ceiling=8, prices={"asset-0": 3})
    [sale] = funding.sales
    assert isinstance(sale, Sell)
    assert [lot.units for lot in sale.lots] == [3]  # Ceiling(8/3).
    drift = plan(
        observation,
        _policy(weights=(1, 1), drift=0),
        policy_index=0,
        floor=0,
        ceiling=0,
        prices={"asset-0": 3, "asset-1": 3},
    )
    [sale] = drift.sales
    assert isinstance(sale, Sell)
    assert [lot.units for lot in sale.lots] == [2]  # Floor((15 - floor(15/2))/3).
    [pending] = drift.buys
    assert isinstance(pending, PendingBuy)
    assert pending.wanted_units == 2


def test_ordered_sources_and_per_lot_rounding_use_economic_units() -> None:
    lots = (
        _lot(lot="new", units=4, scale=10, month=-1),
        _lot(lot="old", units=3, scale=10, month=-12),
        _lot(account="second", lot="older-other-account", units=1, scale=1, month=-24),
        _lot(account="excluded", lot="never", units=100),
    )
    proposal = plan(
        _observation(lots=lots), _policy(scale=10), policy_index=0, floor=3, ceiling=3, prices={"asset-0": 3}
    )
    [sale] = proposal.sales
    assert isinstance(sale, Sell)
    # A whole-unit target cannot buy the next account's indivisible unit after
    # taking 0.7 units in the first account. Never sum those raw counts as 8 units.
    assert [(lot.lot_id, lot.units) for lot in sale.lots] == [("old", 3), ("new", 4)]
    assert sum(sleeves._quoted_value(lot.units, 3, 10) for lot in sale.lots) == 2


def test_zero_target_exit_includes_zero_mark_units_and_a_worthless_managed_sleeve() -> None:
    observation = _observation(lots=(_lot(units=1, scale=10, price=1), _lot(asset="asset-1", units=10, price=1)))
    proposal = plan(
        observation,
        _policy(weights=(0, 1), drift=1_000_000_000),
        policy_index=0,
        floor=0,
        ceiling=0,
        prices={"asset-0": 1, "asset-1": 1},
    )
    [sale] = proposal.sales
    assert isinstance(sale, Sell)
    assert sale.lots[0].units == 1
    assert proposal.buys == []
    managed = _observation(portfolios=(_managed(0, accepts_contributions=False),))
    proposal = plan(
        managed,
        _policy(weights=(0, 1), drift=0, managed=True),
        policy_index=0,
        floor=0,
        ceiling=0,
        prices={"asset-1": 1},
    )
    assert isinstance(proposal.sales[0], Liquidate)


@pytest.mark.parametrize("cash", [0, 100])
def test_a_worthless_managed_index_takes_no_contribution_and_has_nothing_to_withdraw(cash: int) -> None:
    observation = _observation(cash=cash, portfolios=(_managed(0, accepts_contributions=False),))
    assert plan(observation, _policy(managed=True), policy_index=0, floor=0, ceiling=0, prices={}).buys == []
    raised = plan(observation, _policy(managed=True), policy_index=0, floor=200, ceiling=200, prices={})
    assert raised.sales == []
    assert raised.buys == []


def test_an_empty_portfolio_that_accepts_money_takes_the_surplus() -> None:
    """Value and basis alone cannot tell this portfolio from the worthless one above."""
    proposal = plan(
        _observation(cash=100, portfolios=(_managed(0),)),
        _policy(managed=True),
        policy_index=0,
        floor=0,
        ceiling=0,
        prices={},
    )
    [pending] = proposal.buys
    assert isinstance(pending, PendingContribution)
    assert pending.wanted_amount == 100


def test_a_managed_withdrawal_is_the_money_the_band_raises() -> None:
    raised = plan(
        _observation(cash=100, portfolios=(_managed(500),)),
        _policy(managed=True),
        policy_index=0,
        floor=200,
        ceiling=200,
        prices={},
    )
    [sale] = raised.sales
    assert isinstance(sale, Withdraw)
    assert sale.amount == 100
    assert raised.buys == []


def test_lots_of_an_index_and_a_portfolio_pegged_to_it_are_separate_sleeves() -> None:
    """The $15 of `asset-0` lots and the $500 portfolio on the same index are weighed apart.

    Merged, they would be one $515 sleeve; apart, the equal-weight raise of $100 comes entirely
    from the overweight portfolio, and the lots are not sold.
    """
    raised = plan(
        _observation(cash=100, lots=(_lot(),), portfolios=(_managed(500),)),
        replace(
            _policy(),
            sleeves=(
                _ManagedSleeveTarget(portfolio_id="managed", weight=1),
                _SecuritySleeveTarget(asset_id="asset-0", weight=1, quantity_scale=1),
            ),
        ),
        policy_index=0,
        floor=200,
        ceiling=200,
        prices={"asset-0": 3},
    )
    [sale] = raised.sales
    assert isinstance(sale, Withdraw)
    assert (sale.portfolio_id, sale.amount) == ("managed", 100)


def test_cashflow_only_and_deposit_do_not_trigger_zero_target_drift() -> None:
    observation = _observation(lots=(_lot(),))
    quiet = plan(
        observation, _policy(weights=(0, 1)), policy_index=0, floor=0, ceiling=0, prices={"asset-0": 3, "asset-1": 3}
    )
    assert quiet.sales == []
    assert quiet.buys == []
    deposit = plan(
        _observation(cash=10, lots=(_lot(),)),
        _policy(weights=(0, 1), drift=0),
        policy_index=0,
        floor=0,
        ceiling=0,
        prices={"asset-0": 3, "asset-1": 3},
    )
    assert deposit.sales == []
    [pending] = deposit.buys
    assert isinstance(pending, PendingBuy)
    assert pending.asset_id == "asset-1"
    no_purchases = plan(
        _observation(cash=10),
        replace(_policy(), allow_purchases=False),
        policy_index=0,
        floor=0,
        ceiling=0,
        prices={"asset-0": 3},
    )
    assert no_purchases.sales == []
    assert no_purchases.buys == []


@pytest.mark.parametrize(
    ("values", "weights", "tolerance", "expected"),
    [
        ([900, 100], [1, 1], 250_000_000, ([400, 0], [0, 400])),
        ([600, 400], [1, 1], 250_000_000, ([0, 0], [0, 0])),
        ([1600, 1100, 300], [1, 1, 1], 250_000_000, ([600, 100, 0], [0, 0, 700])),
        ([501, 499], [1, 1], 0, ([1, 0], [0, 1])),
        ([1, 0], [1, 1], 0, ([0, 0], [0, 0])),
        ([1, 999], [0, 1], 1_000_000_000, ([1, 0], [0, 1])),
    ],
)
def test_shared_drift_arithmetic(
    values: list[int], weights: list[int], tolerance: int, expected: tuple[list[int], list[int]]
) -> None:
    assert sleeves._rebalance_amounts(values, weights, tolerance) == expected


@pytest.mark.parametrize("weights", [(0, 0), (-1, 1)])
def test_shared_allocation_rejects_invalid_weights_even_for_zero_budget(weights: tuple[int, int]) -> None:
    for withdrawing in (False, True):
        with pytest.raises(ValueError, match=r"positive target|nonnegative"):
            sleeves._allocate([1, 1], list(weights), 0, withdrawing=withdrawing)
    with pytest.raises(ValueError, match=r"positive target|nonnegative"):
        sleeves._rebalance_amounts([1, 1], list(weights), 0)


if __name__ == "__main__":
    pytest_bazel.main()
