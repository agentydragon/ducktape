"""Session summaries from the History Service fold."""

from collections.abc import Set
from typing import cast
from uuid import UUID

from google.protobuf.json_format import ParseDict
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.history_service import protocol_pb2
from agentplane.history_service.storage.db import SessionHistory
from agentplane.history_service.storage.store import HistoryNotFoundError
from agentplane.proto import event_pb2
from agentplane.thread_fold.models import ThreadCheckpoint, ThreadHistorySummary

MAX_BATCH = 200


class SummaryReader:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    async def get_many(self, session_ids: Set[UUID]) -> list[protocol_pb2.SessionSummary]:
        if len(session_ids) > MAX_BATCH:
            raise ValueError("too many Sessions in one summary read")
        async with self._sessions() as session:
            rows = await session.execute(_summaries().where(SessionHistory.id.in_(session_ids)))
        return [_summary(*row) for row in rows]

    async def get(self, session_id: UUID) -> protocol_pb2.SessionSummary:
        async with self._sessions() as session:
            row = (await session.execute(_summaries().where(SessionHistory.id == session_id))).one_or_none()
        if row is None:
            raise HistoryNotFoundError(session_id)
        return _summary(*row)


def _summaries() -> Select[tuple[SessionHistory, ThreadHistorySummary, ThreadCheckpoint]]:
    # Outer joins: a Session the fold has not discovered yet has neither row.
    return (
        select(SessionHistory, ThreadHistorySummary, ThreadCheckpoint)
        .outerjoin(ThreadHistorySummary, ThreadHistorySummary.thread_id == SessionHistory.id)
        .outerjoin(ThreadCheckpoint, ThreadCheckpoint.thread_id == SessionHistory.id)
    )


def _summary(
    history: SessionHistory, summary: ThreadHistorySummary | None, checkpoint: ThreadCheckpoint | None
) -> protocol_pb2.SessionSummary:
    message = protocol_pb2.SessionSummary(
        session_id=str(history.id),
        sandbox_namespace=history.sandbox_namespace,
        sandbox_name=history.sandbox_name,
        committed_cursor=history.last_cursor,
    )
    if checkpoint is not None:
        message.projection_epoch = checkpoint.projection_epoch
        message.folded_through = checkpoint.through_cursor
    if summary is None:
        return message
    if summary.first_event_at is not None:
        message.first_event_at.FromDatetime(summary.first_event_at)
    if summary.last_event_at is not None:
        message.last_event_at.FromDatetime(summary.last_event_at)
    if summary.last_model_activity_at is not None:
        message.last_model_activity_at.FromDatetime(summary.last_model_activity_at)
    if summary.last_turn_status is not None:
        message.last_turn_status = cast(event_pb2.TurnStatus, summary.last_turn_status)
    if summary.attached is not None:
        ParseDict(summary.attached, message.attached)
        message.feed_status = (
            protocol_pb2.FEED_STATUS_ACTIVE
            if summary.end is None
            else protocol_pb2.FEED_STATUS_FAILED
            if summary.end
            else protocol_pb2.FEED_STATUS_ENDED
        )
        if summary.end:
            message.feed_error = summary.end["message"]
    return message
