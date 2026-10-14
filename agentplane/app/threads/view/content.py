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
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.threads.events.debug import (
    EvidenceObservation,
    EvidencePage,
    NativeFrame,
    NativeFramePage,
    ThreadEvidenceNotFoundError,
    ThreadScopeChangedError,
)
from agentplane.app.threads.events.event_log import ThreadNotFoundError
from agentplane.app.threads.models import Thread
from agentplane.grpc_calls import ServiceError
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.fold.views import EntityKind, ThreadCommandEntityView
from agentplane.proto import command_pb2, event_log_pb2, history_service_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class CommandIdConflictError(ValueError):
    """A Thread command id was already admitted with a different immutable Command."""


@dataclass(frozen=True)
class ThreadScope:
    projection_epoch: str
    through_cursor: int


class ContentStore:
    """The History Service fold's reads, for the Threads the app links to a Session."""

    def __init__(self, engine: AsyncEngine, *, history_reader: HistoryServiceClient) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._history_reader = history_reader

    async def current_scope(self, thread_id: UUID) -> ThreadScope | None:
        try:
            summary = await self._history_reader.get_session_summary(str(thread_id))
        except ServiceError as error:
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
                history_service_pb2.ReadEntityEvidenceRequest(
                    session_id=str(thread_id),
                    projection_epoch=projection_epoch,
                    entity=history_service_pb2.EntityKey(entity_kind=entity_kind, entity_id=entity_id),
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
                history_service_pb2.ReadNativeFramesRequest(
                    session_id=str(thread_id),
                    projection_epoch=projection_epoch,
                    entity=history_service_pb2.EntityKey(entity_kind=entity_kind, entity_id=entity_id),
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
            if await session.get(Thread, thread_id) is None:
                raise ThreadNotFoundError(thread_id)

    async def _commands(self, thread_id: UUID, command_ids: Sequence[str]) -> list[ThreadCommandEntityView] | None:
        """The commands' entities in the current projection epoch; None when there is none."""
        try:
            page = await self._history_reader.read_thread_entities(
                history_service_pb2.ReadThreadEntitiesRequest(
                    session_id=str(thread_id),
                    keys=history_service_pb2.EntityKeys(
                        keys=[
                            history_service_pb2.EntityKey(entity_kind=EntityKind.COMMAND, entity_id=command_id)
                            for command_id in command_ids
                        ]
                    ),
                )
            )
        except ServiceError as error:
            if error.code not in {grpc.StatusCode.FAILED_PRECONDITION, grpc.StatusCode.NOT_FOUND}:
                raise
            return None
        return [ThreadCommandEntityView.model_validate_json(entity.view_json) for entity in page.entities]

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


def _admission(entry: event_log_pb2.EventEntry, command: command_pb2.Command) -> event_log_pb2.EventEntry:
    if (
        not entry.event.HasField("command_admitted")
        or entry.event.command_admitted.command.command_id != command.command_id
    ):
        raise ValueError("command summary does not point to its archived admission")
    if entry.event.command_admitted.command == command:
        return entry
    raise CommandIdConflictError(f"command id {command.command_id!r} was already admitted with different work")


@contextmanager
def _evidence_errors() -> Iterator[None]:
    try:
        yield
    except ServiceError as error:
        if error.code == grpc.StatusCode.FAILED_PRECONDITION:
            raise ThreadScopeChangedError("the thread fold projection epoch has changed") from error
        if error.code == grpc.StatusCode.NOT_FOUND:
            raise ThreadEvidenceNotFoundError("no selected thread entity") from error
        raise
