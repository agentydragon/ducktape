import asyncio
from collections.abc import AsyncIterator

import grpc
import httpx
import pytest
import pytest_bazel
from grpc_health.v1 import health, health_pb2

from agentplane.app.system_status import (
    ComponentState,
    ComponentStatus,
    Probe,
    SystemStatusChecker,
    grpc_health_probe,
    http_health_probe,
)
from agentplane.grpc_serving import add_health, serving

# The generated health stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


@pytest.fixture
async def health_channel() -> AsyncIterator[tuple[grpc.aio.Channel, health.aio.HealthServicer]]:
    server = grpc.aio.server()
    servicer = add_health(server)
    async with (
        serving(server, "127.0.0.1:0", grace_s=0) as port,
        grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel,
    ):
        yield channel, servicer


async def check_one(probe: Probe, *, timeout_s: float = 5) -> ComponentStatus:
    [status] = await SystemStatusChecker([probe], timeout_s=timeout_s).check_all()
    return status


async def test_serving_grpc_service_is_ok(health_channel: tuple[grpc.aio.Channel, health.aio.HealthServicer]) -> None:
    channel, _ = health_channel
    status = await check_one(grpc_health_probe("Test gRPC", channel))
    assert (status.name, status.state, status.detail) == ("Test gRPC", ComponentState.OK, None)


async def test_not_serving_grpc_service_is_down(
    health_channel: tuple[grpc.aio.Channel, health.aio.HealthServicer],
) -> None:
    channel, servicer = health_channel
    await servicer.set("", health_pb2.HealthCheckResponse.NOT_SERVING)
    status = await check_one(grpc_health_probe("Test gRPC", channel))
    assert (status.state, status.detail) == (ComponentState.DOWN, "ConnectionError: NOT_SERVING")


async def test_unreachable_grpc_service_is_down_with_the_status_code() -> None:
    async with grpc.aio.insecure_channel("127.0.0.1:1") as channel:
        status = await check_one(grpc_health_probe("Test gRPC", channel))
    assert status.state == ComponentState.DOWN
    assert status.detail is not None
    assert status.detail.startswith("UNAVAILABLE: ")


@pytest.mark.parametrize(
    ("answer_status", "state"), [(204, ComponentState.OK), (503, ComponentState.DOWN)], ids=["healthy", "unhealthy"]
)
async def test_http_probe_follows_the_status_code(answer_status: int, state: ComponentState) -> None:
    async with httpx.AsyncClient(
        base_url="http://test.invalid", transport=httpx.MockTransport(lambda request: httpx.Response(answer_status))
    ) as client:
        status = await check_one(http_health_probe("Test HTTP", client, "/test-health"))
    assert status.state == state
    assert (status.detail is None) == (state == ComponentState.OK)


async def test_unreachable_http_service_is_down() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("test connection refused")

    async with httpx.AsyncClient(base_url="http://test.invalid", transport=httpx.MockTransport(refuse)) as client:
        status = await check_one(http_health_probe("Test HTTP", client, "/test-health"))
    assert status.detail == "ConnectError: test connection refused"


async def test_probe_that_never_answers_is_down_after_the_timeout() -> None:
    async def hang() -> None:
        await asyncio.Event().wait()

    status = await check_one(Probe("Test hanging", hang), timeout_s=0.01)
    assert (status.state, status.detail) == (ComponentState.DOWN, "no answer within 0.01s")


async def test_one_down_probe_leaves_the_others_reported() -> None:
    async def answers() -> None:
        pass

    async def fails() -> None:
        raise RuntimeError("test failure")

    statuses = await SystemStatusChecker(
        [Probe("Test ok", answers), Probe("Test down", fails)], timeout_s=5
    ).check_all()
    assert [(status.name, status.state) for status in statuses] == [
        ("Test ok", ComponentState.OK),
        ("Test down", ComponentState.DOWN),
    ]


if __name__ == "__main__":
    pytest_bazel.main()
