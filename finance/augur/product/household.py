"""The app's household: the configured funding policies' sales, then every claim its cash covers."""

from collections import defaultdict

from finance.augur.policy.configured_allocation import plan
from finance.augur.sim.actions import Action, Liquidate, LotSale, PayClaim, Sell, Withdraw
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.ids import AgentId
from finance.augur.sim.money import checked_count, mul_div, position_value
from finance.augur.sim.observations import Observation
from finance.augur.sim.prepared import PreparedAmount, PreparedFixedAmount, _AllocationPolicy, _ScheduledSale


class ConfiguredHousehold(EconomicAgent):
    """Sells on schedule and on the funding policies' terms, then pays each account's claims all or none.

    The sales come first and the claims are judged on the cash they leave: an account whose
    month's claims exceed that pays none of them, so the month's shortfall is the whole due and
    the path stops on it. Purchases are outside this household; the app's policies never buy.
    """

    def __init__(
        self,
        agent_id: AgentId,
        policies: tuple[_AllocationPolicy, ...],
        *,
        scheduled_sales: tuple[_ScheduledSale, ...] = (),
    ) -> None:
        super().__init__(agent_id)
        for policy in policies:
            if policy.agent_id != agent_id:
                raise ValueError("a funding policy belongs to the household that consults it")
            if policy.allow_purchases:
                raise ValueError("the app household sells on its policies; it does not purchase")
        if any(sale.agent_id != agent_id for sale in scheduled_sales):
            raise ValueError("a scheduled sale belongs to the household that makes it")
        self.policies = policies
        self.scheduled_sales = scheduled_sales

    def decide(self, observation: Observation) -> list[Action]:
        prices = {pool.asset_id: pool.price for pool in observation.holding_pools}
        actions: list[Action] = [
            self._scheduled(sale, observation) for sale in self.scheduled_sales if sale.month == observation.month
        ]
        for index, policy in enumerate(self.policies):
            proposal = plan(
                observation,
                policy,
                policy_index=index,
                floor=self._bound(policy.cash_floor, observation),
                ceiling=self._bound(policy.cash_ceiling, observation),
                prices={sleeve.asset_id: prices[sleeve.asset_id] for sleeve in policy.sleeves},
            )
            actions.extend(proposal.sales)
        cash = dict(observation.accounts)
        for action in actions:
            account, proceeds = self._proceeds(action, observation)
            cash[account] = checked_count(cash[account] + proceeds, "projected cash")
        due: defaultdict[str, int] = defaultdict(int)
        for claim in observation.claims:
            due[claim.from_account.account_id] = checked_count(
                due[claim.from_account.account_id] + claim.amount_due, "claims due"
            )
        actions.extend(
            PayClaim(
                request_id=index + 1,
                cause_id=claim.cause_id,
                claim=claim,
                from_account=claim.from_account,
                amount=claim.amount_due,
            )
            for index, claim in enumerate(observation.claims)
            if cash[claim.from_account.account_id] >= due[claim.from_account.account_id]
        )
        return actions

    @staticmethod
    def _scheduled(sale: _ScheduledSale, observation: Observation) -> Sell:
        """The scheduled units from the oldest lots first; a managed portfolio has no unit-denominated sale."""
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

    @staticmethod
    def _bound(amount: PreparedAmount, observation: Observation) -> int:
        """A band bound as money this month; an indexed one rides the CPI the market statement reports."""
        if isinstance(amount, int):
            return amount
        if isinstance(amount, PreparedFixedAmount):
            return amount.amount
        if amount.series_id != "inflation" or amount.base_month_index != 0 or amount.adjustment_period_months != 1:
            raise ValueError("the app household indexes a band bound to monthly CPI from month zero only")
        if observation.cpi is None:
            raise ValueError("an inflation-indexed band bound needs a modeled CPI")
        current, origin = observation.cpi
        return mul_div(amount.base_amount, current, origin, "series-indexed amount")

    @staticmethod
    def _proceeds(action: Action, observation: Observation) -> tuple[str, int]:
        """The cash a proposed sale lands in its account, on the prices the observation quotes."""
        if isinstance(action, Sell):
            quoted = {position.lot_id: position for position in observation.public_positions}
            total = 0
            for lot in action.lots:
                position = quoted[lot.lot_id]
                total = checked_count(
                    total + position_value(position.price, lot.units, position.quantity_scale), "sale proceeds"
                )
            return action.proceeds_account_id, total
        if isinstance(action, Withdraw):
            return action.cash_account_id, action.amount
        if isinstance(action, Liquidate):
            portfolio = next(item for item in observation.tlh_portfolios if item.portfolio_id == action.portfolio_id)
            return action.cash_account_id, portfolio.value
        raise TypeError(f"a funding policy proposes sales, not {type(action).__name__}")
