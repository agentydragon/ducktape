"""Session summaries from the History Service fold, paged newest first under stable tokens."""

from datetime import UTC
from typing import cast
from uuid import UUID

from google.protobuf.json_format import ParseDict
from google.protobuf.message import DecodeError
from sqlalchemy import ColumnElement, Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.history_service import protocol_pb2
from agentplane.protocol import event_pb2
from agentplane.sandbox_service.session_history.db import SessionHistory
from agentplane.sandbox_service.session_history.store import HistoryNotFoundError
from agentplane.thread_fold.models import ThreadCheckpoint, ThreadHistorySummary

MAX_PAGE = 200


class SummaryReader:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    async def list_page(
        self, request: protocol_pb2.ListSessionSummariesRequest
    ) -> protocol_pb2.ListSessionSummariesResponse:
        if not 1 <= request.limit <= MAX_PAGE:
            raise ValueError("invalid summary page size")
        if request.HasField("sandbox_namespace") != request.HasField("sandbox_name"):
            raise ValueError("a Sandbox filter names both its namespace and name")
        query = _summaries()
        if request.HasField("sandbox_name"):
            query = query.where(
                SessionHistory.sandbox_namespace == request.sandbox_namespace,
                SessionHistory.sandbox_name == request.sandbox_name,
            )
        if request.page_token:
            query = query.where(_after(_token(request)))
        async with self._sessions() as session:
            rows = list(
                await session.execute(
                    query.order_by(
                        ThreadHistorySummary.first_event_at.desc().nulls_first(), SessionHistory.id.desc()
                    ).limit(request.limit + 1)
                )
            )
        response = protocol_pb2.ListSessionSummariesResponse(
            summaries=[_summary(*row) for row in rows[: request.limit]]
        )
        if len(rows) > request.limit:
            last = response.summaries[-1]
            token = protocol_pb2.SessionSummaryPageToken(session_id=last.session_id)
            if request.HasField("sandbox_name"):
                token.sandbox_namespace = request.sandbox_namespace
                token.sandbox_name = request.sandbox_name
            if last.HasField("first_event_at"):
                token.first_event_at.CopyFrom(last.first_event_at)
            response.next_page_token = token.SerializeToString()
        return response

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


def _token(request: protocol_pb2.ListSessionSummariesRequest) -> protocol_pb2.SessionSummaryPageToken:
    try:
        token = protocol_pb2.SessionSummaryPageToken.FromString(request.page_token)
    except DecodeError as error:
        raise ValueError("malformed summary page token") from error
    # A token continues only the listing it was issued for, never a wider one.
    if (token.HasField("sandbox_namespace"), token.sandbox_namespace, token.sandbox_name) != (
        request.HasField("sandbox_namespace"),
        request.sandbox_namespace,
        request.sandbox_name,
    ):
        raise ValueError("summary page token was issued for another filter")
    return token


def _after(token: protocol_pb2.SessionSummaryPageToken) -> ColumnElement[bool]:
    session_id = UUID(token.session_id)
    if not token.HasField("first_event_at"):
        return or_(
            ThreadHistorySummary.first_event_at.is_not(None),
            and_(ThreadHistorySummary.first_event_at.is_(None), SessionHistory.id < session_id),
        )
    at = token.first_event_at.ToDatetime(tzinfo=UTC)
    return or_(
        ThreadHistorySummary.first_event_at < at,
        and_(ThreadHistorySummary.first_event_at == at, SessionHistory.id < session_id),
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
