"""The raw observations behind a folded entity, and the native harness frames each one carries."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.history_service import protocol_pb2
from agentplane.history_service.thread_reads import ThreadScopeResetError
from agentplane.sandbox_service.session_history.store import Store
from agentplane.thread_fold.models import ThreadCheckpoint, ThreadEntity, ThreadEvidence, ThreadNativeLink

# gazelle:include_dep @pypi//protobuf

MAX_PAGE = 200


class EvidenceNotFoundError(LookupError):
    pass


class EvidenceReader:
    def __init__(self, engine: AsyncEngine, history: Store) -> None:
        self._sessions = async_sessionmaker(engine)
        self._history = history

    async def evidence(
        self, request: protocol_pb2.ReadEntityEvidenceRequest
    ) -> protocol_pb2.ReadEntityEvidenceResponse:
        if not 1 <= request.limit <= MAX_PAGE:
            raise ValueError("invalid evidence page size")
        session_id = UUID(request.session_id)
        async with self._sessions() as session:
            entity_cursor, _ = await _entity(session, session_id, request.projection_epoch, request.entity)
            has_native = (
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
                    select(ThreadEvidence.observation_cursor, has_native)
                    .where(
                        ThreadEvidence.thread_id == session_id,
                        ThreadEvidence.projection_epoch == request.projection_epoch,
                        ThreadEvidence.entity_cursor == entity_cursor,
                        ThreadEvidence.observation_cursor > request.after_cursor,
                    )
                    .order_by(ThreadEvidence.observation_cursor)
                    .limit(request.limit + 1)
                )
            )
        response = protocol_pb2.ReadEntityEvidenceResponse(
            observations=[
                protocol_pb2.EvidenceObservation(observation_cursor=cursor, has_native=native)
                for cursor, native in rows[: request.limit]
            ]
        )
        if len(rows) > request.limit:
            response.next_after_cursor = rows[request.limit - 1][0]
        return response

    async def native_frames(
        self, request: protocol_pb2.ReadNativeFramesRequest
    ) -> protocol_pb2.ReadNativeFramesResponse:
        if not 1 <= request.limit <= MAX_PAGE:
            raise ValueError("invalid native frame page size")
        session_id = UUID(request.session_id)
        async with self._sessions() as session:
            entity_cursor, source_id = await _entity(session, session_id, request.projection_epoch, request.entity)
            key = (session_id, request.projection_epoch, entity_cursor, request.observation_cursor)
            if await session.get(ThreadEvidence, key) is None:
                raise EvidenceNotFoundError(session_id)
            sequences = list(
                await session.scalars(
                    select(ThreadNativeLink.source_sequence)
                    .where(
                        ThreadNativeLink.thread_id == session_id,
                        ThreadNativeLink.projection_epoch == request.projection_epoch,
                        ThreadNativeLink.entity_cursor == entity_cursor,
                        ThreadNativeLink.observation_cursor == request.observation_cursor,
                        ThreadNativeLink.source_sequence > request.after_sequence,
                    )
                    .order_by(ThreadNativeLink.source_sequence)
                    .limit(request.limit + 1)
                )
            )
        response = protocol_pb2.ReadNativeFramesResponse()
        for sequence in sequences[: request.limit]:
            frame = response.frames.add(source_sequence=sequence)
            page = await self._history.read_page(session_id, after_cursor=sequence - 1, limit=1)
            entry = page.entries[0] if page.entries else None
            if entry is not None and (entry.cursor != sequence or entry.origin.source_id != source_id):
                raise ValueError("retained history does not match the folded native link")
            if entry is not None and entry.event.HasField("native"):
                frame.entry.CopyFrom(entry)
        if len(sequences) > request.limit:
            response.next_after_sequence = sequences[request.limit - 1]
        return response


async def _entity(
    session: AsyncSession, session_id: UUID, projection_epoch: str, key: protocol_pb2.EntityKey
) -> tuple[int, str]:
    """The entity's cursor and the Session's runner source, in the caller's still-current epoch."""
    checkpoint = await session.get(ThreadCheckpoint, session_id)
    if checkpoint is None:
        raise EvidenceNotFoundError(session_id)
    if checkpoint.projection_epoch != projection_epoch:
        raise ThreadScopeResetError(session_id)
    entity = await session.get(ThreadEntity, (session_id, projection_epoch, key.entity_kind, key.entity_id))
    if entity is None:
        raise EvidenceNotFoundError(session_id)
    return entity.cursor, checkpoint.source_id
