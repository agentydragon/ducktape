"""The configured household: scheduled sales and one cash-band household per configured funding policy."""

from finance.augur.policy.cash_band_household import (
    BandBound,
    CashBandHousehold,
    CpiIndexed,
    ManagedSleeve,
    PendingBuy,
    PendingContribution,
    Reinvest,
    SecuritySleeve,
    settle,
)
from finance.augur.sim.actions import Action, LotSale, Sell
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.books import AccountRef
from finance.augur.sim.ids import AgentId
from finance.augur.sim.observations import Observation
from finance.augur.sim.prepared import (
    PreparedAmount,
    PreparedFixedAmount,
    _AllocationPolicy,
    _ScheduledSale,
    _SecuritySleeveTarget,
)
from finance.augur.sim.world import World


def _bound(amount: PreparedAmount) -> BandBound:
    if isinstance(amount, int):
        return amount
    if isinstance(amount, PreparedFixedAmount):
        return amount.amount
    if amount.series_id != "inflation":
        raise ValueError("the configured household indexes a band bound to CPI only")
    if amount.base_month_index != 0:
        raise ValueError("allocation indexed bound starts before its base month")
    return CpiIndexed(base_amount=amount.base_amount, adjustment_period_months=amount.adjustment_period_months)


def _household(policy: _AllocationPolicy) -> CashBandHousehold:
    return CashBandHousehold(
        AgentId(policy.agent_id),
        cash_account_id=policy.account_id,
        floor=_bound(policy.cash_floor),
        ceiling=_bound(policy.cash_ceiling),
        sleeves=tuple(
            SecuritySleeve(asset_id=sleeve.asset_id, weight=sleeve.weight)
            if isinstance(sleeve, _SecuritySleeveTarget)
            else ManagedSleeve(portfolio_id=sleeve.portfolio_id, weight=sleeve.weight)
            for sleeve in policy.sleeves
        ),
        source_account_ids=policy.source_account_ids or (policy.account_id,),
        reinvest=Reinvest(rebalance_tolerance_ppb=policy.rebalance_tolerance_ppb) if policy.allow_purchases else None,
        cause_id_prefix=policy.cause_id_prefix,
    )


class ConfiguredHousehold(EconomicAgent):
    """Sells on schedule and on the funding policies' terms, pays every due claim in full in observed order, then buys."""

    def __init__(
        self,
        agent_id: AgentId,
        policies: tuple[_AllocationPolicy, ...],
        *,
        scheduled_sales: tuple[_ScheduledSale, ...] = (),
    ) -> None:
        super().__init__(agent_id)
        if any(policy.agent_id != agent_id for policy in policies):
            raise ValueError("a funding policy belongs to the household that consults it")
        if any(sale.agent_id != agent_id for sale in scheduled_sales):
            raise ValueError("a scheduled sale belongs to the household that makes it")
        if len({policy.account_id for policy in policies}) != len(policies):
            raise ValueError("duplicate allocation funding account")
        if any(
            sleeve.quantity_scale <= 0 or str(sleeve.quantity_scale).rstrip("0") != "1"
            for policy in policies
            for sleeve in policy.sleeves
            if isinstance(sleeve, _SecuritySleeveTarget)
        ):
            raise ValueError("allocation requires a power-of-ten quantity grid")
        if any(policy.rebalance_tolerance_ppb is not None and not policy.allow_purchases for policy in policies):
            raise ValueError("allocation drift requires purchases")
        self.policies = policies
        self.households = tuple(_household(policy) for policy in policies)
        self.scheduled_sales = scheduled_sales

    def check(self, world: World) -> None:
        """Refuse policies and scheduled sales naming what `world` does not declare; call before tracking."""
        pools = {(pool.agent_id, pool.account_id, pool.asset_id): pool.quantity_scale for pool in world.holdings.pools}
        for policy, household in zip(self.policies, self.households, strict=True):
            household.check(world)
            for sleeve in policy.sleeves:
                if isinstance(sleeve, _SecuritySleeveTarget) and any(
                    pools.get((policy.agent_id, source, sleeve.asset_id), sleeve.quantity_scale)
                    != sleeve.quantity_scale
                    for source in household.source_account_ids
                ):
                    raise ValueError("allocation quantity grid disagrees with a source holding pool")
        pool_slots = set(pools)
        for sale in self.scheduled_sales:
            if not 0 <= sale.month < world.horizon_months:
                raise ValueError(f"sale {sale.cause_id!r} has month {sale.month}, outside the horizon")
            if AccountRef(agent_id=sale.agent_id, account_id=sale.proceeds_account_id) not in world.accounting.declared:
                raise ValueError(f"sale {sale.cause_id!r} references an unknown proceeds account")
            if (sale.agent_id, sale.account_id, sale.asset_id) not in pool_slots:
                raise ValueError(f"sale {sale.cause_id!r} references no holding pool")
            if f"security:{sale.asset_id}" not in world.market.series:
                raise ValueError(f'missing series "security:{sale.asset_id}"')

    def decide(self, observation: Observation) -> list[Action]:
        actions: list[Action] = [
            self._scheduled(sale, observation) for sale in self.scheduled_sales if sale.month == observation.month
        ]
        purchases: list[tuple[CashBandHousehold, PendingBuy | PendingContribution]] = []
        for household in self.households:
            proposal = household.propose(observation)
            actions.extend(proposal.sales)
            purchases.extend((household, pending) for pending in proposal.purchases)
        return [*actions, *settle(observation, actions, purchases)]

    @staticmethod
    def _scheduled(sale: _ScheduledSale, observation: Observation) -> Sell:
        """The scheduled units from the oldest lots first."""
        remaining = sale.units
        lots = []
        for position in sorted(
            (
                position
                for position in observation.public_positions
                if (position.account_id, position.asset_id) == (sale.account_id, sale.asset_id)
            ),
            key=lambda position: (position.purchase_month, position.lot_id),
        ):
            if remaining <= 0:
                break
            units = min(remaining, position.units)
            lots.append(LotSale(account_id=position.account_id, lot_id=position.lot_id, units=units))
            remaining -= units
        if remaining > 0:
            raise ValueError(f"scheduled sale {sale.cause_id!r} exceeds the units held")
        return Sell(
            cause_id=sale.cause_id,
            agent_id=sale.agent_id,
            proceeds_account_id=sale.proceeds_account_id,
            asset_id=sale.asset_id,
            lots=tuple(lots),
        )
