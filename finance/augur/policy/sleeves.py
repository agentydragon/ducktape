"""Public-portfolio policy proposals over explicitly selected account/asset pools.

Weights are relative nonnegative integers; zero targets remain sellable but receive
no deposits. Money is integer currency quanta. Helpers neither settle trades nor
estimate taxes. Callers reserve claims/consumption and submit the ordered actions.
Independent calls on the same observation do not reserve each other's lots or cash.
"""

from fractions import Fraction

from finance.augur.sim.actions import Action, Buy, LotSale, Sell
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE, quantity_for_value
from finance.augur.sim.observations import HoldingPool, Observation, PublicPosition


def _count(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("sleeve amounts and weights must be integer counts")
    if not 0 <= value < 1 << 63:
        raise ValueError("sleeve amounts and weights must fit nonnegative signed-64-bit counts")
    return value


def _allocate(values: list[int], weights: list[int], amount: int, *, withdrawing: bool) -> list[int]:
    """Water-fill by value/weight; conserve the exact budget after currency rounding."""
    _validate_values(values, weights)
    _count(amount)
    _count(sum(values))
    wanted = min(amount, sum(values)) if withdrawing else amount
    result = [0] * len(values)
    if withdrawing:
        for index, weight in enumerate(weights):
            if weight == 0:
                result[index] = min(values[index], wanted)
                wanted -= result[index]
    if not wanted:
        return result
    order = sorted(
        (index for index, weight in enumerate(weights) if weight),
        key=lambda index: Fraction(-values[index] if withdrawing else values[index], weights[index]),
    )
    prefix_value = prefix_weight = 0
    for rank, index in enumerate(order):
        prefix_value += values[index]
        prefix_weight += weights[index]
        level = Fraction(prefix_value - wanted if withdrawing else prefix_value + wanted, prefix_weight)
        if rank == len(order) - 1:
            break
        following = Fraction(values[order[rank + 1]], weights[order[rank + 1]])
        if (withdrawing and level >= following) or (not withdrawing and level <= following):
            break
    adjusted = []
    for value, weight in zip(values, weights, strict=True):
        target = (2 * level.numerator * weight + level.denominator) // (2 * level.denominator)
        adjusted.append(min(value, max(0, value - target)) if withdrawing else max(0, target - value))
        if not weight:
            adjusted[-1] = 0
    residual = wanted - sum(adjusted)
    capacities = [
        (value - change if withdrawing else wanted) if residual > 0 else change
        for value, change in zip(values, adjusted, strict=True)
    ]
    for index in sorted(range(len(values)), key=lambda index: -capacities[index]):
        if not weights[index]:
            continue
        change = min(abs(residual), capacities[index])
        adjusted[index] += change if residual > 0 else -change
        residual += -change if residual > 0 else change
        if not residual:
            break
    if residual:
        raise ValueError("allocation residual exceeds available sleeve capacity")
    return [a + b for a, b in zip(result, adjusted, strict=True)]


def _validate_values(values: list[int], weights: list[int]) -> None:
    if not values or len(values) != len(weights) or not any(weights):
        raise ValueError("allocation needs matching nonempty values and at least one positive target")
    for value in (*values, *weights):
        _count(value)


def _rebalance_amounts(
    values: list[int], weights: list[int], tolerance_ppb: int, *, force: bool = False
) -> tuple[list[int], list[int]]:
    """All sleeves rebalance together after any relative-drift threshold is reached."""
    _validate_values(values, weights)
    _count(tolerance_ppb)
    total = _count(sum(values))
    weight_total = sum(weights)
    wanted = [total * weight // weight_total for weight in weights]
    drifts = [value - target for value, target in zip(values, wanted, strict=True)]
    fires = any(
        (weight == 0 and value > 0) or (target > 0 and abs(drift) * MONEY_FACTOR_SCALE >= tolerance_ppb * target)
        for value, weight, target, drift in zip(values, weights, wanted, drifts, strict=True)
    )
    if not fires and not force:
        return [0] * len(values), [0] * len(values)
    return [max(0, drift) for drift in drifts], [max(0, -drift) for drift in drifts]


def _pools(
    observation: Observation, targets: dict[tuple[str, str], int], cash_account_id: str
) -> list[tuple[HoldingPool, list[PublicPosition], int]]:
    if cash_account_id not in dict(observation.accounts):
        raise ValueError("funding account must belong to the observed actor")
    if not targets or not any(targets.values()):
        raise ValueError("at least one selected sleeve must have a positive target")
    pools = {(pool.account_id, pool.asset_id): pool for pool in observation.holding_pools}
    selected = []
    for key, weight in targets.items():
        _count(weight)
        if key not in pools:
            raise ValueError(f"undeclared holding pool {key}")
        lots = sorted(
            (lot for lot in observation.public_positions if (lot.account_id, lot.asset_id) == key),
            key=lambda lot: (lot.purchase_month, lot.lot_id),
        )
        selected.append((pools[key], lots, weight))
    return selected


def _quoted_value(units: int, price: int, scale: int) -> int:
    return _count((2 * units * price + scale) // (2 * scale))


def _sale_lots(
    lots: list[PublicPosition], amount: int, *, full_exit: bool = False, unit_target: Fraction | None = None
) -> tuple[list[LotSale], int]:
    """Select an ordered lot prefix by gross money or an explicit economic-unit budget.

    Lots keep their own grids and per-lot proceeds rounding. A full exit selects
    every unit, including positions whose entire mark rounds to zero.
    """
    selected = []
    proceeds = 0
    remaining = amount
    for lot in lots:
        if not full_exit and (unit_target <= 0 if unit_target is not None else remaining <= 0):
            break
        if full_exit:
            units = lot.units
        elif unit_target is not None:
            units = min(lot.units, unit_target.numerator * lot.quantity_scale // unit_target.denominator)
        else:
            units = (
                lot.units
                if remaining >= lot.value
                else min(lot.units, quantity_for_value(remaining, lot.price, lot.quantity_scale, round_up=True))
            )
        if not units:
            continue
        value = _quoted_value(units, lot.price, lot.quantity_scale)
        remaining -= value
        proceeds += value
        if unit_target is not None:
            unit_target -= Fraction(units, lot.quantity_scale)
        selected.append(LotSale(account_id=lot.account_id, lot_id=lot.lot_id, units=units))
    return selected, _count(proceeds)


def _sales(
    observation: Observation,
    selected: list[tuple[HoldingPool, list[PublicPosition], int]],
    amounts: list[int],
    full_exits: list[bool],
    cash_account_id: str,
    cause_id: str,
) -> tuple[list[Action], int]:
    actions: list[Action] = []
    proceeds = 0
    for index, ((pool, lots, _), amount, full_exit) in enumerate(zip(selected, amounts, full_exits, strict=True)):
        sale_lots, raised = _sale_lots(lots, amount, full_exit=full_exit)
        proceeds += raised
        if sale_lots:
            actions.append(
                Sell(
                    cause_id=f"{cause_id}-sell-{index}",
                    agent_id=observation.agent_id,
                    proceeds_account_id=cash_account_id,
                    asset_id=pool.asset_id,
                    lots=tuple(sale_lots),
                )
            )
    return actions, _count(proceeds)


def _buys(
    observation: Observation,
    selected: list[tuple[HoldingPool, list[PublicPosition], int]],
    amounts: list[int],
    cash_account_id: str,
    cash_budget: int,
    cause_id: str,
) -> list[Action]:
    actions: list[Action] = []
    for index, ((pool, _, weight), amount) in enumerate(zip(selected, amounts, strict=True)):
        if not amount or not weight:
            continue
        units = quantity_for_value(min(amount, cash_budget), pool.price, pool.quantity_scale, round_up=False)
        if not units:
            continue
        cash_budget -= _quoted_value(units, pool.price, pool.quantity_scale)
        actions.append(
            Buy(
                cause_id=f"{cause_id}-buy-{index}",
                agent_id=observation.agent_id,
                cash_account_id=cash_account_id,
                holding_account_id=pool.account_id,
                asset_id=pool.asset_id,
                lot_id=f"{cause_id}-buy-{index}",
                units=units,
                quantity_scale=pool.quantity_scale,
            )
        )
    return actions


def withdraw(
    observation: Observation, *, targets: dict[tuple[str, str], int], cash_account_id: str, amount: int, cause_id: str
) -> list[Action]:
    """Raise gross cash overweight-first, then FIFO within each selected pool.

    A request beyond available marked value liquidates the selected lots; it does
    not promise funding. Partial lots round quantity upward at their own scale.
    Unselected accounts/assets are never sold, including on exhaustion.
    """
    selected = _pools(observation, targets, cash_account_id)
    return _withdraw(observation, selected, cash_account_id, amount, cause_id)


def withdraw_by_symbol(
    observation: Observation,
    *,
    targets: dict[str, int],
    source_account_ids: tuple[str, ...],
    cash_account_id: str,
    amount: int,
    cause_id: str,
) -> list[Action]:
    """Weight each symbol once across accounts; sell in account order, then scoped FIFO.

    Unlike ``withdraw``'s independent account/asset targets, one symbol's value sums
    all its selected pools. Source accounts are tried in the supplied order, not
    global oldest-lot order. Individual lots keep their own price/quantity grids.
    Core zero targets remain sellable; callers implement exclusions by omitting them.
    """
    if not source_account_ids or len(set(source_account_ids)) != len(source_account_ids):
        raise ValueError("source accounts must be nonempty and distinct")
    pools = {(pool.account_id, pool.asset_id): pool for pool in observation.holding_pools}
    pool_targets = {
        (account, symbol): weight
        for symbol, weight in targets.items()
        for account in source_account_ids
        if (account, symbol) in pools
    }
    if set(targets) != {symbol for _, symbol in pool_targets}:
        raise ValueError("each targeted symbol must have a declared pool in the selected source accounts")
    selected = _pools(observation, pool_targets, cash_account_id)
    grouped = [
        (
            next(pool for pool, _, _ in selected if pool.asset_id == symbol),
            [lot for pool, lots, _ in selected if pool.asset_id == symbol for lot in lots],
            weight,
        )
        for symbol, weight in targets.items()
    ]
    return _withdraw(observation, grouped, cash_account_id, amount, cause_id)


def _withdraw(
    observation: Observation,
    selected: list[tuple[HoldingPool, list[PublicPosition], int]],
    cash_account_id: str,
    amount: int,
    cause_id: str,
) -> list[Action]:
    values = [sum(lot.value for lot in lots) for _, lots, _ in selected]
    amounts = _allocate(values, [weight for _, _, weight in selected], amount, withdrawing=True)
    actions, _ = _sales(
        observation,
        selected,
        amounts,
        [
            amount > 0 and (amount >= sum(values) or taken == value > 0)
            for taken, value in zip(amounts, values, strict=True)
        ],
        cash_account_id,
        cause_id,
    )
    return actions


def deposit(
    observation: Observation,
    *,
    targets: dict[tuple[str, str], int],
    cash_account_id: str,
    cash_budget: int,
    cause_id: str,
) -> list[Action]:
    """Invest an explicit available-cash budget underweight-first, leaving quantity dust.

    The caller excludes reserved payments. This budget cannot include hypothetical
    prior sales; use rebalance for a jointly budgeted sale-and-purchase proposal.
    Each call needs a unique cause ID, also used to name its new lots.
    """
    selected = _pools(observation, targets, cash_account_id)
    if _count(cash_budget) > dict(observation.accounts)[cash_account_id]:
        raise ValueError("deposit budget exceeds observed funding-account cash")
    values = [sum(lot.value for lot in lots) for _, lots, _ in selected]
    amounts = _allocate(values, list(targets.values()), cash_budget, withdrawing=False)
    return _buys(observation, selected, amounts, cash_account_id, cash_budget, cause_id)


def rebalance(
    observation: Observation,
    *,
    targets: dict[tuple[str, str], int],
    cash_account_id: str,
    cash_budget: int,
    tolerance_ppb: int,
    cause_id: str,
) -> list[Action]:
    """Propose sells then buys on relative drift, with one shared purchase budget.

    Targets exclude cash; target money floors to currency quanta. Purchases use
    only the caller's unreserved cash plus these exact per-lot quoted sale proceeds.
    Zero targets exit every selected unit, even a lot whose mark rounds to zero.
    No tax or later settlement effects are projected.
    """
    selected = _pools(observation, targets, cash_account_id)
    if _count(cash_budget) > dict(observation.accounts)[cash_account_id]:
        raise ValueError("rebalance budget exceeds observed funding-account cash")
    values = [sum(lot.value for lot in lots) for _, lots, _ in selected]
    exits = any(weight == 0 and lots for _, lots, weight in selected)
    sales_amounts, buy_amounts = _rebalance_amounts(values, list(targets.values()), tolerance_ppb, force=exits)
    if not any(sales_amounts) and not any(buy_amounts) and not exits:
        return []
    sales, proceeds = _sales(
        observation, selected, sales_amounts, [weight == 0 for weight in targets.values()], cash_account_id, cause_id
    )
    return sales + _buys(observation, selected, buy_amounts, cash_account_id, _count(cash_budget + proceeds), cause_id)
