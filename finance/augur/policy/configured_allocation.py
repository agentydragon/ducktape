"""Configured cash-band/drift proposals using the shared sleeve arithmetic.

Sales precede claim settlement. Pending purchases retain their chosen quantity
(a managed sleeve's, its chosen amount) but are clamped to actual post-claim cash. The caller owns execution, current
quotes, resolved indexed bounds, and per-path lot identities; nothing here
settles a trade, estimates tax, or reaches into a managed component's holdings.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction

from more_itertools import only

from finance.augur.policy import sleeves
from finance.augur.policy.cash_band import Hold, Invest, Raise, cash_band, validate_band_bounds
from finance.augur.sim.actions import Action, Buy, Contribute, Liquidate, Sell, Withdraw
from finance.augur.sim.fixed_point import quantity_for_value
from finance.augur.sim.observations import Observation, PublicPosition
from finance.augur.sim.prepared import PreparedAmount, PreparedFixedAmount, _AllocationPolicy
from finance.augur.sim.world import World


def _base_bound(amount: PreparedAmount, world: World) -> int:
    if isinstance(amount, int):
        return sleeves._count(amount)
    if isinstance(amount, PreparedFixedAmount):
        return sleeves._count(amount.amount)
    base = sleeves._count(amount.base_amount)
    if not 0 < amount.adjustment_period_months < 1 << 32 or not 0 <= amount.base_month_index < 1 << 32:
        raise ValueError("allocation indexed bound has an invalid base month or reset period")
    if amount.base_month_index != 0:
        raise ValueError("allocation indexed bound starts before its base month")
    if amount.series_id != "inflation" and not (amount.series_id.startswith("rent:") and len(amount.series_id) > 5):
        raise ValueError("allocation indexed bounds require inflation or a rent series")
    if amount.series_id not in world.market.series:
        raise ValueError(f"allocation indexed bound is missing series {amount.series_id!r}")
    for month in {amount.base_month_index, *range(0, world.horizon_months, amount.adjustment_period_months)}:
        if not sleeves._count(world.market.value(amount.series_id, month)):
            raise ValueError("allocation indexed bound requires positive index levels")
    return base


def check_policies(world: World, policies: Iterable[_AllocationPolicy]) -> None:
    """Refuse configured policies that are malformed or name what `world` does not declare.

    Native accounting validates its own facts. These guards belong here because
    configured policy records are not sent to the financial kernel.
    """
    accounts = {(account.agent_id, account.account_id) for account in world.accounting.declared}
    pools = {(pool.agent_id, pool.account_id, pool.asset_id): pool.quantity_scale for pool in world.holdings.pools}
    funding = set()
    for policy_index, policy in enumerate(policies):
        key = (policy.agent_id, policy.account_id)
        if not policy.cause_id_prefix.strip() or key not in accounts:
            raise ValueError("allocation requires a nonempty cause and declared funding account")
        if key in funding:
            raise ValueError("duplicate allocation funding account")
        funding.add(key)
        sources = _sources(policy)
        if len(set(sources)) != len(sources):
            raise ValueError("allocation source accounts must be unique")
        weights = [item.weight for item in policy.sleeves]
        sleeves._validate_values([0] * len(weights), weights)
        if len({item.asset_id for item in policy.sleeves}) != len(policy.sleeves):
            raise ValueError("duplicate allocation sleeve")
        tolerance = policy.rebalance_tolerance_ppb
        if tolerance is not None:
            sleeves._count(tolerance)
            if not policy.allow_purchases:
                raise ValueError("allocation drift requires purchases")
        validate_band_bounds(
            floor=_base_bound(policy.cash_floor, world), ceiling=_base_bound(policy.cash_ceiling, world)
        )
        for sleeve_index, sleeve in enumerate(policy.sleeves):
            scale = sleeves._count(sleeve.quantity_scale)
            if not sleeve.asset_id.strip() or str(scale).rstrip("0") != "1":
                raise ValueError("allocation requires an asset identity and a power-of-ten quantity grid")
            scales = {
                pools[(policy.agent_id, source, sleeve.asset_id)]
                for source in sources
                if (policy.agent_id, source, sleeve.asset_id) in pools
            }
            if scales and scales != {scale}:
                raise ValueError("allocation quantity grid disagrees with a source holding pool")
            if not policy.allow_purchases:
                continue
            destination = (policy.agent_id, sources[0], sleeve.asset_id)
            if destination not in pools and destination not in world.holdings.managed:
                raise ValueError("allocation purchase pool is not declared")
            prefix = f"{policy.cause_id_prefix}_buy_p{policy_index}_s{sleeve_index}_"
            for lot in world.holdings.lots:
                suffix = lot.spec.lot_id.removeprefix(prefix)
                if (
                    lot.spec.lot_id.startswith(prefix)
                    and suffix.isascii()
                    and suffix.isdigit()
                    and str(int(suffix)) == suffix
                    and int(suffix) < 1 << 32
                ):
                    raise ValueError(f"opening lot {lot.spec.lot_id!r} uses a reserved allocation-purchase identity")


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


@dataclass(frozen=True, kw_only=True)
class PendingContribution:
    """Money for a managed sleeve: the portfolio takes exactly this amount, no unit grid."""

    cause_id_prefix: str
    agent_id: str
    cash_account_id: str
    portfolio_id: str
    asset_id: str
    wanted_amount: int


@dataclass(frozen=True)
class AllocationPlan:
    sales: list[Action]
    buys: list[PendingBuy | PendingContribution]


def _quantity(amount: int, price: int, scale: int, *, round_up: bool) -> int:
    # A worthless managed index has no units to size a lot sale against.
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
    buys: list[PendingBuy | PendingContribution] = []
    for index, sleeve in enumerate(policy.sleeves):
        price = prices[sleeve.asset_id]
        sleeves._count(price)
        destination = only(
            (item for item in portfolios if item.account_id == sources[0] and item.asset_id == sleeve.asset_id), None
        )
        if policy.allow_purchases and destination is not None:
            amount = sleeves._count(deposits[index] + drift_buys[index])
            # At a zero index mark the portfolio refuses money, and the world would stop the path on it.
            if amount and destination.accepts_contributions:
                buys.append(
                    PendingContribution(
                        cause_id_prefix=policy.cause_id_prefix,
                        agent_id=policy.agent_id,
                        cash_account_id=policy.account_id,
                        portfolio_id=destination.portfolio_id,
                        asset_id=sleeve.asset_id,
                        wanted_amount=amount,
                    )
                )
        elif policy.allow_purchases:
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
                        lots=tuple(lot_sales),
                    )
                )
                scales = {lot.lot_id: lot.quantity_scale for lot in candidates}
                wanted -= sum(Fraction(lot.units, scales[lot.lot_id]) for lot in lot_sales)
                remaining_value -= proceeds
    return AllocationPlan(sales=sales, buys=buys)


def materialize_buy(observation: Observation, pending: PendingBuy, *, lot_sequence: int) -> Buy | None:
    """Clamp one planned purchase to fresh cash after claims and earlier purchases.

    The driver increments the per-path policy/sleeve sequence only when this returns a Buy.
    """
    if observation.agent_id != pending.agent_id:
        raise ValueError("pending purchase received another actor's observation")
    cash = max(0, dict(observation.accounts)[pending.cash_account_id])
    units = min(pending.wanted_units, _quantity(cash, pending.price, pending.quantity_scale, round_up=False))
    if not units:
        return None
    sleeves._count(lot_sequence)
    return Buy(
        cause_id=f"{pending.cause_id_prefix}_buy_m{observation.month}_security:{pending.asset_id}",
        agent_id=pending.agent_id,
        cash_account_id=pending.cash_account_id,
        holding_account_id=pending.holding_account_id,
        asset_id=pending.asset_id,
        lot_id=f"{pending.cause_id_prefix}_buy_p{pending.policy_index}_s{pending.sleeve_index}_{lot_sequence}",
        units=units,
        quantity_scale=pending.quantity_scale,
    )


def materialize_contribution(observation: Observation, pending: PendingContribution) -> Contribute | None:
    """Clamp one planned contribution to fresh cash after claims and earlier purchases.

    A managed contribution has no household-visible lot.
    """
    if observation.agent_id != pending.agent_id:
        raise ValueError("pending contribution received another actor's observation")
    amount = min(pending.wanted_amount, max(0, dict(observation.accounts)[pending.cash_account_id]))
    if not amount:
        return None
    return Contribute(
        cause_id=f"{pending.cause_id_prefix}_buy_m{observation.month}_security:{pending.asset_id}",
        agent_id=pending.agent_id,
        portfolio_id=pending.portfolio_id,
        cash_account_id=pending.cash_account_id,
        amount=amount,
    )
