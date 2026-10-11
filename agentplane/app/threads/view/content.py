"""Reads of a thread's content as the fold assembled it: the scope it has materialized, the
evidence behind an entity, and command admissions.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import grpc
from google.protobuf.json_format import MessageToDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.app.threads.events.debug import (
    EvidenceObservation,
    EvidencePage,
    NativeFrame,
    NativeFramePage,
    ThreadEvidenceNotFoundError,
    ThreadScopeChangedError,
)
from agentplane.app.threads.events.event_log import ThreadNotFoundError
from agentplane.app.threads.models import EventLog
from agentplane.history_service import protocol_pb2 as history_pb2
from agentplane.history_service.client import HistoryServiceClient, HistoryServiceError
from agentplane.proto import command_pb2, event_log_pb2
from agentplane.thread_fold.models import ThreadCheckpoint, ThreadEntity, ThreadEvidence, ThreadNativeLink
from agentplane.thread_fold.views import EntityKind, ThreadCommandEntityView

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/proto:sandbox_service_pb2


class CommandIdConflictError(ValueError):
    """A Thread command id was already admitted with a different immutable Command."""


@dataclass(frozen=True)
class ThreadScope:
    projection_epoch: str
    through_cursor: int


class ContentStore:
    def __init__(self, engine: AsyncEngine, *, history_reader: HistoryServiceClient) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._history_reader = history_reader

    async def current_scope(self, thread_id: UUID) -> ThreadScope | None:
        """The sole epoch scope currently materialized for a Thread."""
        async with self._sessions() as session:
            checkpoint = await session.scalar(select(ThreadCheckpoint).where(ThreadCheckpoint.thread_id == thread_id))
            if checkpoint is None:
                return None
            return ThreadScope(checkpoint.projection_epoch, checkpoint.through_cursor)

    async def evidence(
        self, thread_id: UUID, *, projection_epoch: str, entity_kind: str, entity_id: str, after_cursor: int, limit: int
    ) -> EvidencePage:
        if not 1 <= limit <= 200 or after_cursor < 0:
            raise ValueError("invalid evidence page bounds")
        async with self._sessions() as session:
            entity_cursor = await _evidence_entity_cursor(session, thread_id, projection_epoch, entity_kind, entity_id)
            native_exists = (
                select(ThreadNativeLink.source_sequence)
                .where(
                    ThreadNativeLink.thread_id == ThreadEvidence.thread_id,
                    ThreadNativeLink.projection_epoch == ThreadEvidence.projection_epoch,
                    ThreadNativeLink.entity_cursor == ThreadEvidence.entity_cursor,
                    ThreadNativeLink.observation_cursor == ThreadEvidence.observation_cursor,
                )
                .exists()
            )
            rows = list(
                await session.execute(
                    select(ThreadEvidence.observation_cursor, native_exists)
                    .where(
                        ThreadEvidence.thread_id == thread_id,
                        ThreadEvidence.projection_epoch == projection_epoch,
                        ThreadEvidence.entity_cursor == entity_cursor,
                        ThreadEvidence.observation_cursor > after_cursor,
                    )
                    .order_by(ThreadEvidence.observation_cursor)
                    .limit(limit + 1)
                )
            )
            return EvidencePage(
                observations=[
                    EvidenceObservation(observation_cursor=str(cursor), has_native=native)
                    for cursor, native in rows[:limit]
                ],
                next_after_cursor=str(rows[limit - 1][0]) if len(rows) > limit else None,
            )

    async def native_frames(
        self,
        thread_id: UUID,
        *,
        projection_epoch: str,
        entity_kind: str,
        entity_id: str,
        observation_cursor: int,
        after_sequence: int,
        limit: int,
    ) -> NativeFramePage:
        if not 1 <= limit <= 200 or after_sequence < 0:
            raise ValueError("invalid native frame page bounds")
        async with self._sessions() as session:
            entity_cursor = await _evidence_entity_cursor(session, thread_id, projection_epoch, entity_kind, entity_id)
            association = await session.get(
                ThreadEvidence, (thread_id, projection_epoch, entity_cursor, observation_cursor)
            )
            if association is None:
                raise ThreadEvidenceNotFoundError("no evidence association for the selected observation")
            checkpoint = await session.get(ThreadCheckpoint, thread_id)
            assert checkpoint is not None
            source_id = checkpoint.source_id
            sequences = list(
                await session.scalars(
                    select(ThreadNativeLink.source_sequence)
                    .where(
                        ThreadNativeLink.thread_id == thread_id,
                        ThreadNativeLink.projection_epoch == projection_epoch,
                        ThreadNativeLink.entity_cursor == entity_cursor,
                        ThreadNativeLink.observation_cursor == observation_cursor,
                        ThreadNativeLink.source_sequence > after_sequence,
                    )
                    .order_by(ThreadNativeLink.source_sequence)
                    .limit(limit + 1)
                )
            )
        frames: list[NativeFrame] = []
        for sequence in sequences[:limit]:
            entry = await self._archived_entry(thread_id, sequence)
            if entry is not None and entry.origin.source_id != source_id:
                raise ValueError("service history did not return the requested evidence cursor")
            payload = MessageToDict(entry) if entry is not None and entry.event.HasField("native") else None
            frames.append(
                NativeFrame.model_validate(
                    {
                        "source_sequence": str(sequence),
                        "availability": "present" if payload is not None else "unavailable",
                        "entry": payload,
                    }
                )
            )
        return NativeFramePage(
            frames=frames, next_after_sequence=str(sequences[limit - 1]) if len(sequences) > limit else None
        )

    async def _archived_entry(self, thread_id: UUID, cursor: int) -> event_log_pb2.EventEntry | None:
        # Exact indexed lookup through the history API, never an app raw-table fallback.
        page = await self._history_reader.read_session_events(str(thread_id), after_cursor=cursor - 1, limit=1)
        if not page.entries:
            return None
        entry = page.entries[0]
        if (
            len(page.entries) != 1
            or entry.cursor != cursor
            or entry.origin.sequence != cursor
            or page.last_cursor < cursor
        ):
            raise ValueError("service history did not return the requested evidence cursor")
        return entry

    async def admitted_command(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry | None:
        """The archived admission of this immutable command, if the Thread has one.

        This is deliberately an archive lookup rather than a command outbox. A matching result
        lets a retry recover a lost HTTP response without contacting a possibly deleted Sandbox;
        a reused id with different work is a conflict, never an implicit new command.
        """
        async with self._sessions() as session:
            if await session.get(EventLog, thread_id) is None:
                raise ThreadNotFoundError(thread_id)
            checkpoint = await session.get(ThreadCheckpoint, thread_id)
            if checkpoint is None:
                return None
            summary = await session.get(
                ThreadEntity, (thread_id, checkpoint.projection_epoch, EntityKind.COMMAND, command.command_id)
            )
            if summary is None:
                return None
            cursor = summary.cursor
            source_id = checkpoint.source_id
        entry = await self._archived_entry(thread_id, cursor)
        if entry is None or entry.origin.source_id != source_id:
            raise ValueError("command summary has no archived admission")
        return _admission(entry, command)


def _admission(entry: event_log_pb2.EventEntry, command: command_pb2.Command) -> event_log_pb2.EventEntry:
    if (
        not entry.event.HasField("command_admitted")
        or entry.event.command_admitted.command.command_id != command.command_id
    ):
        raise ValueError("command summary does not point to its archived admission")
    if entry.event.command_admitted.command == command:
        return entry
    raise CommandIdConflictError(f"command id {command.command_id!r} was already admitted with different work")


async def _evidence_entity_cursor(
    session: AsyncSession, thread_id: UUID, projection_epoch: str, entity_kind: str, entity_id: str
) -> int:
    checkpoint = await session.get(ThreadCheckpoint, thread_id)
    if checkpoint is None:
        raise ThreadEvidenceNotFoundError("no materialized thread fold")
    if checkpoint.projection_epoch != projection_epoch:
        raise ThreadScopeChangedError("the thread fold projection epoch has changed")
    entity = await session.get(ThreadEntity, (thread_id, projection_epoch, entity_kind, entity_id))
    if entity is None:
        raise ThreadEvidenceNotFoundError("no selected thread entity")
    return entity.cursor


class HistoryContentStore(ContentStore):
    """The same reads, from the History Service fold rather than the app's projection tables."""

    async def current_scope(self, thread_id: UUID) -> ThreadScope | None:
        try:
            summary = await self._history_reader.get_session_summary(str(thread_id))
        except HistoryServiceError as error:
            if error.code != grpc.StatusCode.NOT_FOUND:
                raise
            return None
        if not summary.HasField("projection_epoch"):
            return None
        return ThreadScope(summary.projection_epoch, summary.folded_through)

    async def evidence(
        self, thread_id: UUID, *, projection_epoch: str, entity_kind: str, entity_id: str, after_cursor: int, limit: int
    ) -> EvidencePage:
        if not 1 <= limit <= 200 or after_cursor < 0:
            raise ValueError("invalid evidence page bounds")
        with _evidence_errors():
            page = await self._history_reader.read_entity_evidence(
                history_pb2.ReadEntityEvidenceRequest(
                    session_id=str(thread_id),
                    projection_epoch=projection_epoch,
                    entity=history_pb2.EntityKey(entity_kind=entity_kind, entity_id=entity_id),
                    after_cursor=after_cursor,
                    limit=limit,
                )
            )
        return EvidencePage(
            observations=[
                EvidenceObservation(observation_cursor=str(row.observation_cursor), has_native=row.has_native)
                for row in page.observations
            ],
            next_after_cursor=str(page.next_after_cursor) if page.HasField("next_after_cursor") else None,
        )

    async def native_frames(
        self,
        thread_id: UUID,
        *,
        projection_epoch: str,
        entity_kind: str,
        entity_id: str,
        observation_cursor: int,
        after_sequence: int,
        limit: int,
    ) -> NativeFramePage:
        if not 1 <= limit <= 200 or after_sequence < 0:
            raise ValueError("invalid native frame page bounds")
        with _evidence_errors():
            page = await self._history_reader.read_native_frames(
                history_pb2.ReadNativeFramesRequest(
                    session_id=str(thread_id),
                    projection_epoch=projection_epoch,
                    entity=history_pb2.EntityKey(entity_kind=entity_kind, entity_id=entity_id),
                    observation_cursor=observation_cursor,
                    after_sequence=after_sequence,
                    limit=limit,
                )
            )
        return NativeFramePage(
            frames=[
                NativeFrame.model_validate(
                    {
                        "source_sequence": str(frame.source_sequence),
                        "availability": "present" if frame.HasField("entry") else "unavailable",
                        "entry": MessageToDict(frame.entry) if frame.HasField("entry") else None,
                    }
                )
                for frame in page.frames
            ],
            next_after_sequence=str(page.next_after_sequence) if page.HasField("next_after_sequence") else None,
        )

    async def admitted_command(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry | None:
        await self._require_thread(thread_id)
        commands = await self._commands(thread_id, [command.command_id])
        if not commands:
            return None
        entry = await self._archived_entry(thread_id, commands[0].cursor)
        if entry is None:
            raise ValueError("command summary has no archived admission")
        return _admission(entry, command)

    async def _require_thread(self, thread_id: UUID) -> None:
        async with self._sessions() as session:
            if await session.get(EventLog, thread_id) is None:
                raise ThreadNotFoundError(thread_id)

    async def _commands(self, thread_id: UUID, command_ids: Sequence[str]) -> list[ThreadCommandEntityView] | None:
        """The commands' entities in the current projection epoch; None when there is none."""
        try:
            page = await self._history_reader.read_thread_entities(
                history_pb2.ReadThreadEntitiesRequest(
                    session_id=str(thread_id),
                    keys=history_pb2.EntityKeys(
                        keys=[
                            history_pb2.EntityKey(entity_kind=EntityKind.COMMAND, entity_id=command_id)
                            for command_id in command_ids
                        ]
                    ),
                )
            )
        except HistoryServiceError as error:
            if error.code not in {grpc.StatusCode.FAILED_PRECONDITION, grpc.StatusCode.NOT_FOUND}:
                raise
            return None
        return [ThreadCommandEntityView.model_validate_json(entity.view_json) for entity in page.entities]


@contextmanager
def _evidence_errors() -> Iterator[None]:
    try:
        yield
    except HistoryServiceError as error:
        if error.code == grpc.StatusCode.FAILED_PRECONDITION:
            raise ThreadScopeChangedError("the thread fold projection epoch has changed") from error
        if error.code == grpc.StatusCode.NOT_FOUND:
            raise ThreadEvidenceNotFoundError("no selected thread entity") from error
        raise
