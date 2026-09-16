"""Does a browser's gRPC-Web reach this service through Envoy, and what does proving it cost?

The client here speaks gRPC-Web over plain HTTP, which is the wire a browser puts on the network.
`grpcio` cannot be that client: it speaks native gRPC to the server directly, skipping the very
hop this test exists to cover. So the framing is decoded by hand, exactly as it is for Connect in
x/agentplane/app/test_thread_events.py -- and for the same reason, that Python has no client for
the browser's protocol. Finding 6.

Spike. Findings are in README.md.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import grpc
import httpx
import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed
from testcontainers.core.container import DockerContainer

from third_party.containers import envoy_1_35
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.oci import load_oci_image
from x.agentplane.app import thread_events_pb2_grpc
from x.agentplane.app.grpcweb_spike.server import serve
from x.agentplane.app.shutdown import Drain
from x.agentplane.app.thread_events_pb2 import FollowEventsRequest, FollowEventsResponse
from x.agentplane.app.trajectory import IngestionLease, TrajectoryStore
from x.agentplane.protocol import event_log_pb2, event_pb2
from x.agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

SANDBOX = "grpcweb-spike-sandbox"
SESSION = "grpcweb-1"
SOURCE = "grpcweb-spike-runner"
SPEC = protocol_pb2.SessionSpec(
    harness=protocol_pb2.HARNESS_CLAUDE, cwd="/state/work", model="spike-model", reasoning_effort="low"
)
GRPC_WEB = "application/grpc-web+proto"
FOLLOW = "/ducktape.agentplane.app.v1.ThreadEvents/FollowEvents"
FRAME_S = 30


def _event(cursor: int, **observation: object) -> event_log_pb2.EventEntry:
    at = Timestamp()
    at.FromDatetime(datetime(2026, 9, 16, 12, 0, cursor, tzinfo=UTC))
    event = event_pb2.Event(at=at, **observation)  # type: ignore[arg-type]
    return event_log_pb2.EventEntry(
        cursor=cursor, origin=event_log_pb2.EventOrigin(source_id=SOURCE, sequence=cursor), event=event
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
async def lease(store: TrajectoryStore) -> IngestionLease:
    acquired = await store.acquire_ingestion(SANDBOX, timedelta(minutes=1))
    assert acquired is not None
    return acquired


@pytest.fixture
async def thread_id(store: TrajectoryStore, lease: IngestionLease) -> UUID:
    thread = await store.thread(SANDBOX, SESSION, SPEC)
    await store.set_attached(
        thread,
        protocol_pb2.Attached(
            session_id=SESSION, spec=SPEC, last_cursor=2, harness_state=protocol_pb2.HARNESS_STATE_RUNNING
        ),
        lease=lease,
    )
    await store.record(
        thread,
        [
            _event(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=11)),
            _event(2, turn_started=event_pb2.TurnStarted(turn_id="t1")),
        ],
        lease=lease,
    )
    return thread


@pytest.fixture
async def grpc_server(store: TrajectoryStore) -> AsyncIterator[int]:
    server, port = await serve(store, Drain(), address=f"127.0.0.1:{_free_port()}")
    try:
        yield port
    finally:
        await server.stop(0)


@pytest.fixture
def envoy(grpc_server: int) -> Iterator[str]:
    """Envoy on the host's network, because the gRPC server is a Python object in this process and
    nothing in this repo has previously needed a container to dial back into its test."""
    listen = _free_port()
    config = (
        get_required_path(own_repo_rlocation("x/agentplane/app/grpcweb_spike/envoy.yaml"))
        .read_text()
        .replace("port_value: GRPC_PORT", f"port_value: {grpc_server}")
        .replace(
            "socket_address: { address: 127.0.0.1, port_value: 0 }",
            f"socket_address: {{ address: 127.0.0.1, port_value: {listen} }}",
            1,
        )
    )
    written = Path("/tmp") / f"envoy-{listen}.yaml"
    written.write_text(config)
    load_oci_image(envoy_1_35.IMAGE)
    container = (
        DockerContainer(envoy_1_35.IMAGE.tag)
        .with_volume_mapping(str(written), "/etc/envoy/envoy.yaml", "ro")
        .with_kwargs(network_mode="host")
        .with_command("envoy -c /etc/envoy/envoy.yaml --base-id 1")
    )
    with container:
        yield f"http://127.0.0.1:{listen}"


def _enveloped(request: FollowEventsRequest) -> bytes:
    payload = request.SerializeToString()
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


async def _frames(response: httpx.Response) -> AsyncIterator[FollowEventsResponse]:
    """gRPC-Web framing: a flag byte, a big-endian length, the message; flag 0x80 is the trailer."""
    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        buffer += chunk
        while len(buffer) >= 5 and len(buffer) >= 5 + int.from_bytes(buffer[1:5], "big"):
            end = 5 + int.from_bytes(buffer[1:5], "big")
            flags, payload = buffer[0], bytes(buffer[5:end])
            del buffer[:end]
            if flags & 0x80:
                trailers = dict(line.split(": ", 1) for line in payload.decode().strip().splitlines() if ": " in line)
                if trailers.get("grpc-status", "0") != "0":
                    raise AssertionError(f"{trailers.get('grpc-status')}: {trailers.get('grpc-message', '')}")
                return
            yield FollowEventsResponse.FromString(payload)


async def test_a_browsers_grpc_web_reaches_the_service_through_envoy(envoy: str, thread_id: UUID) -> None:
    """The point of the spike: the translation hop carries a real server stream, frame by frame."""
    request = FollowEventsRequest(thread_id=str(thread_id), after_cursor=0)
    cursors: list[int] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as browser:
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(60), wait=wait_fixed(0.5), retry=retry_if_exception_type(httpx.TransportError)
        ):
            with attempt:
                opened = browser.stream(
                    "POST",
                    f"{envoy}{FOLLOW}",
                    headers={"content-type": GRPC_WEB, "x-grpc-web": "1"},
                    content=_enveloped(request),
                )
        async with asyncio.timeout(FRAME_S), opened as response:
            assert response.status_code == 200, (await response.aread()).decode()
            assert response.headers["content-type"].startswith("application/grpc-web")
            async for frame in _frames(response):
                if frame.WhichOneof("frame") == "entry":
                    cursors.append(frame.entry.cursor)
                if len(cursors) == 2:
                    break
    assert cursors == [1, 2]


async def test_the_service_refuses_a_thread_it_does_not_have(grpc_server: int) -> None:
    """Native gRPC, to show what the Python client can and cannot cover: it reaches the server
    without the hop, so this asserts the servicer's status mapping and nothing about the wire."""
    async with grpc.aio.insecure_channel(f"127.0.0.1:{grpc_server}") as channel:
        stub = thread_events_pb2_grpc.ThreadEventsStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            async for _ in stub.FollowEvents(FollowEventsRequest(thread_id="00000000-0000-4000-8000-000000000000")):
                pass
    assert refused.value.code() is grpc.StatusCode.NOT_FOUND


if __name__ == "__main__":
    pytest_bazel.main()
