"""Sweep the mixed fit's covariance span, scored against both single windows on the same origins.

The mixed fit beat the long record on every marginal at five years and lost to it on JOINT
density. Marginals are per-equation and the joint is not, so the suspect is the one part of the
fit that no window choice covers: the innovation covariance, whose span had never been chosen.

Asserts validity, not a winner, for the reason `holdout_test.py` gives.

Manual, and fetching from the public upstreams, so an upstream outage cannot redden an unrelated
PR.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest_bazel

from finance.augur.study.macro_window.holdout import describe, long_record_state_path
from finance.augur.study.macro_window.mixed_windows import compare_covariance_spans


def test_every_covariance_span_scores_finite() -> None:
    """Report every span, and pin that every score is finite."""

    scores = compare_covariance_spans(asyncio.run(long_record_state_path()))
    print(describe(scores))

    for score in scores:
        assert np.isfinite(score.mean_log_density), f"{score.arm} at {score.horizon} scored non-finite"
        assert all(np.isfinite(value) for value in score.mean_crps.values())


if __name__ == "__main__":
    pytest_bazel.main()
