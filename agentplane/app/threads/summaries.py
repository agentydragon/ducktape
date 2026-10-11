"""A Thread's fold summary, from the app's own projection or the History Service's.

The `thread_reads` setting picks one for a whole replica.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import grpc
from google.protobuf.json_format import ParseDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.database_updates import Channel, notify
from agentplane.history_service import protocol_pb2 as history_pb2
from agentplane.history_service.client import HistoryServiceClient, HistoryServiceError
from agentplane.proto import runner_pb2
from agentplane.thread_fold.models import ThreadCheckpoint, ThreadHistorySummary

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

SUMMARY_PAGE = 200


@dataclass(frozen=True)
class FeedEnd:
    pass


@dataclass(frozen=True)
class FeedError:
    message: str


@dataclass(frozen=True)
class FeedSnapshot:
    attached: runner_pb2.Attached
    end: FeedEnd | FeedError | None


@dataclass(frozen=True)
class FoldSummary:
    folded_through: int
    last_event_at: datetime | None
    last_model_activity_at: datetime | None
    # A `TurnStatus` number, kept open as the fold stores it.
    last_turn_status: int | None
    # None until a runner attachment is folded.
    feed: FeedSnapshot | None


NOTHING_FOLDED = FoldSummary(
    folded_through=0, last_event_at=None, last_model_activity_at=None, last_turn_status=None, feed=None
)


class AppSummaries:
    """The app projector's summary rows; a Thread the app has no row for has no summary."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def get(self, thread_id: UUID) -> FoldSummary | None:
        return (await self.many({thread_id})).get(thread_id)

    async def many(self, thread_ids: Collection[UUID]) -> dict[UUID, FoldSummary]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(ThreadHistorySummary, ThreadCheckpoint.through_cursor)
                .outerjoin(ThreadCheckpoint, ThreadCheckpoint.thread_id == ThreadHistorySummary.thread_id)
                .where(ThreadHistorySummary.thread_id.in_(thread_ids))
            )
            return {summary.thread_id: _app_summary(summary, cursor) for summary, cursor in rows}

    async def folded_through(self, thread_id: UUID) -> int:
        """The checkpoint's cursor, which a Thread can have before its summary row."""
        async with self._sessions() as session:
            return (
                await session.scalar(
                    select(ThreadCheckpoint.through_cursor).where(ThreadCheckpoint.thread_id == thread_id)
                )
            ) or 0

    async def resume_pending(self, thread_id: UUID) -> None:
        """A runner-confirmed restart supersedes a normal terminal feed, not a replay error.

        The ingester still owns the archived cursor and the next attached snapshot; this only
        prevents an SSE reader from mistaking the previous incarnation's end for the new one.
        """
        async with self._sessions.begin() as session:
            state = await session.get(ThreadHistorySummary, thread_id, with_for_update=True)
            if state is not None and state.end == {}:
                state.end = None
                if state.attached is not None:
                    state.resumed_after_cursor = ParseDict(state.attached, runner_pb2.Attached()).last_cursor
                await notify(session, Channel.THREADS)


class HistorySummaries:
    """The History Service fold's summaries, with the app's own record of confirmed resumes.

    A Session the History Service has not folded yet reads as nothing folded.
    """

    def __init__(self, engine: AsyncEngine, history: HistoryServiceClient) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._history = history

    async def get(self, thread_id: UUID) -> FoldSummary:
        try:
            summary = await self._history.get_session_summary(str(thread_id))
        except HistoryServiceError as error:
            if error.code != grpc.StatusCode.NOT_FOUND:
                raise
            return NOTHING_FOLDED
        return _history_summary(summary, (await self._resumes({thread_id})).get(thread_id))

    async def many(self, thread_ids: Collection[UUID]) -> dict[UUID, FoldSummary]:
        # Pages every Session's summary: there is no batch read yet, and Thread lists are unpaged.
        request = history_pb2.ListSessionSummariesRequest(limit=SUMMARY_PAGE)
        listed: dict[UUID, history_pb2.SessionSummary] = {}
        while True:
            page = await self._history.list_session_summaries(request)
            # A Session whose history begins while paging can be listed twice; the later copy is newer.
            listed.update((UUID(summary.session_id), summary) for summary in page.summaries)
            if not page.next_page_token:
                break
            request.page_token = page.next_page_token
        resumes = await self._resumes(thread_ids)
        return {
            thread_id: _history_summary(listed[thread_id], resumes.get(thread_id))
            if thread_id in listed
            else NOTHING_FOLDED
            for thread_id in thread_ids
        }

    async def folded_through(self, thread_id: UUID) -> int:
        return (await self.get(thread_id)).folded_through

    async def resume_pending(self, thread_id: UUID) -> None:
        """Mask a normal terminal feed until the fold attaches past it, as the app projector does."""
        summary = await self.get(thread_id)
        if summary.feed is None or not isinstance(summary.feed.end, FeedEnd):
            return
        async with self._sessions.begin() as session:
            await session.execute(
                insert(ThreadHistorySummary)
                .values(thread_id=thread_id, resumed_after_cursor=summary.feed.attached.last_cursor)
                .on_conflict_do_update(
                    index_elements=[ThreadHistorySummary.thread_id],
                    set_={"resumed_after_cursor": summary.feed.attached.last_cursor},
                )
            )
            await notify(session, Channel.THREADS)

    async def _resumes(self, thread_ids: Collection[UUID]) -> dict[UUID, int]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(ThreadHistorySummary.thread_id, ThreadHistorySummary.resumed_after_cursor).where(
                    ThreadHistorySummary.thread_id.in_(thread_ids)
                )
            )
            return {row.thread_id: cursor for row in rows if (cursor := row.resumed_after_cursor) is not None}


def _app_summary(summary: ThreadHistorySummary, through_cursor: int | None) -> FoldSummary:
    feed = None
    if summary.attached is not None:
        end = None if summary.end is None else FeedError(summary.end["message"]) if summary.end else FeedEnd()
        feed = FeedSnapshot(ParseDict(summary.attached, runner_pb2.Attached()), end)
    return FoldSummary(
        folded_through=through_cursor or 0,
        last_event_at=summary.last_event_at,
        last_model_activity_at=summary.last_model_activity_at,
        last_turn_status=summary.last_turn_status,
        feed=feed,
    )


def _history_summary(summary: history_pb2.SessionSummary, resumed_after_cursor: int | None) -> FoldSummary:
    feed = None
    if summary.HasField("attached"):
        end: FeedEnd | FeedError | None
        match summary.feed_status:
            case history_pb2.FEED_STATUS_ACTIVE:
                end = None
            case history_pb2.FEED_STATUS_ENDED:
                end = FeedEnd()
            case history_pb2.FEED_STATUS_FAILED:
                end = FeedError(summary.feed_error)
            case status:
                raise ValueError(f"attached Session summary without a feed status: {status=}")
        if resumed_after_cursor is not None and summary.attached.last_cursor <= resumed_after_cursor:
            end = None
        feed = FeedSnapshot(summary.attached, end)
    return FoldSummary(
        folded_through=summary.folded_through,
        last_event_at=summary.last_event_at.ToDatetime(tzinfo=UTC) if summary.HasField("last_event_at") else None,
        last_model_activity_at=summary.last_model_activity_at.ToDatetime(tzinfo=UTC)
        if summary.HasField("last_model_activity_at")
        else None,
        last_turn_status=summary.last_turn_status if summary.HasField("last_turn_status") else None,
        feed=feed,
    )
