"""Offline checks for undefined comparisons; no evidence downloads or fitting."""

import pytest
import pytest_bazel

from finance.augur.study.macro_window.stability import Comparison, Tally, describe, tally


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_difference_cannot_establish_a_stable_sign(bad: float) -> None:
    comparison = Comparison("test_left", "test_right", 12, "test_metric", (1.0, bad, 2.0))
    assert comparison.flips is None
    assert tally([comparison]) == Tally(flipped=0, defined=0, undefined=1)
    # The undefined period stays in the report rather than being dropped.
    assert str(bad) in describe([comparison])


def test_finite_sign_changes_still_report_separately_from_undefined_comparisons() -> None:
    comparisons = [
        Comparison("test_left", "test_right", 1, "test_flip", (-1.0, 1.0)),
        Comparison("test_left", "test_right", 12, "test_hold", (2.0, 3.0)),
        Comparison("test_left", "test_right", 60, "test_unknown", (float("inf") - float("inf"), 1.0)),
    ]
    assert [comparison.flips for comparison in comparisons] == [True, False, None]
    assert tally(comparisons) == Tally(flipped=1, defined=2, undefined=1)


if __name__ == "__main__":
    pytest_bazel.main()
