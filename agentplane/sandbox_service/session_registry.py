"""The Sandbox Service's Session registry: public Session IDs, runner locators, Open reservations
and the WatchSessions feed, in the service's own database."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.commands.db import CHANGE_POSITION, Session
from agentplane.sandbox_service.session_history.session_changes import CHANNEL

# gazelle:include_dep @pypi//protobuf

# The service's own database; no other advisory lock uses this key.
CHANGE_POSITION_LOCK = 0x5345_5350  # "SESP"


class SessionNotFoundError(LookupError):
    pass


class OpenConflictError(ValueError):
    """An Open idempotency key was reused with different inputs, or its identity was taken."""


@dataclass(frozen=True)
class OpenReservation:
    session_id: UUID
    launch_spec: bytes


async def next_change_position(session: AsyncSession) -> int:
    """Allocate a WatchSessions position that becomes visible in commit order, and wake watchers.

    The transaction lock is held to commit, so no position commits after a larger one: a reader
    resuming after any position it has seen misses nothing. A rolled-back position is a gap.
    """
    await session.execute(select(func.pg_advisory_xact_lock(CHANGE_POSITION_LOCK)))
    # PostgreSQL delivers NOTIFY only on commit, to every replica listening.
    await session.execute(select(func.pg_notify(CHANNEL, "")))
    position = await session.scalar(select(CHANGE_POSITION.next_value()))
    assert position is not None
    return position


class SessionRegistry:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def reserve(
        self,
        *,
        caller_namespace: str,
        caller_name: str,
        sandbox_namespace: str,
        sandbox_name: str,
        sandbox_uid: UUID,
        open_key: str,
        open_request: bytes,
        launch_spec: Callable[[UUID], bytes],
    ) -> OpenReservation:
        """Reserve a durable Session ID *before* runner attach, or recover a lost reply.

        The key is opaque to the Service and scoped to the authenticated caller and
        Sandbox incarnation. The request bytes identify the caller's original inputs;
        the launch factory freezes effective runner setup for the candidate ID.
        Committing before runner contact lets a retry recover the same setup.
        """
        if not all((caller_namespace, caller_name, sandbox_namespace, sandbox_name, open_key)):
            raise ValueError("caller, sandbox, and Open idempotency key are required")
        query = select(Session).where(
            Session.caller_namespace == caller_namespace,
            Session.caller_name == caller_name,
            Session.sandbox_namespace == sandbox_namespace,
            Session.sandbox_name == sandbox_name,
            Session.sandbox_uid == sandbox_uid,
            Session.open_key == open_key,
        )
        async with self._sessions.begin() as session:
            # Do not recompute today's defaults for an already reserved Open.
            row = await session.scalar(query)
            if row is None:
                candidate = uuid4()
                await session.execute(
                    insert(Session)
                    .values(
                        id=candidate,
                        caller_namespace=caller_namespace,
                        caller_name=caller_name,
                        sandbox_namespace=sandbox_namespace,
                        sandbox_name=sandbox_name,
                        sandbox_uid=sandbox_uid,
                        open_key=open_key,
                        open_request=open_request,
                        launch_spec=launch_spec(candidate),
                        runner_session_id=f"r-{candidate}",
                        change_position=await next_change_position(session),
                    )
                    .on_conflict_do_nothing()
                )
                row = await session.scalar(query)
                if row is None:
                    raise OpenConflictError("Open identity could not be reserved")
        if row.open_request != open_request:
            raise OpenConflictError("Open idempotency key reused with different inputs")
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

    async def read_changes(self, *, after_position: int, limit: int) -> list[protocol_pb2.SessionChange]:
        """Every Session whose latest change is after the position, oldest change first."""
        if after_position < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid Session change page")
        async with self._sessions() as session:
            rows = await session.execute(
                select(Session.id, Session.sandbox_name, Session.sandbox_uid, Session.change_position)
                .where(Session.change_position > after_position)
                .order_by(Session.change_position)
                .limit(limit)
            )
            return [
                protocol_pb2.SessionChange(
                    session_id=str(row.id),
                    sandbox=row.sandbox_name,
                    sandbox_uid=str(row.sandbox_uid) if row.sandbox_uid is not None else "",
                    position=row.change_position,
                )
                for row in rows
            ]

    async def runner_id(self, session_id: UUID, *, sandbox_namespace: str, sandbox_name: str, sandbox_uid: UUID) -> str:
        """Resolve a public Session ID only in its original Sandbox incarnation."""
        async with self._sessions() as session:
            row = await session.get(Session, session_id)
            if (
                row is None
                or (row.sandbox_namespace, row.sandbox_name, row.sandbox_uid)
                != (sandbox_namespace, sandbox_name, sandbox_uid)
                or row.runner_session_id is None
            ):
                raise SessionNotFoundError(session_id)
            return row.runner_session_id

    async def session_id(
        self, *, sandbox_namespace: str, sandbox_name: str, sandbox_uid: UUID, runner_session_id: str
    ) -> UUID | None:
        """Map an inventoried runner ID to a public Session ID, if managed."""
        async with self._sessions() as session:
            return cast(
                UUID | None,
                await session.scalar(
                    select(Session.id).where(
                        Session.sandbox_namespace == sandbox_namespace,
                        Session.sandbox_name == sandbox_name,
                        Session.sandbox_uid == sandbox_uid,
                        Session.runner_session_id == runner_session_id,
                    )
                ),
            )
