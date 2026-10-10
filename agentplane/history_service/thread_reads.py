"""Entity windows and payload bodies from the History Service fold, scoped by projection epoch."""

from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.history_service import protocol_pb2
from agentplane.sandbox_service.session_history.db import SessionHistory
from agentplane.sandbox_service.session_history.store import HistoryNotFoundError
from agentplane.thread_fold.models import ThreadCheckpoint, ThreadEntity, ThreadPayloadChunk, ThreadPayloadManifest
from agentplane.thread_fold.views import ThreadEntityView

# gazelle:include_dep @pypi//protobuf

MAX_ENTITIES = 200
MAX_CHUNKS = 64

_VIEW: TypeAdapter[ThreadEntityView] = TypeAdapter(ThreadEntityView)


class ThreadScopeResetError(Exception):
    """The caller's projection epoch is no longer the Session's current scope."""


class PayloadNotFoundError(LookupError):
    pass


class ThreadReader:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    async def entities(
        self, request: protocol_pb2.ReadThreadEntitiesRequest
    ) -> protocol_pb2.ReadThreadEntitiesResponse:
        session_id = UUID(request.session_id)
        match request.WhichOneof("window"):
            case "range" if 1 <= request.range.limit <= MAX_ENTITIES:
                window = ThreadEntity.entity_index.between(
                    request.range.start_entity_index, request.range.start_entity_index + request.range.limit - 1
                )
            case "keys" if 1 <= len(request.keys.keys) <= MAX_ENTITIES:
                window = tuple_(ThreadEntity.entity_kind, ThreadEntity.entity_id).in_(
                    [(key.entity_kind, key.entity_id) for key in request.keys.keys]
                )
            case _:
                raise ValueError("invalid entity window")
        async with self._sessions() as session:
            checkpoint = await _checkpoint(session, session_id, request.projection_epoch)
            if checkpoint is None:
                return protocol_pb2.ReadThreadEntitiesResponse()
            rows = await session.scalars(
                select(ThreadEntity)
                .where(
                    ThreadEntity.thread_id == session_id,
                    ThreadEntity.projection_epoch == checkpoint.projection_epoch,
                    window,
                )
                .order_by(ThreadEntity.entity_index)
            )
            return protocol_pb2.ReadThreadEntitiesResponse(
                projection_epoch=checkpoint.projection_epoch,
                through_cursor=checkpoint.through_cursor,
                entities=[
                    protocol_pb2.ThreadEntity(entity_index=row.entity_index, view_json=_view_json(row)) for row in rows
                ],
            )

    async def payload(self, request: protocol_pb2.ReadPayloadRequest) -> protocol_pb2.ReadPayloadResponse:
        if not 1 <= request.limit <= MAX_CHUNKS:
            raise ValueError("invalid payload chunk window")
        session_id = UUID(request.session_id)
        ref = request.ref
        async with self._sessions() as session:
            if await _checkpoint(session, session_id, ref.projection_epoch) is None:
                raise PayloadNotFoundError(session_id)
            manifest = await session.get(
                ThreadPayloadManifest,
                (
                    session_id,
                    ref.projection_epoch,
                    ref.owner_cursor,
                    ref.owner_id,
                    ref.field,
                    ref.generation,
                    ref.revision_cursor,
                ),
            )
            if manifest is None:
                raise PayloadNotFoundError(session_id)
            # Chunks are shared by every revision of a generation; a revision spans its first chunk_count.
            chunks = await session.scalars(
                select(ThreadPayloadChunk.text)
                .where(
                    ThreadPayloadChunk.thread_id == session_id,
                    ThreadPayloadChunk.projection_epoch == ref.projection_epoch,
                    ThreadPayloadChunk.owner_cursor == ref.owner_cursor,
                    ThreadPayloadChunk.owner_id == ref.owner_id,
                    ThreadPayloadChunk.field == ref.field,
                    ThreadPayloadChunk.generation == ref.generation,
                    ThreadPayloadChunk.chunk_index >= request.start_chunk,
                    ThreadPayloadChunk.chunk_index < min(manifest.chunk_count, request.start_chunk + request.limit),
                )
                .order_by(ThreadPayloadChunk.chunk_index)
            )
            return protocol_pb2.ReadPayloadResponse(
                chunk_count=manifest.chunk_count, content_bytes=manifest.content_bytes, chunks=list(chunks)
            )


async def _checkpoint(session: AsyncSession, session_id: UUID, projection_epoch: str) -> ThreadCheckpoint | None:
    """The Session's current scope, None before anything is folded; a stale caller epoch raises."""
    if await session.get(SessionHistory, session_id) is None:
        raise HistoryNotFoundError(session_id)
    checkpoint = await session.get(ThreadCheckpoint, session_id)
    if projection_epoch and (checkpoint is None or checkpoint.projection_epoch != projection_epoch):
        raise ThreadScopeResetError(session_id)
    return checkpoint


def _view_json(row: ThreadEntity) -> str:
    return _VIEW.dump_json(
        _VIEW.validate_python(
            {
                "thread_id": row.thread_id,
                "projection_epoch": row.projection_epoch,
                "entity_kind": row.entity_kind,
                "entity_id": row.entity_id,
                "cursor": row.cursor,
                "revision_cursor": row.revision_cursor,
                "pending": row.pending,
                "turn_id": row.turn_id,
                "state": row.state,
                "text_ref": row.text_ref,
                "arguments_ref": row.arguments_ref,
                "output_ref": row.output_ref,
                "input_ref": row.input_ref,
            }
        )
    ).decode()
