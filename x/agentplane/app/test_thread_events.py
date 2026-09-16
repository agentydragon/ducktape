"""Following a Thread over Connect: what a browser is promised when it opens a stream, resumes one,
or asks for a prefix the archive cannot prove.

The server is a real one. An in-process ASGI transport buffers a response, and every claim here is
about what arrives before the stream ends.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
import uvicorn
from google.protobuf.timestamp_pb2 import Timestamp
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from x.agentplane.app.action_policy import ActionPolicyInventory
from x.agentplane.app.api import create_app
from x.agentplane.app.bridge import RunnerBridge
from x.agentplane.app.conftest import AGENT_AUTH
from x.agentplane.app.connect import RPC_PREFIX
from x.agentplane.app.decisions import DecisionsClient
from x.agentplane.app.egress import EgressInventory
from x.agentplane.app.identity import TokenReviewer
from x.agentplane.app.inventory import SandboxInventory
from x.agentplane.app.live import LiveIndex
from x.agentplane.app.presets import Harness
from x.agentplane.app.shutdown import drain_of
from x.agentplane.app.thread_events_pb2 import FollowEventsRequest, FollowEventsResponse
from x.agentplane.app.trajectory import IngestionLease, TrajectoryStore
from x.agentplane.protocol import event_log_pb2, event_pb2
from x.agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

SANDBOX = "events-test-sandbox"
SESSION = "events-1"
SOURCE = "events-test-runner"
SPEC = protocol_pb2.SessionSpec(
    harness=protocol_pb2.HARNESS_CLAUDE, cwd="/state/work", model="events-model", reasoning_effort="low"
)
# The URL a browser posts to, which is the mount prefix plus the service name protoc emitted. It is
# the wire contract a generated client is built against, so it is spelled out rather than derived.
FOLLOW_PATH = f"{RPC_PREFIX}/ducktape.agentplane.app.v1.ThreadEvents/FollowEvents"
CONNECT_PROTO = "application/connect+proto"
# No frame any of these tests waits for depends on a clock, so one that has not arrived within this
# is one the server is not going to send.
FRAME_S = 20


def _event(cursor: int, **observation: object) -> event_log_pb2.EventEntry:
    at = Timestamp()
    at.FromDatetime(datetime(2026, 9, 16, 12, 0, cursor, tzinfo=UTC))
    event = event_pb2.Event(at=at, **observation)  # type: ignore[arg-type]
    return event_log_pb2.EventEntry(
        cursor=cursor, origin=event_log_pb2.EventOrigin(source_id=SOURCE, sequence=cursor), event=event
    )


def _turn(cursor: int, turn_id: str) -> event_log_pb2.EventEntry:
    return _event(cursor, turn_started=event_pb2.TurnStarted(turn_id=turn_id))


@pytest.fixture
async def lease(store: TrajectoryStore) -> IngestionLease:
    acquired = await store.acquire_ingestion(SANDBOX, timedelta(minutes=1))
    assert acquired is not None
    return acquired


@pytest.fixture
async def thread_id(store: TrajectoryStore, lease: IngestionLease) -> UUID:
    """A Thread with an attached feed and three archived Events: the state a browser ordinarily
    opens one in."""
    thread = await store.thread(SANDBOX, SESSION, SPEC)
    await store.set_attached(
        thread,
        protocol_pb2.Attached(
            session_id=SESSION, spec=SPEC, last_cursor=3, harness_state=protocol_pb2.HARNESS_STATE_RUNNING
        ),
        lease=lease,
    )
    await store.record(
        thread,
        [
            _event(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=11)),
            _turn(2, "t1"),
            _event(3, turn_completed=event_pb2.TurnCompleted(turn_id="t1", status=event_pb2.TURN_STATUS_COMPLETED)),
        ],
        lease=lease,
    )
    return thread


@pytest.fixture
async def app_url(
    store: TrajectoryStore,
    inventory: SandboxInventory,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
) -> AsyncIterator[str]:
    async def address_of(name: str) -> str:
        raise AssertionError(f"following the archive must not dial a runner, but {name!r} was asked for")

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    app = create_app(
        inventory,
        RunnerBridge(address_of=address_of, store=store),
        store,
        {harness: ["events-model"] for harness in Harness},
        egress,
        decisions,
        live_index,
        action_policy,
        reviewer=reviewer,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.1), retry=retry_if_exception_type(OSError)
    ):
        with attempt:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        # As `main.py` does: a followed stream waits for as long as its reader keeps it open, and
        # Uvicorn will not finish shutting down while one is still answering.
        drain_of(app).begin()
        server.should_exit = True
        await serving


class StreamError(Exception):
    """The Connect error that ended a stream, carried in its last envelope rather than as a status.

    A server that has already begun answering cannot take the status back, so this is where a
    stream's failure lives even when nothing was sent before it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@pytest.fixture
async def client(app_url: str) -> AsyncIterator[httpx.AsyncClient]:
    # No read timeout: idleness is what a follower does, and `_next` is what bounds these tests.
    async with httpx.AsyncClient(
        base_url=app_url, headers=AGENT_AUTH, timeout=httpx.Timeout(None, connect=10.0)
    ) as session:
        yield session


def _enveloped(request: FollowEventsRequest) -> bytes:
    payload = request.SerializeToString()
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


async def _decode(response: httpx.Response) -> AsyncIterator[FollowEventsResponse]:
    """The Connect streaming envelopes on the wire: a flag byte, a big-endian length, the message.

    Read here rather than through the generated client, whose async implementation buffers the
    whole response body before yielding its first message and so cannot follow an open stream at
    all. `@connectrpc/connect-web`, which is what actually consumes this, does stream.
    """
    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        buffer += chunk
        while len(buffer) >= 5 and len(buffer) >= 5 + int.from_bytes(buffer[1:5], "big"):
            end = 5 + int.from_bytes(buffer[1:5], "big")
            flags, payload = buffer[0], bytes(buffer[5:end])
            del buffer[:end]
            if not flags & 0b10:
                yield FollowEventsResponse.FromString(payload)
                continue
            if error := json.loads(payload).get("error"):
                raise StreamError(error.get("code", ""), error.get("message", ""))
            return


@asynccontextmanager
async def _following(
    client: httpx.AsyncClient, thread_id: UUID, after: int = 0
) -> AsyncIterator[AsyncIterator[FollowEventsResponse]]:
    request = FollowEventsRequest(thread_id=str(thread_id), after_cursor=after)
    async with client.stream(
        "POST", FOLLOW_PATH, headers={"content-type": CONNECT_PROTO}, content=_enveloped(request)
    ) as response:
        assert response.status_code == 200, (await response.aread()).decode()
        yield _decode(response)


async def _next(stream: AsyncIterator[FollowEventsResponse]) -> FollowEventsResponse:
    async with asyncio.timeout(FRAME_S):
        return await anext(stream)


async def _entries(stream: AsyncIterator[FollowEventsResponse], count: int) -> list[int]:
    """The cursors of the next `count` entry frames, skipping whatever is interleaved with them."""
    cursors: list[int] = []
    while len(cursors) < count:
        frame = await _next(stream)
        if frame.WhichOneof("frame") == "entry":
            cursors.append(frame.entry.cursor)
    return cursors


async def _drained(stream: AsyncIterator[FollowEventsResponse]) -> list[FollowEventsResponse]:
    async with asyncio.timeout(FRAME_S):
        return [frame async for frame in stream]


async def test_a_follower_is_told_the_stream_is_open_before_the_thread_produces_anything(
    client: httpx.AsyncClient, thread_id: UUID
) -> None:
    """Connect withholds response headers until the first message, so until the server says
    something, a stream that opened and a request that stalled look the same to a caller."""
    async with _following(client, thread_id) as stream:
        assert (await _next(stream)).WhichOneof("frame") == "heartbeat"


async def test_following_replays_the_archived_prefix_and_then_carries_what_arrives_live(
    client: httpx.AsyncClient, thread_id: UUID, store: TrajectoryStore, lease: IngestionLease
) -> None:
    async with _following(client, thread_id) as stream:
        assert (await _next(stream)).WhichOneof("frame") == "heartbeat"
        # Before the entries it bounds: a caller compares this cursor against its own prefix to
        # tell replay from live, which it cannot do if the promise arrives after the replay.
        attached = await _next(stream)
        assert attached.attached.last_cursor == 3
        assert attached.attached.spec.model == "events-model"

        assert await _entries(stream, 3) == [1, 2, 3]
        await store.record(thread_id, [_turn(4, "t2")], lease=lease)
        assert await _entries(stream, 1) == [4]


async def test_following_resumes_from_the_cursor_the_caller_already_holds(
    client: httpx.AsyncClient, thread_id: UUID
) -> None:
    async with _following(client, thread_id, after=2) as stream:
        assert await _entries(stream, 1) == [3]


async def test_two_followers_of_one_thread_each_read_from_where_their_own_caller_is(
    client: httpx.AsyncClient, thread_id: UUID, store: TrajectoryStore, lease: IngestionLease
) -> None:
    """A second tab is not a second subscription to a shared cursor."""
    async with _following(client, thread_id) as first, _following(client, thread_id, after=2) as second:
        assert await _entries(first, 3) == [1, 2, 3]
        assert await _entries(second, 1) == [3]
        await store.record(thread_id, [_turn(4, "t2")], lease=lease)
        assert await _entries(first, 1) == [4]
        assert await _entries(second, 1) == [4]


async def test_an_ended_feed_ends_the_stream_behind_every_entry_it_committed(
    client: httpx.AsyncClient, thread_id: UUID, store: TrajectoryStore, lease: IngestionLease
) -> None:
    """Ingestion ending is not the stream failing, and the end never overtakes the prefix: a reader
    that sees it has seen everything the feed archived."""
    await store.record(thread_id, [_turn(4, "t2")], lease=lease)
    await store.end_feed(thread_id, lease=lease, error=None)

    async with _following(client, thread_id) as stream:
        frames = await _drained(stream)
    assert [frame.entry.cursor for frame in frames if frame.WhichOneof("frame") == "entry"] == [1, 2, 3, 4]
    assert frames[-1].WhichOneof("frame") == "ended"
    assert not frames[-1].ended.HasField("error")


async def test_a_feed_that_could_not_continue_says_why_rather_than_failing_the_rpc(
    client: httpx.AsyncClient, thread_id: UUID, store: TrajectoryStore, lease: IngestionLease
) -> None:
    """A caller has to tell "this Thread is over" from "your connection broke": the first is a frame
    it can render, the second is a stream to reopen from the cursor it holds."""
    await store.end_feed(thread_id, lease=lease, error="runner log cursor regressed")

    async with _following(client, thread_id) as stream:
        frames = await _drained(stream)
    assert frames[-1].ended.error == "runner log cursor regressed"


async def test_a_cursor_beyond_the_archived_prefix_is_refused_rather_than_waited_out(
    client: httpx.AsyncClient, thread_id: UUID
) -> None:
    """It names a prefix this Thread cannot prove, so its holder has not merely run ahead: it has
    the wrong history, and must not be left waiting for the archive to grow into it."""
    with pytest.raises(StreamError) as refused:
        async with _following(client, thread_id, after=4) as stream:
            await _entries(stream, 1)
    assert refused.value.code == "invalid_argument"


async def test_an_unknown_thread_is_not_an_empty_one(client: httpx.AsyncClient) -> None:
    with pytest.raises(StreamError) as refused:
        async with _following(client, uuid4()) as stream:
            await _entries(stream, 1)
    assert refused.value.code == "not_found"


async def test_the_rpc_mount_refuses_a_caller_it_cannot_identify(app_url: str) -> None:
    """The Connect surface is not a second door into the app. A mount inherits no route dependency,
    so this is what says the guard is installed at all; that a credentialed call reaches the archive
    is the rest of this file."""
    async with httpx.AsyncClient(timeout=FRAME_S) as anonymous:
        refused = await anonymous.post(f"{app_url}{FOLLOW_PATH}", headers={"content-type": CONNECT_PROTO}, content=b"")
    assert refused.status_code == 401, refused.text


if __name__ == "__main__":
    pytest_bazel.main()
