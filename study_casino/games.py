"""Server-side casino game rules and randomness helpers."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from typing import get_args

from pydantic import TypeAdapter

from study_casino.actions import BlackjackHandStateResult, BlackjackSettlement
from study_casino.events import BlackjackSettlementOutcome, Card, RouletteOutcome, SlotsOutcome
from study_casino.models import BlackjackOutcomeKind, HandStatus, RouletteBetType, SlotsPayoutKind
from study_casino.rng import AuditedRandom

RNG_VERSION = "server-hmac-sha256-v1"
RULES_VERSION = "server-rules-v1"


@dataclass(frozen=True)
class SlotSymbol:
    id: str
    glyph: str
    weight: int
    payout: int


@dataclass(frozen=True)
class GameRules:
    # European roulette pockets clockwise from zero. Stored outcomes include the index.
    wheel: list[int]
    red: frozenset[int]
    roulette_multipliers: dict[RouletteBetType, int]
    slot_symbols: list[SlotSymbol]
    slot_pair_multiplier: float


RULES = TypeAdapter(GameRules).validate_json((files("study_casino") / "game_rules.json").read_bytes())
SLOT_WEIGHTS = [symbol.weight for symbol in RULES.slot_symbols]

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
    return "red" if n in RULES.red else "black"


def spin_slots(wager: int, rng: AuditedRandom) -> GameSettlement[SlotsOutcome]:
    picks = [
        rng.weighted_choice(RULES.slot_symbols, weights=SLOT_WEIGHTS, purpose=f"slots.reel.{i}", item_id=lambda s: s.id)
        for i in range(3)
    ]
    a, b, c = picks
    payout_kind: SlotsPayoutKind
    if a.id == b.id == c.id:
        payout = wager * a.payout
        label = f"Triple {a.glyph} · {a.payout}x"
        payout_kind = "triple"
    elif len({a.id, b.id, c.id}) == 2:
        payout = int(wager * RULES.slot_pair_multiplier)
        label = f"Pair · {RULES.slot_pair_multiplier:g}x"
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
    picked_idx = rng.randbelow(
        len(RULES.wheel), purpose="roulette.wheel_index", parameters={"wheel_size": len(RULES.wheel)}
    )
    picked = RULES.wheel[picked_idx]
    won = _roulette_wins(picked, bet_type, bet_number)
    mult = 0 if picked == 0 and bet_type != "number" else RULES.roulette_multipliers[bet_type]
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
        player_value=hand_value(player),
        dealer_value=hand_value(dealer) if done else hand_value(dealer[:1]),
        settlement=BlackjackSettlement(**settlement.outcome.model_dump(), payout_tokens=settlement.payout_tokens)
        if settlement
        else None,
    )


def theoretical_bucket_rtp() -> dict[tuple[str, str], tuple[float, float]]:
    """Closed-form (win_probability, rtp) per (game, bucket_key).

    Derived from the same constants the live RNG uses (`RULES.wheel`, `RULES.red`,
    `RULES.slot_symbols` weights and the slot triple/pair payout structure), so
    the casino stats page can show theoretical-vs-empirical side by side.
    Blackjack is omitted — no clean closed form, only the simulator knows.
    """
    out: dict[tuple[str, str], tuple[float, float]] = {}

    # Roulette: bet against the 37-pocket wheel.
    n_pockets = len(RULES.wheel)
    for bet_type in get_args(RouletteBetType):
        bet_number = 0 if bet_type == "number" else None
        wins = sum(_roulette_wins(num, bet_type, bet_number) for num in RULES.wheel)
        p_win = wins / n_pockets
        rtp = p_win * RULES.roulette_multipliers[bet_type]
        out[("roulette", bet_type)] = (p_win, rtp)

    total_weight = sum(sym.weight for sym in RULES.slot_symbols)
    p_triple = 0.0
    rtp_triple = 0.0
    p_pair = 0.0
    for symbol in RULES.slot_symbols:
        p = symbol.weight / total_weight
        triple = p**3
        p_triple += triple
        rtp_triple += triple * symbol.payout
        # The other symbol can occupy any one of the three reels.
        p_pair += 3 * p**2 * (1 - p)
    # Report the nominal multiplier before whole-token rounding.
    rtp_pair = p_pair * RULES.slot_pair_multiplier
    out[("slots", "triple")] = (p_triple, rtp_triple)
    out[("slots", "pair")] = (p_pair, rtp_pair)
    out[("slots", "none")] = (max(0.0, 1.0 - p_triple - p_pair), 0.0)

    return out


def _roulette_wins(num: int, bet_type: RouletteBetType, bet_number: int | None) -> bool:
    if num == 0 and bet_type != "number":
        return False
    match bet_type:
        case "red":
            return num in RULES.red
        case "black":
            return num not in RULES.red
        case "odd":
            return num % 2 == 1
        case "even":
            return num % 2 == 0
        case "low":
            return 1 <= num <= 18
        case "high":
            return 19 <= num <= 36
        case "dozen1":
            return 1 <= num <= 12
        case "dozen2":
            return 13 <= num <= 24
        case "dozen3":
            return 25 <= num <= 36
        case "number":
            return num == bet_number
