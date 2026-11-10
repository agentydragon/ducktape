from datetime import UTC, datetime, timedelta

import pytest_bazel

from x.ai_sub_value.analyze import ObservedRate, Poll, UnavailableRate, measure_hour

START = datetime(2026, 10, 30, tzinfo=UTC)
RESET = START + timedelta(days=3)


def test_quantized_flat_gauge_does_not_prove_zero_consumption() -> None:
    polls = [Poll(START + timedelta(minutes=5 * i), 30, RESET) for i in range(13)]
    rate = measure_hour(polls, cutoff=polls[-1].at)
    assert isinstance(rate, ObservedRate)
    assert (rate.points_per_hour, rate.lower, rate.upper) == (0, 0, 1)


def test_hour_start_bracket_widens_envelope() -> None:
    polls = [Poll(START + timedelta(minutes=5 * i), 10 + i, RESET) for i in range(14)]
    polls[-1] = Poll(polls[-1].at + timedelta(minutes=2), 23, RESET)
    rate = measure_hour(polls, cutoff=polls[-1].at)
    assert isinstance(rate, ObservedRate)
    assert (rate.points_per_hour, rate.lower, rate.upper) == (11.5, 10, 13)
    assert rate.start_bracket_seconds == 300


def test_reset_with_unchanged_advertised_reset_is_rejected() -> None:
    polls = [Poll(START + timedelta(minutes=5 * i), 90 if i < 6 else 5, RESET) for i in range(13)]
    assert isinstance(measure_hour(polls, cutoff=polls[-1].at), UnavailableRate)


def test_gap_and_saturation_are_unavailable() -> None:
    polls = [Poll(START + timedelta(minutes=5 * i), 100, RESET) for i in range(13)]
    assert isinstance(measure_hour(polls, cutoff=polls[-1].at), UnavailableRate)
    assert isinstance(measure_hour([polls[0], polls[-1]], cutoff=polls[-1].at), UnavailableRate)


def test_discontinued_series_is_unavailable() -> None:
    polls = [Poll(START + timedelta(minutes=5 * i), i, RESET) for i in range(13)]
    assert isinstance(measure_hour(polls, cutoff=polls[-1].at + timedelta(days=1)), UnavailableRate)


if __name__ == "__main__":
    pytest_bazel.main()
