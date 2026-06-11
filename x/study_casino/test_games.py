"""Server-side game-rule tests."""

from __future__ import annotations

import pytest_bazel

from x.study_casino.games import settle_blackjack, theoretical_bucket_rtp


def _card(rank: str, suit: str = "♠") -> dict[str, str]:
    return {"rank": rank, "suit": suit}


def _natural() -> list[dict[str, str]]:
    return [_card("A"), _card("K")]


def _dealer_seventeen() -> list[dict[str, str]]:
    return [_card("10", "♥"), _card("7", "♥")]


def test_blackjack_pays_3_to_2_with_round_half_up():
    # Round half up so wager=1 pays 3 credits (formerly truncated to 2 by int(2.5)).
    s = settle_blackjack(_natural(), _dealer_seventeen(), current_wager=1)
    assert s.payout_tokens == 3
    assert s.outcome["outcome"] == "blackjack"


def test_blackjack_even_wager_unchanged():
    # 2 * 2.5 = 5 — no rounding ambiguity, behaviour preserved.
    s = settle_blackjack(_natural(), _dealer_seventeen(), current_wager=2)
    assert s.payout_tokens == 5


def test_blackjack_odd_wager_three_rounds_up_to_eight():
    # 3 * 2.5 = 7.5 → 8 (player-favouring; conventional in integer-credit casinos).
    s = settle_blackjack(_natural(), _dealer_seventeen(), current_wager=3)
    assert s.payout_tokens == 8


def test_theoretical_roulette_number_is_one_pocket_on_fixed_choice():
    theoretical = theoretical_bucket_rtp()

    payout_rate, rtp = theoretical[("roulette", "number")]

    assert payout_rate == 1 / 37
    assert rtp == 36 / 37


if __name__ == "__main__":
    pytest_bazel.main()
