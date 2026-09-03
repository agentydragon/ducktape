"""The store's contract: a thread per session, events kept verbatim and idempotently, read back
without a runner."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp

from x.agentplane.app.trajectory import TrajectoryStore
from x.agentplane.runner import protocol_pb2 as pb

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

SPEC = pb.SessionSpec(provider=pb.PROVIDER_CLAUDE, cwd="/state/work", model="test-model", reasoning_effort="low")


def _event(sequence: int, **observation: object) -> pb.Event:
    at = Timestamp()
    at.FromDatetime(datetime(2026, 9, 2, 12, 0, sequence, tzinfo=UTC))
    return pb.Event(sequence=sequence, at=at, **observation)  # type: ignore[arg-type]


async def test_a_session_is_one_thread_and_its_events_read_back_in_order(store: TrajectoryStore) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    assert await store.thread("sb-1", "s-1", SPEC) == thread
    other = await store.thread("sb-1", "s-2", SPEC)
    assert other != thread

    await store.record(
        thread,
        [
            _event(1, harness_started=pb.HarnessStarted(resumed=False, pid=7)),
            _event(2, native=pb.Native(direction=pb.DIRECTION_FROM_HARNESS, line='{"type":"x"}')),
            _event(3, turn_started=pb.TurnStarted(turn_id="t1")),
        ],
    )
    # A replay after a reconnect brings sequences already stored: they are not written twice.
    await store.record(
        thread, [_event(3, turn_started=pb.TurnStarted(turn_id="t1")), _event(4, harness_lost=pb.HarnessLost())]
    )

    events = await store.events(thread, limit=100)
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert events[1].native.line == '{"type":"x"}'
    assert events[1].at.ToDatetime(tzinfo=UTC) == datetime(2026, 9, 2, 12, 0, 2, tzinfo=UTC)
    assert [event.sequence for event in await store.events(thread, after_sequence=2, limit=100)] == [3, 4]
    assert [event.sequence for event in await store.events(thread, after_sequence=1, limit=2)] == [2, 3]
    assert await store.last_sequence(thread) == 4
    assert await store.last_sequence(other) == 0


async def test_threads_list_with_their_progress(store: TrajectoryStore) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    empty = await store.thread("sb-2", "s-9", pb.SessionSpec(provider=pb.PROVIDER_CODEX, cwd="/w", model="m"))
    await store.record(
        thread, [_event(1, harness_started=pb.HarnessStarted(pid=1)), _event(2, harness_lost=pb.HarnessLost())]
    )

    views = {view.id: view for view in await store.list_threads()}

    assert views[thread].model_dump(include={"sandbox", "session_id", "provider", "model", "cwd", "last_sequence"}) == {
        "sandbox": "sb-1",
        "session_id": "s-1",
        "provider": "PROVIDER_CLAUDE",
        "model": "test-model",
        "cwd": "/state/work",
        "last_sequence": 2,
    }
    assert views[thread].last_event_at == datetime(2026, 9, 2, 12, 0, 2, tzinfo=UTC)
    assert (views[empty].provider, views[empty].last_sequence, views[empty].last_event_at) == (
        "PROVIDER_CODEX",
        0,
        None,
    )
    assert await store.get_thread(empty) == views[empty]
    assert await store.get_thread(thread) == views[thread]


if __name__ == "__main__":
    pytest_bazel.main()
