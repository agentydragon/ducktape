"""Fold service lifecycle only within the app's materialized prefix."""

from uuid import UUID

from google.protobuf.json_format import MessageToDict, ParseDict
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
) -> bool:
    previous = (summary.attached, summary.end, summary.resumed_after_cursor)
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
        # Successful projection clears an earlier projection error even if imported
        # history has no runner lifecycle snapshot. This does not manufacture EOF.
        return await set_operational(session, thread_id, status="active", error=None)
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
    log = await session.get(EventLog, thread_id)
    assert log is not None
    model_changed = log.model != attached.spec.model
    log.model = attached.spec.model
    operational_changed = await set_operational(
        session,
        thread_id,
        status="active" if end is None else "failed" if end else "ended",
        error=end.get("message") if end else None,
    )
    return (
        previous != (summary.attached, summary.end, summary.resumed_after_cursor)
        or model_changed
        or operational_changed
    )
