"""Trinity's sleeve portfolio without its scheduled withdrawals, composed onto a World.

No `Scenario`: the books are declared straight onto one world per path, and the Python
policy supplies every sale and the consumption itself. Paths and instruments come from
the Trinity experiment.
"""

from finance.augur.sim.books import AccountRef
from finance.augur.sim.fixed_point import rate_to_ppb
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedDistribution,
    PreparedDistributionSlice,
    PreparedHoldingPool,
)
from finance.augur.sim.scenario import ORDINARY_INCOME, InterestIncome
from finance.augur.sim.world import World
from finance.augur.study.trinity.replay import BONDS, BROKERAGE, CHECKING, RETIREE, WORLD, Situation, opening_lots


def compose(case: Situation, rollout_id: int, *, equity_share: float) -> World:
    """`equity_share` of USD 1M in stocks and the rest in bonds, bought the month before; tax-free.

    The bond payout names its (corporate) interest source; nothing here is taxed.
    """
    lots = opening_lots(equity_share)
    holds_bonds = any(lot.asset_id == str(BONDS) for lot in lots)
    world = World(
        MarketPath(case.series, rollout_id, rollout_count=case.rollout_count),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME, InterestIncome(issuer_jurisdiction_id=None))
        if holds_bonds
        else (ORDINARY_INCOME,),
    )
    for agent_id in (RETIREE, WORLD):
        world.declare_account(
            PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=CHECKING), opening_balance=0)
        )
    for lot in lots:
        world.declare_pool(
            PreparedHoldingPool(
                agent_id=lot.agent_id,
                account_id=lot.account_id,
                asset_id=lot.asset_id,
                quantity_scale=lot.quantity_scale,
            )
        )
    for lot in lots:
        world.hold(lot)
    if holds_bonds:
        world.declare_distribution(
            PreparedDistribution(
                agent_id=RETIREE,
                holding_account_id=BROKERAGE,
                asset_id=str(BONDS),
                to_account_id=CHECKING,
                tax_character=(PreparedDistributionSlice(fraction_ppb=rate_to_ppb(1.0), issuer_jurisdiction_id=None),),
            )
        )
    return world
