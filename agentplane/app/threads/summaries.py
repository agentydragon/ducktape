"""A Thread's fold summary, from the History Service, with the app's own record of confirmed resumes."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import grpc
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.database_updates import Channel, notify
from agentplane.app.threads.models import Thread
from agentplane.history_service import runner_pb2 as history_pb2
from agentplane.history_service.client import HistoryServiceClient, HistoryServiceError
from agentplane.proto import runner_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


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


class Summaries:
    """A Session the History Service has not folded yet reads as nothing folded."""

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
        return _summary(summary, (await self._resumes({thread_id})).get(thread_id))

    async def many(self, thread_ids: Collection[UUID]) -> dict[UUID, FoldSummary]:
        """At most one History Service read's worth of Threads."""
        listed = {
            UUID(summary.session_id): summary
            for summary in await self._history.batch_get_session_summaries([str(thread_id) for thread_id in thread_ids])
        }
        resumes = await self._resumes(thread_ids)
        return {
            thread_id: _summary(listed[thread_id], resumes.get(thread_id)) if thread_id in listed else NOTHING_FOLDED
            for thread_id in thread_ids
        }

    async def folded_through(self, thread_id: UUID) -> int:
        return (await self.get(thread_id)).folded_through

    async def resume_pending(self, thread_id: UUID) -> None:
        """A runner-confirmed restart supersedes a normal terminal feed, not a replay error.

        The terminal feed is masked until the fold attaches past it, so an SSE reader does not
        mistake the previous incarnation's end for the new one.
        """
        summary = await self.get(thread_id)
        if summary.feed is None or not isinstance(summary.feed.end, FeedEnd):
            return
        async with self._sessions.begin() as session:
            await session.execute(
                insert(Thread)
                .values(id=thread_id, resumed_after_cursor=summary.feed.attached.last_cursor)
                .on_conflict_do_update(
                    index_elements=[Thread.id], set_={"resumed_after_cursor": summary.feed.attached.last_cursor}
                )
            )
            await notify(session, Channel.THREADS)

    async def _resumes(self, thread_ids: Collection[UUID]) -> dict[UUID, int]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(Thread.id, Thread.resumed_after_cursor).where(
                    Thread.id.in_(thread_ids), Thread.resumed_after_cursor.is_not(None)
                )
            )
            return {row.id: row.resumed_after_cursor for row in rows}


def _summary(summary: history_pb2.SessionSummary, resumed_after_cursor: int | None) -> FoldSummary:
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
