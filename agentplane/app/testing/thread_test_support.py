"""Shared Thread fixtures and event values used by app and thread-store tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.threads.models import Thread
from agentplane.app.threads.store import ThreadStore
from agentplane.proto import event_log_pb2, event_pb2, runner_pb2


@dataclass(frozen=True)
class Replica:
    """Another app replica's stores, over its own connection pool on the same database."""

    store: ThreadStore
    event_logs: SeededEventLogStore
    operator_sessions: OperatorSessionStore


SPEC = runner_pb2.SessionSpec(
    harness=runner_pb2.HARNESS_CLAUDE, cwd="/state/work", model="test-model", reasoning_effort="low"
)


def event_entry(cursor: int, **observation: object) -> event_log_pb2.EventEntry:
    """One runner event at `cursor`, timestamped from it so a thread's order is its cursor order."""
    at = Timestamp()
    at.FromDatetime(datetime(2026, 9, 2, 12, 0, tzinfo=UTC) + timedelta(seconds=cursor))
    event = event_pb2.Event(at=at, **observation)  # type: ignore[arg-type]
    return event_log_pb2.EventEntry(
        cursor=cursor, origin=event_log_pb2.EventOrigin(source_id="test-runner", sequence=cursor), event=event
    )


async def seed_thread(engine: AsyncEngine, *, public_id: UUID | None = None) -> UUID:
    thread_id = public_id if public_id is not None else uuid4()
    async with async_sessionmaker(engine).begin() as session:
        session.add(Thread(id=thread_id))
    return thread_id
