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
from functools import partial
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
    # Quanta per unit, per sleeve. Compile-time because it is a property of the ASSET, not of
    # the position — which is also why it is here rather than derived from the sleeve's lots:
    # a sleeve the agent holds none of still has to be priceable and buyable.
    quantity_scale: NDArray[np.int64]

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

    Both are `(sleeve, rollout)` in `SleeveUniverse` order, **in whole quanta** — the unit the
    lots are actually held in. Orders are units-only per <plans/actor_actions.md>: a
    dollar-denominated order would make the ENGINE divide by a price and floor, which is the
    engine deciding how much to trade. The policy does its own division, so the rounding rule
    is a decision here with a name and a test rather than a property of where a ceiling sits.

    A sell order can still fall short of the units asked for when the sleeve cannot cover it;
    what the executor may not do is choose a different number.

    Never both non-zero in the same month — see the module docstring.
    """

    sell_quanta: jnp.ndarray
    buy_quanta: jnp.ndarray


def _quanta_for_cents(*, cents: jnp.ndarray, unit_price_cents: jnp.ndarray, quantity_scale: jnp.ndarray) -> jnp.ndarray:
    """Whole quanta worth at least `cents`, at the market price.

    The price is an OBSERVATION, not something inferred from the position: what an instrument
    trades at is a fact about the market, so an agent holding none of a sleeve can still price
    it — and buying into exactly that sleeve is what a target allocation is for. Inferring the
    price from `value / quanta` of what is held works for a sale and is incoherent for a
    purchase, which is how the earlier attempt at this collapsed.

    Ceiling division, in integers, so the result is never a quantum short of the ask. A price
    of zero means the sleeve has no modeled price series; it is unpriceable rather than free,
    so it orders nothing.
    """

    priced = unit_price_cents > 0
    quanta = -(-(cents * quantity_scale) // jnp.where(priced, unit_price_cents, 1))  # ceiling division
    return jnp.where(priced & (cents > 0), quanta, 0)


def decide(
    *,
    view: ActorView,
    universe: SleeveUniverse,
    floor_cents: jnp.ndarray,
    ceiling_cents: jnp.ndarray,
    sleeve_unit_price_cents: jnp.ndarray,
) -> SleeveOrders:
    """Choose this month's orders from this month's observation.

    `floor_cents` and `ceiling_cents` are `(rollout,)` because the band may be CPI-indexed
    and inflation differs by path. Their ordering is validated at config time by
    `cash_band.validate_band_bounds`; it cannot be checked here, where they are traced.

    `sleeve_unit_price_cents` is `(sleeve, rollout)` market data from the exogenous model —
    the same path the engine marks lots against. It is passed in rather than reconstructed
    from holdings so that a sleeve the agent holds none of is still priced, which is what the
    buy side needs and the sell side has no reason to do differently.
    """

    order = cash_order(
        cash_cents=view.cash_cents[universe.funding_cash_row],
        scheduled_outflow_cents=view.scheduled_outflow_cents,
        floor_cents=floor_cents,
        ceiling_cents=ceiling_cents,
    )
    # Water-filling stays in cents: "overweight" is a statement about VALUE, and the level
    # where two sleeves meet has no meaning in units of two different assets. Only the last
    # step — turning one sleeve's share into an order — is denominated in what gets traded.
    sleeve_value = view.sleeve_value_cents(universe.lot_rows)
    to_quanta = partial(
        _quanta_for_cents, unit_price_cents=sleeve_unit_price_cents, quantity_scale=universe.quantity_scale[:, None]
    )
    return SleeveOrders(
        sell_quanta=jnp.minimum(
            to_quanta(
                cents=withdrawal_by_sleeve(
                    value_cents=sleeve_value, weights=universe.weights, raise_cents=order.raise_cents
                )
            ),
            # You cannot sell what you do not hold. The buy side has no such cap, and that
            # asymmetry is why the clamp sits here rather than inside the conversion.
            view.sleeve_quanta(universe.lot_rows),
        ),
        buy_quanta=to_quanta(
            cents=deposit_by_sleeve(value_cents=sleeve_value, weights=universe.weights, invest_cents=order.invest_cents)
        ),
    )
