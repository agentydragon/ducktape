"""Runner proxy calls use the fixture SVID and retain exact Sandbox routing."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

import grpc
import pytest
import pytest_bazel
from google.protobuf.message import Message

from agentplane.env.runtime.control import environment_runtime_pb2_grpc
from agentplane.proto import identity_pb2, session_pb2
from agentplane.session_dispatcher import client as gateway_client
from agentplane.session_dispatcher.api import (
    INCARNATION_UID_HEADER,
    SANDBOX_HEADER,
    SANDBOX_NAMESPACE_HEADER,
    SANDBOX_OWNER_NAME_HEADER,
    SANDBOX_OWNER_NAMESPACE_HEADER,
    SANDBOX_UID_HEADER,
)
from agentplane.session_dispatcher.client import SandboxControlClient


async def test_runner_proxy_omits_bearer_and_keeps_sandbox_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[tuple[str, str], ...]] = []

    @asynccontextmanager
    async def fake_dial(_target: str, **_kwargs: object) -> AsyncIterator[grpc.aio.Channel]:
        yield cast(grpc.aio.Channel, object())

    async def get_status(_request: Message, **kwargs: object) -> session_pb2.EnvironmentRuntimeStatus:
        observed.append(cast(tuple[tuple[str, str], ...], kwargs["metadata"]))
        return session_pb2.EnvironmentRuntimeStatus()

    monkeypatch.setattr(gateway_client, "dial", fake_dial)
    monkeypatch.setattr(
        environment_runtime_pb2_grpc, "EnvironmentRuntimeStub", lambda _channel: SimpleNamespace(GetStatus=get_status)
    )

    destination = identity_pb2.SandboxDestination(
        owner=identity_pb2.ServiceAccount(namespace="owner-ns", name="owner"),
        target=identity_pb2.EnvironmentTarget(namespace="sandbox-ns", name="sandbox", uid="sandbox-uid"),
    )
    client = SandboxControlClient(
        control_target="environments.{namespace}.svc:8082", token_file=None, request_timeout_s=10
    )
    await client.runner_unary(
        destination,
        lambda stub: stub.GetStatus,
        session_pb2.GetStatusRequest(),
        expected_incarnation_uid="incarnation-uid",
    )

    assert observed == [
        (
            (SANDBOX_HEADER, "sandbox"),
            (SANDBOX_NAMESPACE_HEADER, "sandbox-ns"),
            (SANDBOX_UID_HEADER, "sandbox-uid"),
            (SANDBOX_OWNER_NAMESPACE_HEADER, "owner-ns"),
            (SANDBOX_OWNER_NAME_HEADER, "owner"),
            (INCARNATION_UID_HEADER, "incarnation-uid"),
        )
    ]


if __name__ == "__main__":
    pytest_bazel.main()
