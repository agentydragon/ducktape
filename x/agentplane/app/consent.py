"""Browser-session-bound BFF for Actions-owned OAuth enrollment transactions."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from x.agentplane.action_service.catalog import Key
from x.agentplane.action_service.client import OperatorActionServiceClient
from x.agentplane.action_service.connections import ConnectionName, Identity
from x.agentplane.action_service.enrollments import (
    EnrollmentAllow,
    EnrollmentDecisionResult,
    EnrollmentDeny,
    EnrollmentPreview,
    EnrollmentPreviewInput,
)

EnrollmentHandle = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
_SESSION_KEY = "connection_enrollments"
_MAX_INTERACTIONS = 32


class ConsentAllow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["allow"]
    csrf_token: str = Field(min_length=1, max_length=100)
    display_name: ConnectionName
    identity_id: Key


class ConsentDeny(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["deny"]
    csrf_token: str = Field(min_length=1, max_length=100)


ConsentDecision = Annotated[ConsentAllow | ConsentDeny, Field(discriminator="verdict")]


class ConsentPreview(BaseModel):
    enrollment: EnrollmentPreview
    identities: dict[str, Identity]
    csrf_token: str
    attempted_decision: ConsentDecision | None


class _Interaction(BaseModel):
    browser_binding: str
    csrf_token: str
    expires_at: float
    idempotency_key: str
    version: int | None = None
    attempted_decision: ConsentDecision | None = None


def _interactions(request: Request) -> dict[str, _Interaction]:
    entries = TypeAdapter(dict[str, _Interaction]).validate_python(request.session.get(_SESSION_KEY, {}))
    now = datetime.now(UTC).timestamp()
    return {handle: value for handle, value in entries.items() if value.expires_at > now}


def _save(request: Request, entries: dict[str, _Interaction]) -> None:
    # OperatorSessionMiddleware serializes concurrent requests to this persistent browser session.
    request.session[_SESSION_KEY] = {handle: value.model_dump(mode="json") for handle, value in entries.items()}


async def preview_enrollment(request: Request, handle: str, client: OperatorActionServiceClient) -> ConsentPreview:
    entries = _interactions(request)
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
        _save(request, entries)
    preview = await client.preview_enrollment(
        handle, EnrollmentPreviewInput(browser_binding=interaction.browser_binding)
    )
    if interaction.version is None:
        interaction.version = preview.version
    interaction.expires_at = preview.expires_at.timestamp()
    _save(request, entries)
    return ConsentPreview(
        enrollment=preview,
        identities=await client.list_identities(),
        csrf_token=interaction.csrf_token,
        attempted_decision=interaction.attempted_decision,
    )


async def decide_enrollment(
    request: Request, handle: str, body: ConsentDecision, client: OperatorActionServiceClient
) -> EnrollmentDecisionResult:
    entries = _interactions(request)
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
    _save(request, entries)
    common = {
        "browser_binding": interaction.browser_binding,
        "expected_version": interaction.version,
        "idempotency_key": interaction.idempotency_key,
    }
    decision = (
        EnrollmentAllow.model_validate({**common, "display_name": body.display_name, "identity_id": body.identity_id})
        if isinstance(body, ConsentAllow)
        else EnrollmentDeny.model_validate(common)
    )
    # This URL is the authenticated authority's server-held continuation, never browser metadata.
    return await client.decide_enrollment(handle, decision)
