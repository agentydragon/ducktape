import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime

import pytest
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from tenacity import wait_fixed, wait_none
from tenacity.wait import wait_base

from devinfra.claude.session_export.api import SessionsApi
from devinfra.claude.session_export.conftest import (
    LIVE_WINDOW,
    ONE,
    RESUME_TOKEN,
    SESSION_STATUS_ACTIVE,
    FakeSessionsService,
    SseConnection,
    background,
    eventually,
    make_delivery_update,
    make_event,
    make_events,
    make_stream_event,
)
from devinfra.claude.session_export.live import DISCOVERY, WATCH, LiveFollower
from devinfra.claude.session_export.models import SESSION_STATUS_ARCHIVED, parse_timestamp
from devinfra.claude.session_export.store import EventRow, SessionStore
from devinfra.claude.session_export.sync import sync_once

NO_WAIT = wait_none()


@dataclass
class Following:
    follower: LiveFollower
    task: asyncio.Task[None]


Follow = Callable[..., Awaitable[Following]]


@pytest.fixture
async def follow(api: SessionsApi, store: SessionStore) -> AsyncIterator[Follow]:
    """Starts a follower when the test says so: its first discovery pass runs at once, so the test seeds first."""
    async with AsyncExitStack() as stack:

        async def start(*, retry_wait: wait_base = NO_WAIT) -> Following:
            follower = LiveFollower(api, store, max_streams=2, window=LIVE_WINDOW, retry_wait=retry_wait)
            return Following(follower, await stack.enter_async_context(background(follower.run())))

        yield start


async def stream_opened(service: FakeSessionsService, session_id: str) -> bool:
    return bool(service.event_streams(session_id))


async def watch_opened(service: FakeSessionsService) -> bool:
    return bool(service.watches())


async def following_one(service: FakeSessionsService, following: Following) -> bool:
    return following.follower.streams == 1 and await stream_opened(service, ONE)


async def dropped(following: Following) -> bool:
    return following.follower.streams == 0


async def reopened_after(service: FakeSessionsService, sequence_num: int) -> bool:
    """The session's stream has been opened a second time, resuming from `sequence_num`."""
    streams = service.event_streams(ONE)
    return len(streams) == 2 and streams[1].request.url.params["from_sequence_num"] == str(sequence_num)


def page_reads(service: FakeSessionsService) -> list[str]:
    """The cursors of the oldest-first event page reads, in order."""
    return [r.url.params["cursor"] for r in service.event_reads()]


async def stored(engine: AsyncEngine, session_id: str) -> list[int]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(EventRow.sequence_num).where(EventRow.session_id == session_id).order_by(EventRow.sequence_num)
        )
        return list(rows.scalars())


async def stamps(engine: AsyncEngine, session_id: str, sequence_num: int) -> tuple[datetime | None, ...]:
    """`received_at`, `processing_at` and `processed_at` of one stored event."""
    async with engine.connect() as connection:
        row = await connection.execute(
            select(EventRow.received_at, EventRow.processing_at, EventRow.processed_at).where(
                EventRow.session_id == session_id, EventRow.sequence_num == sequence_num
            )
        )
        return tuple(row.one())


async def seed_active_session(service: FakeSessionsService, api: SessionsApi, store: SessionStore, count: int) -> None:
    """The session is stored and listed as active, so the next discovery pass follows it."""
    service.events = {ONE: make_events(count)}
    service.statuses = {ONE: SESSION_STATUS_ACTIVE}
    await sync_once(api, store, workers=1)
    service.requests.clear()


async def test_pushed_events_are_stored_as_they_arrive(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore, engine: AsyncEngine, follow: Follow
) -> None:
    await seed_active_session(service, api, store, count=3)
    following = await follow()
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
    assert following.follower.followed == {ONE}
    assert following.follower.last_event_at is not None


async def test_a_delivery_update_stores_the_stamps_the_pushed_event_lacked(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore, engine: AsyncEngine, follow: Follow
) -> None:
    await seed_active_session(service, api, store, count=3)
    await follow()
    await eventually(lambda: stream_opened(service, ONE))
    [stream] = service.event_streams(ONE)
    received, processing = "2026-02-01T00:01:00+00:00", "2026-02-01T00:02:00+00:00"

    stream.send("client_event", {**make_stream_event(4), "source": "client"}, frame_id="4")
    service.events[ONE].append(make_event(4, source="client", received_at=received))
    stream.send("delivery_update", make_delivery_update(4, "DELIVERY_STATUS_RECEIVED"))

    async def received_is_stored() -> bool:
        return await stamps(engine, ONE, 4) == (parse_timestamp(received), None, None)

    await eventually(received_is_stored)

    service.events[ONE][3] = make_event(4, source="client", received_at=received, processing_at=processing)
    stream.send("delivery_update", make_delivery_update(4, "DELIVERY_STATUS_PROCESSING"))

    async def processing_is_stored() -> bool:
        return await stamps(engine, ONE, 4) == (parse_timestamp(received), parse_timestamp(processing), None)

    await eventually(processing_is_stored)


async def test_a_delivery_update_for_an_event_that_is_not_stored_is_reported_and_the_stream_goes_on(
    service: FakeSessionsService,
    api: SessionsApi,
    store: SessionStore,
    engine: AsyncEngine,
    follow: Follow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await seed_active_session(service, api, store, count=3)
    await follow()
    await eventually(lambda: stream_opened(service, ONE))
    [stream] = service.event_streams(ONE)

    stream.send("delivery_update", make_delivery_update(99, "DELIVERY_STATUS_RECEIVED"))
    stream.send("client_event", make_event(4), frame_id="4")

    async def stored_through_four() -> bool:
        return await stored(engine, ONE) == [1, 2, 3, 4]

    await eventually(stored_through_four)
    assert any(
        record.levelno == logging.WARNING and "an event that is not stored" in record.getMessage()
        for record in caplog.records
    )


async def test_a_gap_in_the_stream_is_paged_instead_of_stored_out_of_order(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore, engine: AsyncEngine, follow: Follow
) -> None:
    await seed_active_session(service, api, store, count=3)
    await follow()
    await eventually(lambda: stream_opened(service, ONE))
    service.events[ONE] = make_events(7)
    [first] = service.event_streams(ONE)
    first.send("client_event", make_event(7), frame_id="7")  # 4..6 never arrived

    await eventually(lambda: reopened_after(service, 7))
    assert await stored(engine, ONE) == list(range(1, 8))
    assert page_reads(service) == ["3", "3"]


async def test_a_closed_stream_is_reopened_from_where_it_stopped_without_paging(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore, engine: AsyncEngine, follow: Follow
) -> None:
    await seed_active_session(service, api, store, count=3)
    await follow()
    await eventually(lambda: stream_opened(service, ONE))
    [first] = service.event_streams(ONE)
    first.send("client_event", make_event(4), frame_id="4")
    first.close()

    await eventually(lambda: reopened_after(service, 4))
    assert await stored(engine, ONE) == [1, 2, 3, 4]
    assert page_reads(service) == ["3"]


async def test_a_server_that_cannot_resume_sends_the_follower_back_to_paging(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore, engine: AsyncEngine, follow: Follow
) -> None:
    await seed_active_session(service, api, store, count=3)
    await follow()
    await eventually(lambda: stream_opened(service, ONE))
    service.events[ONE] = make_events(6)
    [first] = service.event_streams(ONE)
    first.send("catch_up_truncated")

    await eventually(lambda: reopened_after(service, 6))
    assert await stored(engine, ONE) == list(range(1, 7))


async def test_discovery_follows_a_new_session_and_stops_when_it_is_archived(
    service: FakeSessionsService, engine: AsyncEngine, follow: Follow
) -> None:
    service.events = {ONE: make_events(2)}  # never seen by a polling cycle
    service.statuses = {ONE: SESSION_STATUS_ACTIVE}
    following = await follow()

    await eventually(lambda: following_one(service, following))
    assert await stored(engine, ONE) == [1, 2]  # paged on the first connection

    service.statuses[ONE] = SESSION_STATUS_ARCHIVED
    following.follower.refresh()

    await eventually(lambda: dropped(following))


async def test_the_watch_starts_following_a_session_between_discovery_passes(
    service: FakeSessionsService, engine: AsyncEngine, follow: Follow
) -> None:
    following = await follow()
    await eventually(lambda: watch_opened(service))
    [watch] = service.watches()
    assert watch.request.url.params["resume_token"] == RESUME_TOKEN
    assert following.follower.watching

    service.events = {ONE: make_events(2)}  # not listed until discovery next runs
    service.statuses = {ONE: SESSION_STATUS_ACTIVE}
    watch.send("added", service.list_item(ONE), frame_id="cursor-1")

    await eventually(lambda: following_one(service, following))
    assert await stored(engine, ONE) == [1, 2]

    service.statuses[ONE] = SESSION_STATUS_ARCHIVED
    watch.send("changed", service.list_item(ONE), frame_id="cursor-2")

    await eventually(lambda: dropped(following))


async def test_a_lost_watch_position_takes_a_fresh_resume_token(service: FakeSessionsService, follow: Follow) -> None:
    service.stream_refusals = [410]
    await follow()
    await eventually(lambda: watch_opened(service))
    token_probes = [r for r in service.requests if r.url.path == "/v1/code/sessions" and r.url.params["limit"] == "1"]
    assert len(token_probes) == 2  # the first token, then the fresh one after the 410


async def test_each_watch_connection_takes_a_fresh_resume_token(service: FakeSessionsService, follow: Follow) -> None:
    def ended_by_the_server(stream: SseConnection) -> None:
        stream.send("sync", frame_id="cursor-1")
        stream.close()

    service.on_open = [ended_by_the_server]
    await follow()

    async def watched_twice() -> bool:
        return len(service.watches()) == 2

    await eventually(watched_twice)
    token_probes = [r for r in service.requests if r.url.path == "/v1/code/sessions" and r.url.params["limit"] == "1"]
    assert len(token_probes) == 2  # the server answers an old token with a watch that delivers nothing


async def test_a_refused_watch_is_reported_with_its_reason_while_discovery_carries_on(
    service: FakeSessionsService, store: SessionStore, follow: Follow
) -> None:
    service.events = {ONE: make_events(2)}
    service.statuses = {ONE: SESSION_STATUS_ACTIVE}
    service.stream_refusals = [404] * 1000  # every stream open, so the session's own stream is refused too
    follower = (await follow(retry_wait=wait_fixed(0.01))).follower

    async def watch_reported() -> bool:
        return any(problem.source == WATCH for problem in follower.problems)

    await eventually(watch_reported)
    [problem] = [p for p in follower.problems if p.source == WATCH]
    assert "404" in problem.message
    assert "/v1/code/sessions/watch" in problem.message
    assert RESUME_TOKEN not in problem.message  # the request's query string, which carries it, stays out

    async def discovery_stored_the_session() -> bool:  # the refused watch did not stop discovery
        return (await store.synced_last_event_at()).keys() == {ONE}

    await eventually(discovery_stored_the_session)

    service.stream_refusals.clear()

    async def watching_again() -> bool:
        return follower.watching and not any(p.source == WATCH for p in follower.problems)

    await eventually(watching_again)


async def test_a_failing_discovery_is_reported_with_its_reason_and_clears_once_it_works(
    service: FakeSessionsService, follow: Follow
) -> None:
    service.list_status = 404  # the watch fetches its token from this route too, so it fails alongside
    follower = (await follow(retry_wait=wait_fixed(0.01))).follower

    async def reported() -> bool:
        return any(problem.source == DISCOVERY for problem in follower.problems)

    await eventually(reported)
    [problem] = [p for p in follower.problems if p.source == DISCOVERY]
    assert "404" in problem.message
    assert "/v1/code/sessions" in problem.message
    assert "refused" in problem.message  # the API's own error body

    service.list_status = None

    async def cleared() -> bool:
        return follower.problems == []

    await eventually(cleared)


async def test_cancelling_the_follower_stops_it_with_streams_open(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore, follow: Follow
) -> None:
    await seed_active_session(service, api, store, count=3)
    following = await follow()
    await eventually(lambda: stream_opened(service, ONE))

    following.task.cancel()
    async with asyncio.timeout(10):  # a retry loop that treated cancellation as a failure would reconnect instead
        with pytest.raises(asyncio.CancelledError):
            await following.task


if __name__ == "__main__":
    pytest_bazel.main()
