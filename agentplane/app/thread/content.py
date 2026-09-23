"""Reads of a thread's content as the fold assembled it: the scope it has materialized, windows
and payloads within that scope, the evidence behind an entity, and command outcomes.

These run in the caller's session; `ThreadStore` owns the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from google.protobuf.json_format import ParseDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.app import thread_fold
from agentplane.app.thread.event_log import ThreadNotFoundError
from agentplane.app.thread.models import (
    Event,
    EventLog,
    ThreadCheckpoint,
    ThreadEntity,
    ThreadEvidence,
    ThreadNativeLink,
    ThreadPayloadManifest,
)
from agentplane.app.thread.views import SEGMENT_KINDS, EntityKind, ThreadCommandState
from agentplane.app.thread_debug import (
    EvidenceObservation,
    EvidencePage,
    NativeFrame,
    NativeFramePage,
    ThreadEvidenceNotFoundError,
    ThreadScopeChangedError,
)
from agentplane.protocol import command_pb2, event_log_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class ThreadInterestExpiredError(ValueError):
    """A bounded browser interest must be resolved again at the current projection position."""


class ThreadScopeResetError(ValueError):
    """A browser's retained projection source or epoch is no longer current."""


class CommandIdConflictError(ValueError):
    """A Thread command id was already admitted with a different immutable Command."""


@dataclass(frozen=True)
class ThreadScope:
    projection_epoch: str
    through_cursor: int


@dataclass(frozen=True)
class ThreadEntityInterest:
    scope: ThreadScope
    anchor_cursor: int
    tail_from: int
    window_from: int | None = None
    window_before: int | None = None


@dataclass(frozen=True)
class ThreadPayloadSelection:
    scope: ThreadScope
    owner_cursor: int
    owner_id: str
    field: str
    generation: int
    revision_cursor: int
    chunk_count: int
    content_bytes: int


async def current_scope(session: AsyncSession, thread_id: UUID) -> ThreadScope | None:
    """The sole epoch scope currently materialized for a Thread."""
    checkpoint = await session.scalar(select(ThreadCheckpoint).where(ThreadCheckpoint.thread_id == thread_id))
    if checkpoint is None:
        return None
    return ThreadScope(checkpoint.projection_epoch, checkpoint.through_cursor)


async def entity_interest(
    session: AsyncSession,
    thread_id: UUID,
    *,
    anchor_cursor: int | None = None,
    before_cursor: int | None = None,
    page_size: int = 30,
) -> ThreadEntityInterest | None:
    if not 1 <= page_size <= 100:
        raise ValueError("entity page size must be between 1 and 100")
    checkpoint = await session.scalar(select(ThreadCheckpoint).where(ThreadCheckpoint.thread_id == thread_id))
    if checkpoint is None:
        return None
    scope = ThreadScope(checkpoint.projection_epoch, checkpoint.through_cursor)
    anchor = scope.through_cursor if anchor_cursor is None else anchor_cursor
    if anchor < 0 or anchor > scope.through_cursor:
        raise ValueError("anchor is outside the projected prefix")
    common = (
        ThreadEntity.thread_id == thread_id,
        ThreadEntity.projection_epoch == scope.projection_epoch,
        ThreadEntity.entity_kind.in_(SEGMENT_KINDS),
    )
    if anchor_cursor is not None:
        newer = list(
            await session.scalars(
                select(ThreadEntity.cursor)
                .where(*common, ThreadEntity.cursor > anchor)
                .order_by(ThreadEntity.cursor)
                .limit(page_size * 2 + 1)
            )
        )
        if len(newer) > page_size * 2:
            raise ThreadInterestExpiredError("entity interest must rotate")

    async def lower(before: int) -> int:
        cursors = list(
            await session.scalars(
                select(ThreadEntity.cursor)
                .where(*common, ThreadEntity.cursor < before)
                .order_by(ThreadEntity.cursor.desc())
                .limit(page_size)
            )
        )
        return cursors[-1] if cursors else before

    tail_from = await lower(anchor + 1)
    if before_cursor is None:
        return ThreadEntityInterest(scope, anchor, tail_from)
    if before_cursor < 0:
        raise ValueError("before cursor cannot be negative")
    return ThreadEntityInterest(scope, anchor, tail_from, await lower(before_cursor), before_cursor)


async def payload_selection(
    session: AsyncSession,
    thread_id: UUID,
    *,
    owner_cursor: int,
    owner_id: str,
    field: str,
    generation: int,
    revision_cursor: int,
) -> ThreadPayloadSelection | None:
    checkpoint = await session.scalar(select(ThreadCheckpoint).where(ThreadCheckpoint.thread_id == thread_id))
    if checkpoint is None:
        return None
    manifest = await session.scalar(
        select(ThreadPayloadManifest).where(
            ThreadPayloadManifest.thread_id == thread_id,
            ThreadPayloadManifest.projection_epoch == checkpoint.projection_epoch,
            ThreadPayloadManifest.owner_cursor == owner_cursor,
            ThreadPayloadManifest.owner_id == owner_id,
            ThreadPayloadManifest.field == field,
            ThreadPayloadManifest.generation == generation,
            ThreadPayloadManifest.revision_cursor == revision_cursor,
        )
    )
    if manifest is None:
        return None
    return ThreadPayloadSelection(
        ThreadScope(manifest.projection_epoch, checkpoint.through_cursor),
        owner_cursor,
        owner_id,
        field,
        generation,
        revision_cursor,
        manifest.chunk_count,
        manifest.content_bytes,
    )


async def evidence(
    session: AsyncSession,
    thread_id: UUID,
    *,
    projection_epoch: str,
    entity_kind: str,
    entity_id: str,
    after_cursor: int,
    limit: int,
) -> EvidencePage:
    if not 1 <= limit <= 200 or after_cursor < 0:
        raise ValueError("invalid evidence page bounds")
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
            EvidenceObservation(observation_cursor=str(cursor), has_native=native) for cursor, native in rows[:limit]
        ],
        next_after_cursor=str(rows[limit - 1][0]) if len(rows) > limit else None,
    )


async def native_frames(
    session: AsyncSession,
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
    entity_cursor = await _evidence_entity_cursor(session, thread_id, projection_epoch, entity_kind, entity_id)
    association = await session.get(ThreadEvidence, (thread_id, projection_epoch, entity_cursor, observation_cursor))
    if association is None:
        raise ThreadEvidenceNotFoundError("no evidence association for the selected observation")
    rows = list(
        await session.execute(
            select(ThreadNativeLink.source_sequence, Event.payload)
            .outerjoin(
                Event,
                (
                    (Event.thread_id == ThreadNativeLink.thread_id)
                    & (Event.cursor == ThreadNativeLink.source_sequence)
                    & (Event.kind == "native")
                ),
            )
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
    return NativeFramePage(
        frames=[
            NativeFrame.model_validate(
                {
                    "source_sequence": str(sequence),
                    "availability": "present" if payload is not None else "unavailable",
                    "entry": payload,
                }
            )
            for sequence, payload in rows[:limit]
        ],
        next_after_sequence=str(rows[limit - 1][0]) if len(rows) > limit else None,
    )


async def command_outcomes(
    session: AsyncSession, thread_id: UUID, projection_epoch: str, command_ids: Sequence[str]
) -> dict[str, thread_fold.CommandOutcome | None]:
    """Current outcomes for a finite browser-held command-id set, keyed by entity primary key."""
    requested = tuple(dict.fromkeys(command_ids))
    if await session.get(EventLog, thread_id) is None:
        raise ThreadNotFoundError(thread_id)
    checkpoint = await session.get(ThreadCheckpoint, thread_id)
    if checkpoint is None or checkpoint.projection_epoch != projection_epoch:
        raise ThreadScopeResetError("thread fold scope was reset")
    rows = await session.scalars(
        select(ThreadEntity).where(
            ThreadEntity.thread_id == thread_id,
            ThreadEntity.projection_epoch == projection_epoch,
            ThreadEntity.entity_kind == EntityKind.COMMAND,
            ThreadEntity.entity_id.in_(requested),
        )
    )
    outcomes = {row.entity_id: ThreadCommandState.model_validate(row.state).outcome for row in rows}
    return {command_id: outcomes.get(command_id) for command_id in requested}


async def admitted_command(
    session: AsyncSession, thread_id: UUID, command: command_pb2.Command
) -> event_log_pb2.EventEntry | None:
    """The archived admission of this immutable command, if the Thread has one.

    This is deliberately an archive lookup rather than a command outbox. A matching result
    lets a retry recover a lost HTTP response without contacting a possibly deleted Sandbox;
    a reused id with different work is a conflict, never an implicit new command.
    """
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
    payload = await session.scalar(
        select(Event.payload).where(Event.thread_id == thread_id, Event.cursor == summary.cursor)
    )
    if payload is None:
        raise ValueError("command summary has no archived admission")
    entry = ParseDict(payload, event_log_pb2.EventEntry())
    if (
        not entry.event.HasField("command_admitted")
        or entry.event.command_admitted.command.command_id != command.command_id
    ):
        raise ValueError("command summary does not point to its archived admission")
    admitted = entry.event.command_admitted.command
    if admitted == command:
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
