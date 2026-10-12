"""A route against an upstream gRPC server that records each call: the route's token replaces the
workload's, other metadata and the deadline pass through, streams flow both ways, and an upstream
status reaches the workload unchanged."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import grpc
import pytest
import pytest_bazel

from agentplane.egress.platform_routes import PlatformRoute, PlatformRoutes

UNARY = "/test.Echo/Unary"
STREAM = "/test.Echo/Stream"
FAILING = "/test.Echo/Failing"


@dataclass
class SeenCall:
    method: str
    metadata: dict[str, str]
    time_remaining: float | None


@dataclass
class Upstream(grpc.GenericRpcHandler):
    """Echoes every request back reversed, and refuses FAILING with NOT_FOUND."""

    seen: list[SeenCall] = field(default_factory=list)

    def service(self, handler_call_details: grpc.HandlerCallDetails) -> grpc.RpcMethodHandler:
        method = handler_call_details.method

        async def echo(requests: AsyncIterator[bytes], context: grpc.aio.ServicerContext) -> AsyncIterator[bytes]:
            self.seen.append(SeenCall(method, dict(context.invocation_metadata() or ()), context.time_remaining()))
            if method == FAILING:
                await context.abort(grpc.StatusCode.NOT_FOUND, "no such thing")
            async for request in requests:
                yield request[::-1]

        return grpc.stream_stream_rpc_method_handler(echo)


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
async def route_channel(upstream: tuple[Upstream, int], token_file: Path) -> AsyncIterator[grpc.aio.Channel]:
    _, port = upstream
    route = PlatformRoute(listen_port=0, upstream=f"127.0.0.1:{port}", token_file=token_file)
    async with (
        PlatformRoutes({"control": route}) as routes,
        grpc.aio.insecure_channel(f"127.0.0.1:{routes.port('control')}") as channel,
    ):
        yield channel


async def test_unary_call_carries_the_routes_token_instead_of_the_workloads(
    route_channel: grpc.aio.Channel, upstream: tuple[Upstream, int]
) -> None:
    handler, _ = upstream
    response = await route_channel.unary_unary(UNARY)(
        b"abc", metadata=(("authorization", "Bearer placeholder"), ("x-session", "s-1")), timeout=30
    )
    assert response == b"cba"
    (call,) = handler.seen
    assert call.metadata["authorization"] == "Bearer route-token-1"
    assert call.metadata["x-session"] == "s-1"
    assert call.time_remaining is not None
    assert 0 < call.time_remaining <= 30


async def test_token_is_reread_on_every_call(
    route_channel: grpc.aio.Channel, upstream: tuple[Upstream, int], token_file: Path
) -> None:
    handler, _ = upstream
    await route_channel.unary_unary(UNARY)(b"a")
    await asyncio.to_thread(token_file.write_text, "route-token-2\n")
    await route_channel.unary_unary(UNARY)(b"a")
    assert [c.metadata["authorization"] for c in handler.seen] == ["Bearer route-token-1", "Bearer route-token-2"]


async def test_stream_flows_both_ways(route_channel: grpc.aio.Channel) -> None:
    call = route_channel.stream_stream(STREAM)()
    replies = []
    for request in (b"ab", b"cd"):
        await call.write(request)
        replies.append(await call.read())
    await call.done_writing()
    assert replies == [b"ba", b"dc"]


async def test_upstream_status_reaches_the_workload(route_channel: grpc.aio.Channel) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await route_channel.unary_unary(FAILING)(b"a")
    assert raised.value.code() == grpc.StatusCode.NOT_FOUND
    assert raised.value.details() == "no such thing"


async def test_missing_token_refuses_without_calling_upstream(
    route_channel: grpc.aio.Channel, upstream: tuple[Upstream, int], token_file: Path
) -> None:
    handler, _ = upstream
    await asyncio.to_thread(token_file.unlink)
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await route_channel.unary_unary(UNARY)(b"a")
    assert raised.value.code() == grpc.StatusCode.UNAVAILABLE
    assert handler.seen == []


if __name__ == "__main__":
    pytest_bazel.main()
