"""Fold service lifecycle only within the app's materialized prefix."""

from uuid import UUID

from google.protobuf.json_format import MessageToDict, ParseDict
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.app.threads.events.event_log import EventReplicationError, project_attached
from agentplane.app.threads.models import EventLog, ThreadHistorySummary
from agentplane.app.threads.view.recording import set_operational
from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.sandbox_service import protocol_pb2

# gazelle:include_dep @pypi//protobuf


async def project_lifecycle(
    session: AsyncSession,
    thread_id: UUID,
    summary: ThreadHistorySummary,
    page: protocol_pb2.ReadSessionEventsResponse,
    *,
    through_cursor: int,
) -> None:
    attached = None if summary.attached is None else ParseDict(summary.attached, runner_pb2.Attached())
    end = summary.end
    if page.HasField("feed_state"):
        feed = page.feed_state
        if not feed.HasField("attached") or feed.attached.last_cursor > page.last_cursor:
            raise EventReplicationError("service lifecycle is outside its committed prefix")
        cursor = feed.attached.last_cursor
        # A page may carry the latest service snapshot while the app is still replaying.
        # Wait until the fold covers it; never leap the UI over unprojected Events.
        if cursor <= through_cursor and (attached is None or cursor >= attached.last_cursor):
            same_cursor = attached is not None and cursor == attached.last_cursor
            attached = runner_pb2.Attached()
            attached.CopyFrom(feed.attached)
            end = {} if feed.ended else end if same_cursor else None
    if attached is None:
        return  # imported/deleted history without an attachment remains unknown
    for entry in page.entries:
        if entry.cursor <= attached.last_cursor:
            continue
        project_attached(attached, entry)
        # Any suffix invalidates EOF at the old prefix, including failed setup/restart.
        end = None
    if summary.resumed_after_cursor is not None:
        if attached.last_cursor <= summary.resumed_after_cursor:
            end = None
        else:
            summary.resumed_after_cursor = None
    summary.attached = MessageToDict(attached)
    summary.end = end
    await session.execute(update(EventLog).where(EventLog.id == thread_id).values(model=attached.spec.model))
    await set_operational(
        session,
        thread_id,
        status="active" if end is None else "failed" if end else "ended",
        error=end.get("message") if end else None,
    )
