"""Invariants of the target-allocation split. Pure math, so every number is exact."""

from __future__ import annotations

import jax
import jax.numpy as jnp
import numpy as np
import pytest
import pytest_bazel

from finance.augur.sim.allocation import (
    deposit_by_sleeve,
    rebalance_by_sleeve,
    target_value_cents,
    withdrawal_by_sleeve,
)


def _deposit(value: list[list[int]], weights: list[int], invest_cents: list[int]) -> list[list[int]]:
    given = deposit_by_sleeve(
        value_cents=jnp.asarray(value, dtype=jnp.int64),
        weights=np.asarray(weights, dtype=np.int64),
        invest_cents=jnp.asarray(invest_cents, dtype=jnp.int64),
    )
    return [[int(x) for x in row] for row in given]


def _withdraw(value: list[list[int]], weights: list[int], raise_cents: list[int]) -> list[list[int]]:
    taken = withdrawal_by_sleeve(
        value_cents=jnp.asarray(value, dtype=jnp.int64),
        weights=np.asarray(weights, dtype=np.int64),
        raise_cents=jnp.asarray(raise_cents, dtype=jnp.int64),
    )
    return [[int(x) for x in row] for row in taken]


def test_nothing_is_sold_to_raise_nothing() -> None:
    """Drift alone never moves THIS split. A portfolio can sit far off target indefinitely
    while only cashflow rebalances it — which is the default, because whether the tax drag of
    turnover is worth the drift it removes is what the allocation study measures rather than
    assumes. `rebalance_by_sleeve` is the opt-in that trades on drift alone."""

    assert _withdraw([[10_000], [1]], [1, 1], [0]) == [[0], [0]]


def test_the_overweight_sleeve_funds_the_whole_withdrawal() -> None:
    """The single most important behavior: an equal-weight portfolio at 900/100 raising 400
    takes it all from the overweight sleeve, because even after the sale it is still the
    richer one (500 vs 100)."""

    assert _withdraw([[900], [100]], [1, 1], [400]) == [[400], [0]]


def test_a_large_withdrawal_lands_the_remainder_exactly_on_target() -> None:
    """Water-filling reaches the target rather than approaching it. From 900/100 at equal
    weight, raising 800 leaves 200 total, so both sleeves must end at 100."""

    assert _withdraw([[900], [100]], [1, 1], [800]) == [[800], [0]]


def test_both_sleeves_contribute_once_the_level_falls_below_both() -> None:
    """From 900/100 raising 900, the remaining 100 must split 50/50 — so the underweight
    sleeve gives up 50 even though it started underweight. Selling only the overweight
    sleeve could not raise it without leaving the ratio wrong."""

    assert _withdraw([[900], [100]], [1, 1], [900]) == [[850], [50]]


def test_weights_are_relative_not_absolute() -> None:
    """(3, 1) and (30, 10) are the same policy. If they ever diverged, the weights would be
    carrying a scale they are not supposed to have."""

    assert _withdraw([[7_000], [3_000]], [3, 1], [1_000]) == _withdraw([[7_000], [3_000]], [30, 10], [1_000])


def test_an_unequal_target_pulls_toward_that_target() -> None:
    """A 3:1 target holding 5000/5000 is short of bonds and long of stocks; raising 2000
    takes it from the sleeve that is over ITS OWN target, not from the larger sleeve."""

    taken = _withdraw([[5_000], [5_000]], [3, 1], [2_000])

    # Post-sale total 8000 splits 6000/2000, so the second sleeve gives up 3000 and the
    # first must therefore be BOUGHT back toward target rather than sold — which a
    # withdrawal cannot do, so it is left alone and the second funds what it can.
    assert taken[0][0] == 0
    assert taken[1][0] == 2_000


def test_the_split_is_exact_when_the_level_does_not_divide() -> None:
    """Rounding the water level cannot lose or invent a cent: the sleeve amounts must sum to
    exactly what was asked for, however awkward the arithmetic."""

    for wanted in (1, 7, 333, 99_991):
        taken = _withdraw([[1_000_003], [700_001], [3]], [5, 3, 1], [wanted])
        assert sum(row[0] for row in taken) == wanted


def test_a_withdrawal_beyond_the_portfolio_drains_it_rather_than_overselling() -> None:
    """Asking for more than exists takes everything and no more. The caller sees a total
    short of the request and can fail the month; silently inventing the difference here
    would hide a ruin."""

    assert _withdraw([[300], [200]], [1, 1], [10_000]) == [[300], [200]]


def test_no_sleeve_is_ever_taken_negative() -> None:
    """Fuzzed over ragged values and weights — the residual fix-up is the part most likely to
    push a sleeve past its own value or below zero."""

    rng = np.random.default_rng(20260805)
    value = rng.integers(0, 5_000_000, size=(4, 64), dtype=np.int64)
    weights = np.asarray([7, 1, 4, 2], dtype=np.int64)
    wanted = rng.integers(0, 8_000_000, size=64, dtype=np.int64)

    taken = withdrawal_by_sleeve(value_cents=jnp.asarray(value), weights=weights, raise_cents=jnp.asarray(wanted))

    assert np.all(taken >= 0)
    assert np.all(taken <= value)
    assert np.all(taken.sum(axis=0) == np.minimum(wanted, value.sum(axis=0)))


def test_rollouts_are_independent() -> None:
    """The batch axis must not couple rollouts: each column's answer is what it would be if
    computed alone. A shared water level across rollouts is exactly the bug this catches."""

    together = _withdraw([[900, 100], [100, 900]], [1, 1], [400, 400])

    assert together[0] == [400, 0]
    assert together[1] == [0, 400]


def test_investing_nothing_deposits_nothing() -> None:
    """Same no-trade guarantee as the sell side: drift alone never provokes a purchase."""

    assert _deposit([[10_000], [1]], [1, 1], [0]) == [[0], [0]]


def test_a_deposit_fills_the_underweight_sleeve_first() -> None:
    """The mirror of taking from the overweight sleeve: 900/100 at equal weight investing
    400 puts it all into the laggard, which is still behind afterwards (900 vs 500)."""

    assert _deposit([[900], [100]], [1, 1], [400]) == [[0], [400]]


def test_a_large_deposit_lands_both_sleeves_on_target() -> None:
    """Investing 1,000 into 900/100 at equal weight gives a 2,000 total, so both must end
    at 1,000 — the laggard takes 900 and the leader 100."""

    assert _deposit([[900], [100]], [1, 1], [1_000]) == [[100], [900]]


def test_a_deposit_respects_unequal_targets() -> None:
    """A 3:1 target holding 5,000/5,000 is short of the first sleeve. Investing 2,000 makes
    the total 12,000, which splits 9,000/3,000 — so the first takes 4,000 and the second
    would have to GIVE UP 2,000, which a deposit cannot do. It fills what it can."""

    assert _deposit([[5_000], [5_000]], [3, 1], [2_000]) == [[2_000], [0]]


def test_a_deposit_is_exact_when_the_level_does_not_divide() -> None:
    """Deposits have no availability cap, so unlike a withdrawal they must ALWAYS sum to
    exactly the amount asked for, however awkward the arithmetic."""

    for wanted in (1, 7, 333, 99_991):
        given = _deposit([[1_000_003], [700_001], [3]], [5, 3, 1], [wanted])
        assert sum(row[0] for row in given) == wanted


def test_no_sleeve_receives_a_negative_deposit() -> None:
    """Fuzzed. An overweight sleeve must receive zero, never a negative amount that would
    quietly turn a buy into a sell."""

    rng = np.random.default_rng(20260806)
    value = rng.integers(0, 5_000_000, size=(4, 64), dtype=np.int64)
    weights = np.asarray([7, 1, 4, 2], dtype=np.int64)
    wanted = rng.integers(0, 8_000_000, size=64, dtype=np.int64)

    given = deposit_by_sleeve(value_cents=jnp.asarray(value), weights=weights, invest_cents=jnp.asarray(wanted))

    assert np.all(given >= 0)
    assert np.all(given.sum(axis=0) == wanted)


def test_a_round_trip_through_both_sides_returns_to_target() -> None:
    """Withdrawing then re-depositing the same amount from an on-target portfolio must
    leave it on target — the two water-fillings have to be genuine inverses, not merely
    similar-looking arithmetic."""

    weights = np.asarray([3, 1], dtype=np.int64)
    value = jnp.asarray([[6_000], [2_000]], dtype=jnp.int64)
    amount = jnp.asarray([1_600], dtype=jnp.int64)

    after_sale = value - withdrawal_by_sleeve(value_cents=value, weights=weights, raise_cents=amount)
    restored = after_sale + deposit_by_sleeve(value_cents=after_sale, weights=weights, invest_cents=amount)

    assert [[int(x) for x in row] for row in restored] == [[6_000], [2_000]]


def test_both_splits_trace_under_jit() -> None:
    """The reason this module is `jnp` and not numpy: it runs inside the jitted scan. A
    numpy op sneaking back in would force a second implementation in the engine, and the
    two would drift — so assert traceability directly rather than trusting review.

    `weights` is closed over rather than passed, which is exactly how the engine will use
    it: compile-time config, never traced.
    """

    weights = np.asarray([3, 1], dtype=np.int64)
    value = jnp.asarray([[6_000], [2_000]], dtype=jnp.int64)

    sell = jax.jit(lambda v, amount: withdrawal_by_sleeve(value_cents=v, weights=weights, raise_cents=amount))
    buy = jax.jit(lambda v, amount: deposit_by_sleeve(value_cents=v, weights=weights, invest_cents=amount))

    assert [int(x) for x in sell(value, jnp.asarray([1_600])).sum(axis=0)] == [1_600]
    assert [int(x) for x in buy(value, jnp.asarray([1_600])).sum(axis=0)] == [1_600]


def test_target_value_splits_by_weight() -> None:
    target = target_value_cents(
        weights=np.asarray([3, 1], dtype=np.int64), total_cents=jnp.asarray([8_000], dtype=jnp.int64)
    )

    assert [int(x) for x in target[:, 0]] == [6_000, 2_000]


def test_zero_and_negative_weights_are_rejected() -> None:
    """A zero weight would mean "target exactly nothing", which is a sleeve that should not
    be in the universe at all — and it divides by zero in the level arithmetic."""

    with pytest.raises(ValueError, match="positive"):
        withdrawal_by_sleeve(
            value_cents=jnp.asarray([[1], [1]], dtype=jnp.int64),
            weights=np.asarray([1, 0], dtype=np.int64),
            raise_cents=jnp.asarray([0], dtype=jnp.int64),
        )


# -- Rebalancing on drift alone ------------------------------------------------------------


def _rebalance(value: list[list[int]], weights: list[int], tolerance: float) -> tuple[list[list[int]], list[list[int]]]:
    sell, buy = rebalance_by_sleeve(
        value_cents=jnp.asarray(value, dtype=jnp.int64),
        weights=np.asarray(weights, dtype=np.int64),
        tolerance=tolerance,
    )
    return ([[int(x) for x in row] for row in sell], [[int(x) for x in row] for row in buy])


def test_a_trigger_goes_all_the_way_back_to_target() -> None:
    """Not to the edge of the tolerance. 900/100 against equal weights is 500/500 on target, so
    400 moves across — even though a 0.25 tolerance would have been satisfied by moving 275."""

    assert _rebalance([[900], [100]], [1, 1], 0.25) == ([[400], [0]], [[0], [400]])


def test_drift_inside_the_tolerance_trades_nothing() -> None:
    """600/400 is 100 off a 500 target: 20% drift, inside a 25% tolerance. The whole point of a
    tolerance is that the portfolio is allowed to sit off target, so this must be exactly zero
    rather than a small trade."""

    assert _rebalance([[600], [400]], [1, 1], 0.25) == ([[0], [0]], [[0], [0]])


def test_one_sleeve_over_the_tolerance_rebalances_every_sleeve() -> None:
    """All-or-nothing. Sleeve 2 is 100/1000 = 10% off its target, well inside the tolerance, but
    sleeve 0 is over it — and once the portfolio is being rebalanced there is no reason to leave
    a sleeve off target, since the trade is already being paid for."""

    sell, buy = _rebalance([[1_600], [1_100], [300]], [1, 1, 1], 0.25)

    assert sell == [[600], [100], [0]]
    assert buy == [[0], [0], [700]]


def test_drift_is_measured_relative_to_each_sleeves_own_target() -> None:
    """A one-point move means something very different for a 90% sleeve than for a 10% one.
    Here both sleeves are 90 cents off, which is 1% of the big sleeve's target and 9% of the
    small one's — so a 5% tolerance fires on the small sleeve and an absolute rule would not
    have fired at all."""

    sell, buy = _rebalance([[8_910], [1_090]], [9, 1], 0.05)

    assert (sell, buy) == ([[0], [90]], [[90], [0]])


def test_a_rebalance_never_spends_more_than_it_raised() -> None:
    """The invariant that lets the executor run the sells and the buys as two independent legs.
    Flooring each sleeve's target discards at most a cent per sleeve, and the discarded cents
    have to land on the SELL side — a buy leg larger than its sell leg would overdraw the
    account by whatever the market moved in between."""

    for total, weights in ((10_000, [1, 1, 1]), (99_991, [7, 3]), (1, [1, 1]), (12_345_678, [5, 3, 2, 1])):
        value = [[total if i == 0 else 0] for i in range(len(weights))]
        sell, buy = _rebalance(value, weights, 0.0)
        assert sum(row[0] for row in sell) >= sum(row[0] for row in buy), f"{total=} {weights=}"


def test_an_empty_portfolio_rebalances_nothing() -> None:
    """Every sleeve is trivially on target, and the relative test would be comparing zero
    against zero."""

    assert _rebalance([[0], [0]], [1, 1], 0.0) == ([[0], [0]], [[0], [0]])


def test_a_portfolio_too_small_to_have_per_sleeve_targets_trades_nothing() -> None:
    """One cent across two equal sleeves floors both targets to zero, so "on target" is a state
    the portfolio cannot reach. Firing anyway would sell the cent and buy nothing back — a
    rebalance that is purely a drain, repeated every month."""

    assert _rebalance([[1], [0]], [1, 1], 0.0) == ([[0], [0]], [[0], [0]])


def test_a_zero_tolerance_still_means_something() -> None:
    """Not a disabled rebalance — the disabled state is the policy not configuring one at all.
    Zero means correct any drift at all, which one cent of drift is."""

    assert _rebalance([[501], [499]], [1, 1], 0.0) == ([[1], [0]], [[0], [1]])


def test_rebalance_rollouts_are_independent() -> None:
    """A rollout inside its tolerance must not be dragged into a trade by a neighbour that
    crossed — `fires` is per-rollout, and an `.any()` over the wrong axis would do exactly that."""

    sell, buy = _rebalance([[900, 550], [100, 450]], [1, 1], 0.25)

    assert sell == [[400, 0], [0, 0]]
    assert buy == [[0, 0], [400, 0]]


def test_a_negative_tolerance_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        _rebalance([[1], [1]], [1, 1], -0.1)


def test_the_rebalance_traces_under_jit() -> None:
    weights = np.asarray([1, 1], dtype=np.int64)
    rebalance = jax.jit(lambda v: rebalance_by_sleeve(value_cents=v, weights=weights, tolerance=0.25))

    sell, buy = rebalance(jnp.asarray([[900], [100]], dtype=jnp.int64))

    assert (int(sell[0, 0]), int(buy[1, 0])) == (400, 400)


if __name__ == "__main__":
    pytest_bazel.main()
