"""Pure reducer: fold events into state.

The frontend sees state shaped like:

    {
      credits: int,
      tokens: int,
      sessions: [{id, subject, seconds, endedAt}, ...],          # completed
      activeSession: {subject, startTime, paused, pausedDuration, pauseStartedAt} | None,
      prizes: [{id, name, cost}, ...],
      prizeLog: [{id, name, cost, at}, ...],
    }

This reducer is the source of truth for supported event types and the
payload fields they expect: it's a big `match` on event type, with one arm
per event. Unknown types and invariant-violating payloads (negative bets,
spending more than you have, etc.) raise ValueError, which bubbles up to
a 400 on POST.

Credits/tokens are derived from events (the deltas come from session
completion, gambling spins, prize redemption, etc.) — there is no stored
"credits" state outside the snapshot cache.
"""

from __future__ import annotations

import copy
from typing import Any

DEFAULT_PRIZES: list[dict[str, Any]] = [
    {"id": "p1", "name": "Anime episode break", "cost": 30},
    {"id": "p2", "name": "Nice coffee shop trip", "cost": 60},
    {"id": "p3", "name": "Takeout night", "cost": 120},
    {"id": "p4", "name": "Nice dinner out with Rai", "cost": 240},
    {"id": "p5", "name": "Buy a new game", "cost": 600},
    {"id": "p6", "name": "Weekend getaway", "cost": 1800},
]


def initial_state() -> dict[str, Any]:
    return {
        "credits": 0,
        "tokens": 0,
        "sessions": [],
        "activeSession": None,
        "prizes": copy.deepcopy(DEFAULT_PRIZES),
        "prizeLog": [],
    }


def _apply_wager(state: dict[str, Any], bet_amount: int, payout: int) -> None:
    """Shared bet+payout validation for roulette/slots/blackjack.

    `payout` is gross (principal + winnings, in old casino convention) — 0 on
    a loss, equal to `bet_amount` on a blackjack push, and `bet_amount * mult`
    on a true win. The reducer splits it so the principal portion refunds
    `credits` (so a push is a true no-op) while only the winnings above the
    bet land in `tokens`. Net effect: gambling can mint tokens but can never
    mint credits — winnings are one-way and can only be spent on prizes.

    Raises ValueError on invariant violations so a malformed/hostile event
    can't bet more credits than the user has.
    """
    if bet_amount < 0:
        raise ValueError(f"invalid bet amount: {bet_amount}")
    if payout < 0:
        raise ValueError(f"invalid payout: {payout}")
    if bet_amount > state["credits"]:
        raise ValueError(f"insufficient credits: have {state['credits']}, need {bet_amount}")
    refund = min(payout, bet_amount)
    state["credits"] += refund - bet_amount
    state["tokens"] += payout - refund


def reduce_event(state: dict[str, Any], event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one event to state, returning the new state. Pure."""
    s = copy.deepcopy(state)
    match event_type:
        case "session_started":
            s["activeSession"] = {
                "subject": payload["subject"],
                "startTime": payload["start_time_ms"],
                "paused": False,
                "pausedDuration": 0,
                "pauseStartedAt": None,
            }
        case "session_paused":
            active = s.get("activeSession")
            if active and not active["paused"]:
                active["paused"] = True
                active["pauseStartedAt"] = payload["at_ms"]
        case "session_resumed":
            active = s.get("activeSession")
            if active and active["paused"]:
                pause_started = active.get("pauseStartedAt") or payload["at_ms"]
                active["pausedDuration"] += max(0, payload["at_ms"] - pause_started)
                active["paused"] = False
                active["pauseStartedAt"] = None
        case "session_completed":
            credits_earned = payload.get("credits_earned", 0)
            if credits_earned < 0:
                raise ValueError(f"invalid credits_earned: {credits_earned}")
            s["sessions"] = [
                {
                    "id": payload["id"],
                    "subject": payload["subject"],
                    "seconds": payload["seconds"],
                    "endedAt": payload["ended_at_ms"],
                },
                *s["sessions"],
            ]
            s["credits"] += credits_earned
            s["activeSession"] = None
        case "session_cancelled":
            s["activeSession"] = None
        case "session_edited":
            # Edits the seconds/subject of a past session; credits_delta is the
            # minute-count change, applied by the client (consistent with the
            # frontend's edit handler).
            for sess in s["sessions"]:
                if sess["id"] == payload["id"]:
                    sess["subject"] = payload.get("subject", sess["subject"])
                    sess["seconds"] = payload.get("seconds", sess["seconds"])
                    break
            s["credits"] = max(0, s["credits"] + payload.get("credits_delta", 0))
        case "session_deleted":
            for sess in s["sessions"]:
                if sess["id"] == payload["id"]:
                    s["credits"] = max(0, s["credits"] - payload.get("credits_refund", 0))
                    break
            s["sessions"] = [sess for sess in s["sessions"] if sess["id"] != payload["id"]]
        case "credits_delta":
            s["credits"] = max(0, s["credits"] + payload["amount"])
        case "tokens_delta":
            s["tokens"] = max(0, s["tokens"] + payload["amount"])
        case "credits_to_tokens":
            amount = payload["amount"]
            if amount < 0:
                raise ValueError(f"invalid credits_to_tokens amount: {amount}")
            if amount > s["credits"]:
                raise ValueError(f"insufficient credits: have {s['credits']}, need {amount}")
            s["credits"] -= amount
            s["tokens"] += amount
        case "roulette_spin" | "slot_spin" | "blackjack_hand":
            _apply_wager(s, payload["bet_amount"], payload.get("payout", 0))
        case "prize_redeemed":
            if s["tokens"] < payload["cost"]:
                raise ValueError(f"insufficient tokens: have {s['tokens']}, need {payload['cost']}")
            s["tokens"] -= payload["cost"]
            s["prizeLog"] = [
                {"id": payload["id"], "name": payload["name"], "cost": payload["cost"], "at": payload["at_ms"]},
                *s["prizeLog"],
            ]
        case "prize_added":
            s["prizes"] = [*s["prizes"], {"id": payload["id"], "name": payload["name"], "cost": payload["cost"]}]
        case "prize_deleted":
            s["prizes"] = [p for p in s["prizes"] if p["id"] != payload["id"]]
        case "import":
            # One-shot replacement used by the legacy-blob migration and the
            # explicit "import from backup" action.
            data = payload["state"]
            s["credits"] = int(data.get("credits", 0))
            s["tokens"] = int(data.get("tokens", 0))
            s["sessions"] = list(data.get("sessions", []))
            s["activeSession"] = data.get("activeSession")
            prizes = data.get("prizes")
            s["prizes"] = list(prizes) if prizes else copy.deepcopy(DEFAULT_PRIZES)
            s["prizeLog"] = list(data.get("prizeLog", []))
        case "reset":
            s = initial_state()
        case _:
            raise ValueError(f"unknown event type: {event_type!r}")
    return s
