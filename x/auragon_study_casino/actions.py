"""Pydantic wire models for server-authoritative Study Casino actions.

State sync is no longer CRDT-based — clients refetch `GET /state` after each
successful action (the server pushes a `state_changed` ping over `/ws` so
other tabs of the same user know to refetch). Every action carries a
`client_action_id` that the server uses as the idempotency key on
`ledger_events`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from x.auragon_study_casino.events import GameEventRead, LedgerEventRead

_ACTION_ID_PATTERN = r"^[a-zA-Z0-9._:@-]+$"


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_action_id: str = Field(min_length=1, max_length=128, pattern=_ACTION_ID_PATTERN)


class ActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_action_id: str
    event: LedgerEventRead
    result: dict[str, Any]
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
    target_user: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "If set, create the prize in this user's catalog. The caller must be an admin. "
            "Non-admin callers may not create prizes for themselves either — admins curate the catalog."
        ),
    )


class PrizeDeleteRequest(ActionRequest):
    prize_id: str = Field(min_length=1, max_length=128)
    target_user: str | None = Field(
        default=None,
        max_length=120,
        description="If set, delete from this user's catalog. The caller must be an admin.",
    )


class PrizeRedeemRequest(ActionRequest):
    prize_id: str = Field(min_length=1, max_length=128)


class ImportRequest(ActionRequest):
    data: dict[str, Any]


class ResetRequest(ActionRequest):
    pass


class SlotsSpinRequest(ActionRequest):
    wager_credits: int = Field(gt=0)


class RouletteSpinRequest(ActionRequest):
    wager_credits: int = Field(gt=0)
    bet_type: Literal["red", "black", "odd", "even", "low", "high", "dozen1", "dozen2", "dozen3", "number"]
    bet_number: int | None = Field(default=None, ge=0, le=36)


class BlackjackDealRequest(ActionRequest):
    wager_credits: int = Field(gt=0)


class BlackjackHandRequest(ActionRequest):
    hand_id: str = Field(min_length=1, max_length=64)
