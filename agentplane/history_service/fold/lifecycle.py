"""Fold service lifecycle only within the fold's materialized prefix."""

from uuid import UUID

from google.protobuf.json_format import MessageToDict, ParseDict
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.history_service.fold.models import ThreadHistorySummary
from agentplane.history_service.fold.recording import EventReplicationError, set_operational
from agentplane.proto import event_log_pb2, history_service_pb2, runner_pb2

# gazelle:include_dep @pypi//protobuf


async def project_lifecycle(
    session: AsyncSession,
    thread_id: UUID,
    summary: ThreadHistorySummary,
    page: history_service_pb2.ReadSessionEventsResponse,
    *,
    through_cursor: int,
) -> bool:
    previous = (summary.attached, summary.end)
    attached = None if summary.attached is None else ParseDict(summary.attached, runner_pb2.Attached())
    end = summary.end
    if page.HasField("feed_state"):
        feed = page.feed_state
        if not feed.HasField("attached") or feed.attached.last_cursor > page.last_cursor:
            raise EventReplicationError("stored feed state is outside the committed prefix")
        cursor = feed.attached.last_cursor
        # A page may carry a feed snapshot past the entries folded so far. Wait until the fold
        # covers it; never leap the view over unfolded Events.
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
    if (
        end is None
        and attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
        and attached.setup_state != runner_pb2.SETUP_STATE_RUNNING
        and attached.launch_outcome != runner_pb2.LAUNCH_OUTCOME_PENDING
    ):
        # A pushed journal has no attachment to reach EOF; its stopped harness is where a followed one ended.
        # A harness waiting on setup or still launching has not started yet, so it has not ended either.
        end = {}
    summary.attached = MessageToDict(attached)
    summary.end = end
    operational_changed = await set_operational(
        session,
        thread_id,
        status="active" if end is None else "failed" if end else "ended",
        error=end.get("message") if end else None,
    )
    return previous != (summary.attached, summary.end) or operational_changed


def project_attached(attached: runner_pb2.Attached, entry: event_log_pb2.EventEntry) -> None:
    attached.last_cursor = entry.cursor
    event = entry.event
    # The runner's own rule (runner/session.py `_apply_prefix`): the first outcome settles the launch.
    # An announcement from a runner that predates the field leaves it unspecified until then.
    undecided = attached.launch_outcome in (runner_pb2.LAUNCH_OUTCOME_UNSPECIFIED, runner_pb2.LAUNCH_OUTCOME_PENDING)
    match event.WhichOneof("observation"):
        case "setup_finished":
            succeeded = event.setup_finished.exit_code == 0
            attached.setup_state = runner_pb2.SETUP_STATE_SUCCEEDED if succeeded else runner_pb2.SETUP_STATE_FAILED
            if undecided:
                attached.launch_outcome = (
                    runner_pb2.LAUNCH_OUTCOME_PENDING if succeeded else runner_pb2.LAUNCH_OUTCOME_FAILED
                )
        case "setup_interrupted":
            attached.setup_state = runner_pb2.SETUP_STATE_INTERRUPTED
            if undecided:
                attached.launch_outcome = runner_pb2.LAUNCH_OUTCOME_FAILED
        case "harness_launch_failed":
            if undecided:
                attached.launch_outcome = runner_pb2.LAUNCH_OUTCOME_FAILED
        case "harness_started":
            attached.harness_state = runner_pb2.HARNESS_STATE_RUNNING
            if undecided:
                attached.launch_outcome = runner_pb2.LAUNCH_OUTCOME_STARTED
        case "harness_exited" | "harness_lost":
            attached.harness_state = runner_pb2.HARNESS_STATE_STOPPED
            attached.active_turn_id = ""
            if undecided:
                attached.launch_outcome = runner_pb2.LAUNCH_OUTCOME_FAILED
        case "turn_started":
            attached.active_turn_id = event.turn_started.turn_id
        case "turn_completed":
            attached.active_turn_id = ""
        case "model_changed":
            attached.spec.model = event.model_changed.model
        case "reasoning_effort_changed":
            attached.spec.reasoning_effort = event.reasoning_effort_changed.effort
