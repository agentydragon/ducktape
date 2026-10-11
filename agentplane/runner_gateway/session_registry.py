"""The Runner Gateway's Session registry: which Sandbox incarnation each Session runs in, in the
service's own database."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import ColumnElement, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.proto import runner_gateway_pb2
from agentplane.runner_gateway.commands.db import Session


class SessionNotFoundError(LookupError):
    pass


class SessionConflictError(ValueError):
    """The Session ID is registered in another Sandbox incarnation."""


@dataclass(frozen=True)
class SessionPlacement:
    sandbox_name: str
    sandbox_uid: UUID


class SessionRegistry:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def register(self, session_id: UUID, *, sandbox_namespace: str, sandbox_name: str, sandbox_uid: UUID) -> None:
        """Register the Session in its Sandbox incarnation before the runner opens it. A retry in the
        same incarnation finds it registered; any other incarnation is refused."""
        async with self._sessions.begin() as session:
            await session.execute(
                insert(Session)
                .values(
                    id=session_id,
                    sandbox_namespace=sandbox_namespace,
                    sandbox_name=sandbox_name,
                    sandbox_uid=sandbox_uid,
                )
                .on_conflict_do_nothing()
            )
        if not await self.registered(
            session_id, sandbox_namespace=sandbox_namespace, sandbox_name=sandbox_name, sandbox_uid=sandbox_uid
        ):
            raise SessionConflictError(session_id)

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
