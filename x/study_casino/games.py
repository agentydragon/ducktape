"""Server-side casino game rules and randomness helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

from pydantic import TypeAdapter

from x.study_casino.actions import BlackjackHandStateResult, BlackjackSettlement
from x.study_casino.events import BlackjackSettlementOutcome, Card, RouletteOutcome, SlotsOutcome
from x.study_casino.models import BlackjackOutcomeKind, HandStatus, RouletteBetType, SlotsPayoutKind
from x.study_casino.rng import AuditedRandom

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


@dataclass(frozen=True)
class SlotSymbol:
    id: str
    glyph: str
    color: str
    weight: int
    payout: int


SLOT_SYMBOLS = [
    SlotSymbol(id="seven", glyph="7", color="#e8b84a", weight=1, payout=50),
    SlotSymbol(id="star", glyph="★", color="#e8b84a", weight=3, payout=20),
    SlotSymbol(id="diamond", glyph="◆", color="#6fc4e8", weight=5, payout=10),
    SlotSymbol(id="spade", glyph="♠", color="#f5e8c7", weight=9, payout=5),
    SlotSymbol(id="club", glyph="♣", color="#f5e8c7", weight=14, payout=3),
]
SLOT_WEIGHTS = [symbol.weight for symbol in SLOT_SYMBOLS]

CARD_SUITS = ["♠", "♥", "♦", "♣"]
CARD_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
BLACKJACK_DECKS = 4

# (De)serializer for the card-list JSON persisted on `blackjack_hands` rows.
_CARD_LIST = TypeAdapter(list[Card])


def dump_cards(cards: list[Card]) -> str:
    return _CARD_LIST.dump_json(cards).decode()


def load_cards(raw: str) -> list[Card]:
    return _CARD_LIST.validate_json(raw)


@dataclass(frozen=True)
class GameSettlement[OutcomeT]:
    payout_tokens: int
    outcome: OutcomeT


def num_color(n: int) -> str:
    if n == 0:
        return "green"
    return "red" if n in RED else "black"


def spin_slots(wager: int, rng: AuditedRandom) -> GameSettlement[SlotsOutcome]:
    picks = [
        rng.weighted_choice(SLOT_SYMBOLS, weights=SLOT_WEIGHTS, purpose=f"slots.reel.{i}", item_id=lambda s: s.id)
        for i in range(3)
    ]
    a, b, c = picks
    payout_kind: SlotsPayoutKind
    if a.id == b.id == c.id:
        payout = wager * a.payout
        label = f"Triple {a.glyph} · {a.payout}x"
        payout_kind = "triple"
    elif len({a.id, b.id, c.id}) == 2:
        payout = int(wager * 1.5)
        label = "Pair · 1.5x"
        payout_kind = "pair"
    else:
        payout = 0
        label = "No match"
        payout_kind = "none"
    return GameSettlement(
        payout_tokens=payout,
        outcome=SlotsOutcome(
            symbols=[p.id for p in picks], glyphs=[p.glyph for p in picks], label=label, payout_kind=payout_kind
        ),
    )


def spin_roulette(
    wager: int, bet_type: RouletteBetType, bet_number: int | None, rng: AuditedRandom
) -> GameSettlement[RouletteOutcome]:
    if bet_type == "number" and bet_number is None:
        raise ValueError("number bets require bet_number")
    picked_idx = rng.randbelow(len(WHEEL), purpose="roulette.wheel_index", parameters={"wheel_size": len(WHEEL)})
    picked = WHEEL[picked_idx]
    won, mult = _roulette_win(picked, bet_type, bet_number)
    return GameSettlement(
        payout_tokens=wager * mult if won else 0,
        outcome=RouletteOutcome(
            bet_type=bet_type,
            bet_number=bet_number if bet_type == "number" else None,
            result_number=picked,
            result_index=picked_idx,
            result_color=num_color(picked),
            won=won,
            multiplier=mult,
        ),
    )


def make_shoe(rng: AuditedRandom, decks: int = BLACKJACK_DECKS) -> list[Card]:
    cards = [Card(suit=s, rank=r) for _ in range(decks) for s in CARD_SUITS for r in CARD_RANKS]
    rng.shuffle(cards, purpose="blackjack.shoe.shuffle", parameters={"decks": decks})
    return cards


def draw_cards(shoe: list[Card], count: int) -> tuple[list[Card], list[Card]]:
    return shoe[-count:], shoe[:-count]


def hand_value(cards: list[Card]) -> int:
    total = 0
    aces = 0
    for card in cards:
        if card.rank == "A":
            total += 11
            aces += 1
        elif card.rank in {"J", "Q", "K"}:
            total += 10
        else:
            total += int(card.rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(cards: list[Card]) -> bool:
    return len(cards) == 2 and hand_value(cards) == 21


def dealer_play(dealer: list[Card], shoe: list[Card]) -> tuple[list[Card], list[Card]]:
    while hand_value(dealer) < 17:
        drawn, shoe = draw_cards(shoe, 1)
        dealer = [*dealer, *drawn]
    return dealer, shoe


def settle_blackjack(
    player: list[Card], dealer: list[Card], current_wager: int
) -> GameSettlement[BlackjackSettlementOutcome]:
    pv = hand_value(player)
    dv = hand_value(dealer)
    p_bj = is_blackjack(player)
    d_bj = is_blackjack(dealer)
    outcome: BlackjackOutcomeKind
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
        outcome=BlackjackSettlementOutcome(
            outcome=outcome,
            text=text,
            player_cards=player,
            dealer_cards=dealer,
            player_value=pv,
            dealer_value=dv,
            player_blackjack=p_bj,
            dealer_blackjack=d_bj,
        ),
    )


def public_blackjack_state(
    *,
    hand_id: str,
    status: HandStatus,
    player: list[Card],
    dealer: list[Card],
    current_wager: int,
    settlement: GameSettlement[BlackjackSettlementOutcome] | None = None,
) -> BlackjackHandStateResult:
    done = status == "done"
    return BlackjackHandStateResult(
        hand_id=hand_id,
        phase=status,
        current_wager=current_wager,
        player_cards=player,
        dealer_cards=dealer if done else dealer[:1],
        hole_hidden=not done and len(dealer) > 1,
        player_value=hand_value(player),
        dealer_value=hand_value(dealer) if done else hand_value(dealer[:1]),
        settlement=BlackjackSettlement(**settlement.outcome.model_dump(), payout_tokens=settlement.payout_tokens)
        if settlement
        else None,
    )


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
    for bet_type in get_args(RouletteBetType):
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
    total_weight = sum(sym.weight for sym in SLOT_SYMBOLS)
    p_triple = 0.0
    rtp_triple = 0.0
    p_pair = 0.0
    rtp_pair = 0.0
    for i, sym in enumerate(SLOT_SYMBOLS):
        p_i = sym.weight / total_weight
        # P(triple of this symbol) = p_i^3, payout = wager * symbol.payout, so RTP contribution = p_i^3 * payout
        p_triple += p_i**3
        rtp_triple += (p_i**3) * sym.payout
        # P(exactly two of this symbol over the three picks): choose 2 of 3 positions = 3 * p_i^2 * (1 - p_i),
        # then the third pick must be any *different* symbol — already captured by (1 - p_i).
        # Payout = floor(wager * 1.5) ≈ 1.5x.
        for j, sym_j in enumerate(SLOT_SYMBOLS):
            if i == j:
                continue
            p_j = sym_j.weight / total_weight
            # Three ordered patterns where exactly two slots are i and the third is j:
            # (i,i,j), (i,j,i), (j,i,i).
            p_pair += 3 * (p_i**2) * p_j
    rtp_pair = p_pair * 1.5
    out[("slots", "triple")] = (p_triple, rtp_triple)
    out[("slots", "pair")] = (p_pair, rtp_pair)
    out[("slots", "none")] = (max(0.0, 1.0 - p_triple - p_pair), 0.0)

    return out


def _roulette_win(num: int, bet_type: RouletteBetType, bet_number: int | None) -> tuple[bool, int]:
    if num == 0 and bet_type != "number":
        return False, 0
    match bet_type:
        case "red":
            return num in RED, 2
        case "black":
            return num != 0 and num not in RED, 2
        case "odd":
            return num % 2 == 1, 2
        case "even":
            return num != 0 and num % 2 == 0, 2
        case "low":
            return 1 <= num <= 18, 2
        case "high":
            return 19 <= num <= 36, 2
        case "dozen1":
            return 1 <= num <= 12, 3
        case "dozen2":
            return 13 <= num <= 24, 3
        case "dozen3":
            return 25 <= num <= 36, 3
        case "number":
            return num == bet_number, 36
