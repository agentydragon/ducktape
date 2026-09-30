import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta

import pytest
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from tenacity import wait_fixed, wait_none

from devinfra.claude.session_export.api import SessionsApi
from devinfra.claude.session_export.conftest import (
    LIVE_WINDOW,
    RESUME_TOKEN,
    TEST_EPOCH,
    FakeSessionsService,
    SseConnection,
    eventually,
    make_event,
    make_events,
    make_session,
    make_stream_event,
)
from devinfra.claude.session_export.live import DISCOVERY, WATCH, LiveFollower
from devinfra.claude.session_export.store import EventRow, SessionStore
from devinfra.claude.session_export.sync import sync_once

ONE = "session_test0001"


@dataclass
class Following:
    follower: LiveFollower
    task: asyncio.Task[None]


@pytest.fixture
async def follow(api: SessionsApi, store: SessionStore) -> AsyncIterator[Callable[[], Following]]:
    """Starts a follower when the test says so: its first discovery pass runs at once, so the test seeds first."""
    started: list[asyncio.Task[None]] = []

    def start() -> Following:
        follower = LiveFollower(api, store, max_streams=2, window=LIVE_WINDOW, retry_wait=wait_none())
        started.append(asyncio.create_task(follower.run()))
        return Following(follower, started[-1])

    yield start
    for task in started:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def stream_opened(service: FakeSessionsService, session_id: str) -> bool:
    return bool(service.event_streams(session_id))


async def watch_opened(service: FakeSessionsService) -> bool:
    return bool(service.watches())


def page_reads(service: FakeSessionsService) -> list[str]:
    """The cursors of the oldest-first event page reads, in order."""
    return [
        r.url.params["cursor"]
        for r in service.requests
        if r.url.path.endswith("/events") and r.url.params["sort_order"] == "asc"
    ]


async def stored(engine: AsyncEngine, session_id: str) -> list[int]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(EventRow.sequence_num).where(EventRow.session_id == session_id).order_by(EventRow.sequence_num)
        )
        return list(rows.scalars())


async def seed_active_session(service: FakeSessionsService, api: SessionsApi, store: SessionStore, count: int) -> None:
    """The session is stored and listed as active, so the next discovery pass follows it."""
    service.events = {ONE: make_events(count)}
    service.statuses = {ONE: "active"}
    await sync_once(api, store, workers=1)
    service.requests.clear()


async def test_pushed_events_are_stored_as_they_arrive(
    service: FakeSessionsService,
    api: SessionsApi,
    store: SessionStore,
    engine: AsyncEngine,
    follow: Callable[[], Following],
) -> None:
    await seed_active_session(service, api, store, count=3)
    following = follow()
    await eventually(lambda: stream_opened(service, ONE))
    [stream] = service.event_streams(ONE)
    assert stream.request.url.params["from_sequence_num"] == "3"

    stream.send("client_event", make_stream_event(4), frame_id="4")
    stream.send("client_event", make_event(5), frame_id="5")

    async def stored_through_five() -> bool:
        return await stored(engine, ONE) == [1, 2, 3, 4, 5]

    await eventually(stored_through_five)
    assert page_reads(service) == ["3"]  # the catch-up before the stream opened, and nothing since
    assert following.follower.streams == 1
    assert following.follower.last_event_at is not None


async def test_a_gap_in_the_stream_is_paged_instead_of_stored_out_of_order(
    service: FakeSessionsService,
    api: SessionsApi,
    store: SessionStore,
    engine: AsyncEngine,
    follow: Callable[[], Following],
) -> None:
    await seed_active_session(service, api, store, count=3)
    follow()
    await eventually(lambda: stream_opened(service, ONE))
    service.events[ONE] = make_events(7)
    [first] = service.event_streams(ONE)
    first.send("client_event", make_event(7), frame_id="7")  # 4..6 never arrived

    async def reopened_after_seven() -> bool:
        streams = service.event_streams(ONE)
        return len(streams) == 2 and streams[1].request.url.params["from_sequence_num"] == "7"

    await eventually(reopened_after_seven)
    assert await stored(engine, ONE) == list(range(1, 8))
    assert page_reads(service) == ["3", "3"]


async def test_a_closed_stream_is_reopened_from_where_it_stopped_without_paging(
    service: FakeSessionsService,
    api: SessionsApi,
    store: SessionStore,
    engine: AsyncEngine,
    follow: Callable[[], Following],
) -> None:
    await seed_active_session(service, api, store, count=3)
    follow()
    await eventually(lambda: stream_opened(service, ONE))
    [first] = service.event_streams(ONE)
    first.send("client_event", make_event(4), frame_id="4")
    first.close()

    async def reopened_after_four() -> bool:
        streams = service.event_streams(ONE)
        return len(streams) == 2 and streams[1].request.url.params["from_sequence_num"] == "4"

    await eventually(reopened_after_four)
    assert await stored(engine, ONE) == [1, 2, 3, 4]
    assert page_reads(service) == ["3"]


async def test_a_server_that_cannot_resume_sends_the_follower_back_to_paging(
    service: FakeSessionsService,
    api: SessionsApi,
    store: SessionStore,
    engine: AsyncEngine,
    follow: Callable[[], Following],
) -> None:
    await seed_active_session(service, api, store, count=3)
    follow()
    await eventually(lambda: stream_opened(service, ONE))
    service.events[ONE] = make_events(6)
    [first] = service.event_streams(ONE)
    first.send("catch_up_truncated")

    async def reopened_after_six() -> bool:
        streams = service.event_streams(ONE)
        return len(streams) == 2 and streams[1].request.url.params["from_sequence_num"] == "6"

    await eventually(reopened_after_six)
    assert await stored(engine, ONE) == list(range(1, 7))


async def test_discovery_follows_a_new_session_and_stops_when_it_is_archived(
    service: FakeSessionsService, engine: AsyncEngine, follow: Callable[[], Following]
) -> None:
    service.events = {ONE: make_events(2)}  # never seen by a polling cycle
    service.statuses = {ONE: "active"}
    following = follow()

    async def following_one() -> bool:
        return following.follower.streams == 1 and bool(service.event_streams(ONE))

    await eventually(following_one)
    assert await stored(engine, ONE) == [1, 2]  # paged on the first connection

    service.statuses[ONE] = "archived"
    following.follower.refresh()

    async def dropped() -> bool:
        return following.follower.streams == 0

    await eventually(dropped)


async def test_the_watch_starts_following_a_session_between_discovery_passes(
    service: FakeSessionsService, engine: AsyncEngine, follow: Callable[[], Following]
) -> None:
    following = follow()
    await eventually(lambda: watch_opened(service))
    [watch] = service.watches()
    assert watch.request.url.params["resume_token"] == RESUME_TOKEN
    assert following.follower.watching

    service.events = {ONE: make_events(2)}  # not listed until discovery next runs
    service.statuses = {ONE: "active"}
    watch.send("added", service.list_item(ONE), frame_id="cursor-1")

    async def following_one() -> bool:
        return following.follower.streams == 1 and bool(service.event_streams(ONE))

    await eventually(following_one)
    assert await stored(engine, ONE) == [1, 2]

    service.statuses[ONE] = "archived"
    watch.send("changed", service.list_item(ONE), frame_id="cursor-2")

    async def dropped() -> bool:
        return following.follower.streams == 0

    await eventually(dropped)


async def test_a_lost_watch_position_takes_a_fresh_resume_token(
    service: FakeSessionsService, follow: Callable[[], Following]
) -> None:
    service.stream_refusals = [410]
    follow()
    await eventually(lambda: watch_opened(service))
    token_probes = [r for r in service.requests if r.url.path == "/v1/code/sessions" and r.url.params["limit"] == "1"]
    assert len(token_probes) == 2  # the first token, then the fresh one after the 410


async def test_each_watch_connection_takes_a_fresh_resume_token(
    service: FakeSessionsService, follow: Callable[[], Following]
) -> None:
    def ended_by_the_server(stream: SseConnection) -> None:
        stream.send("sync", frame_id="cursor-1")
        stream.close()

    service.on_open = [ended_by_the_server]
    follow()

    async def watched_twice() -> bool:
        return len(service.watches()) == 2

    await eventually(watched_twice)
    token_probes = [r for r in service.requests if r.url.path == "/v1/code/sessions" and r.url.params["limit"] == "1"]
    assert len(token_probes) == 2  # the server answers an old token with a watch that delivers nothing


async def test_a_refused_watch_is_reported_with_its_reason_while_discovery_carries_on(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore, engine: AsyncEngine
) -> None:
    service.events = {ONE: make_events(2)}
    service.statuses = {ONE: "active"}
    service.stream_refusals = [404] * 1000  # every stream open, so the session's own stream is refused too
    follower = LiveFollower(api, store, max_streams=2, window=LIVE_WINDOW, retry_wait=wait_fixed(0.01))
    running = asyncio.create_task(follower.run())
    try:

        async def watch_reported() -> bool:
            return any(problem.source == WATCH for problem in follower.problems)

        await eventually(watch_reported)
        [problem] = [p for p in follower.problems if p.source == WATCH]
        assert "404 Not Found from GET /v1/code/sessions/watch" in problem.message

        async def discovery_stored_the_session() -> bool:  # the refused watch did not stop discovery
            return (await store.synced_last_event_at()).keys() == {ONE}

        await eventually(discovery_stored_the_session)

        service.stream_refusals.clear()

        async def watching_again() -> bool:
            return follower.watching and not any(p.source == WATCH for p in follower.problems)

        await eventually(watching_again)
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running


async def test_a_failing_discovery_is_reported_with_its_reason_and_clears_once_it_works(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore
) -> None:
    service.list_status = 404  # the watch fetches its token from this route too, so it fails alongside
    follower = LiveFollower(api, store, max_streams=2, window=LIVE_WINDOW, retry_wait=wait_fixed(0.01))
    running = asyncio.create_task(follower.run())
    try:

        async def reported() -> bool:
            return any(problem.source == DISCOVERY for problem in follower.problems)

        await eventually(reported)
        [problem] = [p for p in follower.problems if p.source == DISCOVERY]
        assert "404 Not Found from GET /v1/code/sessions" in problem.message
        assert "refused" in problem.message  # the API's own error body

        service.list_status = None

        async def cleared() -> bool:
            return follower.problems == []

        await eventually(cleared)
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running


async def test_cancelling_the_follower_stops_it_with_streams_open(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore, follow: Callable[[], Following]
) -> None:
    await seed_active_session(service, api, store, count=3)
    following = follow()
    await eventually(lambda: stream_opened(service, ONE))

    following.task.cancel()
    async with asyncio.timeout(10):  # a retry loop that treated cancellation as a failure would reconnect instead
        with pytest.raises(asyncio.CancelledError):
            await following.task


async def test_only_recent_unarchived_sessions_are_worth_a_stream(store: SessionStore) -> None:
    since = TEST_EPOCH + timedelta(days=1)
    day = timedelta(days=1)
    await store.upsert_sessions(
        [
            make_session("session_recent0001", status="active", last_event_at=(since + day).isoformat()),
            make_session("session_recent0002", status="paused", last_event_at=(since + 2 * day).isoformat()),
            make_session("session_recent0003", status="active", last_event_at=(since + 3 * day).isoformat()),
            make_session("session_archived", status="archived", last_event_at=(since + 4 * day).isoformat()),
            make_session("session_stale", status="active", last_event_at=(since - day).isoformat()),
        ]
    )
    assert await store.live_session_ids(active_since=since, limit=10) == [
        "session_recent0003",
        "session_recent0002",
        "session_recent0001",
    ]
    assert await store.live_session_ids(active_since=since, limit=2) == ["session_recent0003", "session_recent0002"]


if __name__ == "__main__":
    pytest_bazel.main()
