"""Invariants of the target-allocation split. Pure math, so every number is exact."""

from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel

from finance.augur.sim.allocation import target_value_cents, withdrawal_by_sleeve


def _withdraw(value: list[list[int]], weights: list[int], raise_cents: list[int]) -> list[list[int]]:
    taken = withdrawal_by_sleeve(
        value_cents=np.asarray(value, dtype=np.int64),
        weights=np.asarray(weights, dtype=np.int64),
        raise_cents=np.asarray(raise_cents, dtype=np.int64),
    )
    return [[int(x) for x in row] for row in taken]


def test_nothing_is_sold_to_raise_nothing() -> None:
    """Drift alone must never trigger a trade. A portfolio can sit far off target
    indefinitely; only a cashflow moves it, because rebalancing turnover would cost more in
    tax drag than the drift costs in risk."""

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

    taken = withdrawal_by_sleeve(value_cents=value, weights=weights, raise_cents=wanted)

    assert np.all(taken >= 0)
    assert np.all(taken <= value)
    assert np.all(taken.sum(axis=0) == np.minimum(wanted, value.sum(axis=0)))


def test_rollouts_are_independent() -> None:
    """The batch axis must not couple rollouts: each column's answer is what it would be if
    computed alone. A shared water level across rollouts is exactly the bug this catches."""

    together = _withdraw([[900, 100], [100, 900]], [1, 1], [400, 400])

    assert together[0] == [400, 0]
    assert together[1] == [0, 400]


def test_target_value_splits_by_weight() -> None:
    target = target_value_cents(
        weights=np.asarray([3, 1], dtype=np.int64), total_cents=np.asarray([8_000], dtype=np.int64)
    )

    assert [int(x) for x in target[:, 0]] == [6_000, 2_000]


def test_zero_and_negative_weights_are_rejected() -> None:
    """A zero weight would mean "target exactly nothing", which is a sleeve that should not
    be in the universe at all — and it divides by zero in the level arithmetic."""

    with pytest.raises(ValueError, match="positive"):
        withdrawal_by_sleeve(
            value_cents=np.asarray([[1], [1]], dtype=np.int64),
            weights=np.asarray([1, 0], dtype=np.int64),
            raise_cents=np.asarray([0], dtype=np.int64),
        )


if __name__ == "__main__":
    pytest_bazel.main()
