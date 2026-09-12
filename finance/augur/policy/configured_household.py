"""The configured household: its policies' funding sales, the claims its cash covers, then its purchases."""

from collections import defaultdict

from finance.augur.policy.configured_allocation import PendingBuy, materialize_buy, plan
from finance.augur.sim.actions import Action, Buy, Liquidate, LotSale, PayClaim, Sell, Withdraw
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.ids import AgentId
from finance.augur.sim.money import checked_count, mul_div, position_value
from finance.augur.sim.observations import Observation
from finance.augur.sim.prepared import PreparedAmount, PreparedFixedAmount, _AllocationPolicy, _ScheduledSale


class ConfiguredHousehold(EconomicAgent):
    """Sells on schedule and on the funding policies' terms, pays each account's claims all or none, then buys.

    The sales come first and the claims are judged on the cash they leave: an account whose
    month's claims exceed that pays none of them, so the month's shortfall is the whole due and
    the path stops on it. The purchases come last and are sized from the same projection, less
    the claims this batch decided to pay, so each order is exact against the cash the month will
    actually leave.
    """

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
        self.policies = policies
        self.scheduled_sales = scheduled_sales
        # One lot-identity counter per (policy, sleeve), advanced only by an emitted Buy.
        self.lot_sequences: defaultdict[tuple[int, int], int] = defaultdict(int)
        # The CPI level of every month this household has seen, so an indexed band bound can be
        # read at its own reset month rather than only at the current one.
        self.cpi_levels: list[int] = []

    def decide(self, observation: Observation) -> list[Action]:
        if observation.cpi is not None:
            if len(self.cpi_levels) != observation.month:
                raise ValueError("the household reads every month's CPI once, in order")
            self.cpi_levels.append(observation.cpi[0])
        prices = {pool.asset_id: pool.price for pool in observation.holding_pools}
        actions: list[Action] = [
            self._scheduled(sale, observation) for sale in self.scheduled_sales if sale.month == observation.month
        ]
        pending: list[PendingBuy] = []
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
            pending.extend(proposal.buys)
        cash = dict(observation.accounts)
        for action in actions:
            account, proceeds = self._proceeds(action, observation)
            cash[account] = checked_count(cash[account] + proceeds, "projected cash")
        due: defaultdict[str, int] = defaultdict(int)
        for claim in observation.claims:
            due[claim.from_account.account_id] = checked_count(
                due[claim.from_account.account_id] + claim.amount_due, "claims due"
            )
        payments = [
            PayClaim(
                request_id=index + 1,
                cause_id=claim.cause_id,
                claim=claim,
                from_account=claim.from_account,
                amount=claim.amount_due,
            )
            for index, claim in enumerate(observation.claims)
            if cash[claim.from_account.account_id] >= due[claim.from_account.account_id]
        ]
        actions.extend(payments)
        for payment in payments:
            account = payment.from_account.account_id
            cash[account] = checked_count(cash[account] - payment.amount, "projected cash")
        actions.extend(self._purchases(observation, pending, cash))
        return actions

    def _purchases(self, observation: Observation, pending: list[PendingBuy], cash: dict[str, int]) -> list[Action]:
        """Exact orders, each sized from the cash left once this batch's sales and payments settle."""
        orders: list[Action] = []
        for buy in pending:
            key = (buy.policy_index, buy.sleeve_index)
            order = materialize_buy(
                observation.model_copy(update={"accounts": tuple(cash.items())}),
                buy,
                lot_sequence=self.lot_sequences[key],
            )
            if order is None:
                continue
            if isinstance(order, Buy):
                spent = position_value(buy.price, order.units, buy.quantity_scale)
                self.lot_sequences[key] += 1
            else:
                spent = order.amount
            cash[buy.cash_account_id] = checked_count(cash[buy.cash_account_id] - spent, "projected cash")
            orders.append(order)
        return orders

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
