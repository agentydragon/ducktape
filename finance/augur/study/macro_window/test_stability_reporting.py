"""Offline checks for undefined comparisons; no evidence downloads or fitting."""

from datetime import date

import pytest
import pytest_bazel

from finance.augur.study.macro_window.stability import Comparison, describe


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_difference_cannot_establish_a_stable_sign(bad: float) -> None:
    comparison = Comparison("test_left", "test_right", 12, "test_metric", (1.0, bad, 2.0))
    assert comparison.flips is None
    report = describe([comparison])
    assert "UNDEFINED" in report
    assert "0 of 0 defined" in report
    assert "1 comparisons have undefined stability" in report
    assert "nan" in report or "inf" in report


def test_finite_sign_changes_still_report_separately_from_undefined_comparisons() -> None:
    comparisons = [
        Comparison("test_left", "test_right", 1, "test_flip", (-1.0, 1.0)),
        Comparison("test_left", "test_right", 12, "test_hold", (2.0, 3.0)),
        Comparison("test_left", "test_right", 60, "test_unknown", (float("inf") - float("inf"), 1.0)),
    ]
    assert [comparison.flips for comparison in comparisons] == [True, False, None]
    report = describe(comparisons, origin_starts=(date(2000, 1, 1), date(2001, 1, 1)))
    assert "1 of 2 defined" in report
    assert "1 comparisons have undefined stability" in report


if __name__ == "__main__":
    pytest_bazel.main()
