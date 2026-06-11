"""Unit tests for the quote -> probability / confidence layer."""

from __future__ import annotations

import pytest
import pytest_bazel

from finance.augur.calibration.quote import BookQuote, PoolQuote, implied_probability, quote_confidence


def test_micro_price_weights_each_side_by_the_opposite_size() -> None:
    # Heavy resting size on the bid (300) vs the ask (100) pulls the estimate toward the ask:
    # (0.6*100 + 0.8*300)/400 = 0.75, above the plain midpoint of 0.70.
    quote = BookQuote(bid=0.6, ask=0.8, bid_size=300.0, ask_size=100.0, last_trade=0.62)
    assert implied_probability(quote, volume=1000.0) == pytest.approx(0.75)


def test_micro_price_degenerates_to_midpoint_without_sizes() -> None:
    assert implied_probability(
        BookQuote(bid=0.6, ask=0.8, bid_size=None, ask_size=None, last_trade=0.99), volume=1.0
    ) == pytest.approx(0.7)
    assert implied_probability(
        BookQuote(bid=0.6, ask=0.8, bid_size=50.0, ask_size=50.0, last_trade=0.99), volume=1.0
    ) == pytest.approx(0.7)


def test_last_trade_ignored_when_a_two_sided_book_exists() -> None:
    # The whole point of the fix: a stale/fractional last trade (0.04) does not override a live book.
    quote = BookQuote(bid=0.69, ask=0.84, bid_size=10.0, ask_size=10.0, last_trade=0.04)
    assert implied_probability(quote, volume=7000.0) == pytest.approx(0.765)


def test_one_sided_book_falls_back_to_volume_backed_last() -> None:
    # A deep-OTM bucket with only a 1-cent ask but real volume keeps its ~1% via the last trade.
    quote = BookQuote(bid=None, ask=0.01, bid_size=None, ask_size=600.0, last_trade=0.01)
    assert implied_probability(quote, volume=60000.0) == pytest.approx(0.01)


def test_untraded_contract_is_no_observation_not_zero() -> None:
    # Kalshi encodes "never traded" as last 0 / a 0 bid / a 1 ask; that must be None, not P=0.
    untraded = BookQuote(bid=0.0, ask=0.99, bid_size=0.0, ask_size=75.0, last_trade=0.0)
    assert implied_probability(untraded, volume=0.0) is None
    # A last trade with no volume behind it is equally untrustworthy.
    assert (
        implied_probability(BookQuote(bid=None, ask=None, bid_size=None, ask_size=None, last_trade=0.5), volume=None)
        is None
    )


def test_pool_quote_passes_through_and_none_is_none() -> None:
    assert implied_probability(PoolQuote(price=0.42), volume=None) == 0.42
    assert implied_probability(None, volume=100.0) is None


def test_confidence_ranks_tight_over_wide_over_one_sided_over_none() -> None:
    tight = BookQuote(bid=0.58, ask=0.62, bid_size=100.0, ask_size=100.0, last_trade=0.6)
    wide = BookQuote(bid=0.10, ask=0.55, bid_size=100.0, ask_size=100.0, last_trade=0.3)
    one_sided = BookQuote(bid=None, ask=0.01, bid_size=None, ask_size=600.0, last_trade=0.01)
    assert quote_confidence(tight, volume=1000.0) > quote_confidence(wide, volume=1000.0)
    assert quote_confidence(wide, volume=1000.0) > quote_confidence(one_sided, volume=60000.0)
    assert quote_confidence(None, volume=1000.0) == 0.0


if __name__ == "__main__":
    pytest_bazel.main()
