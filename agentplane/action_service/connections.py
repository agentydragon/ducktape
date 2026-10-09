"""Single-operator Connection authority, independent of OAuth protocol and browser ceremony.

Only an in-process OAuth adapter may bind/activate grants after verifying consent and the
upstream principal. The operator API exposes inventory, rename, audited rebinding and revocation. A grant acts
as one labeled ServiceAccount, checked against the policy informer's index on every resolution:
removing the label or the ServiceAccount is the disable.
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

from agentplane.action_service.db import ConnectionGrantRow, ConnectionRebindRow, ConnectionRow, SessionMaker
from agentplane.action_service.models import CallerPrincipal, ExternalGrantProvenance, OperatorPrincipal
from agentplane.action_service.policy_informer import PolicyIndex
from agentplane.subjects import ServiceAccountRef

ConnectionName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ConnectionVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class ConnectionRename(ConnectionVersion):
    display_name: ConnectionName


class ConnectionRebind(ConnectionVersion):
    service_account: ServiceAccountRef


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
    service_account: ServiceAccountRef
    issuer: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    activation_deadline: AwareDatetime
    connection: Annotated[NewConnection | ReconnectConnection, Field(discriminator="kind")]


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: UUID
    connection_id: UUID
    revision: int
    caller: ServiceAccountRef
    issuer: str
    client_id: str
    status: GrantStatus
    activation_deadline: datetime
    created_at: datetime
    activated_at: datetime | None
    revoked_at: datetime | None
    binding_version: int = Field(default=0, ge=0)

    def principal(self) -> CallerPrincipal:
        """The ServiceAccount owns receipts; the submitting grant remains separate evidence."""
        if self.status is not GrantStatus.ACTIVE:
            raise GrantRejectedError("grant is not active")
        return self.provenance().principal()

    def provenance(self) -> ExternalGrantProvenance:
        return ExternalGrantProvenance(
            caller=self.caller,
            issuer=self.issuer,
            client_id=self.client_id,
            connection_id=self.connection_id,
            grant_id=self.id,
            revision=self.revision,
            binding_version=self.binding_version,
        )


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    display_name: str
    version: int
    created_at: datetime
    updated_at: datetime
    grants: list[Grant]
    bound_caller: ServiceAccountRef | None = None


class ConnectionNotFoundError(Exception):
    pass


class ConnectionConflictError(Exception):
    pass


class GrantRejectedError(Exception):
    pass


class ConnectionAuthority:
    def __init__(self, sessions: SessionMaker, callers: PolicyIndex) -> None:
        self._sessions = sessions
        self._callers = callers

    def caller_service_accounts(self) -> list[ServiceAccountRef]:
        """The ServiceAccounts a new grant may act as, as the informer currently sees them."""
        return self._callers.caller_service_accounts()

    def require_caller(self, caller: ServiceAccountRef) -> ServiceAccountRef:
        """Refuse unless the informer currently lists the ServiceAccount as labeled."""
        if self._callers.admits(caller):
            return caller
        raise GrantRejectedError("caller ServiceAccount is missing or not labeled as an Action caller")

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
                return _original_grant(existing)
            self.require_caller(request.service_account)
            now = datetime.now(UTC)
            if request.activation_deadline <= now:
                raise GrantRejectedError("grant activation deadline has expired")
            match request.connection:
                case NewConnection(display_name=name):
                    connection = ConnectionRow(
                        id=uuid4(),
                        display_name=name,
                        bound_caller=request.service_account.model_dump(mode="json"),
                        binding_version=0,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(connection)
                    await db.flush()
                    revision = 1
                case ReconnectConnection(connection_id=connection_id, expected_version=version):
                    connection = await _locked_connection(db, connection_id)
                    _require_version(connection, version)
                    prior = await _grants(db, connection_id)
                    revision = max((grant.revision for grant in prior), default=0) + 1
                    _revoke(prior, now)
                    connection.bound_caller = request.service_account.model_dump(mode="json")
                    connection.binding_version = 0
                    connection.version += 1
                    connection.updated_at = now
            grant = ConnectionGrantRow(
                id=request.grant_id,
                connection_id=connection.id,
                revision=revision,
                caller=request.service_account.model_dump(mode="json"),
                original_caller=request.service_account.model_dump(mode="json"),
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
            return _original_grant(grant)

    async def validate_pending(self, grant_id: UUID, *, issuer: str, client_id: str) -> Grant:
        """Check authority before the OAuth SDK consumes an authorization code.

        The OAuth transaction authority separately claims code exchange; this check does not
        consume a pending grant or permit another exchange after it has become active.
        """
        async with self._sessions.begin() as db:
            grant = await self._locked_grant(db, grant_id)
            self._require_row_caller(grant)
            if (
                grant.status != GrantStatus.PENDING
                or (grant.issuer, grant.client_id) != (issuer, client_id)
                or grant.activation_deadline <= datetime.now(UTC)
            ):
                raise GrantRejectedError("grant is not pending authorization")
            return _original_grant(grant)

    async def activate(self, grant_id: UUID) -> Grant:
        """OAuth adapter calls after verified issuance; pending/revoked tokens never resolve."""
        async with self._sessions.begin() as db:
            grant = await self._locked_grant(db, grant_id)
            self._require_row_caller(grant)
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
            return _original_grant(grant)

    async def resolve(self, grant_id: UUID, *, issuer: str, client_id: str) -> Grant:
        """Resolve current authority from verified token claims on each admission."""
        async with self._sessions.begin() as db:
            grant = await self._locked_grant(db, grant_id)
            if grant.status != GrantStatus.ACTIVE or (grant.issuer, grant.client_id) != (issuer, client_id):
                raise GrantRejectedError("grant is not authorized")
            connection = await _locked_connection(db, grant.connection_id)
            return self._effective_grant(grant, connection)

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

    async def authorize_action(self, session: AsyncSession, grant: ExternalGrantProvenance) -> bool:
        """Validate admission/dispatch inside the ActionStore transaction, not before it.

        Connection row locking makes revocation atomic with admission and the dispatch claim.
        The caller owns the transaction and must keep it open until its Action write commits.
        """
        try:
            row = await self._locked_grant(session, grant.grant_id)
            if row.status != GrantStatus.ACTIVE:
                return False
            connection = await _locked_connection(session, row.connection_id)
            return self._effective_grant(row, connection).provenance() == grant
        except GrantRejectedError, ConnectionNotFoundError:
            return False

    def _effective_grant(self, grant: ConnectionGrantRow, connection: ConnectionRow) -> Grant:
        # Older replicas can still create a Connection during the rolling deploy.
        # They omit bound_caller; only version zero may use the grant as fallback.
        bound = connection.bound_caller
        if bound is None and connection.binding_version == 0:
            bound = grant.caller
        if bound is None:
            raise GrantRejectedError("connection has no bound caller")
        caller = ServiceAccountRef.model_validate(bound)
        # An older replica may have reconnected this Connection without updating
        # bound_caller. Never silently give that new grant the stale binding.
        if ServiceAccountRef.model_validate(grant.caller) != caller:
            raise GrantRejectedError("connection and grant caller differ")
        self.require_caller(caller)
        return _original_grant(grant).model_copy(
            update={"caller": caller, "binding_version": connection.binding_version}
        )

    def _require_row_caller(self, row: ConnectionGrantRow) -> None:
        self.require_caller(Grant.model_validate(row).caller)

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

    async def rebind(
        self, connection_id: UUID, *, expected_version: int, caller: ServiceAccountRef, operator: OperatorPrincipal
    ) -> Connection:
        """Retarget future uses of an issued token; past grants and Action provenance stay immutable."""
        async with self._sessions.begin() as db:
            row = await _locked_connection(db, connection_id)
            _require_version(row, expected_version)
            grants = await _grants(db, connection_id)
            # A pending OAuth exchange could still be activated by a legacy replica using
            # its original caller. Do not rebind until that consent flow finishes.
            if any(grant.status == GrantStatus.PENDING for grant in grants):
                raise GrantRejectedError("connection has a pending grant")
            if not any(grant.status == GrantStatus.ACTIVE for grant in grants):
                raise GrantRejectedError("connection has no active grant")
            previous_binding = row.bound_caller
            if previous_binding is None and row.binding_version == 0:
                previous_binding = next(grant.caller for grant in grants if grant.status == GrantStatus.ACTIVE)
            if previous_binding is None:
                raise GrantRejectedError("connection is unbound")
            self.require_caller(caller)
            previous = ServiceAccountRef.model_validate(previous_binding)
            if previous == caller:
                return _view(row, grants)
            now = datetime.now(UTC)
            row.bound_caller = caller.model_dump(mode="json")
            # Legacy replicas resolve grant.caller, not Connection.bound_caller. Keep
            # them aligned atomically so old readers cannot act as the previous SA.
            # The original caller remains available for immutable grant history.
            for grant in grants:
                if grant.status == GrantStatus.ACTIVE:
                    # Older replicas can still insert grants without original_caller.
                    if grant.original_caller is None:
                        grant.original_caller = grant.caller
                    grant.caller = row.bound_caller
            row.binding_version += 1
            row.version += 1
            row.updated_at = now
            db.add(
                ConnectionRebindRow(
                    id=uuid4(),
                    connection_id=connection_id,
                    version=row.version,
                    previous_caller=previous.model_dump(mode="json"),
                    caller=row.bound_caller,
                    operator_issuer=operator.issuer,
                    operator_subject=operator.subject,
                    at=now,
                )
            )
            return _view(row, grants)

    async def unbind(self, connection_id: UUID, *, expected_version: int) -> Connection:
        async with self._sessions.begin() as db:
            row = await _locked_connection(db, connection_id)
            grants = await _grants(db, connection_id)
            if all(grant.status == GrantStatus.REVOKED for grant in grants):
                return _view(row, grants)
            _require_version(row, expected_version)
            now = datetime.now(UTC)
            _revoke(grants, now)
            row.bound_caller = None
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


def _original_grant(row: ConnectionGrantRow) -> Grant:
    return Grant.model_validate(row).model_copy(
        update={"caller": ServiceAccountRef.model_validate(row.original_caller or row.caller)}
    )


def _view(row: ConnectionRow, grants: list[ConnectionGrantRow]) -> Connection:
    bound = row.bound_caller
    if bound is None and row.binding_version == 0:
        bound = next((grant.caller for grant in reversed(grants) if grant.status != GrantStatus.REVOKED), None)
    return Connection(
        id=row.id,
        display_name=row.display_name,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        grants=[_original_grant(grant) for grant in grants],
        bound_caller=ServiceAccountRef.model_validate(bound) if bound is not None else None,
    )
