"""The Runner Gateway's Session registry: public Session IDs, runner locators and Open reservations,
in the service's own database."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import ColumnElement, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.proto import runner_gateway_pb2
from agentplane.runner_gateway.commands.db import Session


class SessionNotFoundError(LookupError):
    pass


class OpenConflictError(ValueError):
    """An Open idempotency key was reused with different inputs, or its identity was taken."""


@dataclass(frozen=True)
class OpenReservation:
    session_id: UUID
    launch_spec: bytes


@dataclass(frozen=True)
class SessionPlacement:
    sandbox_name: str
    sandbox_uid: UUID


class SessionRegistry:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def reserve(
        self,
        *,
        session_id: UUID,
        caller_namespace: str,
        caller_name: str,
        sandbox_namespace: str,
        sandbox_name: str,
        sandbox_uid: UUID,
        open_key: str | None,
        open_request: bytes,
        launch_spec: Callable[[UUID], bytes],
    ) -> OpenReservation:
        """Reserve a durable Session ID *before* runner attach, or recover a lost reply.

        A retry names the same client-minted Session ID, scoped to the authenticated caller and
        Sandbox incarnation. The request bytes identify the caller's original inputs; the launch
        factory freezes effective runner setup for the Session ID. Committing before runner contact
        lets a retry recover the same setup.
        """
        if not all((caller_namespace, caller_name, sandbox_namespace, sandbox_name)):
            raise ValueError("caller and sandbox are required")
        scope = (
            Session.caller_namespace == caller_namespace,
            Session.caller_name == caller_name,
            Session.sandbox_namespace == sandbox_namespace,
            Session.sandbox_name == sandbox_name,
            Session.sandbox_uid == sandbox_uid,
        )
        query = select(Session).where(*scope, Session.id == session_id)
        async with self._sessions.begin() as session:
            # Do not recompute today's defaults for an already reserved Open.
            row = await session.scalar(query)
            if row is None:
                await session.execute(
                    insert(Session)
                    .values(
                        id=session_id,
                        caller_namespace=caller_namespace,
                        caller_name=caller_name,
                        sandbox_namespace=sandbox_namespace,
                        sandbox_name=sandbox_name,
                        sandbox_uid=sandbox_uid,
                        open_key=open_key or None,
                        open_request=open_request,
                        launch_spec=launch_spec(session_id),
                    )
                    .on_conflict_do_nothing()
                )
                # Another Session already holds this ID or Open key.
                row = await session.scalar(query)
                if row is None:
                    raise OpenConflictError("Open identity could not be reserved")
        if row.open_request != open_request:
            raise OpenConflictError("Open identity reused with different inputs")
        assert row.launch_spec is not None
        return OpenReservation(row.id, row.launch_spec)

    async def lookup_open(
        self,
        *,
        caller_namespace: str,
        caller_name: str,
        sandbox_namespace: str,
        sandbox_name: str,
        sandbox_uid: UUID,
        open_key: str,
    ) -> UUID | None:
        """Return only the public ID, scoped identically to a reservation; never return stored specs."""
        if not all((caller_namespace, caller_name, sandbox_namespace, sandbox_name, open_key)):
            raise ValueError("caller, sandbox, and Open idempotency key are required")
        async with self._sessions() as session:
            return cast(
                UUID | None,
                await session.scalar(
                    select(Session.id).where(
                        Session.caller_namespace == caller_namespace,
                        Session.caller_name == caller_name,
                        Session.sandbox_namespace == sandbox_namespace,
                        Session.sandbox_name == sandbox_name,
                        Session.sandbox_uid == sandbox_uid,
                        Session.open_key == open_key,
                    )
                ),
            )

    async def sandboxes(self, session_ids: set[UUID]) -> list[runner_gateway_pb2.SessionSandbox]:
        """Where each registered Session among `session_ids` runs."""
        if len(session_ids) > 1000:
            raise ValueError("at most 1000 Session IDs per read")
        return await self._sandboxes(Session.id.in_(session_ids))

    async def find(
        self, *, sandbox_name: str | None = None, sandbox_name_contains: str | None = None
    ) -> list[runner_gateway_pb2.SessionSandbox]:
        """Every Session in a Sandbox named `sandbox_name`, or whose name contains `sandbox_name_contains`."""
        if sandbox_name:
            if sandbox_name_contains is not None:
                raise ValueError("exactly one Sandbox name match is required")
            return await self._sandboxes(Session.sandbox_name == sandbox_name)
        if not sandbox_name_contains:
            raise ValueError("a non-empty Sandbox name match is required")
        return await self._sandboxes(Session.sandbox_name.icontains(sandbox_name_contains, autoescape=True))

    async def _sandboxes(self, where: ColumnElement[bool]) -> list[runner_gateway_pb2.SessionSandbox]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(Session.id, Session.sandbox_namespace, Session.sandbox_name, Session.sandbox_uid).where(where)
            )
            return [
                runner_gateway_pb2.SessionSandbox(
                    session_id=str(row.id),
                    sandbox_namespace=row.sandbox_namespace,
                    sandbox=row.sandbox_name,
                    sandbox_uid="" if row.sandbox_uid is None else str(row.sandbox_uid),
                )
                for row in rows
            ]

    async def runner_id(self, session_id: UUID, *, sandbox_namespace: str, sandbox_name: str, sandbox_uid: UUID) -> str:
        """Resolve a public Session ID only in its original Sandbox incarnation."""
    async def placement(self, session_id: UUID) -> SessionPlacement:
        """The Sandbox incarnation the Session runs in. Imported Sessions with no recorded
        incarnation have none, and raise `SessionNotFoundError` like an unknown ID."""
        async with self._sessions() as session:
            row = await session.get(Session, session_id)
        if row is None or row.sandbox_uid is None:
            raise SessionNotFoundError(session_id)
        return SessionPlacement(sandbox_name=row.sandbox_name, sandbox_uid=row.sandbox_uid)

    async def require(self, session_id: UUID, *, sandbox_namespace: str, sandbox_name: str, sandbox_uid: UUID) -> None:
        """Raise `SessionNotFoundError` unless the Session was registered in this Sandbox incarnation."""
        if not await self.registered(
            session_id, sandbox_namespace=sandbox_namespace, sandbox_name=sandbox_name, sandbox_uid=sandbox_uid
        ):
            raise SessionNotFoundError(session_id)

    async def public_id(
        self, runner_session_id: str, *, sandbox_namespace: str, sandbox_name: str, sandbox_uid: UUID
    ) -> UUID | None:
        """The public Session a runner Session ID names, if registered in this Sandbox incarnation."""
        try:
            session_id = UUID(runner_session_id)
        except ValueError:
            return None
        registered = await self.registered(
            session_id, sandbox_namespace=sandbox_namespace, sandbox_name=sandbox_name, sandbox_uid=sandbox_uid
        )
        return session_id if registered else None

    async def registered(
        self, session_id: UUID, *, sandbox_namespace: str, sandbox_name: str, sandbox_uid: UUID
    ) -> bool:
        async with self._sessions() as session:
            row = await session.get(Session, session_id)
            return row is not None and (row.sandbox_namespace, row.sandbox_name, row.sandbox_uid) == (
                sandbox_namespace,
                sandbox_name,
                sandbox_uid,
            )
