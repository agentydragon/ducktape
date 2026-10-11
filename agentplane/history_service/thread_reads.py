"""Entity windows from the History Service fold, scoped by projection epoch."""

from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.history_service import protocol_pb2
from agentplane.history_service.storage.db import SessionHistory
from agentplane.history_service.storage.store import HistoryNotFoundError
from agentplane.thread_fold.models import ThreadCheckpoint, ThreadEntity
from agentplane.thread_fold.views import ThreadEntityView

# gazelle:include_dep @pypi//protobuf

MAX_ENTITIES = 200

_VIEW: TypeAdapter[ThreadEntityView] = TypeAdapter(ThreadEntityView)


class ThreadScopeResetError(Exception):
    """The caller's projection epoch is no longer the Session's current scope."""


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
