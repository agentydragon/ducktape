"""Server-side casino game rules and randomness helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from x.auragon_study_casino.rng import AuditedRandom

RNG_VERSION = "server-hmac-sha256-v1"
RULES_VERSION = "server-rules-v1"

WHEEL = [
    0,
    32,
    15,
    19,
    4,
    21,
    2,
    25,
    17,
    34,
    6,
    27,
    13,
    36,
    11,
    30,
    8,
    23,
    10,
    5,
    24,
    16,
    33,
    1,
    20,
    14,
    31,
    9,
    22,
    18,
    29,
    7,
    28,
    12,
    35,
    3,
    26,
]
RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}

SLOT_SYMBOLS: list[dict[str, Any]] = [
    {"id": "seven", "glyph": "7", "color": "#e8b84a", "weight": 1, "payout": 50},
    {"id": "star", "glyph": "★", "color": "#e8b84a", "weight": 3, "payout": 20},
    {"id": "diamond", "glyph": "◆", "color": "#6fc4e8", "weight": 5, "payout": 10},
    {"id": "spade", "glyph": "♠", "color": "#f5e8c7", "weight": 9, "payout": 5},
    {"id": "club", "glyph": "♣", "color": "#f5e8c7", "weight": 14, "payout": 3},
]
SLOT_WEIGHTS = [int(symbol["weight"]) for symbol in SLOT_SYMBOLS]

CARD_SUITS = ["♠", "♥", "♦", "♣"]
CARD_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
BLACKJACK_DECKS = 4


@dataclass(frozen=True)
class GameSettlement:
    payout_tokens: int
    outcome: dict


def num_color(n: int) -> str:
    if n == 0:
        return "green"
    return "red" if n in RED else "black"


def spin_slots(wager: int, rng: AuditedRandom) -> GameSettlement:
    picks = [
        rng.weighted_choice(SLOT_SYMBOLS, weights=SLOT_WEIGHTS, purpose=f"slots.reel.{i}", item_id_key="id")
        for i in range(3)
    ]
    a, b, c = picks
    if a["id"] == b["id"] == c["id"]:
        payout = wager * int(a["payout"])
        label = f"Triple {a['glyph']} · {a['payout']}x"
        payout_kind = "triple"
    elif a["id"] == b["id"] or b["id"] == c["id"] or a["id"] == c["id"]:
        payout = int(wager * 1.5)
        label = "Pair · 1.5x"
        payout_kind = "pair"
    else:
        payout = 0
        label = "No match"
        payout_kind = "none"
    return GameSettlement(
        payout_tokens=payout,
        outcome={
            "symbols": [p["id"] for p in picks],
            "glyphs": [p["glyph"] for p in picks],
            "label": label,
            "payout_kind": payout_kind,
        },
    )


def spin_roulette(wager: int, bet_type: str, bet_number: int | None, rng: AuditedRandom) -> GameSettlement:
    if bet_type == "number" and bet_number is None:
        raise ValueError("number bets require bet_number")
    picked_idx = rng.randbelow(len(WHEEL), purpose="roulette.wheel_index", parameters={"wheel_size": len(WHEEL)})
    picked = WHEEL[picked_idx]
    won, mult = _roulette_win(picked, bet_type, bet_number)
    return GameSettlement(
        payout_tokens=wager * mult if won else 0,
        outcome={
            "bet_type": bet_type,
            "bet_number": bet_number if bet_type == "number" else None,
            "result_number": picked,
            "result_index": picked_idx,
            "result_color": num_color(picked),
            "won": won,
            "multiplier": mult,
        },
    )


def make_shoe(rng: AuditedRandom, decks: int = BLACKJACK_DECKS) -> list[dict[str, str]]:
    cards = [{"suit": s, "rank": r} for _ in range(decks) for s in CARD_SUITS for r in CARD_RANKS]
    rng.shuffle(cards, purpose="blackjack.shoe.shuffle", parameters={"decks": decks})
    return cards


def draw_cards(shoe: list[dict[str, str]], count: int) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    return shoe[-count:], shoe[:-count]


def hand_value(cards: list[dict[str, str]]) -> int:
    total = 0
    aces = 0
    for card in cards:
        rank = card["rank"]
        if rank == "A":
            total += 11
            aces += 1
        elif rank in {"J", "Q", "K"}:
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(cards: list[dict[str, str]]) -> bool:
    return len(cards) == 2 and hand_value(cards) == 21


def dealer_play(
    dealer: list[dict[str, str]], shoe: list[dict[str, str]]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    while hand_value(dealer) < 17:
        drawn, shoe = draw_cards(shoe, 1)
        dealer = [*dealer, *drawn]
    return dealer, shoe


def settle_blackjack(player: list[dict[str, str]], dealer: list[dict[str, str]], current_wager: int) -> GameSettlement:
    pv = hand_value(player)
    dv = hand_value(dealer)
    p_bj = is_blackjack(player)
    d_bj = is_blackjack(dealer)
    if pv > 21:
        outcome = "bust"
        payout = 0
        text = "Bust. Dealer takes it."
    elif p_bj and not d_bj:
        outcome = "blackjack"
        # 3:2 in integer credits — round half up so wager=1 pays 3 (not 2 from truncating int(2.5)).
        payout = (current_wager * 5 + 1) // 2
        text = "Blackjack! Pays 3:2."
    elif p_bj and d_bj:
        outcome = "push"
        payout = current_wager
        text = "Both blackjack. Push."
    elif not p_bj and d_bj:
        outcome = "lose"
        payout = 0
        text = "Dealer blackjack."
    elif dv > 21:
        outcome = "dealerBust"
        payout = current_wager * 2
        text = "Dealer busts. You win."
    elif pv > dv:
        outcome = "win"
        payout = current_wager * 2
        text = "You win."
    elif pv == dv:
        outcome = "push"
        payout = current_wager
        text = "Push."
    else:
        outcome = "lose"
        payout = 0
        text = "Dealer wins."
    return GameSettlement(
        payout_tokens=payout,
        outcome={
            "outcome": outcome,
            "text": text,
            "player_cards": player,
            "dealer_cards": dealer,
            "player_value": pv,
            "dealer_value": dv,
            "player_blackjack": p_bj,
            "dealer_blackjack": d_bj,
        },
    )


def public_blackjack_state(
    *,
    hand_id: str,
    status: str,
    player: list[dict[str, str]],
    dealer: list[dict[str, str]],
    current_wager: int,
    settlement: GameSettlement | None = None,
) -> dict:
    done = status == "done"
    return {
        "hand_id": hand_id,
        "phase": "done" if done else "playing",
        "current_wager": current_wager,
        "player_cards": player,
        "dealer_cards": dealer if done else dealer[:1],
        "hole_hidden": not done and len(dealer) > 1,
        "player_value": hand_value(player),
        "dealer_value": hand_value(dealer) if done else hand_value(dealer[:1]),
        "settlement": settlement.outcome | {"payout_tokens": settlement.payout_tokens} if settlement else None,
    }


def theoretical_bucket_rtp() -> dict[tuple[str, str], tuple[float, float]]:
    """Closed-form (win_probability, rtp) per (game, bucket_key).

    Derived from the same constants the live RNG uses (`WHEEL`, `RED`,
    `SLOT_SYMBOLS` weights and the slot triple/pair payout structure), so
    the casino stats page can show theoretical-vs-empirical side by side.
    Blackjack is omitted — no clean closed form, only the simulator knows.
    """
    out: dict[tuple[str, str], tuple[float, float]] = {}

    # Roulette: bet against the 37-pocket wheel.
    n_pockets = len(WHEEL)
    for bet_type in ("red", "black", "odd", "even", "low", "high", "dozen1", "dozen2", "dozen3", "number"):
        bet_number = 0 if bet_type == "number" else None
        wins = sum(1 for num in WHEEL if _roulette_win(num, bet_type, bet_number)[0])
        # All winning multipliers are constant per bet_type — pull mult from any winning pocket.
        mult = next(
            (
                _roulette_win(num, bet_type, bet_number)[1]
                for num in WHEEL
                if _roulette_win(num, bet_type, bet_number)[0]
            ),
            0,
        )
        p_win = wins / n_pockets
        rtp = p_win * mult
        out[("roulette", bet_type)] = (p_win, rtp)

    # Slots: enumerate the joint distribution of three independent weighted picks.
    total_weight = sum(int(sym["weight"]) for sym in SLOT_SYMBOLS)
    p_triple = 0.0
    rtp_triple = 0.0
    p_pair = 0.0
    rtp_pair = 0.0
    for i, sym in enumerate(SLOT_SYMBOLS):
        p_i = int(sym["weight"]) / total_weight
        # P(triple of this symbol) = p_i^3, payout = wager * symbol.payout, so RTP contribution = p_i^3 * payout
        p_triple += p_i**3
        rtp_triple += (p_i**3) * int(sym["payout"])
        # P(exactly two of this symbol over the three picks): choose 2 of 3 positions = 3 * p_i^2 * (1 - p_i),
        # then the third pick must be any *different* symbol — already captured by (1 - p_i).
        # Payout = floor(wager * 1.5) ≈ 1.5x.
        for j, _sym_j in enumerate(SLOT_SYMBOLS):
            if i == j:
                continue
            p_j = int(_sym_j["weight"]) / total_weight
            # Three ordered patterns where exactly two slots are i and the third is j:
            # (i,i,j), (i,j,i), (j,i,i).
            p_pair += 3 * (p_i**2) * p_j
    rtp_pair = p_pair * 1.5
    out[("slots", "triple")] = (p_triple, rtp_triple)
    out[("slots", "pair")] = (p_pair, rtp_pair)
    out[("slots", "none")] = (max(0.0, 1.0 - p_triple - p_pair), 0.0)

    return out


def _roulette_win(num: int, bet_type: str, bet_number: int | None) -> tuple[bool, int]:
    if num == 0 and bet_type != "number":
        return False, 0
    if bet_type == "red":
        return num in RED, 2
    if bet_type == "black":
        return num != 0 and num not in RED, 2
    if bet_type == "odd":
        return num % 2 == 1, 2
    if bet_type == "even":
        return num != 0 and num % 2 == 0, 2
    if bet_type == "low":
        return 1 <= num <= 18, 2
    if bet_type == "high":
        return 19 <= num <= 36, 2
    if bet_type == "dozen1":
        return 1 <= num <= 12, 3
    if bet_type == "dozen2":
        return 13 <= num <= 24, 3
    if bet_type == "dozen3":
        return 25 <= num <= 36, 3
    if bet_type == "number":
        return num == bet_number, 36
    return False, 0
