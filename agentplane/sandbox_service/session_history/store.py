"""Transactionally append and replay a runner's contiguous Event prefix."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.protocol import event_log_pb2
from agentplane.sandbox_service.session_history.db import SessionEvent, SessionHistory
from agentplane.sandbox_service.session_history.submissions import reconcile_admission

# The generated protobuf stubs need the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf


class HistoryConflictError(ValueError):
    """An entry skips or disagrees with the already stored runner prefix."""


class HistoryNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class OpenReservation:
    session_id: UUID
    launch_spec: bytes


@dataclass(frozen=True)
class HistoryLocator:
    session_id: UUID
    sandbox_name: str
    sandbox_uid: UUID
    runner_session_id: str


class Store:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def runnable_locators(self, namespace: str, sandbox_names: list[str]) -> list[HistoryLocator]:
        """Only current-Sandbox locators; deleted/Suspended histories remain retained."""
        if not sandbox_names:
            return []
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(SessionHistory).where(
                        SessionHistory.sandbox_namespace == namespace,
                        SessionHistory.sandbox_name.in_(sandbox_names),
                        SessionHistory.sandbox_uid.is_not(None),
                        SessionHistory.runner_session_id.is_not(None),
                    )
                )
            ).all()
            return [
                HistoryLocator(row.id, row.sandbox_name, row.sandbox_uid, row.runner_session_id)
                for row in rows
                if row.sandbox_uid is not None and row.runner_session_id is not None
            ]

    async def open(
        self,
        session_id: UUID,
        *,
        sandbox_namespace: str,
        sandbox_name: str,
        sandbox_uid: UUID | None,
        runner_session_id: str,
    ) -> None:
        """Register a durable ID and its immutable physical locator, including legacy IDs.

        The ID belongs to the Session (and remains usable after the Sandbox is deleted).
        Re-opening the same ID with another runner is an error, not a history overwrite.
        """
        async with self._sessions.begin() as session:
            await session.execute(
                insert(SessionHistory)
                .values(
                    id=session_id,
                    sandbox_namespace=sandbox_namespace,
                    sandbox_name=sandbox_name,
                    sandbox_uid=sandbox_uid,
                    runner_session_id=runner_session_id,
                    source_id=None,
                    last_cursor=0,
                )
                .on_conflict_do_nothing()
            )
            row = await session.get(SessionHistory, session_id)
            if row is None:
                raise HistoryConflictError(f"runner locator already belongs to another session, not {session_id}")
            if (row.sandbox_namespace, row.sandbox_name, row.sandbox_uid, row.runner_session_id) != (
                sandbox_namespace,
                sandbox_name,
                sandbox_uid,
                runner_session_id,
            ):
                raise HistoryConflictError(f"session {session_id} has a different runner locator")

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
        Imported histories use `open` to keep their existing public IDs.
        """
        if not all((caller_namespace, caller_name, sandbox_namespace, sandbox_name, open_key)):
            raise ValueError("caller, sandbox, and Open idempotency key are required")
        async with self._sessions.begin() as session:
            # Do not recompute today's defaults for an already reserved Open.
            query = select(SessionHistory).where(
                SessionHistory.caller_namespace == caller_namespace,
                SessionHistory.caller_name == caller_name,
                SessionHistory.sandbox_namespace == sandbox_namespace,
                SessionHistory.sandbox_name == sandbox_name,
                SessionHistory.sandbox_uid == sandbox_uid,
                SessionHistory.open_key == open_key,
            )
            existing = await session.scalar(query)
            if existing is not None:
                if existing.open_request != open_request:
                    raise HistoryConflictError("Open idempotency key reused with different inputs")
                assert existing.launch_spec is not None
                return OpenReservation(existing.id, existing.launch_spec)
            candidate = uuid4()
            frozen = launch_spec(candidate)
            created = await session.scalar(
                insert(SessionHistory)
                .values(
                    id=candidate,
                    caller_namespace=caller_namespace,
                    caller_name=caller_name,
                    sandbox_namespace=sandbox_namespace,
                    sandbox_name=sandbox_name,
                    sandbox_uid=sandbox_uid,
                    open_key=open_key,
                    open_request=open_request,
                    launch_spec=frozen,
                    runner_session_id=f"r-{candidate}",
                    source_id=None,
                    last_cursor=0,
                )
                .on_conflict_do_nothing()
                .returning(SessionHistory.id)
            )
            if created is not None:
                return OpenReservation(created, frozen)
            existing = await session.scalar(query)
            if existing is None:
                raise HistoryConflictError("Open identity could not be reserved")
            if existing.open_request != open_request:
                raise HistoryConflictError("Open idempotency key reused with different inputs")
            assert existing.launch_spec is not None
            return OpenReservation(existing.id, existing.launch_spec)

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
                    select(SessionHistory.id).where(
                        SessionHistory.caller_namespace == caller_namespace,
                        SessionHistory.caller_name == caller_name,
                        SessionHistory.sandbox_namespace == sandbox_namespace,
                        SessionHistory.sandbox_name == sandbox_name,
                        SessionHistory.sandbox_uid == sandbox_uid,
                        SessionHistory.open_key == open_key,
                    )
                ),
            )

    async def runner_id(self, session_id: UUID, *, sandbox_namespace: str, sandbox_name: str, sandbox_uid: UUID) -> str:
        """Resolve a public Session ID only in its original Sandbox incarnation."""
        async with self._sessions() as session:
            row = await session.get(SessionHistory, session_id)
            if (
                row is None
                or (row.sandbox_namespace, row.sandbox_name, row.sandbox_uid)
                != (sandbox_namespace, sandbox_name, sandbox_uid)
                or row.runner_session_id is None
            ):
                raise HistoryNotFoundError(session_id)
            return row.runner_session_id

    async def session_id(
        self, *, sandbox_namespace: str, sandbox_name: str, sandbox_uid: UUID, runner_session_id: str
    ) -> UUID | None:
        """Map an inventoried runner ID to a public Session ID, if managed."""
        async with self._sessions() as session:
            result = await session.scalar(
                select(SessionHistory.id).where(
                    SessionHistory.sandbox_namespace == sandbox_namespace,
                    SessionHistory.sandbox_name == sandbox_name,
                    SessionHistory.sandbox_uid == sandbox_uid,
                    SessionHistory.runner_session_id == runner_session_id,
                )
            )
            return cast(UUID | None, result)

    async def append(self, session_id: UUID, entries: Sequence[event_log_pb2.EventEntry]) -> int:
        """Replay exact duplicates or extend the prefix; serialize concurrent writers by Session ID.

        Validation and the new checkpoint commit together. On any error the entire batch rolls
        back. An empty batch is a checkpoint read, not an inferred runner high-water mark.
        """
        async with self._sessions.begin() as session:
            # Serialize prefix writers, but allow FK KEY SHARE checks from independent
            # command acceptance. Neither the Session ID nor any locator key changes here.
            row = await session.scalar(
                select(SessionHistory).where(SessionHistory.id == session_id).with_for_update(key_share=True)
            )
            if row is None:
                raise HistoryNotFoundError(session_id)
            for entry in entries:
                cursor = entry.cursor
                if cursor == 0 or not entry.origin.source_id or entry.origin.sequence != cursor:
                    raise HistoryConflictError(f"invalid runner origin at {cursor}")
                if row.source_id is not None and row.source_id != entry.origin.source_id:
                    raise HistoryConflictError(f"source changed at {cursor}")
                payload = entry.SerializeToString(deterministic=True)
                if cursor <= row.last_cursor:
                    existing = await session.get(SessionEvent, (session_id, cursor))
                    if existing is None or existing.payload != payload:
                        raise HistoryConflictError(f"conflicting entry at {cursor}")
                    await reconcile_admission(session, session_id, entry)
                    continue
                if cursor != row.last_cursor + 1:
                    raise HistoryConflictError(f"expected {row.last_cursor + 1}, received {cursor}")
                session.add(SessionEvent(session_id=session_id, cursor=cursor, payload=payload))
                row.source_id = entry.origin.source_id
                row.last_cursor = cursor
                await reconcile_admission(session, session_id, entry)
            return row.last_cursor

    async def read(
        self, session_id: UUID, *, after_cursor: int = 0, limit: int = 128
    ) -> tuple[int, list[event_log_pb2.EventEntry]]:
        """Internal replay only; never expose this as an unscoped workload read API."""
        if after_cursor < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid history page")
        async with self._sessions() as session:
            history = await session.get(SessionHistory, session_id)
            if history is None:
                raise HistoryNotFoundError(session_id)
            if after_cursor > history.last_cursor:
                raise ValueError("cursor beyond stored prefix")
            rows = await session.scalars(
                select(SessionEvent)
                .where(
                    SessionEvent.session_id == session_id,
                    SessionEvent.cursor > after_cursor,
                    SessionEvent.cursor <= history.last_cursor,
                )
                .order_by(SessionEvent.cursor)
                .limit(limit)
            )
            return history.last_cursor, [event_log_pb2.EventEntry.FromString(row.payload) for row in rows]
