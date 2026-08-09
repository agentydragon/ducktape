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

- Cashflow is the DEFAULT rebalancing mechanism: a withdrawal or a deposit moves the
  portfolio toward target for free, because the trade was going to happen anyway. With no
  drift and no cashflow both return all zeros. `rebalance_by_sleeve` is the opt-in third
  mechanism — a trade taken for no reason but the drift itself — and it is off unless a
  policy configures a tolerance, because its turnover and tax drag are exactly what the
  allocation study exists to measure rather than assume.
- `L` is a level, not a fraction of the sale, so a sleeve that is far overweight can fund
  the entire withdrawal on its own while the others are untouched.
"""

from __future__ import annotations

import jax.numpy as jnp
import numpy as np
from numpy.typing import NDArray


def target_value_cents(*, weights: NDArray[np.int64] | jnp.ndarray, total_cents: jnp.ndarray) -> jnp.ndarray:
    """Each sleeve's on-target value: `total * weight_i / sum(weight)`, floored.

    `weights` is `(sleeve,)`, `total_cents` is `(rollout,)`, result is `(sleeve, rollout)`.
    """

    _validate_weights(weights)
    # The divisor is TRACED, not a Python int. It used to be `int(weights.sum())` on the grounds
    # that weights are compile-time known — true, but the cost of making them so was that every
    # distinct weight vector became a separate XLA compile, so a sweep over allocations paid a
    # full compile per arm. Weights are swept numeric config; only their RATIOS matter, and
    # nothing here needs their values at trace time.
    weights = jnp.asarray(weights)
    return (weights[:, None] * total_cents[None, :]) // jnp.sum(weights)


def withdrawal_by_sleeve(
    *, value_cents: jnp.ndarray, weights: NDArray[np.int64] | jnp.ndarray, raise_cents: jnp.ndarray
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
    *, value_cents: jnp.ndarray, weights: NDArray[np.int64] | jnp.ndarray, invest_cents: jnp.ndarray
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


def rebalance_by_sleeve(
    *, value_cents: jnp.ndarray, weights: NDArray[np.int64] | jnp.ndarray, tolerance: float
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Trim every sleeve above target and top up every sleeve below it — or trade nothing at all.

    The mechanism neither `withdrawal_by_sleeve` nor `deposit_by_sleeve` can express: those two
    only ever move money that was already moving, so a sleeve that quietly doubles is never sold
    down. This one trades for no reason but the drift.

    Returns `(sell_cents, buy_cents)`, both `(sleeve, rollout)`. They are disjoint per sleeve — a
    sleeve is above target or below it, never both — and each is zero in a rollout that did not
    trigger.

    ALL-OR-NOTHING per rollout, and all the way back to TARGET rather than to the edge of the
    tolerance. Stopping at the edge would leave the portfolio sitting on its own trigger, which is
    the forced-trading trap the cash band avoids by refilling to its far edge; here it would mean
    paying the tax on a sale that buys almost no correction.

    Drift is measured RELATIVE to each sleeve's own target, because a one-point move means
    something very different for a 90% sleeve than for a 1% one — `tolerance=0.25` is the "25" of
    the standard 5/25 rule. `tolerance=0.0` is a meaningful setting, not a disabled one: it
    rebalances to target whenever anything is off by a cent.

    Sells exceed buys by the cents that flooring each sleeve's target discards (at most one per
    sleeve), so a rebalance can never spend more than it raised. That remainder stays in cash.
    """

    _validate_weights(weights)
    if tolerance < 0:
        raise ValueError(f"rebalance tolerance must not be negative; got {tolerance=}")
    if value_cents.ndim != 2:
        raise ValueError(f"value_cents must be (sleeve, rollout), got {value_cents.shape}")
    if value_cents.shape[0] != weights.shape[0]:
        raise ValueError(f"value_cents has {value_cents.shape[0]} sleeves but weights has {weights.shape[0]}")

    target = target_value_cents(weights=weights, total_cents=value_cents.sum(axis=0))
    drift = value_cents - target
    # `target > 0` guards the empty portfolio: with nothing held every sleeve is exactly on target,
    # and the relative test would be comparing zero against zero.
    fires = ((jnp.abs(drift) >= tolerance * target) & (target > 0)).any(axis=0)
    return jnp.where(fires, jnp.maximum(drift, 0), 0), jnp.where(fires, jnp.maximum(-drift, 0), 0)


def _settle_residual(*, taken: jnp.ndarray, value_cents: jnp.ndarray, wanted: jnp.ndarray) -> jnp.ndarray:
    """Absorb the cent that rounding the water level costs, so the split is exact.

    The level is fractional, so per-sleeve amounts round and the total drifts from `wanted`
    by a few cents. Correcting on the largest contributor keeps the correction proportionally
    smallest and cannot push a sleeve negative or past its value.
    """

    residual = wanted - taken.sum(axis=0)
    # Room each sleeve has to move in the needed direction: toward its cap when the residual
    # is positive, toward zero when negative.
    headroom = jnp.where(residual >= 0, value_cents - taken, taken)
    # The sleeve with the MOST room, so the correction is absorbed whenever any single sleeve
    # can absorb it. Selecting by size of contribution instead would pick a fully-drained
    # sleeve over a roomy one.
    target = jnp.argmax(headroom, axis=0)
    # One-hot rather than a scatter: jnp is functional, and this traces without an index update.
    sleeve_rows = jnp.arange(taken.shape[0])[:, None]
    adjusted = taken + jnp.where(sleeve_rows == target[None, :], residual[None, :], 0)
    # Bounds are inviolable; exactness is not. Clipping cannot bite in practice — the residual
    # is a rounding artifact of a few cents and `wanted` is capped at what the sleeves hold, so
    # some sleeve always has room — but if those ever stopped holding, a total off by a cent is
    # a far better failure than a negative holding or an oversold sleeve, which would create
    # money. This is the guarantee the fuzz tests assert.
    return jnp.clip(adjusted, 0, value_cents)


def _round_half_up(values: jnp.ndarray) -> jnp.ndarray:
    return jnp.floor(values + 0.5).astype(jnp.int64)


def _validate_weights(weights: NDArray[np.int64] | jnp.ndarray) -> None:
    """Shape always; POSITIVITY whenever the values are concrete.

    `weights` may arrive TRACED — the engine passes a device array so that sweeping an allocation
    does not recompile (see `target_value_cents`) — and a tracer has no values to test, so
    `np.any(weights <= 0)` would raise on the tracer itself instead of rejecting a bad weight.
    Shape stays checkable either way, because a tracer's shape is static.

    The guarantee is not weaker on the traced path, only enforced earlier: `_build_program` checks
    the concrete `ta_policies.weights` while building that operand, and `SleeveTarget.weight` is a
    `PositiveInt`, so a non-positive weight is unrepresentable at the scenario boundary.
    """
    if weights.ndim != 1 or weights.shape[0] == 0:
        raise ValueError(f"weights must be a non-empty 1-D array, got {weights.shape}")
    if isinstance(weights, np.ndarray) and np.any(weights <= 0):
        raise ValueError(f"weights must all be positive; got {weights.tolist()}")
