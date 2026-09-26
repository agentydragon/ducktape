"""The configured household: its policies' funding sales, full payment of its claims in order, then its purchases."""

from collections import defaultdict

from finance.augur.policy.configured_allocation import (
    PendingBuy,
    PendingContribution,
    check_policies,
    materialize_buy,
    materialize_contribution,
    plan,
)
from finance.augur.policy.funding import full_payments
from finance.augur.sim.actions import Action, Buy, Contribute, Liquidate, Sell, Withdraw
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.ids import AgentId
from finance.augur.sim.money import checked_count, mul_div, position_value
from finance.augur.sim.observations import Observation
from finance.augur.sim.prepared import PreparedAmount, PreparedFixedAmount, _AllocationPolicy, _SecuritySleeveTarget
from finance.augur.sim.world import World


class ConfiguredHousehold(EconomicAgent):
    """Sells on the funding policies' terms, pays every due claim in full in observed order, then buys.

    The purchases are sized from the cash the sales leave less every due claim, so each order is
    exact against the cash the month will actually leave. A claim that cash cannot cover is
    rejected and stops the path, so a month with one buys nothing.
    """

    def __init__(self, agent_id: AgentId, policies: tuple[_AllocationPolicy, ...]) -> None:
        super().__init__(agent_id)
        if any(policy.agent_id != agent_id for policy in policies):
            raise ValueError("a funding policy belongs to the household that consults it")
        self.policies = policies
        # One lot-identity counter per (policy, sleeve), advanced only by an emitted Buy.
        self.lot_sequences: defaultdict[tuple[int, int], int] = defaultdict(int)
        # The CPI level of every month this household has seen, so an indexed band bound can be
        # read at its own reset month rather than only at the current one.
        self.cpi_levels: list[int] = []

    def check(self, world: World) -> None:
        """Refuse policies naming what `world` does not declare; call before tracking."""
        check_policies(world, self.policies)

    def decide(self, observation: Observation) -> list[Action]:
        if observation.cpi is not None:
            if len(self.cpi_levels) != observation.month:
                raise ValueError("the household reads every month's CPI once, in order")
            self.cpi_levels.append(observation.cpi[0])
        prices = {pool.asset_id: pool.price for pool in observation.holding_pools}
        actions: list[Action] = []
        pending: list[PendingBuy | PendingContribution] = []
        for index, policy in enumerate(self.policies):
            proposal = plan(
                observation,
                policy,
                policy_index=index,
                floor=self._bound(policy.cash_floor, observation),
                ceiling=self._bound(policy.cash_ceiling, observation),
                prices={
                    sleeve.asset_id: prices[sleeve.asset_id]
                    for sleeve in policy.sleeves
                    if isinstance(sleeve, _SecuritySleeveTarget)
                },
            )
            actions.extend(proposal.sales)
            pending.extend(proposal.buys)
        cash = dict(observation.accounts)
        for action in actions:
            account, proceeds = self._proceeds(action, observation)
            cash[account] = checked_count(cash[account] + proceeds, "projected cash")
        payments = full_payments(observation.claims)
        actions.extend(payments)
        for payment in payments:
            account = payment.from_account.account_id
            cash[account] = checked_count(cash[account] - payment.amount, "projected cash")
        if all(cash[payment.from_account.account_id] >= 0 for payment in payments):
            actions.extend(self._purchases(observation, pending, cash))
        return actions

    def _purchases(
        self, observation: Observation, pending: list[PendingBuy | PendingContribution], cash: dict[str, int]
    ) -> list[Action]:
        """Exact orders, each sized from the cash left once this batch's sales and payments settle."""
        orders: list[Action] = []
        for buy in pending:
            projected = observation.model_copy(update={"accounts": tuple(cash.items())})
            order: Buy | Contribute | None
            if isinstance(buy, PendingContribution):
                order = materialize_contribution(projected, buy)
                if order is None:
                    continue
                spent = order.amount
            else:
                key = (buy.policy_index, buy.sleeve_index)
                order = materialize_buy(projected, buy, lot_sequence=self.lot_sequences[key])
                if order is None:
                    continue
                spent = position_value(buy.price, order.units, buy.quantity_scale)
                self.lot_sequences[key] += 1
            cash[buy.cash_account_id] = checked_count(cash[buy.cash_account_id] - spent, "projected cash")
            orders.append(order)
        return orders

    def _bound(self, amount: PreparedAmount, observation: Observation) -> int:
        """A band bound as money this month; an indexed one rides the CPI level of its last reset."""
        if isinstance(amount, int):
            return amount
        if isinstance(amount, PreparedFixedAmount):
            return amount.amount
        if amount.series_id != "inflation":
            raise ValueError("the configured household indexes a band bound to CPI only")
        if observation.cpi is None:
            raise ValueError("an inflation-indexed band bound needs a modeled CPI")
        elapsed = observation.month - amount.base_month_index
        if elapsed < 0 or amount.base_month_index >= len(self.cpi_levels):
            raise ValueError("an indexed band bound must not precede its base month")
        reset = amount.base_month_index + elapsed // amount.adjustment_period_months * amount.adjustment_period_months
        return mul_div(
            amount.base_amount,
            self.cpi_levels[reset],
            self.cpi_levels[amount.base_month_index],
            "series-indexed amount",
        )

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
