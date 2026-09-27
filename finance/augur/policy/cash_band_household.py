"""A household that holds one cash account in a band, funded from target-weighted sleeves.

Each month it proposes the sales the band and drift call for, pays every due claim in full
in observed order, then (when it reinvests) buys with the cash those leave. A security
sleeve trades whole units of its lots at the pool's quote; a managed sleeve trades money
with its portfolio and has no quote. Sales are sized before claims settle; purchases are
clamped to the cash the month will actually leave. Nothing here settles a trade, estimates
tax, or reaches into a managed component's holdings.
"""

from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction

from finance.augur.policy import sleeves
from finance.augur.policy.cash_band import Hold, Invest, Raise, cash_band, validate_band_bounds
from finance.augur.policy.funding import full_payments
from finance.augur.sim.actions import Action, Buy, Contribute, Liquidate, Sell, Withdraw
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.fixed_point import quantity_for_value
from finance.augur.sim.holdings import private_issuer
from finance.augur.sim.ids import AccountId, AgentId, AssetId, LotId, PortfolioId
from finance.augur.sim.money import checked_count, mul_div, position_value
from finance.augur.sim.observations import Observation, PublicPosition, TlhPortfolioObservation
from finance.augur.sim.world import World


@dataclass(frozen=True, kw_only=True)
class SecuritySleeve:
    """Lots of one public security across the source accounts, traded in whole units at its quote."""

    asset_id: AssetId
    weight: int


@dataclass(frozen=True, kw_only=True)
class ManagedSleeve:
    """One managed portfolio, sized in money: it has a value but no units and no unit price."""

    portfolio_id: PortfolioId
    weight: int


type Sleeve = SecuritySleeve | ManagedSleeve


@dataclass(frozen=True, kw_only=True)
class CpiIndexed:
    """`base_amount` at month 0, rescaled by CPI at month 0 and every `adjustment_period_months` after."""

    base_amount: int
    adjustment_period_months: int


type BandBound = int | CpiIndexed


@dataclass(frozen=True, kw_only=True)
class Reinvest:
    """Invest cash projected above the ceiling down to the floor, into the most underweight sleeves.

    With a drift tolerance, a month whose cash stays inside the band also rebalances every sleeve
    once any one strays that far (relative to its own target) from it.
    """

    rebalance_tolerance_ppb: int | None


@dataclass(frozen=True, kw_only=True)
class PendingBuy:
    sleeve_index: int
    asset_id: AssetId
    wanted_units: int
    price: int
    quantity_scale: int


@dataclass(frozen=True, kw_only=True)
class PendingContribution:
    """Money for a managed sleeve: the portfolio takes exactly this amount, no unit grid."""

    portfolio_id: PortfolioId
    wanted_amount: int


@dataclass(frozen=True)
class Proposal:
    sales: list[Action]
    purchases: list[PendingBuy | PendingContribution]


@dataclass(frozen=True, kw_only=True)
class _SleeveOrders:
    """One sleeve's money budgets for this month, from the shared water-fill and drift arithmetic."""

    withdrawal: int
    deposit: int
    drift_sale: int
    drift_buy: int
    full_exit: bool


def _label(sleeve: Sleeve) -> str:
    """The sleeve's identity within the household, as its orders' cause IDs name it."""
    if isinstance(sleeve, SecuritySleeve):
        return f"security:{sleeve.asset_id}"
    return f"portfolio:{sleeve.portfolio_id}"


def _quantity(amount: int, price: int, scale: int, *, round_up: bool) -> int:
    return quantity_for_value(max(0, amount), price, scale, round_up=round_up)


def _validate(*, targets: tuple[Sleeve, ...], reinvest: Reinvest | None, floor: BandBound, ceiling: BandBound) -> None:
    """The household's own knobs, before any world is in view."""
    sleeves._validate_values([0] * len(targets), [target.weight for target in targets])
    if len({_label(target) for target in targets}) != len(targets):
        raise ValueError("duplicate funding sleeve")
    if reinvest is not None and reinvest.rebalance_tolerance_ppb is not None:
        sleeves._count(reinvest.rebalance_tolerance_ppb)
    validate_band_bounds(floor=_base_amount(floor), ceiling=_base_amount(ceiling))


def _base_amount(bound: BandBound) -> int:
    if isinstance(bound, int):
        return sleeves._count(bound)
    if not 0 < bound.adjustment_period_months < 1 << 32:
        raise ValueError("an indexed band bound has an invalid reset period")
    return sleeves._count(bound.base_amount)


class CashBandHousehold(EconomicAgent):
    """Keeps `cash_account_id` inside [floor, ceiling] by selling toward the sleeves' target weights.

    Crossing below the floor raises cash back to the ceiling, taking from the most overweight
    sleeves first. Sources are sold FIFO within each account, the accounts in the order given;
    a purchase lands in the first source account. A zero-weight sleeve stays sellable: a raise
    takes it first and drift rebalancing exits it. Holdings no sleeve names are never sold.
    """

    def __init__(
        self,
        agent_id: AgentId,
        *,
        cash_account_id: AccountId,
        floor: BandBound,
        ceiling: BandBound,
        sleeves: tuple[Sleeve, ...],
        source_account_ids: tuple[AccountId, ...],
        reinvest: Reinvest | None,
        cause_id_prefix: str,
    ) -> None:
        super().__init__(agent_id)
        if not cause_id_prefix.strip():
            raise ValueError("a funding household requires a nonempty cause prefix")
        if not source_account_ids or len(set(source_account_ids)) != len(source_account_ids):
            raise ValueError("funding source accounts must be unique and nonempty")
        _validate(targets=sleeves, reinvest=reinvest, floor=floor, ceiling=ceiling)
        self.cash_account_id = cash_account_id
        self.floor = floor
        self.ceiling = ceiling
        self.sleeves = sleeves
        self.source_account_ids = source_account_ids
        self.reinvest = reinvest
        self.cause_id_prefix = cause_id_prefix
        # One lot-identity counter per sleeve, advanced only by an emitted Buy.
        self.lot_sequences: defaultdict[int, int] = defaultdict(int)
        # The CPI level of every month this household has seen, so an indexed band bound can be
        # read at its own reset month rather than only at the current one.
        self.cpi_levels: list[int] = []

    def check(self, world: World) -> None:
        """Refuse sleeves, accounts and indices `world` does not declare; call before tracking."""
        if not any(
            (account.agent_id, account.account_id) == (self.agent_id, self.cash_account_id)
            for account in world.accounting.declared
        ):
            raise ValueError(f"undeclared funding account {self.cash_account_id!r}")
        for bound in (self.floor, self.ceiling):
            if isinstance(bound, CpiIndexed):
                if "inflation" not in world.market.series:
                    raise ValueError("an indexed band bound is missing series 'inflation'")
                for month in range(0, world.horizon_months, bound.adjustment_period_months):
                    if not sleeves._count(world.market.value("inflation", month)):
                        raise ValueError("an indexed band bound requires positive index levels")
        pools = {(pool.agent_id, pool.account_id, pool.asset_id): pool.quantity_scale for pool in world.holdings.pools}
        for index, sleeve in enumerate(self.sleeves):
            if isinstance(sleeve, ManagedSleeve):
                spec = world.specs.get(sleeve.portfolio_id)
                if (
                    spec is None
                    or spec.owner_agent_id != self.agent_id
                    or spec.account_id not in self.source_account_ids
                ):
                    raise ValueError(
                        f"sleeve names portfolio {sleeve.portfolio_id!r}, which is not declared in one of its source accounts"
                    )
                continue
            # The household prices the sleeve off the quote its observation carries for a public pool.
            if private_issuer(sleeve.asset_id) is not None or not any(
                (agent_id, asset_id) == (self.agent_id, sleeve.asset_id) for agent_id, _, asset_id in pools
            ):
                raise ValueError(f"sleeve {sleeve.asset_id!r} has no priced holding pool")
            scales = {
                pools[(self.agent_id, source, sleeve.asset_id)]
                for source in self.source_account_ids
                if (self.agent_id, source, sleeve.asset_id) in pools
            }
            if len(scales) > 1:
                raise ValueError(f"sleeve {sleeve.asset_id!r} source holding pools disagree on the quantity grid")
            if self.reinvest is None:
                continue
            destination = (self.agent_id, self.source_account_ids[0], sleeve.asset_id)
            if destination not in pools or destination in world.holdings.managed:
                raise ValueError("purchase pool is not declared, or a managed portfolio owns it")
            prefix = self._lot_prefix(index)
            for lot in world.holdings.lots:
                suffix = lot.spec.lot_id.removeprefix(prefix)
                if (
                    lot.spec.lot_id.startswith(prefix)
                    and suffix.isascii()
                    and suffix.isdigit()
                    and str(int(suffix)) == suffix
                    and int(suffix) < 1 << 32
                ):
                    raise ValueError(f"opening lot {lot.spec.lot_id!r} uses a reserved purchase identity")

    def decide(self, observation: Observation) -> list[Action]:
        """This month's sales, then every due claim paid in full in observed order, then the purchases.

        Each purchase is sized from the cash the sales, the payments and the earlier purchases
        leave, so each order is exact against the cash the month will actually leave. A claim
        that cash cannot cover is rejected and stops the path, so a month with one buys nothing.
        """
        proposal = self.propose(observation)
        cash = dict(observation.accounts)
        for action in proposal.sales:
            account, proceeds = _proceeds(action, observation)
            cash[account] = checked_count(cash[account] + proceeds, "projected cash")
        payments = full_payments(observation.claims)
        for payment in payments:
            account = payment.from_account.account_id
            cash[account] = checked_count(cash[account] - payment.amount, "projected cash")
        actions: list[Action] = [*proposal.sales, *payments]
        if any(cash[payment.from_account.account_id] < 0 for payment in payments):
            return actions
        for pending in proposal.purchases:
            projected = observation.model_copy(update={"accounts": tuple(cash.items())})
            order: Buy | Contribute | None
            if isinstance(pending, PendingContribution):
                order = self.contribution(projected, pending)
                spent = 0 if order is None else order.amount
            else:
                order = self.buy(projected, pending)
                spent = 0 if order is None else position_value(pending.price, order.units, pending.quantity_scale)
            if order is None:
                continue
            cash[self.cash_account_id] = checked_count(cash[self.cash_account_id] - spent, "projected cash")
            actions.append(order)
        return actions

    def propose(self, observation: Observation) -> Proposal:
        """Ordered sales and post-claim purchase intents, sleeve by sleeve; call once a month, in order."""
        if observation.cpi is not None:
            if len(self.cpi_levels) != observation.month:
                raise ValueError("the household reads every month's CPI once, in order")
            self.cpi_levels.append(observation.cpi[0])
        prices = {pool.asset_id: pool.price for pool in observation.holding_pools}
        cash = dict(observation.accounts)[self.cash_account_id]
        due = sum(
            claim.amount_due
            for claim in observation.claims
            if claim.from_account.agent_id == self.agent_id and claim.from_account.account_id == self.cash_account_id
        )
        band = cash_band(
            projected_cash=cash - due,
            floor=self._bound(self.floor, observation),
            ceiling=self._bound(self.ceiling, observation),
        )
        lots = [lot for lot in observation.public_positions if lot.account_id in self.source_account_ids]
        portfolios = {portfolio.portfolio_id: portfolio for portfolio in observation.tlh_portfolios}
        values = [
            sum(lot.value for lot in lots if lot.asset_id == sleeve.asset_id)
            if isinstance(sleeve, SecuritySleeve)
            else portfolios[sleeve.portfolio_id].value
            for sleeve in self.sleeves
        ]
        weights = [sleeve.weight for sleeve in self.sleeves]
        withdrawals = sleeves._allocate(
            values, weights, band.amount if isinstance(band, Raise) else 0, withdrawing=True
        )
        deposits = sleeves._allocate(values, weights, band.amount if isinstance(band, Invest) else 0, withdrawing=False)
        tolerance = None if self.reinvest is None else self.reinvest.rebalance_tolerance_ppb
        drifting = isinstance(band, Hold) and tolerance is not None
        if drifting and tolerance is not None:
            drift_sales, drift_buys = sleeves._rebalance_amounts(values, weights, tolerance)
        else:
            drift_sales = drift_buys = [0] * len(values)
        sales: list[Action] = []
        purchases: list[PendingBuy | PendingContribution] = []
        for index, sleeve in enumerate(self.sleeves):
            orders = _SleeveOrders(
                withdrawal=withdrawals[index],
                deposit=deposits[index],
                drift_sale=drift_sales[index],
                drift_buy=drift_buys[index],
                full_exit=sleeve.weight == 0 and (drifting or withdrawals[index] == values[index] > 0),
            )
            cause_id = f"{self.cause_id_prefix}_m{observation.month}_{_label(sleeve)}"
            if isinstance(sleeve, ManagedSleeve):
                portfolio = portfolios[sleeve.portfolio_id]
                sales.extend(self._managed_sales(portfolio, orders, cause_id))
                amount = sleeves._count(orders.deposit + orders.drift_buy)
                # At a zero index mark the portfolio refuses money, and the world would stop the path on it.
                if self.reinvest is not None and amount and portfolio.accepts_contributions:
                    purchases.append(PendingContribution(portfolio_id=portfolio.portfolio_id, wanted_amount=amount))
                continue
            price = sleeves._count(prices[sleeve.asset_id])
            held = [lot for lot in lots if lot.asset_id == sleeve.asset_id]
            sales.extend(self._security_sales(sleeve, held, price, orders, cause_id))
            if self.reinvest is None:
                continue
            scale = next(
                pool.quantity_scale
                for pool in observation.holding_pools
                if (pool.account_id, pool.asset_id) == (self.source_account_ids[0], sleeve.asset_id)
            )
            units = sleeves._count(
                _quantity(orders.deposit, price, scale, round_up=False)
                + _quantity(orders.drift_buy, price, scale, round_up=False)
            )
            if units:
                purchases.append(
                    PendingBuy(
                        sleeve_index=index,
                        asset_id=sleeve.asset_id,
                        wanted_units=units,
                        price=price,
                        quantity_scale=scale,
                    )
                )
        return Proposal(sales=sales, purchases=purchases)

    def contribution(self, observation: Observation, pending: PendingContribution) -> Contribute | None:
        """Clamp a planned contribution to `observation`'s cash; a managed contribution has no visible lot."""
        amount = min(pending.wanted_amount, max(0, dict(observation.accounts)[self.cash_account_id]))
        if not amount:
            return None
        return Contribute(
            cause_id=f"{self.cause_id_prefix}_buy_m{observation.month}_portfolio:{pending.portfolio_id}",
            agent_id=self.agent_id,
            portfolio_id=pending.portfolio_id,
            cash_account_id=self.cash_account_id,
            amount=amount,
        )

    def buy(self, observation: Observation, pending: PendingBuy) -> Buy | None:
        """Clamp a planned purchase to `observation`'s cash; each emitted Buy opens the sleeve's next lot."""
        cash = max(0, dict(observation.accounts)[self.cash_account_id])
        units = min(pending.wanted_units, _quantity(cash, pending.price, pending.quantity_scale, round_up=False))
        if not units:
            return None
        sequence = sleeves._count(self.lot_sequences[pending.sleeve_index])
        self.lot_sequences[pending.sleeve_index] += 1
        return Buy(
            cause_id=f"{self.cause_id_prefix}_buy_m{observation.month}_security:{pending.asset_id}",
            agent_id=self.agent_id,
            cash_account_id=self.cash_account_id,
            holding_account_id=self.source_account_ids[0],
            asset_id=pending.asset_id,
            lot_id=LotId(f"{self._lot_prefix(pending.sleeve_index)}{sequence}"),
            units=units,
            quantity_scale=pending.quantity_scale,
        )

    def _lot_prefix(self, sleeve_index: int) -> str:
        return f"{self.cause_id_prefix}_buy_s{sleeve_index}_"

    def _managed_sales(self, portfolio: TlhPortfolioObservation, orders: _SleeveOrders, cause_id: str) -> list[Action]:
        """The portfolio's exact money: all of it on a full exit, else what the band and drift ask for."""
        if orders.full_exit:
            return [
                Liquidate(
                    cause_id=cause_id,
                    agent_id=self.agent_id,
                    portfolio_id=portfolio.portfolio_id,
                    cash_account_id=self.cash_account_id,
                )
            ]
        amount = min(orders.withdrawal + orders.drift_sale, portfolio.value)
        if not amount:
            return []
        return [
            Withdraw(
                cause_id=cause_id,
                agent_id=self.agent_id,
                portfolio_id=portfolio.portfolio_id,
                cash_account_id=self.cash_account_id,
                amount=amount,
            )
        ]

    def _security_sales(
        self, sleeve: SecuritySleeve, held: list[PublicPosition], price: int, orders: _SleeveOrders, cause_id: str
    ) -> list[Action]:
        """Whole units, FIFO within each source account, the accounts in the household's order."""
        selected = [lot for lot in held if lot.units > 0]
        if not selected:
            return []
        # Use an economic-unit grid, never sum raw counts from different lot grids.
        scale = max(lot.quantity_scale for lot in selected)
        wanted = Fraction(
            _quantity(orders.withdrawal, price, scale, round_up=True)
            + _quantity(orders.drift_sale, price, scale, round_up=False),
            scale,
        )
        sales: list[Action] = []
        for account in self.source_account_ids:
            candidates = sorted(
                (lot for lot in selected if lot.account_id == account), key=lambda lot: (lot.purchase_month, lot.lot_id)
            )
            lot_sales, _ = sleeves.sale_lots(candidates, 0, full_exit=orders.full_exit, unit_target=wanted)
            if lot_sales:
                sales.append(
                    Sell(
                        cause_id=cause_id,
                        agent_id=self.agent_id,
                        proceeds_account_id=self.cash_account_id,
                        asset_id=sleeve.asset_id,
                        lots=tuple(lot_sales),
                    )
                )
                scales = {lot.lot_id: lot.quantity_scale for lot in candidates}
                wanted -= sum(Fraction(lot.units, scales[lot.lot_id]) for lot in lot_sales)
        return sales

    def _bound(self, bound: BandBound, observation: Observation) -> int:
        """A band bound as money this month; an indexed one rides the CPI level of its last reset."""
        if isinstance(bound, int):
            return bound
        if observation.cpi is None or not self.cpi_levels:
            raise ValueError("an inflation-indexed band bound needs a modeled CPI")
        reset = observation.month // bound.adjustment_period_months * bound.adjustment_period_months
        return mul_div(bound.base_amount, self.cpi_levels[reset], self.cpi_levels[0], "series-indexed amount")


def _proceeds(action: Action, observation: Observation) -> tuple[AccountId, int]:
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
    raise TypeError(f"a funding sale is not a {type(action).__name__}")
