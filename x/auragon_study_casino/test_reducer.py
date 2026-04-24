"""Reducer semantics — covers every event type with its characteristic effect."""

from __future__ import annotations

import pytest
import pytest_bazel

from x.auragon_study_casino.reducer import DEFAULT_PRIZES, initial_state, reduce_event


def _reduce(events: list[tuple[str, dict]]) -> dict:
    state = initial_state()
    for t, p in events:
        state = reduce_event(state, t, p)
    return state


def test_initial_state_has_default_prizes() -> None:
    s = initial_state()
    assert s["credits"] == 0
    assert s["tokens"] == 0
    assert s["sessions"] == []
    assert s["activeSession"] is None
    assert s["prizeLog"] == []
    assert [p["id"] for p in s["prizes"]] == [p["id"] for p in DEFAULT_PRIZES]


def test_unknown_event_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown event type"):
        reduce_event(initial_state(), "bogus", {})


def test_session_lifecycle_full_flow() -> None:
    s = _reduce(
        [
            ("session_started", {"subject": "Biochemistry", "start_time_ms": 1000}),
            ("session_paused", {"at_ms": 4000}),
            ("session_resumed", {"at_ms": 5000}),
            (
                "session_completed",
                {"id": "s1", "subject": "Biochemistry", "seconds": 1500, "ended_at_ms": 10000, "credits_earned": 25},
            ),
        ]
    )
    assert s["activeSession"] is None
    assert s["credits"] == 25
    assert len(s["sessions"]) == 1
    assert s["sessions"][0]["subject"] == "Biochemistry"


def test_session_paused_idempotent_when_already_paused() -> None:
    # Second pause should be a no-op (keeps original pauseStartedAt).
    s = _reduce(
        [
            ("session_started", {"subject": "Anatomy", "start_time_ms": 1000}),
            ("session_paused", {"at_ms": 2000}),
            ("session_paused", {"at_ms": 3000}),
        ]
    )
    assert s["activeSession"]["pauseStartedAt"] == 2000


def test_session_resumed_accumulates_paused_duration() -> None:
    s = _reduce(
        [
            ("session_started", {"subject": "Physio", "start_time_ms": 0}),
            ("session_paused", {"at_ms": 1000}),
            ("session_resumed", {"at_ms": 3500}),
            ("session_paused", {"at_ms": 5000}),
            ("session_resumed", {"at_ms": 6000}),
        ]
    )
    # 2.5s paused + 1s paused = 3.5s = 3500ms
    assert s["activeSession"]["pausedDuration"] == 3500


def test_session_cancelled_clears_active_without_logging() -> None:
    s = _reduce([("session_started", {"subject": "Anatomy", "start_time_ms": 0}), ("session_cancelled", {})])
    assert s["activeSession"] is None
    assert s["sessions"] == []


def test_session_edited_updates_fields_and_credits() -> None:
    s = _reduce(
        [
            ("session_completed", {"id": "s1", "subject": "A", "seconds": 600, "ended_at_ms": 1, "credits_earned": 10}),
            ("session_edited", {"id": "s1", "subject": "B", "seconds": 900, "credits_delta": 5}),
        ]
    )
    assert s["sessions"][0]["subject"] == "B"
    assert s["sessions"][0]["seconds"] == 900
    assert s["credits"] == 15


def test_session_deleted_removes_and_refunds() -> None:
    s = _reduce(
        [
            ("session_completed", {"id": "s1", "subject": "A", "seconds": 600, "ended_at_ms": 1, "credits_earned": 10}),
            ("session_deleted", {"id": "s1", "credits_refund": 10}),
        ]
    )
    assert s["sessions"] == []
    assert s["credits"] == 0


def test_credits_delta_clamps_at_zero() -> None:
    s = _reduce([("credits_delta", {"amount": -50})])
    assert s["credits"] == 0


def test_credits_to_tokens_swaps_one_for_one() -> None:
    s = _reduce([("credits_delta", {"amount": 100}), ("credits_to_tokens", {"amount": 30})])
    assert s["credits"] == 70
    assert s["tokens"] == 30


def test_roulette_spin_loss() -> None:
    s = _reduce(
        [
            ("credits_delta", {"amount": 100}),
            ("roulette_spin", {"bet_amount": 10, "bet_type": "red", "winning_number": 0, "payout": 0}),
        ]
    )
    assert s["credits"] == 90
    assert s["tokens"] == 0


def test_roulette_spin_win_winnings_become_tokens() -> None:
    # Single-number bet pays 36x gross. Bet returns to credits; the winnings
    # (35x) land in tokens — the casino can never mint credits.
    s = _reduce(
        [
            ("credits_delta", {"amount": 100}),
            ("roulette_spin", {"bet_amount": 10, "bet_type": "number", "winning_number": 7, "payout": 360}),
        ]
    )
    assert s["credits"] == 100
    assert s["tokens"] == 350


def test_slot_win_pays_winnings_in_tokens_principal_in_credits() -> None:
    s = _reduce(
        [
            ("credits_delta", {"amount": 100}),
            ("slot_spin", {"bet_amount": 5, "symbols": ["seven", "seven", "seven"], "payout": 250}),
            ("blackjack_hand", {"bet_amount": 20, "result": "lose", "payout": 0}),
        ]
    )
    # Slots: bet 5 returned, +245 tokens. Blackjack loss: -20 credits.
    assert s["credits"] == 80
    assert s["tokens"] == 245


def test_blackjack_push_is_a_true_no_op() -> None:
    # Push: payout == bet_amount. Credits and tokens both unchanged — the
    # principal refund cancels the bet exactly, no winnings accrue.
    s = _reduce(
        [("credits_delta", {"amount": 100}), ("blackjack_hand", {"bet_amount": 30, "result": "push", "payout": 30})]
    )
    assert s["credits"] == 100
    assert s["tokens"] == 0


def test_prize_redeemed_spends_tokens_and_logs() -> None:
    s = _reduce(
        [
            ("tokens_delta", {"amount": 100}),
            ("prize_redeemed", {"id": "log1", "name": "Coffee", "cost": 60, "at_ms": 123}),
        ]
    )
    assert s["tokens"] == 40
    assert len(s["prizeLog"]) == 1
    assert s["prizeLog"][0]["name"] == "Coffee"


def test_prize_redeemed_refuses_insufficient_tokens() -> None:
    with pytest.raises(ValueError, match="insufficient tokens"):
        reduce_event(initial_state(), "prize_redeemed", {"id": "x", "name": "y", "cost": 1, "at_ms": 0})


def test_prize_added_and_deleted() -> None:
    s = _reduce(
        [
            ("prize_added", {"id": "custom1", "name": "A pony", "cost": 999}),
            ("prize_deleted", {"id": "p1"}),  # Delete default "Anime episode break"
        ]
    )
    ids = [p["id"] for p in s["prizes"]]
    assert "custom1" in ids
    assert "p1" not in ids


def test_import_replaces_state() -> None:
    s = _reduce(
        [
            ("credits_delta", {"amount": 50}),
            ("import", {"state": {"credits": 999, "tokens": 10, "sessions": [], "prizeLog": []}}),
        ]
    )
    assert s["credits"] == 999
    assert s["tokens"] == 10


def test_reset_restores_initial_state() -> None:
    s = _reduce([("credits_delta", {"amount": 500}), ("tokens_delta", {"amount": 20}), ("reset", {})])
    assert s == initial_state()


def test_session_completed_rejects_negative_credits_earned() -> None:
    with pytest.raises(ValueError, match="invalid credits_earned"):
        reduce_event(
            initial_state(),
            "session_completed",
            {"id": "s1", "subject": "A", "seconds": 60, "ended_at_ms": 0, "credits_earned": -5},
        )


def test_credits_to_tokens_rejects_insufficient_credits() -> None:
    state = _reduce([("credits_delta", {"amount": 10})])
    with pytest.raises(ValueError, match="insufficient credits"):
        reduce_event(state, "credits_to_tokens", {"amount": 50})


def test_credits_to_tokens_rejects_negative_amount() -> None:
    with pytest.raises(ValueError, match="invalid credits_to_tokens amount"):
        reduce_event(initial_state(), "credits_to_tokens", {"amount": -5})


def test_gambling_rejects_bet_larger_than_credits() -> None:
    state = _reduce([("credits_delta", {"amount": 5})])
    for game in ("roulette_spin", "slot_spin", "blackjack_hand"):
        with pytest.raises(ValueError, match="insufficient credits"):
            reduce_event(state, game, {"bet_amount": 100, "payout": 0})


def test_gambling_rejects_negative_bet() -> None:
    with pytest.raises(ValueError, match="invalid bet amount"):
        reduce_event(initial_state(), "roulette_spin", {"bet_amount": -1, "payout": 0})


def test_gambling_rejects_negative_payout() -> None:
    state = _reduce([("credits_delta", {"amount": 100})])
    with pytest.raises(ValueError, match="invalid payout"):
        reduce_event(state, "roulette_spin", {"bet_amount": 10, "payout": -5})


def test_reducer_is_pure_does_not_mutate_input() -> None:
    before = initial_state()
    snapshot = {**before, "prizes": list(before["prizes"])}
    reduce_event(before, "credits_delta", {"amount": 42})
    assert before == snapshot


if __name__ == "__main__":
    pytest_bazel.main()
