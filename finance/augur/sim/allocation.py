"""Target-allocation arithmetic: how much to take from each sleeve to raise a sum.

Pure math, no engine state — the counterpart to `bonds.py`. Everything is integer cents
and everything carries a trailing rollout axis, because the policy that calls this is a
batched function `(observations, R) -> (actions, R)`.

Written in `jnp`, not numpy, because this runs INSIDE the jitted scan. A numpy version
would force a second implementation in the engine, and the two would drift — the mistake
`tensor_fifo.py` and the engine's `_fifo_sell_*` already illustrate. `weights` stays a
plain numpy array on purpose: it is compile-time config, never traced, so it can be
validated with ordinary raises. Traced VALUES cannot be, which is why nothing here raises
on an amount.

## The rule

Sleeves carry integer relative weights. A fraction is derivable from weights, so storing
fractions would store a computed quantity and need a float sum-to-one validator to defend
it; weights need neither. Only ratios matter, so `(3, 1)` and `(30, 10)` are the same
policy.

To raise `S`, take from the most overweight sleeve first. Stated exactly, that is a water
level `L` with

    sum_i max(0, value_i - L * weight_i) = S

and each sleeve contributes `max(0, value_i - L * weight_i)`. Every sleeve left holding
anything ends at the same value-per-weight `L`, which is the definition of on-target — so
this lands the post-sale portfolio ON the target ratios rather than merely nearer them.
Sleeves already at or below `L` contribute nothing, which is what "don't sell the
underweight sleeve" means.

Two consequences worth naming:

- A withdrawal is the ONLY rebalancing mechanism here. There is no separate rebalance
  action, because turnover and its tax drag would otherwise swamp the effect the whole
  study is trying to measure. With no drift and no cashflow this returns all zeros.
- `L` is a level, not a fraction of the sale, so a sleeve that is far overweight can fund
  the entire withdrawal on its own while the others are untouched.
"""

from __future__ import annotations

import jax.numpy as jnp
import numpy as np
from numpy.typing import NDArray


def target_value_cents(*, weights: NDArray[np.int64], total_cents: jnp.ndarray) -> jnp.ndarray:
    """Each sleeve's on-target value: `total * weight_i / sum(weight)`, floored.

    `weights` is `(sleeve,)`, `total_cents` is `(rollout,)`, result is `(sleeve, rollout)`.
    """

    _validate_weights(weights)
    # `weights` is static numpy; lift it explicitly so the product is a typed jax Array
    # rather than `Any`, and take the divisor as a Python int since it is compile-time known.
    return (jnp.asarray(weights)[:, None] * total_cents[None, :]) // int(weights.sum())


def withdrawal_by_sleeve(
    *, value_cents: jnp.ndarray, weights: NDArray[np.int64], raise_cents: jnp.ndarray
) -> jnp.ndarray:
    """Split a cash-raise across sleeves so what remains is as close to target as possible.

    `value_cents` is `(sleeve, rollout)`, `weights` is `(sleeve,)`, `raise_cents` is
    `(rollout,)`. Result is `(sleeve, rollout)` and sums to exactly `raise_cents` per
    rollout, except where the sleeves cannot cover it, in which case every sleeve is
    drained and the caller sees a total short of the request.

    Asking for zero withdraws zero from every sleeve — drift alone never triggers a trade.
    """

    _validate_weights(weights)
    if value_cents.ndim != 2:
        raise ValueError(f"value_cents must be (sleeve, rollout), got {value_cents.shape}")
    if value_cents.shape[0] != weights.shape[0]:
        raise ValueError(f"value_cents has {value_cents.shape[0]} sleeves but weights has {weights.shape[0]}")
    if raise_cents.shape != (value_cents.shape[1],):
        raise ValueError(f"raise_cents must be (rollout,), got {raise_cents.shape}")

    available = value_cents.sum(axis=0)
    wanted = jnp.minimum(jnp.maximum(raise_cents, 0), available)

    # Water level per rollout. Candidate levels are the sleeve value-per-weight ratios: between
    # two adjacent ratios the set of contributing sleeves is fixed, so the level solves a linear
    # equation there. Walking the ratios in descending order grows that set one sleeve at a time.
    # Ratios are compared as float only to ORDER them; every amount below stays in integer cents.
    ratio = value_cents / weights[:, None]
    order = jnp.argsort(-ratio, axis=0, stable=True)
    value_sorted = jnp.take_along_axis(value_cents, order, axis=0)
    weight_sorted = jnp.take_along_axis(jnp.broadcast_to(weights[:, None], value_cents.shape), order, axis=0)
    ratio_sorted = jnp.take_along_axis(ratio, order, axis=0)

    value_prefix = jnp.cumsum(value_sorted, axis=0)
    weight_prefix = jnp.cumsum(weight_sorted, axis=0)
    # Level implied by taking the top k sleeves, for each k. Valid when it does not fall below
    # the next sleeve's ratio (which would mean that sleeve should be contributing too).
    level = (value_prefix - wanted[None, :]) / weight_prefix
    next_ratio = jnp.concatenate([ratio_sorted[1:], jnp.full((1, ratio.shape[1]), -jnp.inf)], axis=0)
    feasible = level >= next_ratio
    # The smallest feasible k is the answer; later k give a level below some included sleeve's
    # share and would over-take from it.
    chosen = jnp.argmax(feasible, axis=0)
    water = jnp.take_along_axis(level, chosen[None, :], axis=0)

    taken = jnp.maximum(value_cents - _round_half_up(water * weights[:, None]), 0)
    taken = jnp.minimum(taken, value_cents)
    return _settle_residual(taken=taken, value_cents=value_cents, wanted=wanted)


def deposit_by_sleeve(
    *, value_cents: jnp.ndarray, weights: NDArray[np.int64], invest_cents: jnp.ndarray
) -> jnp.ndarray:
    """Split cash to invest across sleeves so the result is as close to target as possible.

    The mirror of `withdrawal_by_sleeve`: fill the most UNDERWEIGHT sleeve first, which is
    a water level `L` with `sum_i max(0, L * weight_i - value_i) = S`, each sleeve
    receiving `max(0, L * weight_i - value_i)`.

    Simpler than the withdrawal in one respect — there is no availability cap, since you
    can always buy more of a sleeve but cannot sell more than you hold. So the result
    always sums to exactly `invest_cents`.

    Shapes and exactness match `withdrawal_by_sleeve`. Investing zero deposits nothing.
    """

    _validate_weights(weights)
    if value_cents.ndim != 2:
        raise ValueError(f"value_cents must be (sleeve, rollout), got {value_cents.shape}")
    if value_cents.shape[0] != weights.shape[0]:
        raise ValueError(f"value_cents has {value_cents.shape[0]} sleeves but weights has {weights.shape[0]}")
    if invest_cents.shape != (value_cents.shape[1],):
        raise ValueError(f"invest_cents must be (rollout,), got {invest_cents.shape}")

    wanted = jnp.maximum(invest_cents, 0)

    # Same construction as the withdrawal with the inequality flipped: walk the ratios
    # ASCENDING, so the set of receiving sleeves grows from the most underweight up.
    ratio = value_cents / weights[:, None]
    order = jnp.argsort(ratio, axis=0, stable=True)
    value_sorted = jnp.take_along_axis(value_cents, order, axis=0)
    weight_sorted = jnp.take_along_axis(jnp.broadcast_to(weights[:, None], value_cents.shape), order, axis=0)
    ratio_sorted = jnp.take_along_axis(ratio, order, axis=0)

    value_prefix = jnp.cumsum(value_sorted, axis=0)
    weight_prefix = jnp.cumsum(weight_sorted, axis=0)
    level = (value_prefix + wanted[None, :]) / weight_prefix
    # Valid when the level does not reach the next sleeve's ratio; if it did, that sleeve is
    # underweight too and should be receiving as well.
    next_ratio = jnp.concatenate([ratio_sorted[1:], jnp.full((1, ratio.shape[1]), jnp.inf)], axis=0)
    chosen = jnp.argmax(level <= next_ratio, axis=0)
    water = jnp.take_along_axis(level, chosen[None, :], axis=0)

    given = jnp.maximum(_round_half_up(water * weights[:, None]) - value_cents, 0)
    return _settle_residual(taken=given, value_cents=given + wanted[None, :], wanted=wanted)


def _settle_residual(*, taken: jnp.ndarray, value_cents: jnp.ndarray, wanted: jnp.ndarray) -> jnp.ndarray:
    """Absorb the cent that rounding the water level costs, so the split is exact.

    The level is fractional, so per-sleeve amounts round and the total drifts from `wanted`
    by a few cents. Correcting on the largest contributor keeps the correction proportionally
    smallest and cannot push a sleeve negative or past its value.
    """

    residual = wanted - taken.sum(axis=0)
    headroom = jnp.where(residual >= 0, value_cents - taken, taken)
    # Largest contributor with room to absorb the correction, falling back to the largest sleeve.
    candidate = jnp.where(headroom >= jnp.abs(residual)[None, :], taken, -1)
    target = jnp.argmax(candidate, axis=0)
    # One-hot rather than a scatter: jnp is functional, and this traces without an index update.
    sleeve_rows = jnp.arange(taken.shape[0])[:, None]
    return taken + jnp.where(sleeve_rows == target[None, :], residual[None, :], 0)


def _round_half_up(values: jnp.ndarray) -> jnp.ndarray:
    return jnp.floor(values + 0.5).astype(jnp.int64)


def _validate_weights(weights: NDArray[np.int64]) -> None:
    if weights.ndim != 1 or weights.shape[0] == 0:
        raise ValueError(f"weights must be a non-empty 1-D array, got {weights.shape}")
    if np.any(weights <= 0):
        raise ValueError(f"weights must all be positive; got {weights.tolist()}")
