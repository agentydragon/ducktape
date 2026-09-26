"""The household's proposals preserve exact budgets, source order, and opaque managed value."""

import pytest
import pytest_bazel

from finance.augur.policy import sleeves
from finance.augur.policy.cash_band_household import (
    CashBandHousehold,
    ManagedSleeve,
    PendingBuy,
    PendingContribution,
    Reinvest,
    SecuritySleeve,
    Sleeve,
)
from finance.augur.sim.actions import Liquidate, Sell, Withdraw
from finance.augur.sim.books import AccountRef
from finance.augur.sim.ids import AccountId, AgentId, AssetId, LotId, PortfolioId
from finance.augur.sim.observations import Claim, HoldingPool, Observation, PublicPosition, TlhPortfolioObservation


def _household(
    *,
    weights: tuple[int, ...] = (1,),
    drift: int | None = None,
    managed: bool = False,
    reinvest: bool = True,
    floor: int = 0,
    ceiling: int = 0,
    targets: tuple[Sleeve, ...] | None = None,
) -> CashBandHousehold:
    """Sleeve `i` holds `asset-i`; with `managed`, sleeve 0 is the managed portfolio instead."""
    default: list[Sleeve] = [
        SecuritySleeve(asset_id=AssetId(f"asset-{index}"), weight=weight) for index, weight in enumerate(weights)
    ]
    if managed:
        default[0] = ManagedSleeve(portfolio_id=PortfolioId("managed"), weight=weights[0])
    return CashBandHousehold(
        AgentId("owner"),
        cash_account_id=AccountId("cash"),
        floor=floor,
        ceiling=ceiling,
        sleeves=tuple(default) if targets is None else targets,
        source_account_ids=(AccountId("first"), AccountId("second")),
        reinvest=Reinvest(rebalance_tolerance_ppb=drift) if reinvest else None,
        cause_id_prefix="fund",
    )


def _observation(
    *,
    cash: int = 0,
    lots: tuple[PublicPosition, ...] = (),
    portfolios: tuple[TlhPortfolioObservation, ...] = (),
    due: int = 0,
    prices: dict[str, int] | None = None,
    scale: int = 1,
) -> Observation:
    """`prices` quotes each asset's pool in the first source account, on the `scale` grid."""
    return Observation(
        agent_id=AgentId("owner"),
        month=0,
        cpi=None,
        cash=cash,
        public_holdings=sum(lot.value for lot in lots),
        accounts=((AccountId("cash"), cash),),
        holding_pools=tuple(
            HoldingPool(account_id=AccountId("first"), asset_id=AssetId(asset_id), quantity_scale=scale, price=price)
            for asset_id, price in (prices or {}).items()
        ),
        public_positions=lots,
        held_bonds=(),
        tlh_portfolios=portfolios,
        claims=(
            Claim(
                month=0,
                index=0,
                cause_id="bill",
                obligation_type="spending",
                from_account=AccountRef(agent_id=AgentId("owner"), account_id=AccountId("cash")),
                to_account=AccountRef(agent_id=AgentId("world"), account_id=AccountId("cash")),
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
        account_id=AccountId(account),
        asset_id=AssetId(asset),
        lot_id=LotId(lot),
        purchase_month=month,
        units=units,
        quantity_scale=scale,
        book_basis=7,
        price=price,
        value=sleeves.quoted_value(units, price, scale),
    )


def _managed(value: int, *, accepts_contributions: bool = True) -> TlhPortfolioObservation:
    """A portfolio in the first source account, pegged to the index `asset-0` also names."""
    return TlhPortfolioObservation(
        portfolio_id=PortfolioId("managed"),
        owner_agent_id=AgentId("owner"),
        account_id=AccountId("first"),
        asset_id=AssetId("asset-0"),
        value=value,
        reported_tax_basis=80,
        accepts_contributions=accepts_contributions,
    )


def test_post_claim_purchase_clamp_and_lot_identity() -> None:
    household = _household()
    quoted = {"asset-0": 3}
    proposal = household.propose(_observation(cash=100, due=70, prices=quoted, scale=10))
    assert proposal.sales == []
    [pending] = proposal.purchases
    assert isinstance(pending, PendingBuy)
    assert pending.wanted_units == 100  # Only the unreserved 30 quanta are planned.
    # A purchase cash cannot fund emits nothing and takes no lot identity.
    assert household.buy(_observation(prices=quoted, scale=10), pending) is None
    purchase = household.buy(_observation(cash=7, prices=quoted, scale=10), pending)
    assert purchase is not None
    assert (purchase.units, purchase.quantity_scale, purchase.lot_id) == (23, 10, "fund_buy_s0_0")
    assert sleeves.quoted_value(purchase.units, pending.price, purchase.quantity_scale) == 7
    again = household.buy(_observation(cash=7, prices=quoted, scale=10), pending)
    assert again is not None
    assert again.lot_id == "fund_buy_s0_1"


def test_a_managed_sleeve_plans_money_not_units_and_clamps_to_cash() -> None:
    managed = (_managed(100),)
    household = _household(managed=True)
    [pending] = household.propose(_observation(cash=100, due=70, portfolios=managed)).purchases
    assert isinstance(pending, PendingContribution)
    # All 30 unreserved quanta, with no quote to round them to units against.
    assert pending.wanted_amount == 30
    contribution = household.contribution(_observation(cash=7, portfolios=managed), pending)
    assert contribution is not None
    assert (contribution.portfolio_id, contribution.amount) == ("managed", 7)
    assert household.contribution(_observation(portfolios=managed), pending) is None


def test_funding_ceil_and_drift_floor_are_distinct_quantity_controls() -> None:
    observation = _observation(lots=(_lot(),), prices={"asset-0": 3, "asset-1": 3})
    [sale] = _household(floor=8, ceiling=8).propose(observation).sales
    assert isinstance(sale, Sell)
    assert [lot.units for lot in sale.lots] == [3]  # Ceiling(8/3).
    drift = _household(weights=(1, 1), drift=0).propose(observation)
    [sale] = drift.sales
    assert isinstance(sale, Sell)
    assert [lot.units for lot in sale.lots] == [2]  # Floor((15 - floor(15/2))/3).
    [pending] = drift.purchases
    assert isinstance(pending, PendingBuy)
    assert pending.wanted_units == 2


def test_ordered_sources_and_per_lot_rounding_use_economic_units() -> None:
    lots = (
        _lot(lot="new", units=4, scale=10, month=-1),
        _lot(lot="old", units=3, scale=10, month=-12),
        _lot(account="second", lot="older-other-account", units=1, scale=1, month=-24),
        _lot(account="excluded", lot="never", units=100),
    )
    proposal = _household(floor=3, ceiling=3).propose(_observation(lots=lots, prices={"asset-0": 3}, scale=10))
    [sale] = proposal.sales
    assert isinstance(sale, Sell)
    # A whole-unit target cannot buy the next account's indivisible unit after
    # taking 0.7 units in the first account. Never sum those raw counts as 8 units.
    assert [(lot.lot_id, lot.units) for lot in sale.lots] == [("old", 3), ("new", 4)]
    assert sum(sleeves.quoted_value(lot.units, 3, 10) for lot in sale.lots) == 2


def test_zero_target_exit_includes_zero_mark_units_and_a_worthless_managed_sleeve() -> None:
    observation = _observation(
        lots=(_lot(units=1, scale=10, price=1), _lot(asset="asset-1", units=10, price=1)),
        prices={"asset-0": 1, "asset-1": 1},
    )
    proposal = _household(weights=(0, 1), drift=1_000_000_000).propose(observation)
    [sale] = proposal.sales
    assert isinstance(sale, Sell)
    assert sale.lots[0].units == 1
    assert proposal.purchases == []
    managed = _observation(portfolios=(_managed(0, accepts_contributions=False),), prices={"asset-1": 1})
    proposal = _household(weights=(0, 1), drift=0, managed=True).propose(managed)
    assert isinstance(proposal.sales[0], Liquidate)


@pytest.mark.parametrize("cash", [0, 100])
def test_a_worthless_managed_index_takes_no_contribution_and_has_nothing_to_withdraw(cash: int) -> None:
    observation = _observation(cash=cash, portfolios=(_managed(0, accepts_contributions=False),))
    assert _household(managed=True).propose(observation).purchases == []
    raised = _household(managed=True, floor=200, ceiling=200).propose(observation)
    assert raised.sales == []
    assert raised.purchases == []


def test_an_empty_portfolio_that_accepts_money_takes_the_surplus() -> None:
    """Value and basis alone cannot tell this portfolio from the worthless one above."""
    [pending] = _household(managed=True).propose(_observation(cash=100, portfolios=(_managed(0),))).purchases
    assert isinstance(pending, PendingContribution)
    assert pending.wanted_amount == 100


def test_a_managed_withdrawal_is_the_money_the_band_raises() -> None:
    raised = _household(managed=True, floor=200, ceiling=200).propose(
        _observation(cash=100, portfolios=(_managed(500),))
    )
    [sale] = raised.sales
    assert isinstance(sale, Withdraw)
    assert sale.amount == 100
    assert raised.purchases == []


def test_lots_of_an_index_and_a_portfolio_pegged_to_it_are_separate_sleeves() -> None:
    """The $15 of `asset-0` lots and the $500 portfolio on the same index are weighed apart.

    Merged, they would be one $515 sleeve; apart, the equal-weight raise of $100 comes entirely
    from the overweight portfolio, and the lots are not sold.
    """
    household = _household(
        floor=200,
        ceiling=200,
        targets=(
            ManagedSleeve(portfolio_id=PortfolioId("managed"), weight=1),
            SecuritySleeve(asset_id=AssetId("asset-0"), weight=1),
        ),
    )
    [sale] = household.propose(
        _observation(cash=100, lots=(_lot(),), portfolios=(_managed(500),), prices={"asset-0": 3})
    ).sales
    assert isinstance(sale, Withdraw)
    assert (sale.portfolio_id, sale.amount) == ("managed", 100)


def test_cashflow_only_and_deposit_do_not_trigger_zero_target_drift() -> None:
    quoted = {"asset-0": 3, "asset-1": 3}
    quiet = _household(weights=(0, 1)).propose(_observation(lots=(_lot(),), prices=quoted))
    assert quiet.sales == []
    assert quiet.purchases == []
    deposit = _household(weights=(0, 1), drift=0).propose(_observation(cash=10, lots=(_lot(),), prices=quoted))
    assert deposit.sales == []
    [pending] = deposit.purchases
    assert isinstance(pending, PendingBuy)
    assert pending.asset_id == "asset-1"
    sales_only = _household(reinvest=False).propose(_observation(cash=10, prices={"asset-0": 3}))
    assert sales_only.sales == []
    assert sales_only.purchases == []


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
