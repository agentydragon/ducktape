"""When a Session's harness last became idle, folded from its turn and harness events."""

from datetime import UTC, datetime

from agentplane.proto import event_pb2

# gazelle:include_dep @pypi//protobuf


def next_idle_since(idle_since: datetime | None, event: event_pb2.Event) -> datetime | None:
    """A harness is idle from its start, or from the end of a turn, until the next turn starts or it stops."""
    match event.WhichOneof("observation"):
        case "harness_started" | "turn_completed":
            return event.at.ToDatetime(tzinfo=UTC)
        case "turn_started" | "harness_exited" | "harness_lost":
            return None
        case _:
            return idle_since
