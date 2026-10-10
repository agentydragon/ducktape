"""Real PostgreSQL exercises transactional history, including independent replay."""

import asyncio
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.protocol import event_log_pb2
from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.session_history.store import (
    HistoryConflictError,
    HistoryNotFoundError,
    ObservationPage,
    OpenReservation,
    Store,
)
from agentplane.sandbox_service.testing.delta_spans import claude_text

# gazelle:include_dep @pypi//protobuf


def entry(cursor: int, source: str = "source", *, resumed: bool = False) -> event_log_pb2.EventEntry:
    result = event_log_pb2.EventEntry(cursor=cursor)
    result.origin.source_id = source
    result.origin.sequence = cursor
    result.event.harness_started.resumed = resumed
    return result


async def opened(store: Store, session_id: UUID) -> None:
    await store.open(
        session_id,
        sandbox_namespace="testing",
        sandbox_name="gone",
        sandbox_uid=None,
        runner_session_id="original-native-path",
    )


@pytest.mark.asyncio
async def test_replay_survives_new_store_and_deleted_sandbox(engine: AsyncEngine) -> None:
    session_id = uuid4()
    store = Store(engine, settle_deltas=False)
    await opened(store, session_id)
    await opened(store, session_id)
    native = entry(3)
    native.event.ClearField("harness_started")
    native.event.native.line = "frame with a NUL\x00 and provider bytes"
    assert await store.append(session_id, [entry(1), entry(2, resumed=True)]) == 2
    assert await Store(engine, settle_deltas=False).append(session_id, [entry(1), entry(2, resumed=True), native]) == 3
    high_water, events = await Store(engine, settle_deltas=False).read(session_id, after_cursor=1, limit=2)
    assert high_water == 3
    assert events == [entry(2, resumed=True), native]
    assert await Store(engine, settle_deltas=False).read(session_id, after_cursor=3) == (3, [])
    with pytest.raises(ValueError, match="beyond"):
        await store.read(session_id, after_cursor=4)


@pytest.mark.asyncio
async def test_bad_batch_rolls_back_and_conflicting_duplicate_rejected(engine: AsyncEngine) -> None:
    store = Store(engine, settle_deltas=False)
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
        await store.open(
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
    left, right = Store(engine, settle_deltas=False), Store(engine, settle_deltas=False)
    await opened(left, session_id)
    results = await asyncio.gather(
        left.append(session_id, [entry(1), entry(2)]), right.append(session_id, [entry(1), entry(2)])
    )
    assert list(results) == [2, 2]
    assert await right.read(session_id) == (2, [entry(1), entry(2)])


async def store_retry_with_new_defaults(store: Store, sandbox_uid: UUID) -> OpenReservation:
    return await store.reserve(
        caller_namespace="testing",
        caller_name="app",
        sandbox_namespace="testing",
        sandbox_name="worker",
        sandbox_uid=sandbox_uid,
        open_key="open-1",
        open_request=b"spec",
        launch_spec=lambda _: b"changed default should be ignored",
        settle_deltas=None,
    )


@pytest.mark.asyncio
async def test_reservation_is_stable_across_retries_and_replicas(engine: AsyncEngine) -> None:
    left, right = Store(engine, settle_deltas=False), Store(engine, settle_deltas=False)
    uid = uuid4()

    async def reserve(
        store: Store,
        *,
        key: str = "open-1",
        caller_name: str = "app",
        sandbox_uid: UUID = uid,
        payload: bytes = b"spec",
    ) -> OpenReservation:
        return await store.reserve(
            caller_namespace="testing",
            caller_name=caller_name,
            sandbox_namespace="testing",
            sandbox_name="worker",
            sandbox_uid=sandbox_uid,
            open_key=key,
            open_request=payload,
            launch_spec=lambda candidate: f"frozen {candidate}".encode(),
            settle_deltas=None,
        )

    first, second = await asyncio.gather(reserve(left), reserve(right))
    assert first == second
    assert await reserve(Store(engine, settle_deltas=False)) == first
    assert await left.read(first.session_id) == (0, [])  # no runner was contacted
    assert first.launch_spec == f"frozen {first.session_id}".encode()
    assert (
        await left.runner_id(first.session_id, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid)
        == f"r-{first.session_id}"
    )
    assert (
        await right.session_id(
            sandbox_namespace="testing",
            sandbox_name="worker",
            sandbox_uid=uid,
            runner_session_id=f"r-{first.session_id}",
        )
        == first.session_id
    )
    with pytest.raises(HistoryNotFoundError):
        await left.runner_id(first.session_id, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uuid4())
    assert await store_retry_with_new_defaults(right, uid) == first
    assert await reserve(left, key="open-2") != first
    assert await reserve(left, caller_name="other") != first
    assert await reserve(left, sandbox_uid=uuid4()) != first
    with pytest.raises(HistoryConflictError, match="different inputs"):
        await reserve(left, payload=b"different caller request")
    assert await reserve(right) == first  # a bad retry does not change the reservation
    with pytest.raises(ValueError, match="required"):
        await reserve(left, key="")

    async def lookup(*, caller_name: str = "app", sandbox_uid: UUID = uid, key: str = "open-1") -> UUID | None:
        return await right.lookup_open(
            caller_namespace="testing",
            caller_name=caller_name,
            sandbox_namespace="testing",
            sandbox_name="worker",
            sandbox_uid=sandbox_uid,
            open_key=key,
        )

    assert await lookup() == first.session_id
    assert await lookup(caller_name="not-this-caller") is None
    assert await lookup(sandbox_uid=uuid4()) is None
    assert await lookup(key="absent") is None


@pytest.mark.asyncio
async def test_imported_history_keeps_its_id_and_fences_duplicate_locator(engine: AsyncEngine) -> None:
    store = Store(engine, settle_deltas=False)
    session_id, uid = uuid4(), uuid4()
    await store.open(
        session_id, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid, runner_session_id="s-1"
    )
    await store.open(
        session_id, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid, runner_session_id="s-1"
    )
    with pytest.raises(HistoryConflictError, match="belongs to another"):
        await store.open(
            uuid4(), sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid, runner_session_id="s-1"
        )
    # A nullable legacy UID must not permit another ID for the same legacy locator.
    await opened(store, uuid4())
    with pytest.raises(HistoryConflictError, match="belongs to another"):
        await opened(store, uuid4())
    # A new Open is identified by its caller key, not by this physical runner path.
    reservation = await store.reserve(
        caller_namespace="testing",
        caller_name="app",
        sandbox_namespace="testing",
        sandbox_name="worker",
        sandbox_uid=uid,
        open_key="new",
        open_request=b"spec",
        launch_spec=lambda _: b"effective spec",
        settle_deltas=None,
    )
    assert reservation.session_id != session_id


@pytest.mark.asyncio
async def test_observation_pages_seek_retained_prefix_without_returning_payloads(engine: AsyncEngine) -> None:
    store = Store(engine, settle_deltas=False)
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
    store = Store(engine, settle_deltas=False)
    session_id = uuid4()
    await opened(store, session_id)
    assert not (await store.read_page(session_id)).HasField("feed_state")
    feed = protocol_pb2.SessionFeedState(
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
    assert (await Store(engine, settle_deltas=False).read_page(session_id)).feed_state.ended
    feed.attached.harness_state = runner_pb2.HARNESS_STATE_RUNNING
    with pytest.raises(HistoryConflictError, match="conflicting attachment"):
        await store.record_feed_state(session_id, feed)
    await store.append(session_id, [entry(2, resumed=True)])
    feed.attached.last_cursor = 2
    await store.record_feed_state(session_id, feed)
    stale = protocol_pb2.SessionFeedState(
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


def settled(page: protocol_pb2.ReadSessionEventsResponse) -> list[tuple[int, int]]:
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
async def test_session_override_decides_over_service_default(engine: AsyncEngine) -> None:
    store = Store(engine, settle_deltas=False)
    kept = uuid4()
    await opened(store, kept)
    span = claude_text(["Hel", "lo"]).entries
    await store.append(kept, span)
    assert len((await store.read_page(kept)).entries) == 8

    reserved = await store.reserve(
        caller_namespace="testing",
        caller_name="app",
        sandbox_namespace="testing",
        sandbox_name="worker",
        sandbox_uid=uuid4(),
        open_key="open-settled",
        open_request=b"spec",
        launch_spec=lambda _: b"spec",
        settle_deltas=True,
    )
    await store.append(reserved.session_id, span)
    assert settled(await store.read_page(reserved.session_id)) == [(3, 6)]


if __name__ == "__main__":
    pytest_bazel.main()
