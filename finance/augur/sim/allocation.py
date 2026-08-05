"""Target-allocation arithmetic: how much to take from each sleeve to raise a sum.

Pure math, no engine state — the counterpart to `bonds.py`. Everything is integer cents
and everything carries a trailing rollout axis, because the policy that calls this is a
batched function `(observations, R) -> (actions, R)`.

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

import numpy as np
from numpy.typing import NDArray


def target_value_cents(*, weights: NDArray[np.int64], total_cents: NDArray[np.int64]) -> NDArray[np.int64]:
    """Each sleeve's on-target value: `total * weight_i / sum(weight)`, floored.

    `weights` is `(sleeve,)`, `total_cents` is `(rollout,)`, result is `(sleeve, rollout)`.
    """

    _validate_weights(weights)
    return (weights[:, None] * total_cents[None, :]) // weights.sum()


def withdrawal_by_sleeve(
    *, value_cents: NDArray[np.int64], weights: NDArray[np.int64], raise_cents: NDArray[np.int64]
) -> NDArray[np.int64]:
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
    wanted = np.minimum(np.maximum(raise_cents, 0), available)

    # Water level per rollout. Candidate levels are the sleeve value-per-weight ratios: between
    # two adjacent ratios the set of contributing sleeves is fixed, so the level solves a linear
    # equation there. Walking the ratios in descending order grows that set one sleeve at a time.
    # Ratios are compared as float only to ORDER them; every amount below stays in integer cents.
    ratio = value_cents / weights[:, None]
    order = np.argsort(-ratio, axis=0, kind="stable")
    value_sorted = np.take_along_axis(value_cents, order, axis=0)
    weight_sorted = np.take_along_axis(np.broadcast_to(weights[:, None], value_cents.shape), order, axis=0)
    ratio_sorted = np.take_along_axis(ratio, order, axis=0)

    value_prefix = np.cumsum(value_sorted, axis=0)
    weight_prefix = np.cumsum(weight_sorted, axis=0)
    # Level implied by taking the top k sleeves, for each k. Valid when it does not fall below
    # the next sleeve's ratio (which would mean that sleeve should be contributing too).
    level = (value_prefix - wanted[None, :]) / weight_prefix
    next_ratio = np.concatenate([ratio_sorted[1:], np.full((1, ratio.shape[1]), -np.inf)], axis=0)
    feasible = level >= next_ratio
    # The smallest feasible k is the answer; later k give a level below some included sleeve's
    # share and would over-take from it.
    chosen = np.argmax(feasible, axis=0)
    water = np.take_along_axis(level, chosen[None, :], axis=0)

    taken = np.maximum(value_cents - _round_half_up(water * weights[:, None]), 0)
    taken = np.minimum(taken, value_cents)
    return _settle_residual(taken=taken, value_cents=value_cents, wanted=wanted)


def _settle_residual(
    *, taken: NDArray[np.int64], value_cents: NDArray[np.int64], wanted: NDArray[np.int64]
) -> NDArray[np.int64]:
    """Absorb the cent that rounding the water level costs, so the split is exact.

    The level is fractional, so per-sleeve amounts round and the total drifts from `wanted`
    by a few cents. Correcting on the largest contributor keeps the correction proportionally
    smallest and cannot push a sleeve negative or past its value.
    """

    residual = wanted - taken.sum(axis=0)
    headroom = np.where(residual >= 0, value_cents - taken, taken)
    # Largest contributor with room to absorb the correction, falling back to the largest sleeve.
    candidate = np.where(headroom >= np.abs(residual)[None, :], taken, -1)
    target = np.argmax(candidate, axis=0)
    adjustment = np.zeros_like(taken)
    np.put_along_axis(adjustment, target[None, :], residual[None, :], axis=0)
    return taken + adjustment


def _round_half_up(values: NDArray[np.float64]) -> NDArray[np.int64]:
    return np.floor(values + 0.5).astype(np.int64)


def _validate_weights(weights: NDArray[np.int64]) -> None:
    if weights.ndim != 1 or weights.shape[0] == 0:
        raise ValueError(f"weights must be a non-empty 1-D array, got {weights.shape}")
    if np.any(weights <= 0):
        raise ValueError(f"weights must all be positive; got {weights.tolist()}")
