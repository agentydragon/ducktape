"""Shared thread/event values used by app and thread-store tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from google.protobuf.timestamp_pb2 import Timestamp

from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.ingestion import Ingestion
from agentplane.app.threads.store import ThreadStore
from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.runner import protocol_pb2


@dataclass(frozen=True)
class Replica:
    """Another app replica's stores, over its own connection pool on the same database."""

    store: ThreadStore
    event_logs: EventLogStore
    ingestion: Ingestion
    operator_sessions: OperatorSessionStore


SPEC = protocol_pb2.SessionSpec(
    harness=protocol_pb2.HARNESS_CLAUDE, cwd="/state/work", model="test-model", reasoning_effort="low"
)


def event_entry(cursor: int, **observation: object) -> event_log_pb2.EventEntry:
    """One runner event at `cursor`, timestamped from it so a thread's order is its cursor order."""
    at = Timestamp()
    at.FromDatetime(datetime(2026, 9, 2, 12, 0, tzinfo=UTC) + timedelta(seconds=cursor))
    event = event_pb2.Event(at=at, **observation)  # type: ignore[arg-type]
    return event_log_pb2.EventEntry(
        cursor=cursor, origin=event_log_pb2.EventOrigin(source_id="test-runner", sequence=cursor), event=event
    )
