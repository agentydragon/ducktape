"""Pydantic schemas for casino audit event reads.

Both `client_reported` (pre-2026-05-07 cutover) and `server_resolved` rows live
in `game_events`; the corresponding `legacy_client_sync` and `server_action`
rows in `ledger_events` are likewise historical. The source vocabularies
(defined in models.py next to the columns that carry them) preserve those
values so old rows still deserialize cleanly.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from x.study_casino.models import (
    BlackjackOutcomeKind,
    Game,
    GameEventRow,
    GameEventSource,
    GameEventType,
    LedgerEventRow,
    LedgerEventSource,
    RouletteBetType,
    SlotsPayoutKind,
)


class Card(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: str
    suit: str


class RouletteOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bet_type: RouletteBetType
    bet_number: int | None
    multiplier: int
    result_color: str
    result_number: int
    # Server-resolved rows include the wheel index; pre-2026-05-07
    # `client_reported` rows predate that field.
    result_index: int | None = None
    won: bool


class SlotsOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str]
    glyphs: list[str]
    label: str
    payout_kind: SlotsPayoutKind


class BlackjackSettlementOutcome(BaseModel):
    """Settlement fields games.py produces — shared by the in-flight action
    response (`actions.BlackjackSettlement`) and the persisted event below."""

    model_config = ConfigDict(extra="forbid")

    outcome: BlackjackOutcomeKind
    text: str
    player_cards: list[Card]
    dealer_cards: list[Card]
    player_value: int
    dealer_value: int
    player_blackjack: bool
    dealer_blackjack: bool


class BlackjackOutcome(BlackjackSettlementOutcome):
    initial_wager: int
    doubled: bool


GameOutcome = RouletteOutcome | SlotsOutcome | BlackjackOutcome

_OUTCOME_BY_GAME: dict[Game, type[RouletteOutcome] | type[SlotsOutcome] | type[BlackjackOutcome]] = {
    "roulette": RouletteOutcome,
    "slots": SlotsOutcome,
    "blackjack": BlackjackOutcome,
}


class GameEventMutation(BaseModel):
    """Per-action fields a mutator emits when it wants to write a
    `game_events` row. `run_server_action` adds the persistence-side fields
    (id, occurred_at_ms, credits/tokens before/after, etc.) at commit time.
    """

    model_config = ConfigDict(extra="forbid")

    game: Game
    wager_credits: int
    payout_tokens: int
    outcome: GameOutcome


class GameEventRead(BaseModel):
    """Balance snapshots (`*_millis`) are integer millicredits;
    `wager_credits` is the wager in whole credits."""

    model_config = ConfigDict(extra="forbid")

    id: int
    client_event_id: str
    server_at_ms: int
    occurred_at_ms: int
    game: Game
    event_type: GameEventType
    source: GameEventSource
    wager_credits: int
    payout_tokens: int
    credits_before_millis: int
    credits_after_millis: int
    tokens_before: int
    tokens_after: int
    server_credits_millis: int
    server_tokens: int
    rules_version: str | None = None
    rng_version: str | None = None
    outcome: GameOutcome


def game_event_from_row(row: GameEventRow) -> GameEventRead:
    outcome = _OUTCOME_BY_GAME[row.game].model_validate_json(row.outcome_json)
    return GameEventRead(
        id=row.id,
        client_event_id=row.client_event_id,
        server_at_ms=row.server_at_ms,
        occurred_at_ms=row.occurred_at_ms,
        game=row.game,
        event_type=row.event_type,
        source=row.source,
        wager_credits=row.wager_credits,
        payout_tokens=row.payout_tokens,
        credits_before_millis=row.credits_before,
        credits_after_millis=row.credits_after,
        tokens_before=row.tokens_before,
        tokens_after=row.tokens_after,
        server_credits_millis=row.server_credits,
        server_tokens=row.server_tokens,
        rules_version=row.rules_version,
        rng_version=row.rng_version,
        outcome=outcome,
    )


class LedgerEventRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    client_action_id: str
    server_at_ms: int
    action_type: str
    source: LedgerEventSource
    rules_version: str
    rng_version: str | None = None
    credits_before_millis: int
    credits_after_millis: int
    tokens_before: int
    tokens_after: int
    details: dict[str, Any]
    result: dict[str, Any]


def ledger_event_from_row(row: LedgerEventRow) -> LedgerEventRead:
    return LedgerEventRead(
        id=row.id,
        client_action_id=row.client_action_id,
        server_at_ms=row.server_at_ms,
        action_type=row.action_type,
        source=row.source,
        rules_version=row.rules_version,
        rng_version=row.rng_version,
        credits_before_millis=row.credits_before,
        credits_after_millis=row.credits_after,
        tokens_before=row.tokens_before,
        tokens_after=row.tokens_after,
        details=json.loads(row.details_json),
        result=json.loads(row.result_json),
    )
