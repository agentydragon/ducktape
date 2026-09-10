"""Public-portfolio policy proposals over explicitly selected account/asset pools.

Weights are relative nonnegative integers; zero targets remain sellable but receive
no deposits. Money is integer currency quanta. Helpers neither settle trades nor
estimate taxes. Callers reserve claims/consumption and submit the ordered actions.
Independent calls on the same observation do not reserve each other's lots or cash.
"""

from fractions import Fraction

from finance.augur.rust.simulator import Action, HoldingPool, Observation, PublicPosition
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE, quantity_for_value


def _count(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("sleeve amounts and weights must be integer counts")
    if not 0 <= value < 1 << 63:
        raise ValueError("sleeve amounts and weights must fit nonnegative signed-64-bit counts")
    return value


def _allocate(values: list[int], weights: list[int], amount: int, *, withdrawing: bool) -> list[int]:
    """Water-fill by value/weight; conserve the exact budget after currency rounding."""
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


def _sales(
    observation: Observation,
    selected: list[tuple[HoldingPool, list[PublicPosition], int]],
    amounts: list[int],
    full_exits: list[bool],
    cash_account_id: str,
    cause_id: str,
) -> tuple[list[Action], int]:
    actions = []
    proceeds = 0
    for index, ((pool, lots, _), amount, full_exit) in enumerate(zip(selected, amounts, full_exits, strict=True)):
        sale_lots = []
        remaining = amount
        for lot in lots:
            if not full_exit and remaining <= 0:
                break
            units = (
                lot.units
                if full_exit or remaining >= lot.value
                else min(lot.units, quantity_for_value(remaining, lot.price, lot.quantity_scale, round_up=True))
            )
            value = _quoted_value(units, lot.price, lot.quantity_scale)
            remaining -= value
            proceeds += value
            sale_lots.append((lot.account_id, lot.lot_id, units))
        if sale_lots:
            actions.append(
                Action.sell(
                    cause_id=f"{cause_id}-sell-{index}",
                    agent_id=observation.agent_id,
                    proceeds_account_id=cash_account_id,
                    asset_id=pool.asset_id,
                    lots=sale_lots,
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
    actions = []
    for index, ((pool, _, weight), amount) in enumerate(zip(selected, amounts, strict=True)):
        if not amount or not weight:
            continue
        units = quantity_for_value(min(amount, cash_budget), pool.price, pool.quantity_scale, round_up=False)
        if not units:
            continue
        cash_budget -= _quoted_value(units, pool.price, pool.quantity_scale)
        actions.append(
            Action.buy(
                cause_id=f"{cause_id}-buy-{index}",
                from_account=(observation.agent_id, cash_account_id),
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
    values = [sum(lot.value for lot in lots) for _, lots, _ in selected]
    amounts = _allocate(values, list(targets.values()), amount, withdrawing=True)
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
    _count(tolerance_ppb)
    if _count(cash_budget) > dict(observation.accounts)[cash_account_id]:
        raise ValueError("rebalance budget exceeds observed funding-account cash")
    values = [sum(lot.value for lot in lots) for _, lots, _ in selected]
    total = _count(sum(values))
    weight_total = sum(targets.values())
    wanted = [total * weight // weight_total for weight in targets.values()]
    drifts = [value - target for value, target in zip(values, wanted, strict=True)]
    fires = any(
        (weight == 0 and bool(lots)) or (target > 0 and abs(drift) * MONEY_FACTOR_SCALE >= tolerance_ppb * target)
        for (_, lots, weight), target, drift in zip(selected, wanted, drifts, strict=True)
    )
    if not fires:
        return []
    sales, proceeds = _sales(
        observation,
        selected,
        [max(0, drift) for drift in drifts],
        [weight == 0 for weight in targets.values()],
        cash_account_id,
        cause_id,
    )
    return sales + _buys(
        observation,
        selected,
        [max(0, -drift) for drift in drifts],
        cash_account_id,
        _count(cash_budget + proceeds),
        cause_id,
    )
