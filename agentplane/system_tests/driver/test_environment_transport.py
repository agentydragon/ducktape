"""EnvironmentService transport follows the Controller's SVID-only contract."""

from contextlib import aclosing
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import grpc
import pytest
from google.protobuf.empty_pb2 import Empty

from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2
from agentplane.system_tests.driver import environment_transport
from agentplane.system_tests.driver.environment_transport import EnvironmentServiceClient

# The generated service stub's dependency chain needs the protobuf runtime as a direct dependency.
# gazelle:include_dep @pypi//protobuf


class _Stream:
    def __init__(self, frames: list[environment_service_pb2.WatchEnvironmentsResponse]) -> None:
        self._frames = iter(frames)
        self.cancelled = False

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> environment_service_pb2.WatchEnvironmentsResponse:
        try:
            return next(self._frames)
        except StopIteration:
            raise StopAsyncIteration from None

    def cancel(self) -> None:
        self.cancelled = True


async def test_environment_unary_and_watch_send_no_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    target = identity_pb2.EnvironmentTarget(namespace="test", name="native", uid="sandbox-uid")
    create = AsyncMock(return_value=target)
    stream = _Stream([environment_service_pb2.WatchEnvironmentsResponse(snapshot_complete=Empty())])
    watch = Mock(return_value=stream)
    stub = SimpleNamespace(CreateEnvironment=create, WatchEnvironments=watch)
    monkeypatch.setattr(environment_transport.environment_service_pb2_grpc, "EnvironmentServiceStub", lambda _: stub)

    client = EnvironmentServiceClient(cast(grpc.aio.Channel, object()))

    assert (
        await client.create_environment(
            environment_service_pb2.CreateEnvironmentRequest(name="native", template="pod", identity="identity")
        )
        == target
    )
    create_call = create.await_args
    assert create_call is not None
    assert create_call.kwargs["metadata"] == ()

    async with aclosing(client.environment_snapshots()) as snapshots:
        assert await anext(snapshots) == []
    watch_call = watch.call_args
    assert watch_call is not None
    assert watch_call.kwargs["metadata"] == ()
    assert stream.cancelled
