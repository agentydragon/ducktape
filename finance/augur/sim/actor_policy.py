"""The actor policy: observations in, actions out.

The box itself. `decide` is a pure, jittable function from a batched `ActorView` to a
batched `ActorActions` — a policy in the RL sense, with no access to engine internals and
nothing but what `ActorView` exposes. A learned policy would replace this function and
nothing around it.

## What this policy does

Two mechanisms, composed:

- `cash_band.cash_order` sizes the month — how much to raise or invest, from the balance
  the month is projected to END at.
- `allocation.withdrawal_by_sleeve` / `deposit_by_sleeve` place it — which sleeves the
  amount comes from or goes to, so the portfolio moves toward its target ratios.

The composition has a property worth stating: because the band's two sides are mutually
exclusive, this policy can never sell and buy in the same month. That is not a coincidence
to be checked at the call site, it follows from the band having an interior.

Rebalancing happens ONLY through cashflow that was going to happen anyway. There is no
periodic rebalance and no drift tolerance, because turnover and its tax drag would swamp
the effect the allocation study is trying to measure. A month inside the band with no
obligations emits nothing at all.

## Spending

`ActorActions` carries `spend_cents`, and this policy always emits zero — it is a funding
policy, and discretionary spending is a decision about lifestyle rather than about how to
pay for one. The field is here rather than added later because retrofitting a third action
kind into an already-wired dense action tensor means designing the type twice. A
tier-aware policy (#3738) is what fills it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import jax.numpy as jnp
import numpy as np
from numpy.typing import NDArray

from finance.augur.sim.actor_view import ActorView
from finance.augur.sim.allocation import deposit_by_sleeve, withdrawal_by_sleeve
from finance.augur.sim.cash_band import cash_order


@dataclass(frozen=True)
class SleeveUniverse:
    """The sleeves this policy governs, and the account it funds from. Compile-time.

    `lot_rows` indexes the VIEW's lot axis, which has already narrowed to one agent.

    Anything not named here is deliberately outside the policy's scope and out of the
    target denominator. That is what makes a target over an untradeable holding (private
    equity before liquidity, a ladder rung that will not be broken) expressible at all —
    without it the policy would be permanently overweight something it cannot sell, and
    would try to sell it every month forever.
    """

    weights: NDArray[np.int64]
    lot_rows: tuple[tuple[int, ...], ...]
    funding_cash_row: int

    def __post_init__(self) -> None:
        if self.weights.shape[0] != len(self.lot_rows):
            raise ValueError(f"sleeve universe has {self.weights.shape[0]} weights but {len(self.lot_rows)} lot groups")
        if not self.lot_rows:
            raise ValueError("sleeve universe must name at least one sleeve")
        seen: set[int] = set()
        for rows in self.lot_rows:
            if overlap := seen.intersection(rows):
                raise ValueError(
                    f"lot row(s) {sorted(overlap)} appear in more than one sleeve; a lot counted twice "
                    "inflates the portfolio total and skews every target"
                )
            seen.update(rows)


class ActorActions(NamedTuple):
    """What the actor does this month, batched over rollouts.

    `sell_cents` and `buy_cents` are `(sleeve, R)` in `SleeveUniverse` order; `spend_cents`
    is `(R,)`. Amounts are targets in cents — execution converts them to whole quanta and
    may fall short of a sell target when a sleeve cannot cover it.
    """

    sell_cents: jnp.ndarray
    buy_cents: jnp.ndarray
    spend_cents: jnp.ndarray


def decide(
    *, view: ActorView, universe: SleeveUniverse, floor_cents: jnp.ndarray, ceiling_cents: jnp.ndarray
) -> ActorActions:
    """Choose this month's actions from this month's observation.

    `floor_cents` and `ceiling_cents` are `(rollout,)` because the band may be CPI-indexed
    and inflation differs by path. Their ordering is validated at config time by
    `cash_band.validate_band_bounds` — it cannot be checked here, where they are traced.
    """

    order = cash_order(
        cash_cents=view.cash_cents[universe.funding_cash_row],
        scheduled_outflow_cents=view.scheduled_outflow_cents,
        floor_cents=floor_cents,
        ceiling_cents=ceiling_cents,
    )
    sleeve_value = view.sleeve_value_cents(universe.lot_rows)
    return ActorActions(
        sell_cents=withdrawal_by_sleeve(
            value_cents=sleeve_value, weights=universe.weights, raise_cents=order.raise_cents
        ),
        buy_cents=deposit_by_sleeve(
            value_cents=sleeve_value, weights=universe.weights, invest_cents=order.invest_cents
        ),
        spend_cents=jnp.zeros_like(order.raise_cents),
    )
