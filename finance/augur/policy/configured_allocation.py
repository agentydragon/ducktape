"""Configured cash-band/drift proposals using the shared sleeve arithmetic.

Sales precede claim settlement. Pending purchases retain their chosen quantity
but are clamped to actual post-claim cash. The caller owns execution, current
quotes, resolved indexed bounds, and per-path lot identities; nothing here
settles a trade, estimates tax, or reaches into a managed component's holdings.
"""

from dataclasses import dataclass
from fractions import Fraction

from finance.augur.policy import sleeves
from finance.augur.policy.cash_band import Hold, Invest, Raise, cash_band
from finance.augur.sim.actions import Action, Buy, Contribute, Liquidate, Sell, Withdraw
from finance.augur.sim.fixed_point import quantity_for_value
from finance.augur.sim.observations import Observation, PublicPosition
from finance.augur.sim.prepared import _AllocationPolicy


@dataclass(frozen=True, kw_only=True)
class PendingBuy:
    policy_index: int
    sleeve_index: int
    cause_id_prefix: str
    agent_id: str
    cash_account_id: str
    holding_account_id: str
    asset_id: str
    wanted_units: int
    price: int
    quantity_scale: int


@dataclass(frozen=True)
class AllocationPlan:
    sales: list[Action]
    buys: list[PendingBuy]


def _quantity(amount: int, price: int, scale: int, *, round_up: bool) -> int:
    # A worthless managed index has no purchasable units. It may still report
    # internal rounding cash, which withdrawals below use without inferring units.
    return 0 if not price else quantity_for_value(max(0, amount), price, scale, round_up=round_up)


def _sources(policy: _AllocationPolicy) -> tuple[str, ...]:
    return policy.source_account_ids or (policy.account_id,)


def plan(
    observation: Observation,
    policy: _AllocationPolicy,
    *,
    policy_index: int,
    floor: int,
    ceiling: int,
    prices: dict[str, int],
) -> AllocationPlan:
    """Propose ordered source-account FIFO sales and post-claim purchase intents."""
    if observation.agent_id != policy.agent_id:
        raise ValueError("allocation policy received another actor's observation")
    sources = _sources(policy)
    cash = dict(observation.accounts)[policy.account_id]
    due = sum(
        claim.amount_due
        for claim in observation.claims
        if claim.from_account.agent_id == policy.agent_id and claim.from_account.account_id == policy.account_id
    )
    band = cash_band(projected_cash=cash - due, floor=floor, ceiling=ceiling)
    lots = [lot for lot in observation.public_positions if lot.account_id in sources]
    portfolios = [portfolio for portfolio in observation.tlh_portfolios if portfolio.account_id in sources]
    values = [
        sum(lot.value for lot in lots if lot.asset_id == sleeve.asset_id)
        + sum(portfolio.value for portfolio in portfolios if portfolio.asset_id == sleeve.asset_id)
        for sleeve in policy.sleeves
    ]
    weights = [sleeve.weight for sleeve in policy.sleeves]
    withdrawals = sleeves._allocate(values, weights, band.amount if isinstance(band, Raise) else 0, withdrawing=True)
    deposits = sleeves._allocate(values, weights, band.amount if isinstance(band, Invest) else 0, withdrawing=False)
    quiet = isinstance(band, Hold)
    if quiet and policy.rebalance_tolerance_ppb is not None:
        drift_sales, drift_buys = sleeves._rebalance_amounts(values, weights, policy.rebalance_tolerance_ppb)
    else:
        drift_sales = drift_buys = [0] * len(values)
    sales: list[Action] = []
    buys = []
    for index, sleeve in enumerate(policy.sleeves):
        price = prices[sleeve.asset_id]
        sleeves._count(price)
        if policy.allow_purchases:
            units = sleeves._count(
                _quantity(deposits[index], price, sleeve.quantity_scale, round_up=False)
                + _quantity(drift_buys[index], price, sleeve.quantity_scale, round_up=False)
            )
            if units:
                buys.append(
                    PendingBuy(
                        policy_index=policy_index,
                        sleeve_index=index,
                        cause_id_prefix=policy.cause_id_prefix,
                        agent_id=policy.agent_id,
                        cash_account_id=policy.account_id,
                        holding_account_id=sources[0],
                        asset_id=sleeve.asset_id,
                        wanted_units=units,
                        price=price,
                        quantity_scale=sleeve.quantity_scale,
                    )
                )
        selected = [lot for lot in lots if lot.asset_id == sleeve.asset_id and lot.units > 0]
        # Use an economic-unit grid, never sum raw counts from different lot grids.
        scale = max((lot.quantity_scale for lot in selected), default=sleeve.quantity_scale)
        wanted = Fraction(
            _quantity(withdrawals[index], price, scale, round_up=True)
            + _quantity(drift_sales[index], price, scale, round_up=False),
            scale,
        )
        full_exit = sleeve.weight == 0 and (
            (quiet and policy.rebalance_tolerance_ppb is not None) or withdrawals[index] == values[index] > 0
        )
        remaining_value = withdrawals[index] + drift_sales[index]
        cause_id = f"{policy.cause_id_prefix}_m{observation.month}_security:{sleeve.asset_id}"
        for account in sources:
            portfolio = next(
                (item for item in portfolios if item.account_id == account and item.asset_id == sleeve.asset_id), None
            )
            if portfolio is not None:
                amount = min(max(0, remaining_value), portfolio.value)
                if full_exit:
                    sales.append(
                        Liquidate(
                            cause_id=cause_id,
                            agent_id=policy.agent_id,
                            portfolio_id=portfolio.portfolio_id,
                            cash_account_id=policy.account_id,
                        )
                    )
                    remaining_value -= portfolio.value
                elif amount:
                    sales.append(
                        Withdraw(
                            cause_id=cause_id,
                            agent_id=policy.agent_id,
                            portfolio_id=portfolio.portfolio_id,
                            cash_account_id=policy.account_id,
                            amount=amount,
                        )
                    )
                    remaining_value -= amount
                wanted = Fraction(_quantity(remaining_value, price, scale, round_up=withdrawals[index] > 0), scale)
            candidates: list[PublicPosition] = sorted(
                (lot for lot in selected if lot.account_id == account), key=lambda lot: (lot.purchase_month, lot.lot_id)
            )
            lot_sales, proceeds = sleeves._sale_lots(candidates, 0, full_exit=full_exit, unit_target=wanted)
            if lot_sales:
                sales.append(
                    Sell(
                        cause_id=cause_id,
                        agent_id=policy.agent_id,
                        proceeds_account_id=policy.account_id,
                        asset_id=sleeve.asset_id,
                        lots=lot_sales,
                    )
                )
                scales = {lot.lot_id: lot.quantity_scale for lot in candidates}
                wanted -= sum(Fraction(lot.units, scales[lot.lot_id]) for lot in lot_sales)
                remaining_value -= proceeds
    return AllocationPlan(sales=sales, buys=buys)


def materialize_buy(observation: Observation, pending: PendingBuy, *, lot_sequence: int) -> Buy | Contribute | None:
    """Clamp one planned purchase to fresh cash after claims and earlier purchases.

    The driver increments the per-path policy/sleeve sequence only when this
    returns an ordinary Buy. A managed contribution has no household-visible lot.
    """
    if observation.agent_id != pending.agent_id:
        raise ValueError("pending purchase received another actor's observation")
    cash = max(0, dict(observation.accounts)[pending.cash_account_id])
    units = min(pending.wanted_units, _quantity(cash, pending.price, pending.quantity_scale, round_up=False))
    if not units:
        return None
    cause_id = f"{pending.cause_id_prefix}_buy_m{observation.month}_security:{pending.asset_id}"
    portfolio = next(
        (
            item
            for item in observation.tlh_portfolios
            if item.account_id == pending.holding_account_id and item.asset_id == pending.asset_id
        ),
        None,
    )
    if portfolio is not None:
        amount = sleeves._quoted_value(units, pending.price, pending.quantity_scale)
        if not amount:
            return None
        return Contribute(
            cause_id=cause_id,
            agent_id=pending.agent_id,
            portfolio_id=portfolio.portfolio_id,
            cash_account_id=pending.cash_account_id,
            amount=amount,
        )
    sleeves._count(lot_sequence)
    return Buy(
        cause_id=cause_id,
        agent_id=pending.agent_id,
        cash_account_id=pending.cash_account_id,
        holding_account_id=pending.holding_account_id,
        asset_id=pending.asset_id,
        lot_id=f"{pending.cause_id_prefix}_buy_p{pending.policy_index}_s{pending.sleeve_index}_{lot_sequence}",
        units=units,
        quantity_scale=pending.quantity_scale,
    )
