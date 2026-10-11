"""Real PostgreSQL exercises transactional history, including independent replay."""

import asyncio
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.history_service import protocol_pb2 as history_pb2
from agentplane.history_service.channels import Channel, create_wakeups
from agentplane.history_service.storage.store import (
    HistoryConflictError,
    HistoryNotFoundError,
    ObservationPage,
    Store,
    Writer,
    WriterMismatchError,
)
from agentplane.history_service.testing.delta_spans import claude_text
from agentplane.proto import event_log_pb2, event_pb2, runner_pb2

# gazelle:include_dep @pypi//protobuf


def entry(cursor: int, source: str = "source", *, resumed: bool = False) -> event_log_pb2.EventEntry:
    result = event_log_pb2.EventEntry(cursor=cursor)
    result.origin.source_id = source
    result.origin.sequence = cursor
    result.event.harness_started.resumed = resumed
    return result


async def opened(store: Store, session_id: UUID) -> None:
    await store.record_session(
        session_id,
        sandbox_namespace="testing",
        sandbox_name="gone",
        sandbox_uid=None,
        runner_session_id="original-native-path",
    )


@pytest.mark.asyncio
async def test_replay_survives_new_store_and_deleted_sandbox(engine: AsyncEngine) -> None:
    session_id = uuid4()
    store = Store(engine)
    await opened(store, session_id)
    await opened(store, session_id)
    native = entry(3)
    native.event.ClearField("harness_started")
    native.event.native.line = "frame with a NUL\x00 and provider bytes"
    assert await store.append(session_id, [entry(1), entry(2, resumed=True)]) == 2
    assert await Store(engine).append(session_id, [entry(1), entry(2, resumed=True), native]) == 3
    high_water, events = await Store(engine).read(session_id, after_cursor=1, limit=2)
    assert high_water == 3
    assert events == [entry(2, resumed=True), native]
    assert await Store(engine).read(session_id, after_cursor=3) == (3, [])
    with pytest.raises(ValueError, match="beyond"):
        await store.read(session_id, after_cursor=4)


@pytest.mark.asyncio
async def test_bad_batch_rolls_back_and_conflicting_duplicate_rejected(engine: AsyncEngine) -> None:
    store = Store(engine)
    session_id = uuid4()
    await opened(store, session_id)
    with pytest.raises(HistoryConflictError, match="expected 2"):
        await store.append(session_id, [entry(1), entry(3)])
    assert await store.read(session_id) == (0, [])
    assert await store.append(session_id, [entry(1), entry(2)]) == 2
    for bad in ([entry(2, resumed=True)], [entry(3, "other")], [entry(4)]):
        with pytest.raises(HistoryConflictError):
            await store.append(session_id, bad)
    assert await store.read(session_id) == (2, [entry(1), entry(2)])
    with pytest.raises(HistoryConflictError, match="locator"):
        await store.record_session(
            session_id,
            sandbox_namespace="testing",
            sandbox_name="new",
            sandbox_uid=None,
            runner_session_id="original-native-path",
        )
    with pytest.raises(HistoryNotFoundError):
        await store.append(uuid4(), [entry(1)])


@pytest.mark.asyncio
async def test_two_replica_writers_serialize_on_history_row(engine: AsyncEngine) -> None:
    session_id = uuid4()
    left, right = Store(engine), Store(engine)
    await opened(left, session_id)
    results = await asyncio.gather(
        left.append(session_id, [entry(1), entry(2)]), right.append(session_id, [entry(1), entry(2)])
    )
    assert list(results) == [2, 2]
    assert await right.read(session_id) == (2, [entry(1), entry(2)])


@pytest.mark.asyncio
async def test_recorded_session_keeps_its_id_and_fences_duplicate_locator(engine: AsyncEngine) -> None:
    store = Store(engine)
    session_id, uid = uuid4(), uuid4()
    await store.record_session(
        session_id, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid, runner_session_id="s-1"
    )
    await store.record_session(
        session_id, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid, runner_session_id="s-1"
    )
    with pytest.raises(HistoryConflictError, match="belongs to another"):
        await store.record_session(
            uuid4(), sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid, runner_session_id="s-1"
        )
    # A nullable legacy UID must not permit another ID for the same legacy locator.
    await opened(store, uuid4())
    with pytest.raises(HistoryConflictError, match="belongs to another"):
        await opened(store, uuid4())


async def test_a_follower_and_the_ingress_each_bind_their_half_of_one_session(engine: AsyncEngine) -> None:
    """During the cutover a Session may reach history both ways; neither refuses the other's row."""
    store = Store(engine)
    writer = Writer(namespace="testing", name="worker", uid="test-worker-account-uid")
    pushed_first, followed_first, uid = uuid4(), uuid4(), uuid4()
    await store.bind_writer(pushed_first, writer)
    await store.record_session(
        pushed_first, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid, runner_session_id=None
    )
    await store.record_session(
        followed_first, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid, runner_session_id=None
    )
    await store.bind_writer(followed_first, writer)
    for session_id in (pushed_first, followed_first):
        await store.bind_writer(session_id, writer)
        with pytest.raises(WriterMismatchError):
            await store.bind_writer(session_id, Writer(namespace="testing", name="worker", uid="test-recreated-uid"))
        with pytest.raises(HistoryConflictError):
            await store.record_session(
                session_id, sandbox_namespace="testing", sandbox_name="other", sandbox_uid=uid, runner_session_id=None
            )


async def test_history_notifies_after_commit_and_not_after_rollback(engine: AsyncEngine) -> None:
    store = Store(engine)
    wakeups = create_wakeups(engine.url)
    session_id = uuid4()

    async def wait_for_change(changed: asyncio.Event) -> None:
        await asyncio.wait_for(changed.wait(), 5)

    async def assert_quiet(changed: asyncio.Event) -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(changed.wait(), 0.05)

    changed = asyncio.Event()
    async with wakeups.listener.listen():
        with wakeups.changes[Channel.HISTORY].subscribe(changed):
            await opened(store, session_id)
            await wait_for_change(changed)
            changed.clear()

            with pytest.raises(HistoryConflictError):
                await store.append(session_id, [entry(1), entry(3)])
            await assert_quiet(changed)

            await store.append(session_id, [entry(1)])
            await wait_for_change(changed)
            changed.clear()
            await store.append(session_id, [entry(1)])
            await assert_quiet(changed)

            feed = history_pb2.SessionFeedState(
                attached=runner_pb2.Attached(
                    session_id="original-native-path", last_cursor=1, harness_state=runner_pb2.HARNESS_STATE_STOPPED
                )
            )
            await store.record_feed_state(session_id, feed)
            await wait_for_change(changed)
            changed.clear()
            await store.record_feed_state(session_id, feed)
            await assert_quiet(changed)


@pytest.mark.asyncio
async def test_observation_pages_seek_retained_prefix_without_returning_payloads(engine: AsyncEngine) -> None:
    store = Store(engine)
    session_id = uuid4()
    await opened(store, session_id)
    assert await store.read_observations(session_id) == ObservationPage(0, [], [])
    native = entry(3)
    native.event.ClearField("harness_started")
    native.event.native.line = "retained\0native"
    await store.append(session_id, [entry(1), entry(2), native, entry(4), entry(5)])
    assert await store.read_observations(session_id, limit=2) == ObservationPage(
        5, [(4, "harness_started"), (5, "harness_started")], []
    )
    assert await store.read_observations(session_id, before_cursor=4, limit=2) == ObservationPage(
        5, [(2, "harness_started"), (3, "native")], []
    )
    assert await store.read_observations(session_id, after_cursor=2, limit=2) == ObservationPage(
        5, [(3, "native"), (4, "harness_started")], []
    )
    assert await store.read_observations(session_id, before_cursor=0) == ObservationPage(5, [], [])
    assert await store.read_observations(session_id, after_cursor=9) == ObservationPage(5, [], [])
    with pytest.raises(ValueError, match="invalid observation"):
        await store.read_observations(session_id, before_cursor=3, after_cursor=1)
    with pytest.raises(ValueError, match="invalid observation"):
        await store.read_observations(session_id, limit=201)
    with pytest.raises(HistoryNotFoundError):
        await store.read_observations(uuid4())


@pytest.mark.asyncio
async def test_feed_snapshot_requires_covered_bound_history_and_cannot_regress(engine: AsyncEngine) -> None:
    store = Store(engine)
    session_id = uuid4()
    await opened(store, session_id)
    assert not (await store.read_page(session_id)).HasField("feed_state")
    feed = history_pb2.SessionFeedState(
        attached=runner_pb2.Attached(
            session_id="original-native-path", last_cursor=1, harness_state=runner_pb2.HARNESS_STATE_STOPPED
        )
    )
    with pytest.raises(HistoryConflictError, match="ahead"):
        await store.record_feed_state(session_id, feed)
    await store.append(session_id, [entry(1)])
    await store.record_feed_state(session_id, feed)
    feed.ended = True
    await store.record_feed_state(session_id, feed)
    feed.ended = False
    await store.record_feed_state(session_id, feed)
    assert (await Store(engine).read_page(session_id)).feed_state.ended
    conflicting = history_pb2.SessionFeedState()
    conflicting.CopyFrom(feed)
    conflicting.attached.spec.cwd = "/another-workspace"
    with pytest.raises(HistoryConflictError, match="conflicting attachment"):
        await store.record_feed_state(session_id, conflicting)
    feed.attached.harness_state = runner_pb2.HARNESS_STATE_RUNNING
    await store.record_feed_state(session_id, feed)
    assert (await Store(engine).read_page(session_id)).feed_state == feed
    stopped = history_pb2.SessionFeedState()
    stopped.CopyFrom(feed)
    stopped.attached.harness_state = runner_pb2.HARNESS_STATE_STOPPED
    stopped.ended = True
    await store.record_feed_state(session_id, stopped)
    assert (await Store(engine).read_page(session_id)).feed_state == feed
    await store.append(session_id, [entry(2, resumed=True)])
    feed.attached.last_cursor = 2
    await store.record_feed_state(session_id, feed)
    stale = history_pb2.SessionFeedState(
        attached=runner_pb2.Attached(session_id="original-native-path", last_cursor=1), ended=True
    )
    await store.record_feed_state(session_id, stale)
    page = await store.read_page(session_id, after_cursor=2)
    assert page.last_cursor == 2
    assert page.feed_state == feed
    assert not page.entries
    feed.attached.session_id = "replacement-runner"
    with pytest.raises(HistoryConflictError, match="bound runner"):
        await store.record_feed_state(session_id, feed)


@pytest.mark.asyncio
async def test_followed_snapshot_is_stored_under_runner_id_and_seal_is_sticky(engine: AsyncEngine) -> None:
    store = Store(engine)
    session_id = uuid4()
    await opened(store, session_id)
    await store.append(session_id, [entry(1), entry(2)])
    # A Sandbox Service follow names the public Session ID; a direct runner attach the runner's.
    followed = history_pb2.SessionFeedState(attached=runner_pb2.Attached(session_id=str(session_id), last_cursor=1))
    with pytest.raises(HistoryConflictError, match="seal"):
        await store.record_feed_state(
            session_id, history_pb2.SessionFeedState(attached=followed.attached, sealed=runner_pb2.Sealed(cursor=1))
        )
    followed.sealed.cursor = 2
    await store.record_feed_state(session_id, followed)
    assert (await store.read_page(session_id)).feed_state.attached.session_id == "original-native-path"
    direct = history_pb2.SessionFeedState(
        attached=runner_pb2.Attached(session_id="original-native-path", last_cursor=2)
    )
    await store.record_feed_state(session_id, direct)
    stored = (await store.read_page(session_id)).feed_state
    assert stored.attached == direct.attached
    assert stored.sealed.cursor == 2
    with pytest.raises(HistoryConflictError, match="seal"):
        await store.record_feed_state(
            session_id, history_pb2.SessionFeedState(attached=direct.attached, sealed=runner_pb2.Sealed(cursor=3))
        )


@pytest.mark.asyncio
async def test_row_without_runner_id_stores_followed_snapshot_under_public_id(engine: AsyncEngine) -> None:
    store = Store(engine)
    session_id, uid = uuid4(), uuid4()

    async def record(*, sandbox_name: str = "worker", runner_session_id: str | None = None) -> None:
        await store.record_session(
            session_id,
            sandbox_namespace="testing",
            sandbox_name=sandbox_name,
            sandbox_uid=uid,
            runner_session_id=runner_session_id,
        )

    await record()
    await record()
    with pytest.raises(HistoryConflictError, match="locator"):
        await record(sandbox_name="other")
    with pytest.raises(HistoryConflictError, match="locator"):
        await record(runner_session_id="learned-later")
    await store.append(session_id, [entry(1)])
    followed = history_pb2.SessionFeedState(attached=runner_pb2.Attached(session_id=str(session_id), last_cursor=1))
    await store.record_feed_state(session_id, followed)
    assert (await store.read_page(session_id)).feed_state == followed
    followed.attached.session_id = "some-runner"
    with pytest.raises(HistoryConflictError, match="bound runner"):
        await store.record_feed_state(session_id, followed)


def settled(page: history_pb2.ReadSessionEventsResponse) -> list[tuple[int, int]]:
    return [(cursor_range.first, cursor_range.last) for item in page.settlements for cursor_range in item.ranges]


@pytest.mark.asyncio
async def test_settling_store_drops_matched_chunks_once_their_item_completes(engine: AsyncEngine) -> None:
    store = Store(engine, settle_deltas=True)
    session_id = uuid4()
    await opened(store, session_id)
    # 1-2 start, 3-6 two chunk pairs, 7-8 completion; the completion arrives in a later batch.
    span = claude_text(["Hel", "lo"]).entries
    assert await store.append(session_id, span[:6]) == 6
    assert [entry.cursor for entry in (await store.read_page(session_id)).entries] == [1, 2, 3, 4, 5, 6]
    assert await store.append(session_id, span[6:]) == 8
    page = await store.read_page(session_id)
    assert ([entry.cursor for entry in page.entries], settled(page)) == ([1, 2, 7, 8], [(3, 6)])
    assert page.settlements[0].completion_cursor == 8
    # A runner replaying the whole span, settled chunks included, is still an exact duplicate.
    assert await store.append(session_id, span) == 8
    with pytest.raises(HistoryConflictError):
        await store.append(session_id, [span[0], *span[1:6], entry(7)])
    # A page whose window holds the gap or the completion reports the settlement.
    for after, expected in ((2, [7]), (7, [8])):
        page = await store.read_page(session_id, after_cursor=after, limit=1)
        assert ([entry.cursor for entry in page.entries], settled(page)) == (expected, [(3, 6)])
    assert settled(await store.read_page(session_id, limit=2)) == []
    assert await store.read_observations(session_id, after_cursor=0) == ObservationPage(
        8, [(1, "native"), (2, "item_started"), (7, "native"), (8, "item_completed")], [(3, 6)]
    )
    assert await store.read_observations(session_id, limit=2) == ObservationPage(
        8, [(7, "native"), (8, "item_completed")], []
    )
    assert (await store.read_observations(session_id, limit=3)).settled == [(3, 6)]


@pytest.mark.asyncio
async def test_an_item_without_native_frames_completes_unsettled(engine: AsyncEngine) -> None:
    store = Store(engine, settle_deltas=True)
    session_id = uuid4()
    await opened(store, session_id)
    events = [
        event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="test-reply")),
        event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-reply", text="Hi")),
        event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="test-reply", text="Hi")),
    ]
    span = [
        event_log_pb2.EventEntry(
            cursor=cursor, origin=event_log_pb2.EventOrigin(source_id="source", sequence=cursor), event=event
        )
        for cursor, event in enumerate(events, start=1)
    ]
    assert await store.append(session_id, span) == 3
    page = await store.read_page(session_id)
    assert (len(page.entries), settled(page)) == (3, [])


@pytest.mark.asyncio
async def test_default_store_keeps_every_entry(engine: AsyncEngine) -> None:
    store = Store(engine)
    session_id = uuid4()
    await opened(store, session_id)
    await store.append(session_id, claude_text(["Hel", "lo"]).entries)
    page = await store.read_page(session_id)
    assert (len(page.entries), settled(page)) == (8, [])


if __name__ == "__main__":
    pytest_bazel.main()
