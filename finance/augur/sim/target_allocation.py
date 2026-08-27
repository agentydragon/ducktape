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

Rebalancing rides that cashflow, which is free: the trade was going to happen anyway. A
month inside the band emits nothing at all, however far the portfolio has drifted — so a
sleeve that quietly doubles is never sold down.

`allocation.rebalance_by_sleeve` is the opt-in escape from that, and it is OFF unless a
policy sets `rebalance_tolerance`. Turnover and its tax drag are exactly what the allocation
study is trying to MEASURE, so a default that rebalanced would bake in the answer.

It fires only in a month the band is quiet. When the band is moving money its water-filling
is already the best rebalance that cashflow can buy, and layering a second one on top would
sell a sleeve to raise cash and buy the same sleeve back in the same month.

That third state is why this policy CAN now sell and buy in one month, which it could not
before: the band's two sides remain mutually exclusive (`validate_band_bounds` guarantees
the band has an interior), but a rebalance is inherently both at once.

## What it deliberately does not emit

No spending. This is a FUNDING policy: it decides how to pay for a life, not what that life
costs. A withdrawn draft carried a `spend_quanta` field that this policy always set to zero,
reserved for a tier-aware policy to fill later — a field with no reader, which is dead
payload however well-intentioned the reservation.

Nor is `SleeveOrders` the general action vocabulary (`Pay`/`Buy`/`Sell` per INSTRUMENT)
that every policy will eventually emit, whose shape is deliberately unsettled until a
policy emits through it. This type is
one policy's output, in the units its executor already consumes. Promoting a single
policy's output shape to a boundary is exactly the mistake #3745 was closed for.
"""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from dataclasses import dataclass
from functools import partial
from typing import NamedTuple

import jax.numpy as jnp
import numpy as np
from jaxtyping import Array, Int64

from finance.augur.sim.actor_view import ActorView
from finance.augur.sim.allocation import deposit_by_sleeve, rebalance_by_sleeve, withdrawal_by_sleeve
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

    weights: Int64[np.ndarray, " sleeve"] | Int64[Array, " sleeve"]
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

    Both are `(sleeve, rollout)` in `SleeveUniverse` order, **in whole quanta** — the unit
    the lots are held in. Orders are units-only: a
    dollar-denominated order would leave the ENGINE to divide by a price and round, which is
    the engine choosing how much to trade rather than executing what it was told. Doing the
    division here makes the rounding rule a decision with a name and a test.

    Both can be non-zero in the same month, but only a rebalance does that, and never for the
    same sleeve — see the module docstring.
    """

    sell_quanta: Int64[Array, " sleeve rollout"]
    buy_quanta: Int64[Array, " sleeve rollout"]


def _quanta_for_quanta(
    *,
    cents: Int64[Array, " sleeve rollout"],
    unit_price_quanta: Int64[Array, " instrument rollout"],
    quantity_scale: Int64[Array, " instrument rollout"],
    round_up: bool,
) -> Int64[Array, " sleeve rollout"]:
    """Whole quanta for a cent target, at this month's observed market price.

    `round_up` is where the two sides of the band differ, and it is not a formatting choice.
    A raise must COVER its target, so it rounds up: a sale a quantum short leaves the account
    below the balance it was refilling to. A purchase must not EXCEED its target, so it rounds
    down: the cents a buy is sized against are the cents above the floor the policy is holding
    back, and rounding up would spend into that floor — at a five-figure unit price, by a
    four-figure amount. Either way the sub-quantum remainder stays in cash.

    A zero price means the instrument has no modeled price series: unpriceable rather than
    free, so it orders nothing.
    """

    priced = unit_price_quanta > 0
    scaled = cents * quantity_scale
    divisor = jnp.where(priced, unit_price_quanta, 1)
    quanta = -(-scaled // divisor) if round_up else scaled // divisor
    return jnp.where(priced & (cents > 0), quanta, 0)


def decide(
    *,
    view: ActorView,
    universe: SleeveUniverse,
    floor_quanta: Int64[Array, " rollout"],
    ceiling_quanta: Int64[Array, " rollout"],
    rebalance_tolerance: float | None = None,
) -> SleeveOrders:
    """Choose this month's orders from this month's observation.

    `floor_quanta` and `ceiling_quanta` are `(rollout,)` because the band may be CPI-indexed
    and inflation differs by path. Their ordering is validated at config time by
    `cash_band.validate_band_bounds`; it cannot be checked here, where they are traced.

    `rebalance_tolerance` is compile-time config rather than a traced value, so `None` means
    the drift-triggered rebalance is not merely skipped but never traced at all — a policy
    that does not rebalance compiles to the program it compiled to before the mechanism
    existed.

    Sleeve `s` prices off instrument `s` of the view — one policy's sleeves are the actor's
    tradable set today, in the same order.
    """

    order = cash_order(
        cash_quanta=view.cash_quanta[universe.funding_cash_row],
        scheduled_outflow_quanta=view.scheduled_outflow_quanta,
        floor_quanta=floor_quanta,
        ceiling_quanta=ceiling_quanta,
    )
    # Water-filling stays in cents: "overweight" is a claim about VALUE, and a level where
    # two sleeves meet has no meaning in units of two different assets. Only the last step —
    # turning a sleeve's share into an order — is denominated in what actually moves.
    sleeve_value = view.sleeve_value_quanta(universe.lot_rows)
    to_quanta = partial(
        _quanta_for_quanta,
        unit_price_quanta=view.instrument_price_quanta,
        quantity_scale=view.instrument_quantity_scale[:, None],
    )
    # A raise must COVER what the month needs, so it rounds up. A trim has no such target: it
    # is bounded by the excess over target, and overshooting it would realize tax on a sale
    # nothing asked for. The two are mutually exclusive per rollout — a rebalance fires only
    # when the band is quiet — so summing the quantized sides is a selection, and it keeps
    # each rounding rule attached to the thing that justifies it.
    sell_quanta = to_quanta(
        cents=withdrawal_by_sleeve(
            value_quanta=sleeve_value, weights=universe.weights, raise_quanta=order.raise_quanta
        ),
        round_up=True,
    )
    buy_quanta = to_quanta(
        cents=deposit_by_sleeve(value_quanta=sleeve_value, weights=universe.weights, invest_quanta=order.invest_quanta),
        round_up=False,
    )
    if rebalance_tolerance is not None:
        quiet = (order.raise_quanta == 0) & (order.invest_quanta == 0)
        trim_sell, trim_buy = rebalance_by_sleeve(
            value_quanta=sleeve_value, weights=universe.weights, tolerance=rebalance_tolerance
        )
        sell_quanta = sell_quanta + jnp.where(quiet, to_quanta(cents=trim_sell, round_up=False), 0)
        buy_quanta = buy_quanta + jnp.where(quiet, to_quanta(cents=trim_buy, round_up=False), 0)

    return SleeveOrders(
        # Capped by the holding, because you cannot sell what you do not have. The buy side
        # has no such cap — that asymmetry is why the clamp sits here and not in the
        # conversion, which knows only about prices.
        sell_quanta=jnp.minimum(sell_quanta, view.sleeve_quanta(universe.lot_rows)),
        buy_quanta=buy_quanta,
    )
