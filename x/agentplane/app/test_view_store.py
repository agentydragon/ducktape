"""How a connected client uses the derived view, against a real PostgreSQL.

Each test is one of the use cases the contract exists for, written the way a client drives it: open a
Thread, follow from the Position that came back, submit a command and watch it settle, stream an Item
in, drop and catch up. What they assert is the observable answer, not the table layout.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp

from x.agentplane.app import thread_api_pb2, thread_view_pb2
from x.agentplane.app.trajectory import IngestionLease, TrajectoryStore
from x.agentplane.app.view_store import ProjectionNotReadyError, ViewStore
from x.agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from x.agentplane.runner import protocol_pb2

# gazelle:include_dep @pypi//protobuf

SOURCE = "test-runner"
SPEC = protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CODEX, cwd="/w", model="first-model")


def event(cursor: int, **observation: object) -> event_log_pb2.EventEntry:
    at = Timestamp()
    at.FromDatetime(datetime(2026, 9, 16, 12, 0, min(cursor, 59), tzinfo=UTC))
    return event_log_pb2.EventEntry(
        cursor=cursor,
        origin=event_log_pb2.EventOrigin(source_id=SOURCE, sequence=cursor),
        event=event_pb2.Event(at=at, **observation),  # type: ignore[arg-type]
    )


@pytest.fixture
async def lease(store: TrajectoryStore) -> IngestionLease:
    acquired = await store.acquire_ingestion("sb-1", timedelta(minutes=1))
    assert acquired is not None
    return acquired


@pytest.fixture
async def view(db_url: str) -> ViewStore:
    return ViewStore.connect(db_url)


@pytest.fixture
async def thread(store: TrajectoryStore, view: ViewStore) -> UUID:
    created = await store.thread("sb-1", "s-1", SPEC)
    await view.start(created, SOURCE)
    return created


async def archive(
    store: TrajectoryStore,
    view: ViewStore,
    thread: UUID,
    lease: IngestionLease,
    entries: list[event_log_pb2.EventEntry],
) -> thread_view_pb2.Changes:
    """What the ingestion path does: archive losslessly, then fold that batch into the view."""
    await store.record(thread, entries, lease=lease)
    return await view.project(thread, entries)


def tail(max_older: int) -> thread_api_pb2.SegmentWindow:
    return thread_api_pb2.SegmentWindow(max_older=max_older)


async def test_a_thread_with_no_projection_is_not_an_empty_view(store: TrajectoryStore, view: ViewStore) -> None:
    created = await store.thread("sb-2", "s-2", SPEC)
    with pytest.raises(ProjectionNotReadyError):
        await view.snapshot(created, tail(50))


async def test_opening_a_thread_returns_a_window_and_a_position_to_follow_from(
    store: TrajectoryStore, view: ViewStore, thread: UUID, lease: IngestionLease
) -> None:
    await archive(
        store,
        view,
        thread,
        lease,
        [
            event(1, harness_started=event_pb2.HarnessStarted(pid=7)),
            event(2, turn_started=event_pb2.TurnStarted(turn_id="t1", model="first-model")),
            event(3, item_started=event_pb2.ItemStarted(item_id="i1", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)),
            event(4, text_delta=event_pb2.TextDelta(item_id="i1", text="hello")),
        ],
    )

    snapshot = await view.snapshot(thread, tail(50))

    assert snapshot.position.source_id == SOURCE
    assert snapshot.position.through_cursor == 4
    assert [segment.cursor for segment in snapshot.window.segments] == [1, 2, 3]
    assert snapshot.window.covers_from_cursor == 1
    assert snapshot.window.exhausted
    # Controls are what the Events evidence, and the Item carries what streamed so far.
    assert snapshot.controls.applied_model == "first-model"
    assert snapshot.controls.active_turn_id == "t1"
    assert snapshot.window.segments[2].item.text.value == "hello"


async def test_a_carried_event_is_read_from_the_archive_rather_than_stored_again(
    store: TrajectoryStore, view: ViewStore, thread: UUID, lease: IngestionLease
) -> None:
    """The view keeps no second copy of an Event it carries, so what comes back is the archived one."""
    await archive(store, view, thread, lease, [event(1, turn_started=event_pb2.TurnStarted(turn_id="t1", model="m"))])

    (segment,) = (await view.snapshot(thread, tail(50))).window.segments
    assert segment.WhichOneof("content") == "event"
    assert segment.event.turn_started.turn_id == "t1"
    # Including the Event's own timestamp, which a transcription would have dropped.
    assert segment.event.HasField("at")


async def test_following_delivers_what_changed_since_the_position(
    store: TrajectoryStore, view: ViewStore, thread: UUID, lease: IngestionLease
) -> None:
    await archive(
        store,
        view,
        thread,
        lease,
        [
            event(1, turn_started=event_pb2.TurnStarted(turn_id="t1", model="m")),
            event(2, item_started=event_pb2.ItemStarted(item_id="i1", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)),
        ],
    )
    installed = (await view.snapshot(thread, tail(50))).position

    await archive(store, view, thread, lease, [event(3, text_delta=event_pb2.TextDelta(item_id="i1", text="hi"))])
    update = await view.catch_up(thread, installed)

    assert update.WhichOneof("update") == "changes"
    assert (update.changes.after_cursor, update.changes.through_cursor) == (2, 3)
    # Only the Item moved; the turn Segment did not change and is not resent.
    assert [segment.cursor for segment in update.changes.segments] == [2]
    assert update.changes.segments[0].item.text.value == "hi"


async def test_a_queued_command_stays_pending_until_its_effect_moves_the_controls(
    store: TrajectoryStore, view: ViewStore, thread: UUID, lease: IngestionLease
) -> None:
    change = command_pb2.Command(command_id="c1", change_model=command_pb2.ChangeModel(model="next-model"))
    await archive(
        store,
        view,
        thread,
        lease,
        [
            event(1, turn_started=event_pb2.TurnStarted(turn_id="t1", model="first-model")),
            event(2, command_admitted=event_pb2.CommandAdmitted(command=change)),
        ],
    )

    queued = await view.snapshot(thread, tail(50))
    (summary,) = queued.pending.commands
    assert summary.command_id == "c1"
    assert summary.WhichOneof("outcome") == "pending"
    assert queued.pending.unresolved_count == 1
    # The picker does not move on admission, and the admission is not in the conversation.
    assert queued.controls.applied_model == "first-model"
    assert [segment.cursor for segment in queued.window.segments] == [1]

    await archive(
        store,
        view,
        thread,
        lease,
        [
            event(
                3,
                model_changed=event_pb2.ModelChanged(command_id="c1", previous_model="first-model", model="next-model"),
            )
        ],
    )

    effected = await view.snapshot(thread, tail(50))
    assert effected.controls.applied_model == "next-model"
    assert list(effected.pending.commands) == []
    assert effected.pending.unresolved_count == 0


async def test_a_live_batch_extends_a_streaming_item_rather_than_resending_it(
    store: TrajectoryStore, view: ViewStore, thread: UUID, lease: IngestionLease
) -> None:
    await archive(
        store,
        view,
        thread,
        lease,
        [
            event(1, item_started=event_pb2.ItemStarted(item_id="i1", kind=event_pb2.ITEM_KIND_TOOL_CALL)),
            event(2, tool_output_delta=event_pb2.ToolOutputDelta(item_id="i1", text="first ")),
        ],
    )
    changes = await archive(
        store, view, thread, lease, [event(3, tool_output_delta=event_pb2.ToolOutputDelta(item_id="i1", text="second"))]
    )

    (segment,) = changes.segments
    assert segment.from_revision_cursor == 2
    assert segment.item.output.suffix == "second"


async def test_reconnecting_mid_stream_catches_up_with_whole_segments(
    store: TrajectoryStore, view: ViewStore, thread: UUID, lease: IngestionLease
) -> None:
    """A caller that was away holds no revision to extend, so catch-up reads the Segment whole. That
    is what a stored journal would have replayed, without storing the stream a second time."""
    await archive(
        store,
        view,
        thread,
        lease,
        [
            event(1, item_started=event_pb2.ItemStarted(item_id="i1", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)),
            event(2, text_delta=event_pb2.TextDelta(item_id="i1", text="Hello")),
        ],
    )
    away = (await view.snapshot(thread, tail(50))).position

    for cursor, word in ((3, " wor"), (4, "ld"), (5, "!")):
        await archive(
            store, view, thread, lease, [event(cursor, text_delta=event_pb2.TextDelta(item_id="i1", text=word))]
        )

    update = await view.catch_up(thread, away)
    (segment,) = update.changes.segments
    assert not segment.HasField("from_revision_cursor")
    assert segment.item.text.value == "Hello world!"
    assert update.changes.through_cursor == 5


async def test_opening_a_long_thread_costs_the_window_and_not_the_thread(
    store: TrajectoryStore, view: ViewStore, thread: UUID, lease: IngestionLease
) -> None:
    """The requirement the whole design exists for: a month-old Thread opens like a new one."""
    await archive(
        store,
        view,
        thread,
        lease,
        [
            event(cursor, turn_started=event_pb2.TurnStarted(turn_id=f"t{cursor}", model="m"))
            for cursor in range(1, 201)
        ],
    )

    snapshot = await view.snapshot(thread, tail(10))

    assert len(snapshot.window.segments) == 10
    assert [segment.cursor for segment in snapshot.window.segments] == list(range(191, 201))
    assert not snapshot.window.exhausted
    assert snapshot.position.through_cursor == 200


async def test_a_window_around_a_cursor_restores_a_reader_who_was_scrolled_up(
    store: TrajectoryStore, view: ViewStore, thread: UUID, lease: IngestionLease
) -> None:
    await archive(
        store,
        view,
        thread,
        lease,
        [
            event(cursor, turn_started=event_pb2.TurnStarted(turn_id=f"t{cursor}", model="m"))
            for cursor in range(1, 101)
        ],
    )

    around = await view.snapshot(thread, thread_api_pb2.SegmentWindow(from_cursor=50, max_older=2, max_newer=2))

    assert [segment.cursor for segment in around.window.segments] == [48, 49, 50, 51, 52]
    assert around.position.through_cursor == 100


if __name__ == "__main__":
    pytest_bazel.main()
