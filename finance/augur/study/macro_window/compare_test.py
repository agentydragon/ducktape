"""Fit the joint macro VAR on both candidate windows and report them side by side.

A test rather than a script because one thing here is a genuine requirement rather than a
finding, and it should fail loudly if it ever stops holding: the long record must actually be
longer and start where `load_macro_history` says it does. Both fits being stationary is held by
`MacroVarSpec`, which rejects an explosive transition, so `compare_windows()` raises on one.

Which window is BETTER is deliberately not asserted. #5509 is explicit that a negative result —
the long record scoring worse and the 1955 window staying — is a completed outcome, so pinning
an answer here would prejudge the thing the run exists to measure. The numbers go to the log and
a human records the decision in `x/models/structural_macro.md`.

Manual, and fetching from the public upstreams, for the reason `//finance/augur/study/trinity`
gives: an upstream outage must not redden an unrelated PR.
"""

from __future__ import annotations

import asyncio

import pytest_bazel

from finance.augur.study.macro_window.compare import compare_windows, describe, fitted_var
from finance.augur.x.models.structural_macro_fit import MacroFitWindow


def test_both_windows_are_fit_and_the_long_record_is_longer() -> None:
    """Report both fits, and pin the long record's span, which is a requirement rather than a result."""

    fits = asyncio.run(compare_windows())

    for window, fitted in fits.items():
        print(describe(str(window), fitted, fitted_var(fitted)))
        print()

    short, long_record = fits[MacroFitWindow.FRED_1955], fits[MacroFitWindow.LONG_RECORD_1926]
    # A relationship rather than two dates: the fit's first usable month is a lookback year after
    # the record's own start (the inflation state is TRAILING-year, so twelve months are spent
    # before there is one), and pinning the literal would be re-asserting that arithmetic.
    assert long_record.macro_state_fit.first_month < short.macro_state_fit.first_month
    assert long_record.macro_state_fit.sample_months > short.macro_state_fit.sample_months


if __name__ == "__main__":
    pytest_bazel.main()
