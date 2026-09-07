"""Score the mixed-window fit against both single-window fits, on the same origins.

A test for the reason `holdout_test.py` is one, and asserting the same kind of thing: what makes
the comparison valid, never which arm wins. #5817 counts the mixed fit failing to beat both as a
completed outcome, so an assertion either way would prejudge the run.

Manual, and fetching from the public upstreams, so an upstream outage cannot redden an unrelated
PR.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest_bazel

from finance.augur.study.macro_window.holdout import HORIZONS, describe, long_record_state_path
from finance.augur.study.macro_window.mixed_windows import (
    FRED_WINDOW_START,
    EquationWindows,
    compare_with_mixed,
    fit_mixed_windows,
)


def test_the_mixed_fit_is_scored_beside_both_single_windows() -> None:
    """Report all three arms, and pin what the comparison needs in order to mean anything.

    Same origins across arms is the requirement — the arms differ in training span by
    construction, so if they also differed in WHICH months they were scored on, a gap between
    them could just be a gap between two stretches of history. Finiteness is the other: rows
    estimated on different spans have no guarantee of composing into a stationary transition, and
    a non-stationary one makes the 10-year covariance overflow rather than fail.
    """

    path = asyncio.run(long_record_state_path())
    scores = compare_with_mixed(path)
    print(describe(scores))

    # Rows fitted on different samples have no guarantee of composing into a stationary
    # transition — each equation is estimated without reference to the others' windows. A
    # spectral radius at or above 1 means the sampled paths wander without bound, which would
    # disqualify the fit however well it scored.
    mixed = fit_mixed_windows(
        path,
        EquationWindows(short_rate=FRED_WINDOW_START, term_spread=FRED_WINDOW_START, inflation_rate=path.months[0]),
    )
    print(f"mixed spectral radius {mixed.spectral_radius:.4f}")
    assert mixed.spectral_radius < 1.0, f"mixed windows composed a non-stationary VAR ({mixed.spectral_radius})"

    assert len({score.arm for score in scores}) == 3
    for horizon in HORIZONS:
        at_horizon = [score for score in scores if score.horizon == horizon]
        assert len({score.origins for score in at_horizon}) == 1, f"arms disagree on origins at {horizon=}"
    for score in scores:
        assert np.isfinite(score.mean_log_density), f"{score.arm} at {score.horizon} scored non-finite"
        assert all(np.isfinite(value) for value in score.mean_crps.values())


if __name__ == "__main__":
    pytest_bazel.main()
