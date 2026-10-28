"""Exercise generic unary and streaming gRPC calls through the built Go sidecar binary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import grpc
import pytest
import pytest_bazel

from agentplane.egress.sidecar.configpb import config_pb2
from agentplane.egress.testing.sidecar_process import SidecarProcess

# Generated message stubs import protobuf's own types.
# gazelle:include_dep @pypi//protobuf

UNARY = "/test.Echo/Unary"
STREAM = "/test.Echo/Stream"
FAILING = "/test.Echo/Failing"
BLOCKING = "/test.Echo/Blocking"
ARBITRARY = "/unknown.Service/AnyMethod"


@dataclass
class SeenCall:
    method: str
    metadata: dict[str, str | bytes]
    time_remaining: float | None


@dataclass
class Upstream(grpc.GenericRpcHandler):
    seen: list[SeenCall] = field(default_factory=list)
    blocking_started: asyncio.Event = field(default_factory=asyncio.Event)
    blocking_cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    def service(self, handler_call_details: grpc.HandlerCallDetails) -> grpc.RpcMethodHandler:
        method = handler_call_details.method

        async def echo(requests: AsyncIterator[bytes], context: grpc.aio.ServicerContext) -> AsyncIterator[bytes]:
            metadata: dict[str, str | bytes] = dict(context.invocation_metadata() or grpc.aio.Metadata())
            self.seen.append(SeenCall(method, metadata, context.time_remaining()))
            await context.send_initial_metadata((("x-upstream-header", "header-value"),))
            if method == FAILING:
                await context.abort(
                    grpc.StatusCode.NOT_FOUND,
                    "no such thing",
                    trailing_metadata=(("x-upstream-trailer", "trailer-value"),),
                )
            if method == BLOCKING:
                try:
                    async for _ in requests:
                        self.blocking_started.set()
                        await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.blocking_cancelled.set()
                    raise
            async for request in requests:
                yield request[::-1]
            context.set_trailing_metadata((("x-upstream-trailer", "trailer-value"),))

        return grpc.stream_stream_rpc_method_handler(echo)


@dataclass
class RouteUnderTest:
    channel: grpc.aio.Channel
    upstream: Upstream
    token_file: Path


@pytest.fixture
async def upstream() -> AsyncIterator[tuple[Upstream, int]]:
    handler = Upstream()
    server = grpc.aio.server()
    server.add_generic_rpc_handlers([handler])
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    yield handler, port
    await server.stop(None)


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "control-token"
    path.write_text("route-token-1\n")
    return path


@pytest.fixture
async def route_channel(upstream: tuple[Upstream, int], token_file: Path) -> AsyncIterator[RouteUnderTest]:
    handler, port = upstream
    route_config = {
        "control": config_pb2.PlatformRoute(
            listen_address="127.0.0.1:0", upstream=f"127.0.0.1:{port}", token_file=str(token_file)
        )
    }
    async with (
        SidecarProcess(
            proxy_host="127.0.0.1", proxy_port=1, token_file=token_file, platform_routes=route_config
        ) as sidecar,
        grpc.aio.insecure_channel(
            f"{sidecar.platform_route_listeners['control'].host}:{sidecar.platform_route_listeners['control'].port}"
        ) as channel,
    ):
        yield RouteUnderTest(channel, handler, token_file)


def _unary(channel: grpc.aio.Channel, method: str) -> grpc.aio.UnaryUnaryMultiCallable[bytes, bytes]:
    return channel.unary_unary(method)


async def test_unary_call_replaces_auth_preserves_metadata_deadline_and_trailers(route_channel: RouteUnderTest) -> None:
    call = _unary(route_channel.channel, UNARY)(
        b"abc", metadata=(("authorization", "Bearer placeholder"), ("x-session", "s-1")), timeout=30
    )
    assert await call == b"cba"
    assert dict(await call.initial_metadata())["x-upstream-header"] == "header-value"
    assert dict(await call.trailing_metadata())["x-upstream-trailer"] == "trailer-value"
    (seen,) = route_channel.upstream.seen
    assert seen.method == UNARY
    assert seen.metadata["authorization"] == "Bearer route-token-1"
    assert seen.metadata["x-session"] == "s-1"
    assert seen.time_remaining is not None
    assert 0 < seen.time_remaining <= 30


async def test_token_is_reread_on_every_call(route_channel: RouteUnderTest) -> None:
    await _unary(route_channel.channel, UNARY)(b"a")
    await asyncio.to_thread(route_channel.token_file.write_text, "route-token-2\n")
    await _unary(route_channel.channel, UNARY)(b"a")
    assert [call.metadata["authorization"] for call in route_channel.upstream.seen] == [
        "Bearer route-token-1",
        "Bearer route-token-2",
    ]


async def test_stream_flows_both_ways_without_waiting_for_client_half_close(route_channel: RouteUnderTest) -> None:
    stream: grpc.aio.StreamStreamMultiCallable[bytes, bytes] = route_channel.channel.stream_stream(STREAM)
    call = stream()
    await call.write(b"ab")
    assert await call.read() == b"ba"
    await call.write(b"cd")
    assert await call.read() == b"dc"
    await call.done_writing()
    assert await call.read() is grpc.aio.EOF
    assert dict(await call.initial_metadata())["x-upstream-header"] == "header-value"
    assert dict(await call.trailing_metadata())["x-upstream-trailer"] == "trailer-value"


async def test_arbitrary_method_passes_through_without_service_protos(route_channel: RouteUnderTest) -> None:
    assert await _unary(route_channel.channel, ARBITRARY)(b"opaque payload") == b"daolyap euqapo"
    assert route_channel.upstream.seen[-1].method == ARBITRARY


async def test_upstream_status_and_trailing_metadata_reach_the_workload(route_channel: RouteUnderTest) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await _unary(route_channel.channel, FAILING)(b"a")
    assert raised.value.code() == grpc.StatusCode.NOT_FOUND
    assert raised.value.details() == "no such thing"
    assert dict(raised.value.trailing_metadata() or ()) == {"x-upstream-trailer": "trailer-value"}


async def test_missing_token_refuses_without_calling_upstream(route_channel: RouteUnderTest) -> None:
    await asyncio.to_thread(route_channel.token_file.unlink)
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await _unary(route_channel.channel, UNARY)(b"a")
    assert raised.value.code() == grpc.StatusCode.UNAVAILABLE
    assert [call.method for call in route_channel.upstream.seen] == []


async def test_cancellation_reaches_upstream(route_channel: RouteUnderTest) -> None:
    call: grpc.aio.StreamStreamCall[bytes, bytes] = route_channel.channel.stream_stream(BLOCKING)()
    await call.write(b"stop")
    await asyncio.wait_for(route_channel.upstream.blocking_started.wait(), timeout=2)
    call.cancel()
    await asyncio.wait_for(route_channel.upstream.blocking_cancelled.wait(), timeout=2)


if __name__ == "__main__":
    pytest_bazel.main()
