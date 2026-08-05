"""What an actor can see when its policy runs.

The observation half of the policy contract. A policy is a pure function
`(batch of observations) -> (batch of actions)` in the RL sense, so this is a struct OF
ARRAYS, not an array of structs: every field carries the rollout axis last, and one call
decides for every rollout at once. No per-rollout Python anywhere, and a learned policy
would drop into the same signature.

## What "can see" means

Two restrictions, and both are structural rather than conventional:

- **Own rows only.** The view is built by slicing with compile-time index sets naming one
  agent's accounts and lots. Other agents' balances and the `rest_of_world` contra row are
  not merely absent from the type — `ActorSlots.__post_init__` rejects them, so an invalid
  set cannot be constructed and no caller can pass one in by accident.
- **Nothing from the future.** Every field is a function of state as of `month` and of
  marks at `month`. The builder holds no price cube it could index past the current month —
  marks arrive already resolved — so `month` reaches exactly one output, the holding period.
  That is what stops a policy from quietly becoming clairvoyant and making every backtest
  look brilliant.

## What it deliberately carries

Lot-level rather than sleeve-level quantities. A brokerage statement shows lots, and lot
identity is what tax-aware selection needs; aggregating to sleeves is a policy's own step,
done with compile-time index sets. Carrying only sleeve totals would foreclose lot-level
policies for a saving the dense shapes do not need.

`scheduled_outflow_cents` is what the month is already committed to paying. It belongs in
the observation because the agent genuinely knows its own bills before it decides how to
fund them — and because deciding against the projected end-of-month balance rather than
the current one is what lets funding happen once a month instead of twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import jax.numpy as jnp
import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class ActorSlots:
    """Compile-time index sets naming the rows one agent may look at.

    Every visibility rule is checked in `__post_init__`, so an invalid set cannot be
    constructed at all — there is no separate validate step a caller can forget, which is
    the failure mode a "remember to call this" API eventually has.

    That is why the plan's shape travels with the slots. `external_cash_slot` in particular
    is load-bearing: `rest_of_world` holds money that LEFT the modeled world, so an actor
    able to see it would read its own past spending as an asset.
    """

    cash_slots: tuple[int, ...]
    lot_slots: tuple[int, ...]
    external_cash_slot: int
    cash_count: int
    lot_count: int

    def __post_init__(self) -> None:
        if len(set(self.cash_slots)) != len(self.cash_slots):
            raise ValueError(f"duplicate cash slot in actor view: {self.cash_slots}")
        if len(set(self.lot_slots)) != len(self.lot_slots):
            raise ValueError(f"duplicate lot slot in actor view: {self.lot_slots}")
        if self.external_cash_slot in self.cash_slots:
            raise ValueError(
                f"actor view would expose the external contra row ({self.external_cash_slot=}); "
                "an actor may only see its own accounts"
            )
        if any(not 0 <= slot < self.cash_count for slot in self.cash_slots):
            raise ValueError(f"actor view cash slot out of range for cash_count={self.cash_count}: {self.cash_slots}")
        if any(not 0 <= slot < self.lot_count for slot in self.lot_slots):
            raise ValueError(f"actor view lot slot out of range for lot_count={self.lot_count}: {self.lot_slots}")


class ActorView(NamedTuple):
    """One month's observation, batched over rollouts.

    Shapes: `month` is a scalar; `cash_cents` is `(account, R)`; every `lot_*` field is
    `(lot, R)`; the rest are `(R,)`. Account and lot axes are in `ActorSlots` order.
    """

    month: jnp.ndarray
    cash_cents: jnp.ndarray
    lot_quantity: jnp.ndarray
    # Marked at this month's price, NOT held at cost — a policy reasoning about allocation
    # needs what a sleeve is worth now, not what it was bought for.
    lot_value_cents: jnp.ndarray
    lot_cost_basis_per_unit_cents: jnp.ndarray
    # Months since acquisition, so a policy can weigh the long/short capital-gain boundary
    # without reaching into the engine's classification.
    lot_holding_months: jnp.ndarray
    scheduled_outflow_cents: jnp.ndarray

    @property
    def total_cash_cents(self) -> jnp.ndarray:
        return self.cash_cents.sum(axis=0)

    def sleeve_value_cents(self, sleeve_lot_rows: tuple[tuple[int, ...], ...]) -> jnp.ndarray:
        """Aggregate lot values into `(sleeve, R)` using compile-time row groups.

        Rows index the VIEW's lot axis, not the plan's — the view has already narrowed to
        this agent, so a group referring to plan indices would silently read the wrong lots.
        """

        return jnp.stack(
            [self.lot_value_cents[np.asarray(rows, dtype=np.int64)].sum(axis=0) for rows in sleeve_lot_rows]
        )


def build_actor_view(
    *,
    month: jnp.ndarray,
    slots: ActorSlots,
    cash_cents: jnp.ndarray,
    lot_quantity: jnp.ndarray,
    lot_cost_basis_per_unit_cents: jnp.ndarray,
    lot_value_cents: jnp.ndarray,
    lot_purchase_month: jnp.ndarray | NDArray[np.int64],
    scheduled_outflow_cents: jnp.ndarray,
) -> ActorView:
    """Narrow full engine state to one agent's observation.

    `cash_cents` and the `lot_*` tensors are the engine's full `(row, R)` state; the
    slicing to this agent happens here so the visibility rule has exactly one
    implementation.

    `lot_value_cents` is marked-to-this-month VALUE, and it is passed in rather than derived
    from a price here on purpose: valuing quanta is engine accounting
    (`_value_cents_from_quanta`, which rounds half away from zero), and a second
    implementation would round differently. Flooring here would report a sleeve worth a cent
    less than selling it actually yields, so a policy asking for the whole sleeve would come
    up short. One valuation, owned by the side that does the selling.
    """

    cash_rows = np.asarray(slots.cash_slots, dtype=np.int64)
    lot_rows = np.asarray(slots.lot_slots, dtype=np.int64)
    quantity = lot_quantity[lot_rows]
    return ActorView(
        month=month,
        cash_cents=cash_cents[cash_rows],
        lot_quantity=quantity,
        lot_value_cents=lot_value_cents[lot_rows],
        lot_cost_basis_per_unit_cents=lot_cost_basis_per_unit_cents[lot_rows],
        # Broadcast to `(lot, R)` so every field shares one shape. Purchase month is
        # compile-time today; when the policy decides WHEN to buy it becomes per-rollout
        # state and only this line changes.
        lot_holding_months=jnp.broadcast_to((month - lot_purchase_month[lot_rows])[:, None], quantity.shape).astype(
            jnp.int64
        ),
        scheduled_outflow_cents=scheduled_outflow_cents,
    )
