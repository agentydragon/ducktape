"""Pydantic schemas for the Study Casino's read-side wire surface.

These mirror the JSON shapes the frontend consumes from
`GET /state`, `/me`, `/admin/users`, `/deployment`, and `/healthz`. Keeping them as
Pydantic models — instead of `dict[str, Any]` — lets FastAPI emit
real `components.schemas` entries in its OpenAPI doc, which the
frontend codegen (`//study_casino/frontend/lib:schema_zod`)
turns into Zod schemas for runtime parsing at the fetch boundary.

State-mutating action requests/responses live in `actions.py`;
audit-event reads (`GameEventRead`, `LedgerEventRead`) live in `events.py`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from study_casino.changelog import ChangelogEntry


class BalanceRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credits_millis: int = Field(description="Credit balance in integer millicredits (credit value × 1000).")
    tokens: int


class CreditStateRead(BaseModel):
    """Read-only streak/daily-bonus state derived server-side — the frontend
    only displays it (see plans/credit_system_v2.md)."""

    model_config = ConfigDict(extra="forbid")

    streak_days: int
    streak_bonus_percent: int = Field(description="Streak bonus applied to credit awards: 1%/day, capped at 100.")
    rest_days_available: int
    daily_bonus_claimed_today: bool = Field(
        description="Whether the daily first-5-minutes bonus fired today (Pacific)."
    )
    today_study_seconds: int = Field(description="Seconds of completed sessions today (Pacific).")
    daily_bonus_threshold_seconds: int = Field(description="Study seconds per day that unlock the daily bonus.")
    daily_bonus_credits: int = Field(description="Whole-credit daily bonus amount (streak-multiplied at award).")
    pending_bonus_percent: int = Field(
        description="Streak bonus percent a qualifying session completed today would be awarded at — "
        "lets the frontend project an in-progress session without replicating streak rules."
    )


class SessionRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    subject: str
    seconds: int
    ended_at_ms: int


class PrizeRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    cost: int


class PrizeLogRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    cost: int
    at_ms: int


class StateDump(BaseModel):
    """`GET /state` response — full per-user state for the frontend cache."""

    model_config = ConfigDict(extra="forbid")

    balance: BalanceRead
    credit_state: CreditStateRead
    changelog_unacked: list[ChangelogEntry] = Field(
        description="Changelog entries newer than the caller's ack, oldest first. Ack via /actions/changelog/ack."
    )
    sessions: list[SessionRead]
    prizes: list[PrizeRead]
    prize_log: list[PrizeLogRead]


class MeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    is_admin: bool


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool


class DeploymentInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_tag: str | None = None
    source_commit: str | None = None
    source_commit_url: str | None = None


class AdminUsersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    users: list[str]


class WsStateChangedMessage(BaseModel):
    """Payload broadcast on the `/ws` channel after every successful action."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["state_changed"]
