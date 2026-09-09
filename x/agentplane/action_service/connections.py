"""Single-operator Connection authority, independent of OAuth protocol and browser ceremony.

Only an in-process OAuth adapter may bind/activate grants after verifying consent and the
upstream principal. The operator API exposes inventory, rename and revocation only.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from x.agentplane.action_service.catalog import Key
from x.agentplane.action_service.db import ConnectionGrantRow, ConnectionRow, SessionMaker
from x.agentplane.action_service.models import Principal, PrincipalRole

ConnectionName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True


class GrantStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"


class NewConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["new"] = "new"
    display_name: ConnectionName


class ReconnectConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["reconnect"] = "reconnect"
    connection_id: UUID
    expected_version: int = Field(ge=1)


class GrantBinding(BaseModel):
    """Trusted OAuth-adapter input; never accepted by a caller-facing HTTP endpoint.

    grant_id identifies one consumed consent decision and is also its retry key. Issuer and
    client_id are the verified downstream OAuth pair, not upstream Authentik client metadata.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: UUID
    identity_id: Key
    issuer: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    activation_deadline: AwareDatetime
    connection: Annotated[NewConnection | ReconnectConnection, Field(discriminator="kind")]


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: UUID
    connection_id: UUID
    revision: int
    identity_id: str
    issuer: str
    client_id: str
    status: GrantStatus
    activation_deadline: datetime
    created_at: datetime
    activated_at: datetime | None
    revoked_at: datetime | None

    def principal(self) -> Principal:
        """Configured Identity owns receipts; submitting grant remains separate evidence."""
        if self.status is not GrantStatus.ACTIVE:
            raise GrantRejectedError("grant is not active")
        return Principal(issuer="configured-identity", subject=self.identity_id, role=PrincipalRole.CALLER)


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    display_name: str
    version: int
    created_at: datetime
    updated_at: datetime
    grants: list[Grant]


class ConnectionNotFoundError(Exception):
    pass


class ConnectionConflictError(Exception):
    pass


class GrantRejectedError(Exception):
    pass


class ConnectionAuthority:
    def __init__(self, sessions: SessionMaker, identities: dict[Key, Identity]) -> None:
        self._sessions = sessions
        self._identities = dict(identities)

    def identities(self) -> dict[str, Identity]:
        return dict(self._identities)

    def _require_identity(self, identity_id: str) -> None:
        identity = self._identities.get(identity_id)
        if identity is None or not identity.enabled:
            raise GrantRejectedError("configured Identity is missing or disabled")

    async def bind(self, request: GrantBinding) -> Grant:
        """Reserve one immutable grant; exact retries cannot create another Connection.

        Reconnect revokes prior authority immediately. It never changes an issued grant's
        Identity; the replacement stays pending until the OAuth adapter activates it.
        """
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        async with self._sessions.begin() as db:
            # Serialize duplicate consent completion even when both try to create a new row.
            await db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(str(request.grant_id), 0))))
            existing = await db.get(ConnectionGrantRow, request.grant_id)
            if existing is not None:
                if existing.request_digest != digest:
                    raise ConnectionConflictError("grant retry differs from the original binding")
                return Grant.model_validate(existing)
            self._require_identity(request.identity_id)
            now = datetime.now(UTC)
            if request.activation_deadline <= now:
                raise GrantRejectedError("grant activation deadline has expired")
            match request.connection:
                case NewConnection(display_name=name):
                    connection = ConnectionRow(id=uuid4(), display_name=name, version=1, created_at=now, updated_at=now)
                    db.add(connection)
                    await db.flush()
                    revision = 1
                case ReconnectConnection(connection_id=connection_id, expected_version=version):
                    connection = await _locked_connection(db, connection_id)
                    _require_version(connection, version)
                    prior = await _grants(db, connection_id)
                    revision = max((grant.revision for grant in prior), default=0) + 1
                    _revoke(prior, now)
                    connection.version += 1
                    connection.updated_at = now
            grant = ConnectionGrantRow(
                id=request.grant_id,
                connection_id=connection.id,
                revision=revision,
                identity_id=request.identity_id,
                issuer=request.issuer,
                client_id=request.client_id,
                request_digest=digest,
                activation_deadline=request.activation_deadline,
                status=GrantStatus.PENDING,
                created_at=now,
                activated_at=None,
                revoked_at=None,
            )
            db.add(grant)
            await db.flush()
            return Grant.model_validate(grant)

    async def validate_pending(self, grant_id: UUID, *, issuer: str, client_id: str) -> Grant:
        """Check authority before the OAuth SDK consumes an authorization code.

        The OAuth transaction authority separately claims code exchange; this check does not
        consume a pending grant or permit another exchange after it has become active.
        """
        async with self._sessions.begin() as db:
            grant = await self._locked_grant(db, grant_id)
            self._require_identity(grant.identity_id)
            if (
                grant.status != GrantStatus.PENDING
                or (grant.issuer, grant.client_id) != (issuer, client_id)
                or grant.activation_deadline <= datetime.now(UTC)
            ):
                raise GrantRejectedError("grant is not pending authorization")
            return Grant.model_validate(grant)

    async def activate(self, grant_id: UUID) -> Grant:
        """OAuth adapter calls after verified issuance; pending/revoked tokens never resolve."""
        async with self._sessions.begin() as db:
            grant = await self._locked_grant(db, grant_id)
            self._require_identity(grant.identity_id)
            if grant.status == GrantStatus.REVOKED:
                raise GrantRejectedError("grant is revoked")
            if grant.status == GrantStatus.PENDING:
                if grant.activation_deadline <= datetime.now(UTC):
                    raise GrantRejectedError("grant activation deadline has expired")
                grant.status = GrantStatus.ACTIVE
                grant.activated_at = datetime.now(UTC)
                connection = await _locked_connection(db, grant.connection_id)
                connection.version += 1
                connection.updated_at = grant.activated_at
            return Grant.model_validate(grant)

    async def resolve(self, grant_id: UUID, *, issuer: str, client_id: str) -> Grant:
        """Resolve current authority from verified token claims on each admission."""
        async with self._sessions.begin() as db:
            grant = await self._locked_grant(db, grant_id)
            self._require_identity(grant.identity_id)
            if grant.status != GrantStatus.ACTIVE or (grant.issuer, grant.client_id) != (issuer, client_id):
                raise GrantRejectedError("grant is not authorized")
            return Grant.model_validate(grant)

    async def revoke(self, grant_id: UUID) -> None:
        """OAuth revocation ends only this grant; an old token cannot revoke its replacement."""
        async with self._sessions.begin() as db:
            grant = await db.get(ConnectionGrantRow, grant_id)
            if grant is None:
                return
            grant = await self._locked_grant(db, grant_id)
            if grant.status != GrantStatus.REVOKED:
                now = datetime.now(UTC)
                _revoke([grant], now)
                connection = await _locked_connection(db, grant.connection_id)
                connection.version += 1
                connection.updated_at = now

    async def _locked_grant(self, db: AsyncSession, grant_id: UUID) -> ConnectionGrantRow:
        grant = await db.get(ConnectionGrantRow, grant_id)
        if grant is None:
            raise GrantRejectedError("grant is not authorized")
        # All grant state mutations lock the Connection first. Re-read after acquiring the lock
        # so an unbind/reconnect that committed while waiting is observed by this transaction.
        await _locked_connection(db, grant.connection_id)
        await db.refresh(grant)
        return grant

    async def list(self) -> list[Connection]:
        async with self._sessions() as db:
            rows = list(
                await db.scalars(
                    select(ConnectionRow).order_by(ConnectionRow.created_at, ConnectionRow.id).with_for_update()
                )
            )
            grants = list(await db.scalars(select(ConnectionGrantRow).order_by(ConnectionGrantRow.revision)))
            return [_view(row, [grant for grant in grants if grant.connection_id == row.id]) for row in rows]

    async def get(self, connection_id: UUID) -> Connection:
        async with self._sessions() as db:
            row = await _locked_connection(db, connection_id)
            return _view(row, await _grants(db, connection_id))

    async def rename(self, connection_id: UUID, *, expected_version: int, display_name: str) -> Connection:
        name = NewConnection(display_name=display_name).display_name
        async with self._sessions.begin() as db:
            row = await _locked_connection(db, connection_id)
            _require_version(row, expected_version)
            row.display_name = name
            row.version += 1
            row.updated_at = datetime.now(UTC)
            return _view(row, await _grants(db, connection_id))

    async def unbind(self, connection_id: UUID, *, expected_version: int) -> Connection:
        async with self._sessions.begin() as db:
            row = await _locked_connection(db, connection_id)
            grants = await _grants(db, connection_id)
            if all(grant.status == GrantStatus.REVOKED for grant in grants):
                return _view(row, grants)
            _require_version(row, expected_version)
            now = datetime.now(UTC)
            _revoke(grants, now)
            row.version += 1
            row.updated_at = now
            return _view(row, grants)


async def _locked_connection(db: AsyncSession, connection_id: UUID) -> ConnectionRow:
    row = await db.scalar(select(ConnectionRow).where(ConnectionRow.id == connection_id).with_for_update())
    if row is None:
        raise ConnectionNotFoundError
    return row


async def _grants(db: AsyncSession, connection_id: UUID) -> list[ConnectionGrantRow]:
    return list(
        await db.scalars(
            select(ConnectionGrantRow)
            .where(ConnectionGrantRow.connection_id == connection_id)
            .order_by(ConnectionGrantRow.revision)
        )
    )


def _require_version(row: ConnectionRow, expected_version: int) -> None:
    if row.version != expected_version:
        raise ConnectionConflictError("Connection version changed")


def _revoke(grants: list[ConnectionGrantRow], now: datetime) -> None:
    for grant in grants:
        if grant.status != GrantStatus.REVOKED:
            grant.status = GrantStatus.REVOKED
            grant.revoked_at = now


def _view(row: ConnectionRow, grants: list[ConnectionGrantRow]) -> Connection:
    return Connection(
        id=row.id,
        display_name=row.display_name,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        grants=[Grant.model_validate(grant) for grant in grants],
    )
