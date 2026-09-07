"""Run the century-vs-1955 comparison over several scoring periods and report what survives.

Asserts validity, not a winner — and here that matters more than usual, because the thing under
test is whether "a winner" is even a well-posed idea for this comparison.

Manual, and fetching from the public upstreams, so an upstream outage cannot redden an unrelated
PR.
"""

from __future__ import annotations

import asyncio

import pytest_bazel

from finance.augur.study.macro_window.holdout import HORIZONS, long_record_state_path
from finance.augur.study.macro_window.stability import ORIGIN_STARTS, describe, single_window_stability


def test_the_window_comparison_is_reported_across_scoring_periods() -> None:
    """Report every comparison and whether its sign held.

    The requirement is coverage: every horizon compared on every metric, once per origin set. A
    sweep that silently dropped a period would report stability it had not measured, which is the
    one failure mode that would make this module worse than not having it.
    """

    comparisons = single_window_stability(asyncio.run(long_record_state_path()))
    print(describe(comparisons))

    assert {comparison.horizon for comparison in comparisons} == set(HORIZONS)
    for comparison in comparisons:
        assert len(comparison.differences) == len(ORIGIN_STARTS), f"{comparison.metric} missing a period"


if __name__ == "__main__":
    pytest_bazel.main()
