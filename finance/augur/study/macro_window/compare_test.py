"""Fit the joint macro VAR on both candidate windows and report them side by side.

A test rather than a script because two things here are genuine requirements rather than
findings, and they should fail loudly if they ever stop holding: both fits must be STATIONARY,
and the long record must actually be longer and start where `load_macro_history` says it does.

Which window is BETTER is deliberately not asserted. #5509 is explicit that a negative result —
the long record scoring worse and the 1955 window staying — is a completed outcome, so pinning
an answer here would prejudge the thing the run exists to measure. The numbers go to the log and
a human records the decision in `model/SPEC.md`.

Manual, and fetching from the public upstreams, for the reason `//finance/augur/study/trinity`
gives: an upstream outage must not redden an unrelated PR.
"""

from __future__ import annotations

import asyncio

import pytest_bazel

from finance.augur.fit.structural_macro import MacroFitWindow
from finance.augur.study.macro_window.compare import compare_windows, describe, fitted_var, spectral_radius


def test_both_windows_fit_a_stationary_var() -> None:
    """Report both fits, and pin the two properties that are requirements rather than results.

    Stationarity is not a preference: a VAR(1) whose spectral radius reaches 1 has no stationary
    distribution, so its sampled paths wander without bound and every horizon-end quantity the
    simulator reports becomes a function of horizon length rather than of the economy. The long
    record is the one at risk, since it pools the pre-1951 rate peg into one process.
    """

    fits = asyncio.run(compare_windows())

    for window, fitted in fits.items():
        fit = fitted_var(fitted)
        print(describe(str(window), fitted, fit))
        print()
        radius = spectral_radius(fit.transition)
        assert radius < 1.0, f"{window} fitted a non-stationary VAR (spectral radius {radius})"

    short, long_record = fits[MacroFitWindow.FRED_1955], fits[MacroFitWindow.LONG_RECORD_1926]
    # A relationship rather than two dates: the fit's first usable month is a lookback year after
    # the record's own start (the inflation state is TRAILING-year, so twelve months are spent
    # before there is one), and pinning the literal would be re-asserting that arithmetic.
    assert long_record.macro_state_fit.first_month < short.macro_state_fit.first_month
    assert long_record.macro_state_fit.sample_months > short.macro_state_fit.sample_months


if __name__ == "__main__":
    pytest_bazel.main()
