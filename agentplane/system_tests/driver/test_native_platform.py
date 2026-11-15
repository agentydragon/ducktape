"""Ownership and retry boundaries for native Runner control reads."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import grpc
import pytest
import pytest_bazel

from agentplane.grpc.calls import ServiceError
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import identity_pb2
from agentplane.runner_gateway.client import SandboxControlClient, SandboxRunner
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.environment_transport import EnvironmentServiceClient
from agentplane.system_tests.driver.native_platform import NativePlatformClients

# gazelle:include_dep @pypi//protobuf


def _target() -> identity_pb2.EnvironmentTarget:
    return identity_pb2.EnvironmentTarget(namespace="test", name="native", uid="sandbox-uid")


def _clients() -> NativePlatformClients:
    return NativePlatformClients(
        cluster=cast(Cluster, SimpleNamespace(namespace="test")),
        environment=cast(EnvironmentServiceClient, object()),
        dispatcher=cast(SessionDispatcherClient, object()),
        history=cast(HistoryServiceClient, object()),
        control=SandboxControlClient(
            control_target="127.0.0.1:8080", token_file=Path("unused-token"), request_timeout_s=2
        ),
    )


class _Runner:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.status_calls = 0

    async def status(self) -> object:
        self.status_calls += 1
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


async def test_stable_gateway_route_preserves_exact_sandbox_destination_without_endpoint_slices() -> None:
    clients = _clients()
    target = _target()
    runner = await clients.runner(target)
    assert runner.control is clients.control
    assert runner.control.control_target == "127.0.0.1:8080"
    assert runner.destination == identity_pb2.SandboxDestination(
        owner=identity_pb2.ServiceAccount(namespace=target.namespace, name=target.name), target=target
    )
    replacement = identity_pb2.EnvironmentTarget(namespace=target.namespace, name=target.name, uid="replacement-uid")
    successor = await clients.runner(replacement)
    assert successor.control is runner.control
    assert successor.destination.target.uid == "replacement-uid"
    assert runner.destination.target.uid == "sandbox-uid"


@pytest.mark.parametrize(
    "target",
    [
        identity_pb2.EnvironmentTarget(namespace="other", name="native", uid="sandbox-uid"),
        identity_pb2.EnvironmentTarget(namespace="test", name="", uid="sandbox-uid"),
        identity_pb2.EnvironmentTarget(namespace="test", name="native", uid=""),
    ],
)
async def test_stable_gateway_route_requires_a_complete_local_sandbox(target: identity_pb2.EnvironmentTarget) -> None:
    with pytest.raises(ValueError, match="complete Environment target"):
        await _clients().runner(target)


async def test_ready_runner_retries_only_unavailable_status_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _clients()
    runner = _Runner(ServiceError(grpc.StatusCode.UNAVAILABLE, "control route is starting"), object())
    resolutions = 0

    async def resolve(_self: NativePlatformClients, _target: identity_pb2.EnvironmentTarget) -> _Runner:
        nonlocal resolutions
        resolutions += 1
        return runner

    monkeypatch.setattr(NativePlatformClients, "runner", resolve)

    assert await client.ready_runner(_target(), timeout_s=2) is cast(SandboxRunner, runner)
    assert resolutions == 2
    assert runner.status_calls == 2


async def test_ready_runner_does_not_retry_authorization_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _clients()
    denied = ServiceError(grpc.StatusCode.PERMISSION_DENIED, "not authorized")
    runner = _Runner(denied)
    resolutions = 0

    async def resolve(_self: NativePlatformClients, _target: identity_pb2.EnvironmentTarget) -> _Runner:
        nonlocal resolutions
        resolutions += 1
        return runner

    monkeypatch.setattr(NativePlatformClients, "runner", resolve)

    with pytest.raises(ServiceError) as error:
        await client.ready_runner(_target(), timeout_s=2)

    assert error.value is denied
    assert resolutions == 1
    assert runner.status_calls == 1


async def test_ready_runner_deadline_and_cancellation_bound_status_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _clients()
    entered = asyncio.Event()

    class _BlockedRunner:
        async def status(self) -> None:
            entered.set()
            await asyncio.Event().wait()

    runner = _BlockedRunner()

    async def resolve(_self: NativePlatformClients, _target: identity_pb2.EnvironmentTarget) -> _BlockedRunner:
        return runner

    monkeypatch.setattr(NativePlatformClients, "runner", resolve)

    with pytest.raises(TimeoutError):
        await client.ready_runner(_target(), timeout_s=0.01)

    entered.clear()
    task = asyncio.create_task(client.ready_runner(_target(), timeout_s=30))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)


if __name__ == "__main__":
    pytest_bazel.main()
