"""Score the two window lengths out of sample, on one series, and report the result.

A test rather than a script for the reason `compare_test.py` gives, and asserting the same kind
of thing: what makes the comparison VALID, never which arm wins. #5509 is explicit that the long
record scoring worse is a completed outcome, so an assertion either way would prejudge the run.
The numbers go to the log and a human records the decision in `model/SPEC.md`.

Manual, and fetching from the public upstreams, so an upstream outage cannot redden an unrelated
PR.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest_bazel

from finance.augur.study.macro_window.holdout import HORIZONS, compare_start_dates, describe, long_record_state_path


def test_both_window_lengths_are_scored_on_the_same_origins() -> None:
    """Report the held-out scores, and pin what the comparison needs in order to mean anything.

    Equal origin counts are the requirement: the arms differ in training span by construction, so
    if they also differed in WHICH months they were scored on, a gap between them could just be a
    gap between two stretches of history. Finiteness is the other: a fit that came out
    non-stationary at some origin makes the 10-year covariance overflow rather than fail, and the
    mean would quietly become meaningless.
    """

    scores = compare_start_dates(asyncio.run(long_record_state_path()))
    print(describe(scores))

    assert {score.horizon for score in scores} == set(HORIZONS)
    for horizon in HORIZONS:
        at_horizon = [score for score in scores if score.horizon == horizon]
        assert len({score.origins for score in at_horizon}) == 1, f"arms disagree on origins at {horizon=}"
        assert all(score.origins > 0 for score in at_horizon), f"nothing scorable at {horizon=}"
    for score in scores:
        assert np.isfinite(score.mean_log_density), f"{score.arm} at {score.horizon} scored non-finite"
        assert all(np.isfinite(value) for value in score.mean_crps.values())


if __name__ == "__main__":
    pytest_bazel.main()
