"""Pydantic wire models for server-authoritative Study Casino actions.

State sync is no longer CRDT-based — clients refetch `GET /state` after each
successful action (the server pushes a `state_changed` ping over `/ws` so
other tabs of the same user know to refetch). Every action carries a
`client_action_id` that the server uses as the idempotency key on
`ledger_events`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from x.study_casino.events import (
    BlackjackSettlementOutcome,
    Card,
    GameEventRead,
    LedgerEventRead,
    RouletteOutcome,
    SlotsOutcome,
)
from x.study_casino.models import HandStatus, RouletteBetType

_ACTION_ID_PATTERN = r"^[a-zA-Z0-9._:@-]+$"


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_action_id: str = Field(min_length=1, max_length=128, pattern=_ACTION_ID_PATTERN)


# ── Per-endpoint result shapes (the `result` field of `ActionResponse`) ──────
#
# Every action endpoint puts a different payload into `ActionResponse.result`.
# Each variant has a distinct required-field set, so the union below
# discriminates structurally — Pydantic + `extra="forbid"` rejects a wrong
# variant during validation. Order in `ActionResult` matters: Pydantic tries
# variants left-to-right and accepts the first that fully validates.


class SessionCompleteResult(BaseModel):
    """Credit amounts are integer millicredits; `credits_earned_millis` is the
    full award (base + daily bonus, streak-multiplied)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    seconds: int
    credits_earned_millis: int
    daily_bonus_millis: int = Field(description="Streak-multiplied first-5-minutes daily bonus, 0 if not earned.")
    streak_days: int
    streak_bonus_percent: int = Field(description="Streak bonus applied to this award: 1%/day, capped at 100.")


class SessionAddPastResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    credits_earned_millis: int


class SessionCreditsDeltaResult(BaseModel):
    """`/actions/session/{edit,delete}` — `credits_delta_millis` may be negative."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    credits_delta_millis: int


class ConvertResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: int


class PrizeCreateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prize_id: str
    name: str
    cost: int
    user: str


class PrizeDeleteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prize_id: str
    user: str


class PrizeRedeemResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redemption_id: str
    prize_id: str
    cost: int


class ChangelogAckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acked_through: int


class ImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    imported: Literal[True]


class ResetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reset: Literal[True]


class SlotsActionResult(SlotsOutcome):
    payout_tokens: int


class RouletteActionResult(RouletteOutcome):
    payout_tokens: int


class BlackjackSettlement(BlackjackSettlementOutcome):
    """In-flight settlement returned in the blackjack action response.

    The settlement fields games.py produces, plus `payout_tokens` (merged by
    `public_blackjack_state`). The persisted `events.BlackjackOutcome` extends
    the same base with `initial_wager` / `doubled` instead.
    """

    payout_tokens: int


class BlackjackHandStateResult(BaseModel):
    """Public view of a blackjack hand — what `/casino/blackjack/*` returns.

    `dealer_cards` and `dealer_value` reflect only the dealer's upcard while
    the hand is in `phase="playing"`; once `phase="done"`, the full dealer
    hand is revealed alongside the `settlement`.
    """

    model_config = ConfigDict(extra="forbid")

    hand_id: str
    phase: HandStatus
    current_wager: int
    player_cards: list[Card]
    dealer_cards: list[Card]
    hole_hidden: bool
    player_value: int
    dealer_value: int
    settlement: BlackjackSettlement | None


ActionResult = (
    SessionCompleteResult
    | SessionAddPastResult
    | SessionCreditsDeltaResult
    | ConvertResult
    | PrizeCreateResult
    | PrizeDeleteResult
    | PrizeRedeemResult
    | ChangelogAckResult
    | ImportResult
    | ResetResult
    | SlotsActionResult
    | RouletteActionResult
    | BlackjackHandStateResult
)


class ActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_action_id: str
    event: LedgerEventRead
    result: ActionResult
    game_event: GameEventRead | None = None


class SessionCompleteRequest(ActionRequest):
    """An active session (kept in client localStorage) is finishing.

    The client provides the timing data — the server has no in-progress
    session record to consult — and inserts a SessionRow with `seconds`
    computed from the supplied wall-clock interval.
    """

    subject: str = Field(min_length=1, max_length=120)
    start_time_ms: int = Field(ge=0)
    paused_duration_ms: int = Field(default=0, ge=0)
    ended_at_ms: int = Field(ge=0)
    session_id: str | None = Field(default=None, max_length=128)


class AddPastSessionRequest(ActionRequest):
    subject: str = Field(min_length=1, max_length=120)
    seconds: int = Field(gt=0, le=90 * 24 * 60 * 60)
    ended_at_ms: int = Field(ge=0)
    session_id: str | None = Field(default=None, max_length=128)


class EditSessionRequest(ActionRequest):
    session_id: str = Field(min_length=1, max_length=128)
    subject: str | None = Field(default=None, min_length=1, max_length=120)
    seconds: int | None = Field(default=None, ge=0, le=90 * 24 * 60 * 60)


class DeleteSessionRequest(ActionRequest):
    session_id: str = Field(min_length=1, max_length=128)


class ConvertRequest(ActionRequest):
    amount: int = Field(gt=0)


class PrizeCreateRequest(ActionRequest):
    name: str = Field(min_length=1, max_length=120)
    cost: int = Field(gt=0)
    prize_id: str | None = Field(default=None, max_length=128)
    # Matches the DB user_id column width (String(64)) and rejects blank
    # strings — otherwise the request validates but state_dump/insert fails
    # at the DB layer.
    target_user: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "If set, create the prize in this user's catalog. The caller must be an admin. "
            "Non-admin callers may not create prizes for themselves either — admins curate the catalog."
        ),
    )


class PrizeDeleteRequest(ActionRequest):
    prize_id: str = Field(min_length=1, max_length=128)
    target_user: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="If set, delete from this user's catalog. The caller must be an admin.",
    )


class PrizeRedeemRequest(ActionRequest):
    prize_id: str = Field(min_length=1, max_length=128)


class ChangelogAckRequest(ActionRequest):
    last_id: int = Field(ge=1, description="Highest changelog entry id the user has seen.")


class ImportSession(BaseModel):
    """A row inside `ImportData.sessions`.

    `extra="allow"` and `validation_alias` accommodate legacy exports — older
    frontend versions wrote `endedAt` (camelCase) and sometimes carried
    extra fields like `subject_color` we don't want to reject. The
    importer reads typed attributes and falls back to defaults for anything
    absent.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str | None = None
    subject: str = "Imported"
    seconds: int = 0
    ended_at_ms: int = Field(default=0, validation_alias=AliasChoices("ended_at_ms", "endedAt"))


class ImportPrize(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    name: str = "Imported prize"
    cost: int = 1


class ImportPrizeLog(BaseModel):
    """A row inside `ImportData.prize_log` — legacy `at` (camelCase) accepted."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str | None = None
    name: str = "Imported prize"
    cost: int = 0
    at_ms: int = Field(default=0, validation_alias=AliasChoices("at_ms", "at"))


class ImportData(BaseModel):
    """Payload for `POST /actions/import`.

    Mirrors what `useCasino.exportData()` writes to disk. Older exports
    (`version` < 4) carried fewer fields; `extra="allow"` keeps unknowns
    (`version`, `exportedAt`, `activeSession`) from triggering a validation
    error on a legacy restore. `prizes=None` opts into the default catalog;
    `prize_log` accepts both snake and camel keys for the same legacy
    reasons as the per-row models.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    credits: float = 0
    tokens: int = 0
    sessions: list[ImportSession] = Field(default_factory=list)
    prizes: list[ImportPrize] | None = None
    prize_log: list[ImportPrizeLog] | None = Field(default=None, validation_alias=AliasChoices("prize_log", "prizeLog"))


class ImportRequest(ActionRequest):
    data: ImportData


class ResetRequest(ActionRequest):
    pass


class SlotsSpinRequest(ActionRequest):
    wager_credits: int = Field(gt=0)


class RouletteSpinRequest(ActionRequest):
    wager_credits: int = Field(gt=0)
    bet_type: RouletteBetType
    bet_number: int | None = Field(default=None, ge=0, le=36)


class BlackjackDealRequest(ActionRequest):
    wager_credits: int = Field(gt=0)


class BlackjackHandRequest(ActionRequest):
    hand_id: str = Field(min_length=1, max_length=64)
