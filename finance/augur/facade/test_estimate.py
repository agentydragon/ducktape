from collections.abc import Callable

import pytest
import pytest_bazel

from finance.augur.facade.estimate import (
    Direction,
    Estimate,
    Mean,
    MeanDifference,
    Proportion,
    ProportionDifference,
    Resolved,
    Unresolved,
    WindowFrequency,
    verdict,
)

# Four paths whose arms spread over hundreds while their per-path differences are 1, 1, 1 and 5:
# the mean difference is 2, with standard error sqrt(12 / 3) / sqrt(4) = 1.
_WIDE_A = {10: 101.0, 11: 201.0, 12: 301.0, 13: 405.0}
_WIDE_B = {10: 100.0, 11: 200.0, 12: 300.0, 13: 400.0}


@pytest.mark.parametrize(
    ("successes", "paths", "interval"),
    [(81, 263, (0.2553, 0.3662)), (15, 148, (0.0624, 0.1605)), (0, 20, (0.0, 0.1611)), (1, 29, (0.0061, 0.1718))],
)
def test_wilson_interval_matches_published_values(successes: int, paths: int, interval: tuple[float, float]) -> None:
    """Newcombe (1998), Statistics in Medicine 17:857-872, Table II, method 3 (score, no continuity correction)."""
    assert Proportion(successes=successes, paths=paths, confidence=0.95).interval == pytest.approx(interval, abs=5e-5)


def test_wilson_interval_follows_the_stated_confidence() -> None:
    # With no successes the upper bound is z² / (n + z²); at 90%, z = 1.64485 and 2.70554 / 12.70554 = 0.21294.
    assert Proportion(successes=0, paths=10, confidence=0.90).interval == pytest.approx((0.0, 0.21294), abs=5e-6)


def test_paired_proportion_error_comes_from_the_discordant_paths() -> None:
    # Nine paths: only a succeeds on 0-3, only b on 4, both on 5-6, neither on 7-8.
    a = {0: True, 1: True, 2: True, 3: True, 4: False, 5: True, 6: True, 7: False, 8: False}
    b = {0: False, 1: False, 2: False, 3: False, 4: True, 5: True, 6: True, 7: False, 8: False}
    paired = ProportionDifference.from_paths(a=a, b=b)
    assert (paired.a_only, paired.b_only) == (4, 1)
    # (4 - 1) / 9 and sqrt((4 + 1) - (4 - 1)² / 9) / 9.
    assert (paired.difference, paired.standard_error) == pytest.approx((1 / 3, 2 / 9))


def test_paired_mean_error_comes_from_the_per_path_differences() -> None:
    paired = MeanDifference.from_paths(a=_WIDE_A, b=_WIDE_B)
    assert (paired.difference, paired.standard_error, paired.paths) == pytest.approx((2.0, 1.0, 4))


def test_arms_pair_by_path_id_not_position() -> None:
    reordered = dict(reversed(_WIDE_B.items()))
    assert MeanDifference.from_paths(a=_WIDE_A, b=reordered) == MeanDifference.from_paths(a=_WIDE_A, b=_WIDE_B)


@pytest.mark.parametrize(
    "b_paths", [(0, 1, 3), (0, 1), (0, 1, 2, 3)], ids=["different path", "missing path", "extra path"]
)
def test_arms_on_different_paths_are_rejected(b_paths: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="different paths"):
        ProportionDifference.from_paths(a=dict.fromkeys((0, 1, 2), True), b=dict.fromkeys(b_paths, True))
    with pytest.raises(ValueError, match="different paths"):
        MeanDifference.from_paths(a=dict.fromkeys((0, 1, 2), 1.0), b=dict.fromkeys(b_paths, 1.0))


def test_identical_arms_are_unresolved() -> None:
    outcomes = {0: True, 1: False, 2: True}
    values = {0: 1.5, 1: -2.0, 2: 7.25}
    assert verdict(ProportionDifference.from_paths(a=outcomes, b=outcomes), standard_errors=2.0) == Unresolved()
    assert verdict(MeanDifference.from_paths(a=values, b=values), standard_errors=2.0) == Unresolved()


def test_a_resolved_verdict_names_the_higher_arm() -> None:
    # The difference lies 2 standard errors from zero: resolved at 1.5, not at 3.
    assert verdict(MeanDifference.from_paths(a=_WIDE_A, b=_WIDE_B), standard_errors=1.5) == Resolved(Direction.A_HIGHER)
    assert verdict(MeanDifference.from_paths(a=_WIDE_B, b=_WIDE_A), standard_errors=1.5) == Resolved(Direction.B_HIGHER)
    assert verdict(MeanDifference.from_paths(a=_WIDE_A, b=_WIDE_B), standard_errors=3.0) == Unresolved()


@pytest.mark.parametrize(
    ("estimate", "rendered"),
    [
        pytest.param(
            Mean(mean=0.2317, standard_error=0.018, paths=500),
            "0.23 ± 0.02 (SE, 500 paths)",
            id="error to one significant figure",
        ),
        pytest.param(
            Mean(mean=2.3456, standard_error=0.096, paths=100),
            "2.3 ± 0.1 (SE, 100 paths)",
            id="error rounding carries a decade",
        ),
        pytest.param(
            Mean(mean=1234567.0, standard_error=12345.0, paths=100),
            "1,230,000 ± 10,000 (SE, 100 paths)",
            id="error above one rounds the integer part",
        ),
        pytest.param(
            Mean(mean=2.5, standard_error=0.0, paths=3),
            "2.5 ± 0 (SE, 3 paths)",
            id="zero error leaves the value unrounded",
        ),
        pytest.param(
            MeanDifference(difference=-0.0004, standard_error=0.003, paths=100),
            "+0.000 ± 0.003 (SE, 100 paired paths)",
            id="no negative zero",
        ),
        pytest.param(
            ProportionDifference(paths=1000, a_only=40, b_only=17),
            "+2.3pp ± 0.8pp (SE, 1000 paired paths)",
            id="proportion difference in percentage points",
        ),
        pytest.param(
            Proportion(successes=81, paths=263, confidence=0.95),
            "31% (95% CI 26-37%, 81 of 263 paths)",
            id="interval half-width caps a proportion",
        ),
        pytest.param(
            Proportion(successes=2317, paths=10000, confidence=0.95),
            "23.2% (95% CI 22.4-24.0%, 2317 of 10000 paths)",
            id="more paths earn a digit",
        ),
        pytest.param(
            WindowFrequency(successes=34, windows=840, independent_windows=2.33),
            "34 of 840 overlapping windows (≈2.3 independent)",
            id="replay count prints no fraction",
        ),
    ],
)
def test_rendering_prints_no_digit_finer_than_the_error(estimate: Estimate, rendered: str) -> None:
    assert str(estimate) == rendered


@pytest.mark.parametrize(
    ("construct", "message"),
    [
        pytest.param(
            lambda: Proportion(successes=5, paths=4, confidence=0.95), "successes <= paths", id="successes above paths"
        ),
        pytest.param(lambda: Proportion(successes=1, paths=4, confidence=1.0), "confidence", id="certain confidence"),
        pytest.param(lambda: Mean.of([1.0]), "at least two paths", id="mean of one path"),
        pytest.param(
            lambda: ProportionDifference(paths=10, a_only=6, b_only=5), "discordant", id="discordant above paths"
        ),
        pytest.param(
            lambda: WindowFrequency(successes=3, windows=840, independent_windows=900.0),
            "independent_windows <= windows",
            id="more independent windows than windows",
        ),
        pytest.param(
            lambda: verdict(MeanDifference.from_paths(a=_WIDE_A, b=_WIDE_B), standard_errors=0.0),
            "standard_errors",
            id="zero threshold",
        ),
    ],
)
def test_invalid_estimates_are_rejected(construct: Callable[[], object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        construct()


if __name__ == "__main__":
    pytest_bazel.main()
