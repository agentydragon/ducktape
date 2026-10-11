"""Fold service lifecycle only within the fold's materialized prefix."""

from uuid import UUID

from google.protobuf.json_format import MessageToDict, ParseDict
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.history_service import protocol_pb2 as history_pb2
from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.thread_fold.models import ThreadHistorySummary
from agentplane.thread_fold.recording import EventReplicationError, set_operational

# gazelle:include_dep @pypi//protobuf


async def project_lifecycle(
    session: AsyncSession,
    thread_id: UUID,
    summary: ThreadHistorySummary,
    page: history_pb2.ReadSessionEventsResponse,
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
    if end is None and attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED:
        # A pushed journal has no attachment to reach EOF; its stopped harness is where a followed one ended.
        end = {}
    if summary.resumed_after_cursor is not None:
        if attached.last_cursor <= summary.resumed_after_cursor:
            end = None
        else:
            summary.resumed_after_cursor = None
    summary.attached = MessageToDict(attached)
    summary.end = end
    operational_changed = await set_operational(
        session,
        thread_id,
        status="active" if end is None else "failed" if end else "ended",
        error=end.get("message") if end else None,
    )
    return previous != (summary.attached, summary.end, summary.resumed_after_cursor) or operational_changed


def project_attached(attached: runner_pb2.Attached, entry: event_log_pb2.EventEntry) -> None:
    attached.last_cursor = entry.cursor
    event = entry.event
    match event.WhichOneof("observation"):
        case "setup_finished":
            attached.setup_state = (
                runner_pb2.SETUP_STATE_SUCCEEDED
                if event.setup_finished.exit_code == 0
                else runner_pb2.SETUP_STATE_FAILED
            )
        case "setup_interrupted":
            attached.setup_state = runner_pb2.SETUP_STATE_INTERRUPTED
        case "harness_started":
            attached.harness_state = runner_pb2.HARNESS_STATE_RUNNING
        case "harness_exited" | "harness_lost":
            attached.harness_state = runner_pb2.HARNESS_STATE_STOPPED
            attached.active_turn_id = ""
        case "turn_started":
            attached.active_turn_id = event.turn_started.turn_id
        case "turn_completed":
            attached.active_turn_id = ""
        case "model_changed":
            attached.spec.model = event.model_changed.model
        case "reasoning_effort_changed":
            attached.spec.reasoning_effort = event.reasoning_effort_changed.effort
