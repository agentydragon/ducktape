"""Browser-session-bound BFF for Actions-owned OAuth enrollment transactions.

A browser session's interactions live in its row's payload, and each step reads and changes them
under the row lock rather than through `request.session`: two previews of one handle racing on one
session must share its browser binding, since the Action Service keeps the first it sees. The lock
is never held across an Action Service call, whose exchange may need it to renew the login.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agentplane.action_service.client import OperatorActionServiceClient
from agentplane.action_service.connections import Connection
from agentplane.action_service.enrollments import (
    EnrollmentAllow,
    EnrollmentConnection,
    EnrollmentDecisionResult,
    EnrollmentDeny,
    EnrollmentPreview,
    EnrollmentPreviewInput,
)
from agentplane.app.action_federation import OperatorFederationError
from agentplane.app.operator_sessions import SessionRow
from agentplane.subjects import ServiceAccountRef

EnrollmentHandle = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
_SESSION_KEY = "connection_enrollments_v2"
_MAX_INTERACTIONS = 32


class ConsentAllow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["allow"]
    csrf_token: str = Field(min_length=1, max_length=100)
    connection: EnrollmentConnection
    service_account: ServiceAccountRef


class ConsentDeny(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["deny"]
    csrf_token: str = Field(min_length=1, max_length=100)


ConsentDecision = Annotated[ConsentAllow | ConsentDeny, Field(discriminator="verdict")]


class ConsentPreview(BaseModel):
    enrollment: EnrollmentPreview
    service_accounts: list[ServiceAccountRef] = Field(
        description="The labeled caller ServiceAccounts the Action Service currently sees; the picker's choices."
    )
    connections: list[Connection]
    csrf_token: str
    attempted_decision: ConsentDecision | None


class _Interaction(BaseModel):
    browser_binding: str
    csrf_token: str
    expires_at: float
    idempotency_key: str
    version: int | None = None
    attempted_decision: ConsentDecision | None = None


def _interactions(payload: dict[str, Any]) -> dict[str, _Interaction]:
    entries = TypeAdapter(dict[str, _Interaction]).validate_python(payload.get(_SESSION_KEY, {}))
    now = datetime.now(UTC).timestamp()
    return {handle: value for handle, value in entries.items() if value.expires_at > now}


def _save(payload: dict[str, Any], entries: dict[str, _Interaction]) -> None:
    payload[_SESSION_KEY] = {handle: value.model_dump(mode="json") for handle, value in entries.items()}


@asynccontextmanager
async def _held(row: SessionRow) -> AsyncIterator[dict[str, Any]]:
    """The session's payload under the row lock; what the block leaves in it is saved."""
    async with row.locked() as held:
        if held.login is None:
            raise OperatorFederationError("operator_reauthentication_required", status_code=401)
        yield held.payload


async def preview_enrollment(row: SessionRow, handle: str, client: OperatorActionServiceClient) -> ConsentPreview:
    async with _held(row) as payload:
        entries = _interactions(payload)
        interaction = entries.get(handle)
        if interaction is None:
            if len(entries) >= _MAX_INTERACTIONS:
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many open connection authorizations")
            interaction = _Interaction(
                browser_binding=secrets.token_urlsafe(32),
                csrf_token=secrets.token_urlsafe(32),
                expires_at=datetime.now(UTC).timestamp() + 15 * 60,
                idempotency_key=str(uuid4()),
            )
            entries[handle] = interaction
            # Persist even if the upstream response is lost after it accepted this binding.
            _save(payload, entries)
    preview = await client.preview_enrollment(
        handle, EnrollmentPreviewInput(browser_binding=interaction.browser_binding)
    )
    async with _held(row) as payload:
        entries = _interactions(payload)
        # As it is now: a decision may have been attempted meanwhile.
        interaction = entries.setdefault(handle, interaction)
        if interaction.version is None:
            interaction.version = preview.version
        interaction.expires_at = preview.expires_at.timestamp()
        _save(payload, entries)
    return ConsentPreview(
        enrollment=preview,
        service_accounts=await client.caller_service_accounts(),
        connections=await client.connections(),
        csrf_token=interaction.csrf_token,
        attempted_decision=interaction.attempted_decision,
    )


async def decide_enrollment(
    row: SessionRow, handle: str, body: ConsentDecision, client: OperatorActionServiceClient
) -> EnrollmentDecisionResult:
    async with _held(row) as payload:
        entries = _interactions(payload)
        interaction = entries.get(handle)
        if interaction is None or interaction.version is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Preview this authorization in the current browser session first"
            )
        if not secrets.compare_digest(body.csrf_token.encode(), interaction.csrf_token.encode()):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid authorization CSRF token")
        if interaction.attempted_decision is not None and interaction.attempted_decision != body:
            raise HTTPException(status.HTTP_409_CONFLICT, "Only the original authorization decision may be retried")
        interaction.attempted_decision = body
        _save(payload, entries)
    common = {
        "browser_binding": interaction.browser_binding,
        "expected_version": interaction.version,
        "idempotency_key": interaction.idempotency_key,
    }
    decision = (
        EnrollmentAllow.model_validate(
            {**common, "connection": body.connection, "service_account": body.service_account}
        )
        if isinstance(body, ConsentAllow)
        else EnrollmentDeny.model_validate(common)
    )
    # This URL is the authenticated authority's server-held continuation, never browser metadata.
    return await client.decide_enrollment(handle, decision)
