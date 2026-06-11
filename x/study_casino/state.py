"""Pydantic schemas for the Study Casino's read-side wire surface.

These mirror the JSON shapes the frontend consumes from
`GET /state`, `/me`, `/admin/users`, `/deployment`, and `/healthz`. Keeping them as
Pydantic models — instead of `dict[str, Any]` — lets FastAPI emit
real `components.schemas` entries in its OpenAPI doc, which the
frontend codegen (`//x/study_casino/frontend/lib:schema_zod`)
turns into Zod schemas for runtime parsing at the fetch boundary.

State-mutating action requests/responses live in `actions.py`;
audit-event reads (`GameEventRead`, `LedgerEventRead`) live in `events.py`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class BalanceRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credits: int
    tokens: int


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
