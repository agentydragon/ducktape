"""Durable browser consent for validated OAuth transactions, separate from token issuance."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from x.agentplane.action_service.catalog import Key
from x.agentplane.action_service.connections import (
    ConnectionAuthority,
    ConnectionConflictError,
    GrantBinding,
    NewConnection,
    ReconnectConnection,
)
from x.agentplane.action_service.db import EnrollmentRow, SessionMaker
from x.agentplane.action_service.models import Principal, PrincipalRole, Verdict


class EnrollmentInput(BaseModel):
    """Only the OAuth adapter supplies this, after framework authorization validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    client_name: str | None
    redirect_uri: str = Field(min_length=1)
    code_challenge: str = Field(min_length=1)
    upstream_url: str = Field(min_length=1)
    expires_at: AwareDatetime


class CreatedEnrollment(BaseModel):
    handle: str


class EnrollmentPreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    browser_binding: str = Field(min_length=32, max_length=200)


class EnrollmentPreview(BaseModel):
    client_id: str
    client_name: str | None
    redirect_uri: str
    expires_at: datetime
    version: int


class EnrollmentDecisionBase(EnrollmentPreviewInput):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)


class ConfirmedReconnectConnection(ReconnectConnection):
    authority_change_confirmed: Literal[True]


type EnrollmentConnection = Annotated[NewConnection | ConfirmedReconnectConnection, Field(discriminator="kind")]


class EnrollmentAllow(EnrollmentDecisionBase):
    verdict: Literal["allow"] = "allow"
    connection: EnrollmentConnection
    identity_id: Key


class EnrollmentDeny(EnrollmentDecisionBase):
    verdict: Literal["deny"] = "deny"


type EnrollmentDecisionInput = Annotated[EnrollmentAllow | EnrollmentDeny, Field(discriminator="verdict")]


class EnrollmentDecisionResult(BaseModel):
    verdict: Verdict
    redirect_url: str | None


class EnrollmentRejectedError(Exception):
    pass


class EnrollmentNotFoundError(EnrollmentRejectedError):
    pass


class EnrollmentConflictError(EnrollmentRejectedError):
    pass


class EnrollmentExpiredError(EnrollmentRejectedError):
    pass


class EnrollmentAuthority:
    def __init__(self, sessions: SessionMaker, connections: ConnectionAuthority) -> None:
        self._sessions = sessions
        self._connections = connections

    async def create(self, request: EnrollmentInput) -> CreatedEnrollment:
        now = datetime.now(UTC)
        if not now < request.expires_at <= now + timedelta(minutes=15):
            raise EnrollmentRejectedError("enrollment expiry is outside the authorization window")
        handle = secrets.token_urlsafe(32)
        async with self._sessions.begin() as db:
            # The framework's ClientCode retains this tuple, not the upstream OAuth state.
            # Keep correlation tombstones so an old code cannot resolve to a newer consent.
            created = await db.scalar(
                insert(EnrollmentRow)
                .values(
                    id=uuid4(),
                    handle_hash=_digest(handle),
                    issuer=request.issuer,
                    client_id=request.client_id,
                    client_name=request.client_name,
                    redirect_uri=request.redirect_uri,
                    code_challenge=request.code_challenge,
                    upstream_url=request.upstream_url,
                    expires_at=request.expires_at,
                    version=1,
                )
                .on_conflict_do_nothing()
                .returning(EnrollmentRow.id)
            )
            if created is None:
                raise EnrollmentConflictError("authorization correlation was already used; restart with fresh PKCE")
        return CreatedEnrollment(handle=handle)

    async def preview(self, handle: str, request: EnrollmentPreviewInput, operator: Principal) -> EnrollmentPreview:
        _require_operator(operator)
        async with self._sessions.begin() as db:
            row = await db.scalar(
                select(EnrollmentRow).where(EnrollmentRow.handle_hash == _digest(handle)).with_for_update()
            )
            row = _require_live(row)
            binding = _digest(request.browser_binding)
            if row.browser_hash is None:
                row.browser_hash = binding
                row.operator_issuer = operator.issuer
                row.operator_subject = operator.subject
            _require_browser(row, binding, operator)
            return EnrollmentPreview(
                client_id=row.client_id,
                client_name=row.client_name,
                redirect_uri=row.redirect_uri,
                expires_at=row.expires_at,
                version=row.version,
            )

    async def decide(
        self, handle: str, request: EnrollmentDecisionInput, operator: Principal
    ) -> EnrollmentDecisionResult:
        _require_operator(operator)
        async with self._sessions.begin() as db:
            row = await db.scalar(
                select(EnrollmentRow).where(EnrollmentRow.handle_hash == _digest(handle)).with_for_update()
            )
            row = _require_live(row)
            _require_browser(row, _digest(request.browser_binding), operator)
            digest = _digest(request.model_dump_json())
            if row.decision_digest is not None:
                if not secrets.compare_digest(row.decision_digest, digest):
                    raise EnrollmentConflictError("enrollment already has a different decision")
                return _result(row)
            if row.version != request.expected_version:
                raise EnrollmentConflictError("enrollment version changed")
            if isinstance(request, EnrollmentAllow):
                self._require_identity(request.identity_id)
                row.identity_id = request.identity_id
                match request.connection:
                    case NewConnection(display_name=name):
                        row.display_name = name
                    case ConfirmedReconnectConnection(connection_id=connection_id, expected_version=version):
                        connection = await self._connections.get(connection_id)
                        if connection.version != version:
                            raise ConnectionConflictError(
                                "Connection changed; restart authorization and review it again"
                            )
                        row.connection_id = connection_id
                        row.connection_version = version
            row.verdict = request.verdict
            row.decision_digest = digest
            row.version += 1
            return _result(row)

    async def approved(
        self, *, client_id: str, redirect_uri: str, code_challenge: str, operator: Principal
    ) -> GrantBinding:
        """Verify consent before any framework code consumption or grant activation."""
        _require_operator(operator)
        async with self._sessions() as db:
            row = await db.scalar(
                select(EnrollmentRow).where(
                    EnrollmentRow.client_id == client_id,
                    EnrollmentRow.redirect_uri == redirect_uri,
                    EnrollmentRow.code_challenge == code_challenge,
                )
            )
            row = _require_live(row)
            if (row.operator_issuer, row.operator_subject) != (operator.issuer, operator.subject):
                raise EnrollmentRejectedError("issuing operator does not match consent")
            if row.verdict != Verdict.ALLOW or row.identity_id is None:
                raise EnrollmentRejectedError("enrollment was not approved")
            if row.exchange_claimed_at is not None:
                raise EnrollmentRejectedError("token exchange already claimed; restart authorization")
            self._require_identity(row.identity_id)
            connection: NewConnection | ReconnectConnection
            if row.connection_id is not None and row.connection_version is not None:
                connection = ReconnectConnection(
                    connection_id=row.connection_id, expected_version=row.connection_version
                )
            elif row.display_name is not None:
                connection = NewConnection(display_name=row.display_name)
            else:
                raise EnrollmentRejectedError("enrollment has no Connection selection")
            return GrantBinding(
                grant_id=row.id,
                identity_id=row.identity_id,
                issuer=row.issuer,
                client_id=row.client_id,
                activation_deadline=row.expires_at,
                connection=connection,
            )

    async def claim_exchange(self, grant_id: UUID) -> None:
        """One token family per consent; ambiguous post-claim failures need fresh OAuth."""
        async with self._sessions.begin() as db:
            row = _require_live(await db.get(EnrollmentRow, grant_id, with_for_update=True))
            if row.verdict != Verdict.ALLOW or row.identity_id is None or row.exchange_claimed_at is not None:
                raise EnrollmentRejectedError("enrollment cannot issue another token family")
            self._require_identity(row.identity_id)
            row.exchange_claimed_at = datetime.now(UTC)

    def _require_identity(self, identity_id: str) -> None:
        identity = self._connections.identities().get(identity_id)
        if identity is None or not identity.enabled:
            raise EnrollmentRejectedError("configured Identity is missing or disabled")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _require_operator(operator: Principal) -> None:
    if operator.role is not PrincipalRole.OPERATOR:
        raise EnrollmentRejectedError("operator authority is required")


def _require_live(row: EnrollmentRow | None) -> EnrollmentRow:
    if row is None:
        raise EnrollmentNotFoundError("enrollment not found")
    if row.expires_at <= datetime.now(UTC):
        raise EnrollmentExpiredError("enrollment expired; restart authorization")
    return row


def _require_browser(row: EnrollmentRow, binding: str, operator: Principal) -> None:
    if (
        row.browser_hash is None
        or not secrets.compare_digest(row.browser_hash, binding)
        or (row.operator_issuer, row.operator_subject) != (operator.issuer, operator.subject)
    ):
        raise EnrollmentRejectedError("enrollment is bound to another browser or operator")


def _result(row: EnrollmentRow) -> EnrollmentDecisionResult:
    assert row.verdict is not None
    verdict = Verdict(row.verdict)
    return EnrollmentDecisionResult(
        verdict=verdict, redirect_url=row.upstream_url if verdict is Verdict.ALLOW else None
    )
