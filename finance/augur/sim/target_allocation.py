"""The target-allocation policy: observations in, sleeve orders out.

A policy is a pure, jittable function from a batched `ActorView` to a batched decision, in
the RL sense — no access to engine internals, nothing but what the view exposes. A learned
policy would replace this function and nothing around it.

## What this policy does

Two mechanisms, composed:

- `cash_band.cash_order` sizes the month — how much to raise or invest, measured from the
  balance the month is projected to END at rather than the balance sitting there before
  the bills. That is what lets funding happen once a month, like a person, instead of
  twice.
- `allocation.withdrawal_by_sleeve` / `deposit_by_sleeve` place it — which sleeves the
  amount comes from or goes to, so the portfolio moves toward its target ratios.

The composition has a property worth stating rather than checking at the call site: because
the band's two sides are mutually exclusive, this policy can never sell and buy in the same
month. That follows from the band having an interior, and `validate_band_bounds` is what
guarantees it has one.

Rebalancing happens ONLY through cashflow that was going to happen anyway. There is no
periodic rebalance and no drift tolerance, because turnover and its tax drag would swamp
the effect the allocation study is trying to measure. A month inside the band emits nothing
at all.

## What it deliberately does not emit

No spending. This is a FUNDING policy: it decides how to pay for a life, not what that life
costs. A withdrawn draft carried a `spend_cents` field that this policy always set to zero,
reserved for a tier-aware policy to fill later — a field with no reader, which is dead
payload however well-intentioned the reservation.

Nor is `SleeveOrders` the general action vocabulary from <plans/actor_actions.md>. That
vocabulary is `Pay`/`Buy`/`Sell` per INSTRUMENT, it is what every policy will eventually
emit, and its shape is deliberately unsettled until a policy emits through it. This type is
one policy's output, in the units its executor already consumes. Promoting a single
policy's output shape to a boundary is exactly the mistake #3745 was closed for.
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

    `lot_rows` indexes the VIEW's lot axis, which has already narrowed to one agent, so a
    group written against plan indices would silently read the wrong lots.

    Anything not named here is deliberately outside the policy's scope and out of the target
    denominator. That is what makes a target alongside an untradeable holding — private
    equity before liquidity, a ladder rung that will not be broken — expressible at all.
    Without it the policy would be permanently overweight something it cannot sell, and
    would try to sell it every month forever.
    """

    weights: NDArray[np.int64]
    lot_rows: tuple[tuple[int, ...], ...]
    funding_cash_row: int

    def __post_init__(self) -> None:
        if not self.lot_rows:
            raise ValueError("sleeve universe must name at least one sleeve")
        if self.weights.shape[0] != len(self.lot_rows):
            raise ValueError(f"sleeve universe has {self.weights.shape[0]} weights but {len(self.lot_rows)} lot groups")
        seen: set[int] = set()
        for rows in self.lot_rows:
            if overlap := seen.intersection(rows):
                raise ValueError(
                    f"lot row(s) {sorted(overlap)} appear in more than one sleeve; a lot counted twice "
                    "inflates the portfolio total and skews every target"
                )
            seen.update(rows)


class SleeveOrders(NamedTuple):
    """What this policy wants done this month, batched over rollouts.

    Both are `(sleeve, rollout)` in `SleeveUniverse` order, in cents. They are TARGETS:
    execution converts them to whole quanta, and a sell target can fall short when the
    sleeve cannot cover it.

    Never both non-zero in the same month — see the module docstring.
    """

    sell_cents: jnp.ndarray
    buy_cents: jnp.ndarray


def decide(
    *, view: ActorView, universe: SleeveUniverse, floor_cents: jnp.ndarray, ceiling_cents: jnp.ndarray
) -> SleeveOrders:
    """Choose this month's orders from this month's observation.

    `floor_cents` and `ceiling_cents` are `(rollout,)` because the band may be CPI-indexed
    and inflation differs by path. Their ordering is validated at config time by
    `cash_band.validate_band_bounds`; it cannot be checked here, where they are traced.
    """

    order = cash_order(
        cash_cents=view.cash_cents[universe.funding_cash_row],
        scheduled_outflow_cents=view.scheduled_outflow_cents,
        floor_cents=floor_cents,
        ceiling_cents=ceiling_cents,
    )
    sleeve_value = view.sleeve_value_cents(universe.lot_rows)
    return SleeveOrders(
        sell_cents=withdrawal_by_sleeve(
            value_cents=sleeve_value, weights=universe.weights, raise_cents=order.raise_cents
        ),
        buy_cents=deposit_by_sleeve(
            value_cents=sleeve_value, weights=universe.weights, invest_cents=order.invest_cents
        ),
    )
